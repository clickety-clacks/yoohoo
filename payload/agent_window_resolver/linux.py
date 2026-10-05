"""Bounded Linux, tmux, and SSH observation collector."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import PurePath
import selectors
import shlex
import socket
import struct
import subprocess
import time
from typing import Any, Iterable, Mapping, Sequence

from .collector import (
    CommandResult,
    Deadline,
    Endpoint,
    ObservationError,
    ProbeIO,
    ProcessNode,
    TargetObservation,
    TmuxClient,
    TmuxPane,
    TopologySnapshot,
    WindowObservation,
    transport_socket_eligible,
)
from .model import (
    MAX_PID,
    ProcessIdentity,
    Request,
    SocketSelector,
    canonical_machine,
    machine_matches,
)

_MAX_PROC_BYTES = 65_536
_MAX_NET_BYTES = 1_048_576
_MAX_GRAPH_NODES = 512
_MAX_GRAPH_DEPTH = 64
_MAX_CLIENTS = 256
_MAX_REMOTE_BYTES = 524_288
# Best-effort local matching only needs the bounded tmux client listing.  Keep
# this separate from the larger strict target-probe bound so a busy local
# socket cannot turn matching into a broad remote-style observation.
_MAX_LOCAL_MATCH_BYTES = 65_536
# Linux proc fd symlink targets are bounded by PATH_MAX.  128 was too small
# for ordinary desktop paths (for example, a Chromium IndexedDB manifest).
_MAX_PROC_LINK_CHARS = 4096


class CollectionFailure(RuntimeError):
    def __init__(self, code: str, source: str, message: str,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.error = ObservationError(code, source, message, retryable)


class DefaultProbeIO:
    """Real bounded IO. Each run receives an explicit environment copy."""

    def read_bytes(self, path: str, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("negative read bound")
        with open(path, "rb", buffering=0) as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise CollectionFailure("probe_output_too_large", "proc",
                                    "bounded file read exceeded", True)
        return value

    def readlink(self, path: str, max_chars: int) -> str:
        value = os.readlink(path)
        if len(value) > max_chars:
            raise CollectionFailure("probe_output_too_large", "proc",
                                    "bounded link read exceeded", True)
        return value

    def listdir(self, path: str, max_entries: int) -> tuple[str, ...]:
        values: list[str] = []
        own_fd_dir = f"/proc/{os.getpid()}/fd"
        with os.scandir(path) as entries:
            for entry in entries:
                if path == own_fd_dir:
                    # Omit only the descriptor held by this scandir
                    # iterator. It remains available while the iterator is
                    # live; any other vanished entry must fail closed.
                    if os.path.normpath(os.readlink(entry.path)) == path:
                        continue
                values.append(entry.name)
                if len(values) > max_entries:
                    raise CollectionFailure("probe_output_too_large", "proc",
                                            "directory entry bound exceeded", True)
        return tuple(sorted(values))

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_stdout: int,
        max_stderr: int,
        env: Mapping[str, str],
    ) -> CommandResult:
        if timeout <= 0:
            return CommandResult(124, b"", b"", timed_out=True)
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                env=dict(env),
            )
        except (OSError, ValueError) as error:
            return CommandResult(127, b"", str(error).encode()[:max_stderr])
        assert process.stdout is not None and process.stderr is not None
        streams = {
            process.stdout.fileno(): ("stdout", process.stdout),
            process.stderr.fileno(): ("stderr", process.stderr),
        }
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": max_stdout, "stderr": max_stderr}
        select = selectors.DefaultSelector()
        deadline = time.monotonic() + timeout
        timed_out = truncated = False
        try:
            for fd, (_, stream) in streams.items():
                os.set_blocking(fd, False)
                select.register(stream, selectors.EVENT_READ, fd)
            open_fds = set(streams)
            while open_fds:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = select.select(remaining)
                if not events:
                    timed_out = True
                    break
                for key, _ in events:
                    fd = key.data
                    name, stream = streams[fd]
                    try:
                        chunk = os.read(fd, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        select.unregister(stream)
                        open_fds.discard(fd)
                        continue
                    buffer, limit = buffers[name], limits[name]
                    room = limit + 1 - len(buffer)
                    if room > 0:
                        buffer.extend(chunk[:room])
                    if len(buffer) > limit or len(chunk) > room:
                        truncated = True
                        open_fds.clear()
                        break
        finally:
            select.close()
            if timed_out or truncated:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.SubprocessError:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
            process.stdout.close()
            process.stderr.close()
        return CommandResult(
            process.returncode,
            bytes(buffers["stdout"][:max_stdout]),
            bytes(buffers["stderr"][:max_stderr]),
            timed_out=timed_out,
            truncated=truncated,
        )

    def monotonic(self) -> float:
        return time.monotonic()


_REMOTE_PROBE = r"""
import base64, ipaddress, json, os, selectors, subprocess, sys, time

payload = sys.argv[1]
cfg = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
MAX_BYTES = 65536
MAX_NET = 1048576
MAX_DEPTH = 64
MAX_CLIENTS = 256
MAX_FDS = 1024

def bounded(path, limit=MAX_BYTES):
    with open(path, "rb", buffering=0) as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise RuntimeError("bounded read exceeded")
    return value

def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

def stat_identity(raw):
    tail = raw.decode("ascii").rsplit(") ", 1)[1].split()
    return int(tail[1]), str(int(tail[19]))

def run_bounded(argv, limit, timeout=5):
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, close_fds=True, env=probe_env,
    )
    output = bytearray()
    select = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        select.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select(remaining):
                raise RuntimeError("tmux probe timeout")
            chunk = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise RuntimeError("tmux output exceeded")
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if process.returncode != 0:
            raise RuntimeError("tmux command failed")
        return bytes(output)
    finally:
        select.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()

def filtered_environment(pid):
    result = []
    for item in bounded("/proc/%d/environ" % pid).split(b"\0"):
        if item.startswith(b"SSH_CONNECTION=") or item.startswith(b"SSH_CLIENT="):
            result.append(item)
    return b"\0".join(result)

def fd_inodes(pid):
    result = set()
    count = 0
    with os.scandir("/proc/%d/fd" % pid) as entries:
        for entry in entries:
            count += 1
            if count > MAX_FDS:
                raise RuntimeError("fd bound exceeded")
            link = os.readlink(entry.path)
            if link.startswith("socket:[") and link.endswith("]"):
                result.add(link[8:-1])
    return result

def matching_net_lines(pid, inodes):
    result = {}
    for name in ("tcp", "tcp6", "udp", "udp6"):
        lines = bounded(
            "/proc/%d/net/%s" % (pid, name), MAX_NET
        ).splitlines()[1:]
        matched = []
        for line in lines:
            fields = line.split()
            if len(fields) >= 10 and fields[9].decode("ascii", "ignore") in inodes:
                matched.append(b64(line))
        result[name] = matched
    return result

def net_identity(values):
    def fields(value):
        raw = base64.urlsafe_b64decode(
            value + "=" * (-len(value) % 4)
        ).split()
        if len(raw) < 10:
            raise RuntimeError("invalid network row")
        return raw[1], raw[2], raw[9]
    return {
        name: sorted(fields(line) for line in lines)
        for name, lines in values.items()
    }

def raw_node(pid):
    before = bounded("/proc/%d/stat" % pid)
    parent = stat_identity(before)[0]
    cmdline = bounded("/proc/%d/cmdline" % pid)
    environment = filtered_environment(pid)
    inodes = fd_inodes(pid)
    net_lines = matching_net_lines(pid, inodes)
    try:
        tty = os.readlink("/proc/%d/fd/0" % pid)
    except OSError:
        tty = ""
    after = bounded("/proc/%d/stat" % pid)
    environment_after = filtered_environment(pid)
    inodes_after = fd_inodes(pid)
    net_lines_after = matching_net_lines(pid, inodes_after)
    cmdline_after = bounded("/proc/%d/cmdline" % pid)
    final = bounded("/proc/%d/stat" % pid)
    if (stat_identity(before) != stat_identity(after)
            or stat_identity(before) != stat_identity(final)
            or cmdline != cmdline_after
            or environment != environment_after
            or inodes != inodes_after
            or net_identity(net_lines) != net_identity(net_lines_after)):
        raise RuntimeError("process observation changed")
    return {
        "pid": pid,
        "parentPid": parent,
        "statBefore": b64(before),
        "statAfter": b64(final),
        "cmdlineBefore": b64(cmdline),
        "cmdlineAfter": b64(cmdline_after),
        "environmentBefore": b64(environment),
        "environmentAfter": b64(environment_after),
        "fdInodesBefore": sorted(inodes),
        "fdInodesAfter": sorted(inodes_after),
        "netLinesBefore": net_lines,
        "netLinesAfter": net_lines_after,
        "tty": tty,
    }

def valid_ssh_boundary(item):
    environment = base64.urlsafe_b64decode(
        item["environmentBefore"] + "=" * (-len(item["environmentBefore"]) % 4)
    )
    connections = [
        value.split(b"=", 1)[1]
        for value in environment.split(b"\0")
        if value.startswith(b"SSH_CONNECTION=")
    ]
    if len(connections) != 1:
        return False
    fields = connections[0].decode("utf-8", "strict").split()
    if len(fields) != 4:
        return False
    try:
        client = ipaddress.ip_address(fields[0])
        server = ipaddress.ip_address(fields[2])
        client_port = int(fields[1])
        server_port = int(fields[3])
    except (ValueError, UnicodeError):
        return False
    return (
        client.version == server.version
        and not client.is_unspecified
        and not server.is_unspecified
        and 1 <= client_port <= 65535
        and 1 <= server_port <= 65535
    )

def valid_mosh_boundary(item):
    command = base64.urlsafe_b64decode(
        item["cmdlineBefore"] + "=" * (-len(item["cmdlineBefore"]) % 4)
    ).split(b"\0", 1)[0]
    executable = os.path.basename(os.fsdecode(command))
    if executable not in {"mosh-client", "mosh-server"}:
        return False
    rows = [
        (name, value)
        for name in ("udp", "udp6")
        for value in item["netLinesBefore"][name]
    ]
    if len(rows) != 1:
        return False
    name, value = rows[0]
    try:
        fields = base64.urlsafe_b64decode(
            value + "=" * (-len(value) % 4)
        ).split()
        if len(fields) < 10:
            return False
        local_address, local_port = fields[1].decode("ascii").rsplit(":", 1)
        remote_address, remote_port = fields[2].decode("ascii").rsplit(":", 1)
        local_port = int(local_port, 16)
        remote_port = int(remote_port, 16)
    except (ValueError, UnicodeError):
        return False
    width = 8 if name == "udp" else 32
    return (
        len(local_address) == width
        and len(remote_address) == width
        and int(local_address, 16) != 0
        and int(remote_address, 16) != 0
        and 1 <= local_port <= 65535
        and 1 <= remote_port <= 65535
    )

def transport_boundary(item):
    boundaries = []
    if valid_ssh_boundary(item):
        boundaries.append("ssh_environment")
    if valid_mosh_boundary(item):
        boundaries.append("mosh_udp")
    return boundaries[0] if len(boundaries) == 1 else None

def ancestry(pid, stop_at_transport=False, stop_at_pid=None):
    result, seen = [], set()
    for _ in range(MAX_DEPTH):
        if pid <= 0 or pid in seen:
            raise RuntimeError("process ancestry cycle")
        seen.add(pid)
        item = raw_node(pid)
        result.append(item)
        if stop_at_pid is not None and pid == stop_at_pid:
            return result
        if stop_at_transport:
            boundary = transport_boundary(item)
            if boundary is not None:
                return result, boundary
        parent = item["parentPid"]
        if parent <= 1:
            return (result, None) if stop_at_transport else result
        pid = parent
    if stop_at_pid is not None:
        raise RuntimeError("process ancestry boundary not reached")
    raise RuntimeError("process ancestry depth exceeded")

probe_env = dict(os.environ)
probe_env.pop("TMUX", None)
selector = cfg.get("socket")
base = ["tmux"]
if selector:
    base += ["-L" if selector["kind"] == "name" else "-S", selector["value"]]
socket_path = run_bounded(
    base + ["display-message", "-p", "#{socket_path}"], 4096
).decode("utf-8", "strict").strip()
if not socket_path.startswith("/") or "\n" in socket_path:
    raise RuntimeError("invalid socket path")
base = ["tmux", "-S", socket_path]
pane_line = run_bounded(
    base + ["display-message", "-p", "-t", cfg["paneId"],
            "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_pid}"],
    4096,
).decode("utf-8", "strict").rstrip("\n")
pane_fields = pane_line.split("\t")
if len(pane_fields) != 4:
    raise RuntimeError("invalid pane result")
pane_pid = int(pane_fields[3])
client_output = run_bounded(
    base + ["list-clients", "-F",
            "#{client_name}\t#{client_pid}\t#{client_session}\t#{window_index}\t#{pane_id}"],
    65536,
).decode("utf-8", "strict")
client_lines = client_output.splitlines()
if len(client_lines) > MAX_CLIENTS:
    raise RuntimeError("client count exceeded")
clients = []
for line in client_lines:
    fields = line.split("\t")
    if len(fields) != 5:
        raise RuntimeError("invalid client result")
    pid = int(fields[1])
    processes, boundary = ancestry(pid, stop_at_transport=True)
    client = {"line": line, "processes": processes}
    if boundary is not None:
        client["transportBoundary"] = boundary
    clients.append(client)
pane_process = raw_node(pane_pid)
target_processes = ancestry(int(cfg["pid"]), stop_at_pid=pane_pid)
if target_processes[-1]["pid"] != pane_pid:
    raise RuntimeError("target ancestry boundary did not end at pane")
pane_tail = stat_identity(
    base64.urlsafe_b64decode(
        pane_process["statBefore"]
        + "=" * (-len(pane_process["statBefore"]) % 4)
    )
)[1]
socket_path_after = run_bounded(
    base + ["display-message", "-p", "#{socket_path}"], 4096
).decode("utf-8", "strict").strip()
pane_line_after = run_bounded(
    base + ["display-message", "-p", "-t", cfg["paneId"],
            "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_pid}"],
    4096,
).decode("utf-8", "strict").rstrip("\n")
client_output_after = run_bounded(
    base + ["list-clients", "-F",
            "#{client_name}\t#{client_pid}\t#{client_session}\t#{window_index}\t#{pane_id}"],
    65536,
).decode("utf-8", "strict")
if (socket_path_after != socket_path
        or pane_line_after != pane_line
        or client_output_after != client_output):
    raise RuntimeError("tmux topology changed during collection")

print(json.dumps({
    "socketPathBefore": socket_path,
    "socketPathAfter": socket_path_after,
    "paneLineBefore": pane_line,
    "paneLineAfter": pane_line_after,
    "clientLinesBefore": client_lines,
    "clientLinesAfter": client_output_after.splitlines(),
    "paneProcess": pane_process,
    "targetBoundary": {
        "kind": "pane", "pid": pane_pid, "startTimeTicks": pane_tail,
    },
    "processes": target_processes,
    "clients": clients,
}, separators=(",", ":")))
"""


class LinuxCollector:
    def __init__(
        self,
        io: ProbeIO | None = None,
        *,
        proc_root: str = "/proc",
        ssh: str = "ssh",
        python: str = "python3",
    ) -> None:
        self.io = io if io is not None else DefaultProbeIO()
        self.proc_root = proc_root.rstrip("/") or "/proc"
        self.ssh = ssh
        self.python = python

    def _environment(self) -> dict[str, str]:
        allowed = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "SSH_AUTH_SOCK", "TMPDIR", "TMUX_TMPDIR",
        }
        return {key: value for key, value in os.environ.items() if key in allowed}

    def _path(self, *parts: object) -> str:
        return self.proc_root + "/" + "/".join(str(part) for part in parts)

    @staticmethod
    def _parse_stat(
        raw: bytes, pid: int, machine: str
    ) -> tuple[ProcessIdentity, int]:
        identity, parent, _ = LinuxCollector._parse_stat_record(raw, pid, machine)
        return identity, parent

    @staticmethod
    def _parse_stat_record(
        raw: bytes, pid: int, machine: str
    ) -> tuple[ProcessIdentity, int, str]:
        if not 1 <= pid <= MAX_PID:
            raise CollectionFailure("invalid_pid", "proc", "PID is outside bound")
        try:
            tail = raw.decode("ascii").rsplit(") ", 1)[1].split()
            state, parent, ticks = tail[0], int(tail[1]), str(int(tail[19]))
            if not 0 <= parent <= MAX_PID or not 0 <= int(ticks) < (1 << 64):
                raise ValueError("stat value outside bound")
        except (UnicodeError, ValueError, IndexError) as error:
            raise CollectionFailure("proc_stat_invalid", "proc",
                                    "process stat is invalid", True) from error
        return ProcessIdentity(canonical_machine(machine), pid, ticks), parent, state

    def _stat(self, pid: int, machine: str) -> tuple[ProcessIdentity, int]:
        raw = self.io.read_bytes(self._path(pid, "stat"), _MAX_PROC_BYTES)
        return self._parse_stat(raw, pid, machine)

    def _stat_record(
        self, pid: int, machine: str
    ) -> tuple[ProcessIdentity, int, str]:
        raw = self.io.read_bytes(self._path(pid, "stat"), _MAX_PROC_BYTES)
        return self._parse_stat_record(raw, pid, machine)

    @staticmethod
    def _decode_proc_address(value: str, family: str) -> str:
        if family == "ipv4":
            return str(ipaddress.IPv4Address(
                struct.pack("<I", int(value, 16))
            ))
        words = [int(value[index:index + 8], 16)
                 for index in range(0, 32, 8)]
        return socket.inet_ntop(socket.AF_INET6, struct.pack("<IIII", *words))

    @classmethod
    def _parse_net_lines(
        cls,
        inodes: set[str],
        values: Mapping[str, Iterable[bytes]],
    ) -> tuple[Endpoint, ...]:
        result: set[Endpoint] = set()
        for net, protocol, family in (
            ("tcp", "tcp", "ipv4"), ("tcp6", "tcp", "ipv6"),
            ("udp", "udp", "ipv4"), ("udp6", "udp", "ipv6"),
        ):
            for raw_line in values.get(net, ()):
                try:
                    fields = raw_line.decode("ascii").split()
                    if len(fields) < 10 or fields[9] not in inodes:
                        continue
                    local_addr, local_port = fields[1].rsplit(":", 1)
                    remote_addr, remote_port = fields[2].rsplit(":", 1)
                    result.add(Endpoint(
                        protocol, family,
                        cls._decode_proc_address(local_addr, family),
                        int(local_port, 16),
                        cls._decode_proc_address(remote_addr, family),
                        int(remote_port, 16),
                    ))
                except (UnicodeError, ValueError, OSError) as error:
                    raise CollectionFailure(
                        "proc_net_invalid", "proc",
                        "matching socket endpoint row is invalid", True,
                    ) from error
        return tuple(sorted(result, key=repr))

    @staticmethod
    def _parse_ssh_environment(raw: bytes) -> tuple[Endpoint, ...]:
        connections: list[bytes] = []
        for item in raw.split(b"\0"):
            if item.startswith(b"SSH_CONNECTION="):
                connections.append(item.split(b"=", 1)[1])
        if len(connections) != 1:
            return ()
        fields = connections[0].decode("utf-8", "replace").split()
        if len(fields) != 4:
            return ()
        try:
            client_addr, client_port, server_addr, server_port = fields
            client = ipaddress.ip_address(client_addr)
            server = ipaddress.ip_address(server_addr)
            if client.version != server.version:
                return ()
            local_port, remote_port = int(server_port), int(client_port)
            if not (1 <= local_port <= 65535 and 1 <= remote_port <= 65535):
                return ()
            return (Endpoint(
                "tcp", "ipv4" if client.version == 4 else "ipv6",
                str(server), local_port, str(client), remote_port,
            ),)
        except (ValueError, UnicodeError):
            return ()

    def _endpoints(self, pid: int) -> tuple[Endpoint, ...]:
        names = self.io.listdir(self._path(pid, "fd"), 1024)
        inodes: set[str] = set()
        for name in names:
            link = self.io.readlink(
                self._path(pid, "fd", name), _MAX_PROC_LINK_CHARS
            )
            if link.startswith("socket:[") and link.endswith("]"):
                inodes.add(link[8:-1])
        lines: dict[str, tuple[bytes, ...]] = {}
        for net in ("tcp", "tcp6", "udp", "udp6"):
            raw = self.io.read_bytes(
                self._path(pid, "net", net), _MAX_NET_BYTES
            )
            lines[net] = tuple(raw.splitlines()[1:])
        return self._parse_net_lines(inodes, lines)

    def _ssh_endpoints(self, pid: int) -> tuple[Endpoint, ...]:
        raw = self.io.read_bytes(self._path(pid, "environ"), _MAX_PROC_BYTES)
        return self._parse_ssh_environment(raw)

    def _raise_for_zombie(
        self, pid: int, machine: str,
        identity: ProcessIdentity, parent_pid: int,
    ) -> None:
        """Reject a stable zombie after checking it has no descendants."""
        confirmed, confirmed_parent, confirmed_state = self._stat_record(
            pid, machine
        )
        if (confirmed != identity or confirmed_parent != parent_pid
                or confirmed_state != "Z"):
            raise CollectionFailure(
                "process_identity_changed", "proc",
                "process identity changed during collection", True,
            )
        if self._children(pid):
            raise CollectionFailure(
                "zombie_process_has_children", "proc",
                "zombie process unexpectedly has children", True,
            )
        final, final_parent, final_state = self._stat_record(pid, machine)
        if (final != identity or final_parent != parent_pid
                or final_state != "Z"):
            raise CollectionFailure(
                "process_identity_changed", "proc",
                "process identity changed during collection", True,
            )
        raise CollectionFailure(
            "process_not_live", "proc", "process is a zombie", False
        )

    def _node(self, pid: int, machine: str) -> ProcessNode:
        identity, parent_pid, state = self._stat_record(pid, machine)
        if state == "Z":
            self._raise_for_zombie(pid, machine, identity, parent_pid)
        argv_raw = self.io.read_bytes(self._path(pid, "cmdline"), _MAX_PROC_BYTES)
        argv = tuple(argv_raw.decode("utf-8", "replace").rstrip("\0").split("\0"))
        endpoint_eligible = transport_socket_eligible(argv)
        endpoints = self._endpoints(pid) if endpoint_eligible else ()
        ssh_endpoints: tuple[Endpoint, ...] = ()
        try:
            tty = self.io.readlink(
                self._path(pid, "fd", 0), _MAX_PROC_LINK_CHARS
            )
        except OSError:
            tty = ""
        after, after_parent, after_state = self._stat_record(pid, machine)
        if after_state == "Z":
            if after != identity or after_parent != parent_pid:
                raise CollectionFailure(
                    "process_identity_changed", "proc",
                    "process identity changed during collection", True,
                )
            self._raise_for_zombie(pid, machine, identity, parent_pid)
        endpoints_after = self._endpoints(pid) if endpoint_eligible else ()
        argv_raw_after = self.io.read_bytes(
            self._path(pid, "cmdline"), _MAX_PROC_BYTES
        )
        if (after != identity or after_parent != parent_pid
                or argv_raw_after != argv_raw or endpoints_after != endpoints):
            raise CollectionFailure("process_identity_changed", "proc",
                                    "process observation changed during collection", True)
        parent = None
        if parent_pid > 1:
            parent = self._stat(parent_pid, machine)[0]
        final, final_parent, final_state = self._stat_record(pid, machine)
        if final_state == "Z":
            if final != identity or final_parent != parent_pid:
                raise CollectionFailure(
                    "process_identity_changed", "proc",
                    "process identity changed during collection", True,
                )
            self._raise_for_zombie(pid, machine, identity, parent_pid)
        if final != identity or final_parent != parent_pid:
            raise CollectionFailure("process_identity_changed", "proc",
                                    "process identity changed during collection", True)
        return ProcessNode(identity, parent, argv, endpoints, ssh_endpoints, tty)

    def _children(self, pid: int) -> tuple[int, ...]:
        names = self.io.listdir(self._path(pid, "task"), 1024)
        result: set[int] = set()
        for name in names:
            if not name.isdigit():
                continue
            raw = self.io.read_bytes(
                self._path(pid, "task", name, "children"), _MAX_PROC_BYTES
            )
            for value in raw.split():
                child = int(value)
                if 1 <= child <= MAX_PID:
                    result.add(child)
        return tuple(sorted(result))

    def _descendants(
        self, pid: int, machine: str, deadline: Deadline,
        retained: list[ProcessNode] | None = None,
    ) -> tuple[ProcessNode, ...]:
        queue: list[tuple[int, int]] = [(pid, 0)]
        seen: set[int] = set()
        result: list[ProcessNode] = retained if retained is not None else []
        while queue:
            if deadline.expired():
                raise CollectionFailure("deadline_exceeded", "proc",
                                        "process scan deadline expired", True)
            current, depth = queue.pop(0)
            if current in seen:
                continue
            if len(seen) >= _MAX_GRAPH_NODES:
                raise CollectionFailure("process_scan_truncated", "proc",
                                        "process graph node bound exceeded", True)
            seen.add(current)
            try:
                result.append(self._node(current, machine))
            except CollectionFailure as error:
                # A zombie has already exited and therefore cannot own live
                # transport evidence.  Omit only a confirmed non-root zombie
                # leaf; all other process-scan failures remain fail-closed.
                if (current == pid
                        or error.error.code != "process_not_live"):
                    raise
                continue
            if depth >= _MAX_GRAPH_DEPTH:
                raise CollectionFailure("process_scan_truncated", "proc",
                                        "process graph depth bound exceeded", True)
            for child in self._children(current):
                if child not in seen:
                    queue.append((child, depth + 1))
        return tuple(result)

    def _ancestors(
        self, pid: int, machine: str, deadline: Deadline
    ) -> tuple[ProcessNode, ...]:
        result: list[ProcessNode] = []
        seen: set[int] = set()
        current = pid
        for _ in range(_MAX_GRAPH_DEPTH):
            if deadline.expired():
                raise CollectionFailure("deadline_exceeded", "proc",
                                        "process scan deadline expired", True)
            if current in seen:
                raise CollectionFailure("process_graph_cycle", "proc",
                                        "process graph contains a cycle")
            seen.add(current)
            node = self._node(current, machine)
            result.append(node)
            if node.parent is None or node.parent.pid <= 1:
                return tuple(result)
            current = node.parent.pid
        raise CollectionFailure("process_scan_truncated", "proc",
                                "process ancestor depth bound exceeded", True)

    def _window(
        self, request: Request, window: Any, deadline: Deadline
    ) -> WindowObservation:
        retained: list[ProcessNode] = []
        try:
            nodes = self._descendants(window.pid, request.local_machine, deadline, retained)
            return WindowObservation(window, nodes)
        except (OSError, ValueError, CollectionFailure) as error:
            observation = (
                error.error if isinstance(error, CollectionFailure)
                else ObservationError("local_process_unreadable", "proc",
                                      str(error), True)
            )
            state = "unreachable" if observation.code == "deadline_exceeded" else "partial"
            nodes = tuple(retained) if request.operation == "match" else ()
            return WindowObservation(window, nodes, state, (observation,))

    @staticmethod
    def _decode_bundle(value: Any, maximum: int) -> bytes:
        if not isinstance(value, str):
            raise ValueError("bundle value is not text")
        padding = "=" * (-len(value) % 4)
        result = base64.b64decode(
            value + padding, altchars=b"-_", validate=True
        )
        if len(result) > maximum:
            raise ValueError("bundle value exceeds bound")
        return result

    def _raw_network_snapshot(
        self, inodes_raw: Any, net_raw: Any
    ) -> tuple[Endpoint, ...]:
        if (not isinstance(inodes_raw, list) or len(inodes_raw) > 1024
                or not all(isinstance(item, str) and item.isdigit()
                           for item in inodes_raw)):
            raise ValueError("socket inode list is invalid")
        if inodes_raw != sorted(set(inodes_raw)):
            raise ValueError("socket inode list is not canonical")
        if not isinstance(net_raw, Mapping) or set(net_raw) != {
            "tcp", "tcp6", "udp", "udp6"
        }:
            raise ValueError("network line bundle is invalid")
        net_lines: dict[str, tuple[bytes, ...]] = {}
        for name in ("tcp", "tcp6", "udp", "udp6"):
            items = net_raw[name]
            if not isinstance(items, list) or len(items) > 1024:
                raise ValueError("network line list is invalid")
            net_lines[name] = tuple(
                self._decode_bundle(item, 4096) for item in items
            )
        return self._parse_net_lines(set(inodes_raw), net_lines)

    def _raw_process_fields(
        self, machine: str, value: Any
    ) -> tuple[ProcessIdentity, int, tuple[str, ...],
               tuple[Endpoint, ...], tuple[Endpoint, ...], str]:
        if not isinstance(value, Mapping):
            raise ValueError("process is not an object")
        expected_keys = {
            "pid", "parentPid", "statBefore", "statAfter",
            "cmdlineBefore", "cmdlineAfter",
            "environmentBefore", "environmentAfter",
            "fdInodesBefore", "fdInodesAfter",
            "netLinesBefore", "netLinesAfter", "tty",
        }
        if set(value) != expected_keys:
            raise ValueError("process record fields are invalid")
        pid = value.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError("process PID is invalid")
        before = self._decode_bundle(value.get("statBefore"), _MAX_PROC_BYTES)
        after = self._decode_bundle(value.get("statAfter"), _MAX_PROC_BYTES)
        identity, parent_pid = self._parse_stat(before, pid, machine)
        after_identity, after_parent_pid = self._parse_stat(after, pid, machine)
        if identity != after_identity or parent_pid != after_parent_pid:
            raise ValueError("process identity changed")
        if value.get("parentPid") != parent_pid:
            raise ValueError("process parent changed")
        cmdline_before = self._decode_bundle(
            value.get("cmdlineBefore"), _MAX_PROC_BYTES
        )
        cmdline_after = self._decode_bundle(
            value.get("cmdlineAfter"), _MAX_PROC_BYTES
        )
        if cmdline_before != cmdline_after:
            raise ValueError("process command changed")
        argv = tuple(cmdline_before.decode(
            "utf-8", "replace"
        ).rstrip("\0").split("\0"))
        environment_before = self._decode_bundle(
            value.get("environmentBefore"), _MAX_PROC_BYTES
        )
        environment_after = self._decode_bundle(
            value.get("environmentAfter"), _MAX_PROC_BYTES
        )
        if environment_before != environment_after:
            raise ValueError("process environment changed")
        if value.get("fdInodesBefore") != value.get("fdInodesAfter"):
            raise ValueError("process socket identities changed")

        endpoints_before = self._raw_network_snapshot(
            value.get("fdInodesBefore"), value.get("netLinesBefore")
        )
        endpoints_after = self._raw_network_snapshot(
            value.get("fdInodesAfter"), value.get("netLinesAfter")
        )
        if endpoints_before != endpoints_after:
            raise ValueError("process endpoints changed")

        tty = value.get("tty", "")
        if not isinstance(tty, str) or len(tty) > 128:
            raise ValueError("tty is invalid")
        return (
            identity, parent_pid, argv,
            endpoints_before,
            self._parse_ssh_environment(environment_before),
            tty,
        )

    def _processes_from_json(
        self, machine: str, values: Any, *, require_chain: bool = True,
        allow_truncated_tail: bool = False,
    ) -> tuple[ProcessNode, ...]:
        if not isinstance(values, list) or not 1 <= len(values) <= _MAX_GRAPH_NODES:
            raise ValueError("process graph is invalid")
        fields = [self._raw_process_fields(machine, item) for item in values]
        if require_chain or allow_truncated_tail:
            for index, item in enumerate(fields):
                parent_pid = item[1]
                if index + 1 < len(fields):
                    if parent_pid != fields[index + 1][0].pid:
                        raise ValueError("process ancestry is discontinuous")
                elif require_chain and parent_pid > 1:
                    raise ValueError("process ancestry is truncated")

        by_pid: dict[int, ProcessIdentity] = {}
        by_identity: dict[ProcessIdentity, tuple[Any, ...]] = {}
        for item in fields:
            identity = item[0]
            if identity.pid in by_pid and by_pid[identity.pid] != identity:
                raise ValueError("PID identity changed in process graph")
            by_pid[identity.pid] = identity
            prior = by_identity.get(identity)
            if prior is not None and prior != item:
                raise ValueError("conflicting process observations")
            by_identity[identity] = item
        parent_by_pid = {
            identity.pid: item[1] for identity, item in by_identity.items()
        }
        for origin in parent_by_pid:
            current = origin
            seen: set[int] = set()
            while current in parent_by_pid:
                if current in seen:
                    raise ValueError("process ancestry contains a cycle")
                seen.add(current)
                parent_pid = parent_by_pid[current]
                if parent_pid <= 1:
                    break
                current = parent_pid

        nodes: list[ProcessNode] = []
        for identity, item in by_identity.items():
            _, parent_pid, argv, endpoints, ssh_endpoints, tty = item
            nodes.append(ProcessNode(
                identity, by_pid.get(parent_pid), argv,
                endpoints, ssh_endpoints, tty,
            ))
        return tuple(nodes)

    def parse_process_bundle(
        self, machine: str, values: Any, *, require_chain: bool = True
    ) -> tuple[ProcessNode, ...]:
        """Parse bounded raw process records using the production parsers."""
        return self._processes_from_json(
            canonical_machine(machine), values, require_chain=require_chain
        )


    @classmethod
    def _transport_boundary_valid(
        cls, kind: Any, node: ProcessNode, raw_node: Any,
    ) -> bool:
        def complete(endpoints: tuple[Endpoint, ...]) -> bool:
            if len(endpoints) != 1:
                return False
            endpoint = endpoints[0]
            if not all(1 <= port <= 65535 for port in (
                endpoint.local_port, endpoint.remote_port,
            )):
                return False
            try:
                local = ipaddress.ip_address(endpoint.local_address)
                remote = ipaddress.ip_address(endpoint.remote_address)
            except ValueError:
                return False
            return (
                local.version == remote.version
                and not local.is_unspecified
                and not remote.is_unspecified
            )

        try:
            environment = cls._decode_bundle(
                raw_node["environmentBefore"], _MAX_PROC_BYTES,
            )
            ssh_count = sum(
                item.startswith(b"SSH_CONNECTION=")
                for item in environment.split(b"\0")
            )
            inodes = set(raw_node["fdInodesBefore"])

            def udp_count(snapshot: Any) -> int:
                count = 0
                for name in ("udp", "udp6"):
                    for encoded in snapshot[name]:
                        fields = cls._decode_bundle(encoded, 4096).split()
                        if len(fields) >= 10 and fields[9].decode("ascii") in inodes:
                            count += 1
                return count

            udp_before = udp_count(raw_node["netLinesBefore"])
            udp_after = udp_count(raw_node["netLinesAfter"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            return False

        recognized: set[str] = set()
        ssh_endpoints = tuple(
            endpoint for endpoint in node.ssh_endpoints
            if endpoint.protocol == "tcp"
        )
        if ssh_count == 1 and complete(ssh_endpoints):
            recognized.add("ssh_environment")
        executable = PurePath(node.argv[0]).name if node.argv else ""
        mosh_endpoints = tuple(
            endpoint for endpoint in node.endpoints
            if endpoint.protocol == "udp"
        )
        if (udp_before == 1 and udp_after == 1
                and executable in {"mosh-client", "mosh-server"}
                and complete(mosh_endpoints)):
            recognized.add("mosh_udp")
        return recognized == {kind}

    def parse_target_output(
        self, request: Request, output: bytes
    ) -> TargetObservation:
        """Parse one raw tmux/SSH probe bundle without performing IO."""
        target = request.target
        if not isinstance(output, bytes) or len(output) > _MAX_REMOTE_BYTES:
            raise CollectionFailure(
                "probe_output_too_large", "transport",
                "tmux probe output exceeded its bound", True,
            )
        if target.tmux is None:
            raise CollectionFailure(
                "tmux_output_invalid", "tmux",
                "tmux probe output supplied for a non-tmux target",
            )
        try:
            raw = json.loads(output)
            if not isinstance(raw, Mapping):
                raise ValueError("probe response is not an object")
            if set(raw) != {
                "socketPathBefore", "socketPathAfter",
                "paneLineBefore", "paneLineAfter",
                "clientLinesBefore", "clientLinesAfter",
                "paneProcess", "processes", "clients",
            } and set(raw) != {
                "socketPathBefore", "socketPathAfter",
                "paneLineBefore", "paneLineAfter",
                "clientLinesBefore", "clientLinesAfter",
                "paneProcess", "targetBoundary", "processes", "clients",
            }:
                raise ValueError("probe response fields are invalid")
            machine = target.identity.machine
            socket_path = raw["socketPathBefore"]
            socket_path_after = raw["socketPathAfter"]
            if socket_path != socket_path_after:
                raise ValueError("tmux socket changed")
            if (not isinstance(socket_path, str) or not socket_path.startswith("/")
                    or len(socket_path) > 4096 or any(
                        ord(char) < 32 or ord(char) == 127 for char in socket_path
                    )):
                raise ValueError("invalid socket path")
            selector = target.tmux.socket or SocketSelector("path", socket_path)
            pane_line = raw["paneLineBefore"]
            if not isinstance(pane_line, str) or pane_line != raw["paneLineAfter"]:
                raise ValueError("tmux pane changed")
            pane_fields = pane_line.split("\t")
            if len(pane_fields) != 4:
                raise ValueError("invalid pane line")
            pane_pid = int(pane_fields[3])
            pane_process = self._processes_from_json(
                machine, [raw["paneProcess"]], require_chain=False
            )[0]
            if "targetBoundary" in raw:
                boundary = raw["targetBoundary"]
                if (not isinstance(boundary, Mapping)
                        or set(boundary) != {"kind", "pid", "startTimeTicks"}
                        or boundary.get("kind") != "pane"
                        or boundary.get("pid") != pane_pid
                        or boundary.get("startTimeTicks")
                        != pane_process.identity.start_time_ticks):
                    raise ValueError("target ancestry boundary is invalid")
                processes = self._processes_from_json(
                    machine, raw["processes"], require_chain=False,
                    allow_truncated_tail=True,
                )
                if (not processes
                        or processes[-1].identity != pane_process.identity):
                    raise ValueError(
                        "target ancestry does not end at verified pane process"
                    )
            else:
                # Older producers emitted a complete target ancestry chain.
                # Keep accepting that shape; only the explicit boundary form
                # permits a chain to stop at the separately verified pane.
                processes = self._processes_from_json(machine, raw["processes"])
            if pane_process.identity.pid != pane_pid:
                raise ValueError("pane process mismatch")
            if all(node.identity != pane_process.identity for node in processes):
                processes += (pane_process,)
            pane = TmuxPane(
                pane_fields[0], pane_fields[1], pane_fields[2],
                pane_process.identity,
            )
            raw_clients = raw["clients"]
            client_lines = raw["clientLinesBefore"]
            if (not isinstance(client_lines, list)
                    or client_lines != raw["clientLinesAfter"]
                    or len(client_lines) > _MAX_CLIENTS
                    or not all(isinstance(line, str) for line in client_lines)
            ):
                raise ValueError("tmux clients changed")
            if not isinstance(raw_clients, list) or len(raw_clients) > _MAX_CLIENTS:
                raise ValueError("too many clients")
            if len(raw_clients) != len(client_lines):
                raise ValueError("client evidence count mismatch")
            clients: list[TmuxClient] = []
            client_identities: set[ProcessIdentity] = set()
            for index, item in enumerate(raw_clients):
                if not isinstance(item, Mapping):
                    raise ValueError("client is not an object")
                has_boundary = "transportBoundary" in item
                expected = {"line", "processes"}
                if has_boundary:
                    expected.add("transportBoundary")
                if set(item) != expected or item.get("line") != client_lines[index]:
                    raise ValueError("client fields are invalid")
                fields = item["line"].split("\t")
                if len(fields) != 5 or not fields[0]:
                    raise ValueError("invalid client line")
                client_pid = int(fields[1])
                client_processes = self._processes_from_json(
                    machine, item["processes"], require_chain=not has_boundary,
                    allow_truncated_tail=has_boundary,
                )
                if has_boundary and not self._transport_boundary_valid(
                    item["transportBoundary"], client_processes[-1],
                    item["processes"][-1],
                ):
                    raise ValueError("client transport boundary is invalid")
                if client_processes[0].identity.pid != client_pid:
                    raise ValueError("client process mismatch")
                if client_processes[0].identity in client_identities:
                    raise ValueError("duplicate client identity")
                client_identities.add(client_processes[0].identity)
                clients.append(TmuxClient(
                    fields[0], client_processes[0].identity,
                    fields[2], fields[3], fields[4], client_processes,
                ))
            return TargetObservation(
                machine, selector, socket_path, processes, pane, tuple(clients),
                target_boundary=(pane_process.identity
                                 if "targetBoundary" in raw else None),
                target_chain_complete=("targetBoundary" not in raw),
            )
        except (KeyError, TypeError, ValueError, UnicodeError,
                json.JSONDecodeError, RecursionError) as error:
            raise CollectionFailure(
                "tmux_output_invalid", "tmux",
                "tmux probe output was invalid", True,
            ) from error

    def _target_probe(
        self, request: Request, deadline: Deadline
    ) -> TargetObservation:
        target = request.target
        assert target.tmux is not None
        config: dict[str, Any] = {
            "pid": target.identity.pid,
            "session": target.tmux.session,
            "windowIndex": target.tmux.window_index,
            "paneId": target.tmux.pane_id,
        }
        if target.tmux.socket is not None:
            config["socket"] = {
                "kind": target.tmux.socket.kind,
                "value": target.tmux.socket.value,
            }
        payload = base64.urlsafe_b64encode(
            json.dumps(config, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        local = machine_matches(target.identity.machine, request.local_machine)
        if local:
            argv = [self.python, "-c", _REMOTE_PROBE, payload]
        else:
            remote = shlex.join([self.python, "-c", _REMOTE_PROBE, payload])
            argv = [
                self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "ClearAllForwardings=yes",
                "-o", "PermitLocalCommand=no",
                "-T", "--", target.identity.machine, remote,
            ]
        result = self.io.run(
            argv,
            timeout=min(deadline.remaining(), 12.0),
            max_stdout=_MAX_REMOTE_BYTES,
            max_stderr=16_384,
            env=self._environment(),
        )
        if result.timed_out:
            raise CollectionFailure("remote_unreachable", "transport",
                                    "target probe timed out", True)
        if result.truncated:
            raise CollectionFailure("probe_output_too_large", "transport",
                                    "target probe output exceeded its bound", True)
        if result.returncode != 0:
            raise CollectionFailure("remote_unreachable", "transport",
                                    "target probe failed", True)
        return self.parse_target_output(request, result.stdout)

    def _local_match_target(
        self, request: Request, deadline: Deadline
    ) -> TargetObservation:
        """Collect only current local tmux client locations for ``match``.

        This is intentionally not a reduced strict target probe: no pane
        process, transport endpoint, or client ancestry is read here.  The
        matcher combines the current tmux location/PID rows with the local
        compositor window process subtree and reports that combination as
        heuristic evidence.
        """
        target = request.target
        assert target.tmux is not None
        requested_socket = target.tmux.socket
        base = ["tmux"]
        if requested_socket is not None:
            base += [
                "-L" if requested_socket.kind == "name" else "-S",
                requested_socket.value,
            ]

        def run(argv: Sequence[str], maximum: int) -> bytes:
            result = self.io.run(
                argv,
                timeout=min(deadline.remaining(), 5.0),
                max_stdout=maximum,
                max_stderr=16_384,
                env=self._environment(),
            )
            if result.timed_out:
                raise CollectionFailure(
                    "local_tmux_probe_timeout", "tmux",
                    "local tmux match probe timed out", True,
                )
            if result.truncated:
                raise CollectionFailure(
                    "probe_output_too_large", "tmux",
                    "local tmux match output exceeded its bound", True,
                )
            if result.returncode != 0:
                raise CollectionFailure(
                    "local_tmux_probe_failed", "tmux",
                    "local tmux match probe failed", True,
                )
            return result.stdout

        socket_path = run(
            base + ["display-message", "-p", "#{socket_path}"],
            4096,
        ).decode("utf-8", "strict").strip()
        if (not socket_path.startswith("/") or "\n" in socket_path
                or len(socket_path) > 4096
                or any(ord(char) < 32 or ord(char) == 127
                       for char in socket_path)):
            raise CollectionFailure(
                "local_tmux_output_invalid", "tmux",
                "local tmux socket path was invalid", True,
            )

        client_output = run(
            ["tmux", "-S", socket_path, "list-clients", "-F",
             "#{client_name}\t#{client_pid}\t#{client_session}\t"
             "#{window_index}\t#{pane_id}"],
            _MAX_LOCAL_MATCH_BYTES,
        ).decode("utf-8", "strict")
        client_lines = client_output.splitlines()
        if len(client_lines) > _MAX_CLIENTS:
            raise CollectionFailure(
                "local_tmux_output_invalid", "tmux",
                "local tmux client count exceeded its bound", True,
            )

        clients: list[TmuxClient] = []
        errors: list[ObservationError] = [ObservationError(
            "local_tmux_match_only", "tmux",
            "local matching used current tmux client location and PID only; "
            "strict target ancestry was not collected",
        )]
        seen_pids: set[int] = set()
        for line in client_lines:
            fields = line.split("\t")
            if (len(fields) != 5 or not fields[0] or not fields[2]
                    or not fields[3] or not fields[4]):
                errors.append(ObservationError(
                    "local_tmux_client_invalid", "tmux",
                    "local tmux client row was invalid", True,
                ))
                continue
            try:
                client_pid = int(fields[1])
            except ValueError:
                client_pid = 0
            if not 1 <= client_pid <= MAX_PID or client_pid in seen_pids:
                errors.append(ObservationError(
                    "local_tmux_client_invalid", "tmux",
                    "local tmux client PID was invalid or duplicated", True,
                ))
                continue
            try:
                identity, _ = self._stat(client_pid, request.local_machine)
            except (OSError, ValueError, CollectionFailure) as error:
                errors.append(
                    error.error if isinstance(error, CollectionFailure)
                    else ObservationError(
                        "local_tmux_client_unreadable", "proc",
                        str(error), True,
                    )
                )
                continue
            seen_pids.add(client_pid)
            clients.append(TmuxClient(
                fields[0], identity, fields[2], fields[3], fields[4], ()
            ))

        selector = requested_socket or SocketSelector("path", socket_path)
        return TargetObservation(
            request.local_machine, selector, socket_path, (), None,
            tuple(clients), "partial", tuple(errors),
        )

    def _target(
        self, request: Request, deadline: Deadline
    ) -> TargetObservation:
        target = request.target
        try:
            if target.tmux is not None:
                if (request.operation == "match"
                        and machine_matches(
                            target.identity.machine, request.local_machine
                        )):
                    return self._local_match_target(request, deadline)
                return self._target_probe(request, deadline)
            if not machine_matches(target.identity.machine, request.local_machine):
                raise CollectionFailure(
                    "remote_non_tmux_unsupported", "transport",
                    "v1 cannot resolve a remote target without tmux",
                )
            # The resolver only needs the target's live identity here.  A
            # direct target has no transport or pane boundary to prove, and
            # walking unrelated ancestors can fail on an inaccessible
            # descriptor even though the target itself is readable.  Window
            # process subtrees establish the local ancestry relation when a
            # candidate is evaluated.
            nodes = (self._node(
                target.identity.pid, target.identity.machine
            ),)
            return TargetObservation(
                target.identity.machine, None, None, nodes, None, ()
            )
        except (OSError, ValueError, CollectionFailure) as error:
            observation = (
                error.error if isinstance(error, CollectionFailure)
                else ObservationError("target_process_unreadable", "proc",
                                      str(error), True)
            )
            state = "unreachable" if observation.retryable else "partial"
            return TargetObservation(
                target.identity.machine, target.tmux.socket if target.tmux else None,
                None, (), None, (), state, (observation,),
            )

    @staticmethod
    def _remote_match_target(request: Request) -> TargetObservation:
        """Describe a remote target that best-effort matching did not probe."""
        target = request.target
        return TargetObservation(
            target.identity.machine,
            target.tmux.socket if target.tmux is not None else None,
            None, (), None, (), "partial",
            (ObservationError(
                "remote_target_not_probed", "transport",
                "remote target probing is skipped for best-effort matching",
            ),),
        )

    def collect(self, request: Request, deadline: Deadline) -> TopologySnapshot:
        windows = tuple(self._window(request, window, deadline)
                        for window in request.windows)
        if (request.operation == "match"
                and not machine_matches(
                    request.target.identity.machine, request.local_machine
                )):
            target = self._remote_match_target(request)
        else:
            target = self._target(request, deadline)
        return TopologySnapshot(windows, target)


__all__ = ["CollectionFailure", "DefaultProbeIO", "LinuxCollector"]
