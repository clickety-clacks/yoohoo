"""Bounded observation of which remote transports a host accepts.

This module only observes. It reports whether et and mosh would actually
reach the host from here, measured over the network, and never chooses a
transport: that is the calling application's policy.

One SSH connection runs a small standard-library probe on the target. The
probe reports whether ``etserver`` is running (and on which port) and whether
``etterminal`` and ``mosh-server`` are on the non-interactive PATH that et and
mosh will use. It then holds a UDP socket open in mosh's port range while this
side sends a nonce to it and waits for the echo, and this side opens (and
immediately closes) a TCP connection to etserver's port. Both use the server
address SSH itself reached, taken from ``SSH_CONNECTION``.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import selectors
import shlex
import socket
import subprocess
import time
from typing import Any, Callable, Mapping

TRANSPORTS = ("ssh", "et", "mosh")
STATES = {"available", "unavailable", "unknown"}
OBSERVATION_STATES = {"complete", "partial", "unreachable", "not_applicable"}
_MAX_LINE = 4096
_MAX_STDERR = 16_384
_UDP_WAIT = 2.5
_TCP_WAIT = 2.0
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")

_REMOTE_TRANSPORT_PROBE = r"""
import base64, json, os, random, re, select, shutil, socket, sys, time

payload = sys.argv[1]
cfg = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
nonce = bytes.fromhex(cfg["nonce"])

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def config_port(path):
    try:
        with open(path, "rb") as stream:
            text = stream.read(65536).decode("utf-8", "replace")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*port\s*=\s*([0-9]{1,5})\s*$", text)
    return int(match.group(1)) if match else None

def etserver_port():
    running, ports = False, []
    try:
        names = os.listdir("/proc")
    except OSError:
        names = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % name, "rb") as stream:
                if stream.read(64).strip() != b"etserver":
                    continue
            with open("/proc/%s/cmdline" % name, "rb") as stream:
                argv = [os.fsdecode(item) for item in stream.read(65536).split(b"\0")]
        except OSError:
            continue
        running = True
        port, cfgfile = None, None
        for index, value in enumerate(argv):
            following = argv[index + 1] if index + 1 < len(argv) else ""
            if value == "--port" and following.isdigit():
                port = int(following)
            elif value.startswith("--port=") and value[7:].isdigit():
                port = int(value[7:])
            elif value == "--cfgfile":
                cfgfile = following
            elif value.startswith("--cfgfile="):
                cfgfile = value[10:]
        if not port:
            port = config_port(cfgfile) if cfgfile else None
        if not port:
            port = config_port("/etc/et.cfg") or 2022
        if 1 <= port <= 65535:
            ports.append(port)
    return running, sorted(set(ports))

fields = os.environ.get("SSH_CONNECTION", "").split()
server = fields[2] if len(fields) == 4 else ""
running, ports = etserver_port()
mosh_server = shutil.which("mosh-server") is not None
udp, udp_port = None, 0
if mosh_server and server:
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    udp = socket.socket(family, socket.SOCK_DGRAM)
    for _ in range(32):
        candidate = random.randint(60001, 60999)
        try:
            udp.bind((server, candidate))
        except OSError:
            continue
        udp_port = candidate
        break
emit({
    "phase": "ready",
    "server": server,
    "etserverRunning": running,
    "etPorts": ports,
    "etterminal": shutil.which("etterminal") is not None,
    "moshServer": mosh_server,
    "udpPort": udp_port,
})
received = False
if udp_port:
    deadline = time.monotonic() + float(cfg["udpWait"])
    while time.monotonic() < deadline:
        ready, _, _ = select.select([udp], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        data, address = udp.recvfrom(256)
        if data == nonce:
            received = True
            for _ in range(3):
                udp.sendto(nonce, address)
            break
emit({"phase": "done", "udpReceived": received})
"""


def entry(state: str, code: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"state": state, "code": code}
    result.update(details)
    return result


def unknown(observation_state: str, code: str) -> dict[str, Any]:
    return {
        "state": observation_state,
        **{name: entry("unknown", code) for name in TRANSPORTS},
    }


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.is_unspecified or address.is_multicast:
        return None
    return str(address)


def _udp_echo(server: str, port: int, nonce: bytes, wait: float) -> bool:
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        end = time.monotonic() + wait
        next_send = 0.0
        while True:
            now = time.monotonic()
            if now >= end:
                return False
            if now >= next_send:
                try:
                    sock.sendto(nonce, (server, port))
                except OSError:
                    return False
                next_send = now + 0.2
            with selectors.DefaultSelector() as select:
                select.register(sock, selectors.EVENT_READ)
                if select.select(max(0.0, min(next_send, end) - now)):
                    try:
                        data, _ = sock.recvfrom(256)
                    except OSError:
                        continue
                    if data == nonce:
                        return True


def _tcp_open(server: str, port: int, wait: float) -> bool:
    try:
        with socket.create_connection((server, port), timeout=wait):
            return True
    except OSError:
        return False


class TransportProber:
    """Run the bounded probe for one remote machine."""

    def __init__(
        self,
        *,
        ssh: str = "ssh",
        python: str = "python3",
        environment: Mapping[str, str] | None = None,
        udp_echo: Callable[[str, int, bytes, float], bool] = _udp_echo,
        tcp_open: Callable[[str, int, float], bool] = _tcp_open,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.ssh = ssh
        self.python = python
        self.environment = dict(environment) if environment is not None else None
        self.udp_echo = udp_echo
        self.tcp_open = tcp_open
        self.popen = popen

    def argv(self, machine: str, payload: str) -> list[str]:
        remote = shlex.join([self.python, "-c", _REMOTE_TRANSPORT_PROBE, payload])
        return [
            self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no",
            "-T", "--", machine, remote,
        ]

    def probe(self, machine: str, timeout: float) -> dict[str, Any]:
        if timeout < 1.0:
            return unknown("partial", "deadline_exhausted")
        nonce = secrets.token_bytes(16)
        udp_wait = max(0.2, min(_UDP_WAIT, timeout / 3))
        config = {"nonce": nonce.hex(), "udpWait": udp_wait + 0.5}
        payload = base64.urlsafe_b64encode(
            json.dumps(config, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        try:
            process = self.popen(
                self.argv(machine, payload),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
                env=self.environment,
            )
        except (OSError, ValueError):
            return unknown("unreachable", "ssh_unavailable")
        deadline = time.monotonic() + timeout
        try:
            ready = _read_line(process, deadline)
            if ready is None:
                # ssh exits 255 for its own failures; anything else means the
                # host answered but the probe could not run there.
                try:
                    code = process.wait(timeout=max(0.0, min(0.5, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    code = None
                if code is not None and code != 255:
                    return unknown("partial", "probe_failed")
                return unknown("unreachable", "remote_unreachable")
            try:
                report = json.loads(ready)
            except ValueError:
                return unknown("partial", "probe_output_invalid")
            if not isinstance(report, dict) or report.get("phase") != "ready":
                return unknown("partial", "probe_output_invalid")
            result = self._interpret(report, nonce, udp_wait, deadline)
            # The remote side's own verdict is informational; reading it lets
            # the probe exit cleanly instead of being killed mid-write.
            _read_line(process, min(deadline, time.monotonic() + 1.0))
            return result
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.SubprocessError:
                pass
            if process.stdout is not None:
                process.stdout.close()

    def _interpret(
        self, report: Mapping[str, Any], nonce: bytes, udp_wait: float,
        deadline: float,
    ) -> dict[str, Any]:
        server = _address(report.get("server"))
        result: dict[str, Any] = {
            "state": "complete",
            "ssh": entry("available", "ssh_probe_succeeded"),
        }
        ports = report.get("etPorts")
        ports = [
            port for port in ports
            if isinstance(port, int) and not isinstance(port, bool)
            and 1 <= port <= 65535
        ][:8] if isinstance(ports, list) else []
        # UDP first: the remote holds its mosh port open only briefly, and a
        # silently dropped TCP connect must not use up that window.
        udp_port = report.get("udpPort")
        if report.get("moshServer") is not True:
            result["mosh"] = entry("unavailable", "mosh_server_missing")
        elif (server is None or isinstance(udp_port, bool)
              or not isinstance(udp_port, int) or not 1 <= udp_port <= 65535):
            result["mosh"] = entry("unknown", "mosh_udp_not_probed")
        else:
            wait = max(0.1, min(udp_wait, deadline - time.monotonic()))
            result["mosh"] = (
                entry("available", "mosh_udp_passing")
                if self.udp_echo(server, udp_port, nonce, wait)
                else entry("unavailable", "mosh_udp_blocked")
            )
        if report.get("etserverRunning") is not True:
            result["et"] = entry("unavailable", "etserver_not_running")
        elif report.get("etterminal") is not True:
            result["et"] = entry("unavailable", "etterminal_missing")
        elif server is None or not ports:
            result["et"] = entry("unknown", "et_not_probed")
        else:
            reachable = next((
                port for port in ports
                if self.tcp_open(server, port, max(
                    0.1, min(_TCP_WAIT, deadline - time.monotonic())
                ))
            ), None)
            result["et"] = (
                entry("available", "et_reachable", port=reachable)
                if reachable is not None
                else entry("unavailable", "et_port_unreachable", port=ports[0])
            )
        if any(item["state"] == "unknown" for item in (result["et"], result["mosh"])):
            result["state"] = "partial"
        return result


def _read_line(process: Any, deadline: float) -> str | None:
    """Read one bounded line from the probe, or None on EOF or deadline."""
    stream = process.stdout
    buffer = bytearray()
    with selectors.DefaultSelector() as select:
        select.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select(remaining):
                return None
            chunk = os.read(stream.fileno(), 1)
            if not chunk:
                return None
            if chunk == b"\n":
                try:
                    return buffer.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    return None
            buffer.extend(chunk)
            if len(buffer) > _MAX_LINE:
                return None


def normalize(value: Any) -> dict[str, Any]:
    """Return a strictly shaped observation, or a partial unknown one."""
    if not isinstance(value, Mapping) or value.get("state") not in OBSERVATION_STATES:
        return unknown("partial", "collector_observation_invalid")
    result: dict[str, Any] = {"state": value["state"]}
    for name in TRANSPORTS:
        item = value.get(name)
        if (not isinstance(item, Mapping) or item.get("state") not in STATES
                or not isinstance(item.get("code"), str)
                or _CODE.fullmatch(item["code"]) is None
                or set(item) - {"state", "code", "port"}):
            return unknown("partial", "collector_observation_invalid")
        normalized: dict[str, Any] = {"state": item["state"], "code": item["code"]}
        port = item.get("port")
        if port is not None:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                return unknown("partial", "collector_observation_invalid")
            normalized["port"] = port
        result[name] = normalized
    return result


__all__ = ["TRANSPORTS", "TransportProber", "normalize", "unknown"]
