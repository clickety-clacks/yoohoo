"""Best-effort Agentd Hub client and terminal matching helpers.

The hub is deliberately a read-only input.  It publishes complete snapshots
over Server-Sent Events; Yoohoo owns the local acknowledgement ledger and the
decision to open a terminal.  Nothing in this module executes a remote command
while listening to the hub.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import queue
import random
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping
import urllib.error
import urllib.request


HUB_SCHEMA = "agentd-hub.snapshot.v1"
HUB_STATE_VERSION = 1
DEFAULT_RECONNECT_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
HEARTBEAT_SCHEMA = "agentd.hub.heartbeat.v1"
HEARTBEAT_INTERVAL_SECONDS = 15.0
LIVENESS_DEADLINE_SECONDS = 45.0
LIVENESS_CHECK_SECONDS = 1.0
CONNECT_TIMEOUT_SECONDS = 10.0
MAX_SSE_LINE_BYTES = 1 << 20
MAX_SSE_DATA_BYTES = 4 << 20
MAX_AGENTS = 4096

STATUS_CONNECTING = "connecting"
STATUS_LIVE = "live"
STATUS_RECONNECTING = "reconnecting"
STATUS_STALE = "stale"
STATUS_SUSPENDED = "suspended"
STATUS_DISABLED = "disabled"
STATUS_VALUES = {
    STATUS_CONNECTING,
    STATUS_LIVE,
    STATUS_RECONNECTING,
    STATUS_STALE,
    STATUS_SUSPENDED,
    STATUS_DISABLED,
}


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _now_boottime() -> float:
    """Return elapsed real time, including suspend, on Linux."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is not None:
        try:
            return time.clock_gettime(clock)
        except (OSError, ValueError):
            pass
    return time.monotonic()


def _bundled_resolver_adapter():
    """Load Yoohoo's bundled resolver adapter without a runtime dependency."""
    import importlib.util
    import sys
    path = Path(__file__).with_name("agent_window_adapter.py")
    spec = importlib.util.spec_from_file_location("agent_window_adapter", path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("bundled agent_window_adapter is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_window_adapter"] = module
    spec.loader.exec_module(module)
    return module


def resolve_agent_window(
    agent: Mapping[str, Any],
    clients: Iterable[Mapping[str, Any]],
    local_machine: str,
    *,
    prior: Mapping[str, Any] | None = None,
    operation: str = "resolve",
) -> Any:
    """Run the bundled resolver in-process for one bounded operation."""
    adapter = _bundled_resolver_adapter()
    return adapter.resolve_agent(
        agent, clients, local_machine, prior=prior, operation=operation,
    )


def verify_agent_target(
    agent: Mapping[str, Any], local_machine: str, *, probe_transports: bool = False,
) -> Any:
    return _bundled_resolver_adapter().verify_target(
        agent, local_machine, probe_transports=probe_transports,
    )


def resolver_candidate(response: Mapping[str, Any]) -> dict[str, Any] | None:
    return _bundled_resolver_adapter().candidate_window(response)


def resolver_candidate_record(
    response: Mapping[str, Any],
    *,
    clients: Iterable[Mapping[str, Any]] | None = None,
    active_address: str | None = None,
) -> dict[str, Any] | None:
    return _bundled_resolver_adapter().candidate_record(
        response, clients=clients, active_address=active_address,
    )


def resolver_candidate_record_for_window(
    response: Mapping[str, Any], prior: Mapping[str, Any]
) -> dict[str, Any] | None:
    return _bundled_resolver_adapter().candidate_record_for_window(response, prior)


def resolver_window_records(clients: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return _bundled_resolver_adapter().window_records(clients)


def resolver_process_start_ticks(pid: int) -> str | None:
    return _bundled_resolver_adapter().process_start_ticks(pid)


def local_machine_name(config: dict[str, Any] | None = None) -> str:
    """Return the configured local machine label, falling back to hostname."""
    config = config or {}
    section = config.get("hub") if isinstance(config.get("hub"), dict) else config.get("agentd_hub")
    if not isinstance(section, dict):
        section = {}
    value = section.get("machine") or section.get("machine_name") or config.get("agentd_hub_machine")
    return str(value or socket.gethostname()).strip() or socket.gethostname()


def hub_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the optional TOML section without making it mandatory."""
    section = config.get("hub")
    if not isinstance(section, dict):
        section = config.get("agentd_hub")
    if not isinstance(section, dict):
        section = {}
    enabled = section.get("enabled", config.get("agentd_hub_enabled", False))
    url = section.get("events_url", section.get("url", config.get("agentd_hub_url", "")))
    return {
        "enabled": bool(enabled) and bool(str(url or "").strip()),
        "configured": bool(enabled),
        "url": str(url or "").strip(),
        "machine": local_machine_name(config),
    }


def normalize_host(value: str) -> str:
    value = str(value or "").strip().lower().rstrip(".")
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    return value


def machine_matches(left: str, right: str) -> bool:
    """Match short host names and Tailscale FQDNs without broad guessing."""
    left = normalize_host(left)
    right = normalize_host(right)
    if not left or not right:
        return False
    if left == right:
        return True
    # A short label may be compared with its fully-qualified spelling. Do
    # not treat two different dotted names with the same first label as the
    # same machine: that would make a stale/foreign hub claim clickable.
    if "." not in left:
        return right.split(".", 1)[0] == left
    if "." not in right:
        return left.split(".", 1)[0] == right
    return False


def agent_identity(agent: dict[str, Any]) -> str:
    """Stable, same-boot identity for a projected hub agent."""
    ident = agent.get("id") or {}
    return "|".join(
        (
            str(agent.get("machine", "")),
            str(agent.get("instanceId", "")),
            str(ident.get("pid", "")),
            str(ident.get("startTimeTicks", "")),
        )
    )


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("hub snapshot is not an object")
    if value.get("type") != "snapshot" or value.get("schema") != HUB_SCHEMA:
        raise ValueError("unsupported agentd-hub snapshot")
    if not isinstance(value.get("revision"), int) or value["revision"] < 0:
        raise ValueError("hub snapshot revision is invalid")
    if not isinstance(value.get("sources"), list) or not isinstance(value.get("agents"), list):
        raise ValueError("hub snapshot sources or agents is invalid")
    if len(value["agents"]) > MAX_AGENTS:
        raise ValueError("hub snapshot contains too many agents")
    for agent in value["agents"]:
        if not isinstance(agent, dict):
            raise ValueError("hub agent is not an object")
        if not str(agent.get("machine", "")) or not str(agent.get("instanceId", "")):
            raise ValueError("hub agent identity is incomplete")
        ident = agent.get("id")
        if not isinstance(ident, dict) or not isinstance(ident.get("pid"), int) or not isinstance(ident.get("startTimeTicks"), int):
            raise ValueError("hub agent process identity is invalid")
        activity = agent.get("activity")
        if not isinstance(activity, dict) or activity.get("state") not in {"active", "idle", "needs_attention", "unknown"}:
            raise ValueError("hub agent activity is invalid")
    return value


def event_url(base: str) -> str:
    base = str(base or "").strip()
    if base.endswith("/events"):
        return base
    return base.rstrip("/") + "/events"


def parse_sse(lines: Iterable[bytes | str]) -> Iterator[tuple[str, str, str]]:
    """Yield (event, id, data) for complete SSE records."""
    event = "message"
    event_id = ""
    data: list[str] = []

    def emit() -> tuple[str, str, str] | None:
        nonlocal event, event_id, data
        # A named event with no data is still a complete SSE record.  The
        # stream consumer can then reject an empty heartbeat/snapshot instead
        # of silently treating it as healthy input.  Bare comment/blank
        # records remain ignored.
        if not data and event == "message":
            event, event_id = "message", ""
            return None
        item = (event, event_id, "\n".join(data))
        event, event_id, data = "message", "", []
        return item

    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if len(line.encode("utf-8", "replace")) > MAX_SSE_LINE_BYTES:
            raise ValueError("SSE line exceeds limit")
        line = line.rstrip("\r\n")
        if not line:
            item = emit()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        key, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if key == "event":
            event = value
        elif key == "id":
            event_id = value
        elif key == "data":
            data.append(value)
            if sum(len(part.encode("utf-8", "replace")) for part in data) > MAX_SSE_DATA_BYTES:
                raise ValueError("SSE event exceeds limit")
    item = emit()
    if item is not None:
        yield item


def parse_remote_launch(argv: list[str]) -> dict[str, str]:
    """Parse only the explicit Ghostty -> et/mosh/ssh -> host -> tmux forms.

    That includes Yoohoo's own fallback launcher, ``sh -lc SCRIPT
    transport-launch HOST REMOTE PORT STALE WRAPPED PRIMARY``.
    """
    try:
        marker = argv.index("-e")
        command = list(argv[marker + 1 :])
        if (len(command) == 10 and command[:2] in (["sh", "-lc"], ["sh", "-c"])
                and command[3] == "transport-launch"
                and command[9] in {"et", "mosh"}):
            transport, host, command = command[9], command[4], [command[5]]
        elif command and Path(command[0]).name == "et":
            command.pop(0)
            transport, host, remote = "et", "", None
            while command:
                value = command.pop(0)
                if value == "--" and len(command) == 1:
                    host = command.pop(0)
                elif value in {"-p", "--port"} and command:
                    command.pop(0)
                elif value in {"-c", "--command"} and command:
                    remote = command.pop(0)
                elif not value.startswith("-") and not host:
                    host = value
                else:
                    return {}
            if not host or remote is None:
                return {}
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            command = [remote]
        else:
            if not command or Path(command.pop(0)).name not in {"mosh", "ssh"}:
                return {}
            transport = Path(argv[marker + 1]).name
            # Yoohoo forces a remote PTY for ssh; skip only that flag.
            if transport == "ssh" and command and command[0] == "-tt":
                command.pop(0)
            if command and command[0] == "--":
                command.pop(0)
            # Connection options are intentionally not guessed around.  A
            # caller can still use the live tmux/agent PID match in that case.
            if not command or command[0].startswith("-"):
                return {}
            host = command.pop(0)
        # Yoohoo's safe launch form sends one shell-quoted remote command so
        # session names containing spaces survive SSH and mosh. Reparse that
        # command locally for matching; never execute it here.
        if command and command[0] == "sh" and command[1:2] == ["-lc"] and len(command) >= 3:
            try:
                command = shlex.split(command[2])
            except ValueError:
                return {}
        elif len(command) == 1 and (command[0].startswith("exec ")
                                    or command[0].startswith("sh -lc ")):
            try:
                command = shlex.split(command[0])
                if (command[:2] == ["sh", "-lc"] and len(command) == 3):
                    command = shlex.split(command[2])
            except ValueError:
                return {}
        if command and command[0] == "exec":
            command.pop(0)
        if not command or Path(command.pop(0)).name != "tmux":
            return {}
        operation = command.pop(0) if command else ""
        if operation not in {"new", "new-session", "attach", "attach-session"}:
            return {}
        flag = "-s" if operation in {"new", "new-session"} else "-t"
        try:
            index = command.index(flag)
            session = command[index + 1].lstrip("=")
        except (ValueError, IndexError):
            return {}
        if not host or any(ord(char) < 32 for char in host + session):
            return {}
        return {"transport": transport, "host": host, "session": session}
    except (ValueError, IndexError, TypeError):
        return {}


def _safe_remote_part(value: str, *, host: bool = False) -> bool:
    if not value or any(ord(char) < 32 for char in value):
        return False
    if host and any(char in value for char in "/\\;|&$`\"'"):
        return False
    return True


def _process_start_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _default_process_ancestors(pid: int) -> set[int]:
    ancestors: set[int] = set()
    for _ in range(64):
        if pid <= 1 or pid in ancestors:
            break
        ancestors.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def _default_process_argv(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()[:65536].decode("utf-8", "replace").rstrip("\0").split("\0")
    except OSError:
        return []


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _host_from_client(client: dict[str, Any], process_argv: Callable[[int], list[str]]) -> dict[str, str]:
    pid = int(client.get("pid") or 0)
    return parse_remote_launch(process_argv(pid)) if pid else {}


def match_agent_windows(
    agents: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    local_machine: str,
    process_ancestors: Callable[[int], set[int]] = _default_process_ancestors,
    process_argv: Callable[[int], list[str]] = _default_process_argv,
    tmux_panes: list[dict[str, Any]] | None = None,
    tmux_clients: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return exact/one-candidate local window matches keyed by agent identity."""
    result: dict[str, dict[str, Any]] = {}
    for agent in agents:
        key = agent_identity(agent)
        machine = str(agent.get("machine", ""))
        location = agent.get("tmux") or {}
        session = str(location.get("session", ""))
        candidates: list[dict[str, Any]] = []
        # Strongest evidence: the projected agent pid and start ticks are in
        # this terminal's process ancestry.  Never accept a reused pid.
        ident = agent.get("id") or {}
        pid = int(ident.get("pid") or 0)
        ticks = int(ident.get("startTimeTicks") or 0)
        if machine_matches(machine, local_machine) and pid and (
            ticks == 0 or _process_start_ticks(pid) == ticks
        ):
            # tmux's server is detached from the terminal process tree.  The
            # pane PID and client PID are therefore the authoritative bridge
            # from Agentd's pane to the Hyprland terminal.
            for pane in tmux_panes or []:
                if (str(pane.get("session", "")) != session
                        or str(pane.get("paneId", "")) != str(location.get("paneId", ""))
                        or str(pane.get("windowIndex", "")) != str(location.get("windowIndex", ""))):
                    continue
                pane_pid = int(pane.get("panePid") or 0)
                if pane_pid and pane_pid not in process_ancestors(pid) and pane_pid != pid:
                    continue
                for tmux_client in tmux_clients or []:
                    if str(tmux_client.get("session", "")) != session:
                        continue
                    client_pid = int(tmux_client.get("clientPid") or 0)
                    for client in clients:
                        hypr_pid = int(client.get("pid") or 0)
                        if client_pid and hypr_pid in process_ancestors(client_pid):
                            candidates.append(client)
            for client in clients:
                client_pid = int(client.get("pid") or 0)
                if client_pid and client_pid in process_ancestors(pid):
                    candidates.append(client)
        # Remote agents are represented by local mosh/ssh Ghostty launches.
        # Session + host is useful evidence; it is deliberately not a broad
        # title or machine-name guess.
        if not candidates and session:
            for client in clients:
                info = _host_from_client(client, process_argv)
                if info and info.get("session") == session and machine_matches(info.get("host", ""), machine):
                    candidates.append(client)
        unique = {str(candidate.get("address", "")): candidate for candidate in candidates
                  if candidate.get("address")}
        if len(unique) == 1:
            result[key] = next(iter(unique.values()))
    return result


# --- Transport policy -------------------------------------------------------
# One definition shared with Omarchy Ask: agent-window-resolver's
# docs/transport-policy-v1.md. tests/test_transport_policy.py checks this code
# against that repository's vectors, which tests/fixtures carries verbatim.

TRANSPORT_PREFERENCES = ("auto", "local", "et", "mosh", "ssh")
CAPABILITY_SCHEMA = "transport-capabilities.v1"
CAPABILITY_MAX_AGE_MS = 7 * 24 * 3600 * 1000
# A record where every transport is unknown (the probe could not run there)
# is kept briefly: long enough not to re-probe on every click.
UNKNOWN_CAPABILITY_MAX_AGE_MS = 24 * 3600 * 1000
TRANSPORT_LAUNCH_SCRIPT = "\n".join([
    "host=$1 remote=$2 port=$3 stale=$4 wrapped=$5 primary=$6",
    "if [ \"$primary\" = et ]; then",
    "  if [ -n \"$port\" ]; then et -p \"$port\" -c \"$wrapped\" -- \"$host\"; else et -c \"$wrapped\" -- \"$host\"; fi",
    "else",
    "  mosh -- \"$host\" sh -lc \"$remote\"",
    "fi",
    "status=$?",
    "[ \"$status\" -eq 0 ] && exit 0",
    "# A failed start on a reachable host means the recorded capability is stale.",
    "if [ -n \"$stale\" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 -- \"$host\" true >/dev/null 2>&1; then",
    "  rm -f -- \"$stale\"",
    "fi",
    "exec ssh -tt -- \"$host\" \"$wrapped\"",
])


def transport_preference(config: Mapping[str, Any] | None) -> str:
    """Read ``[agentd_hub] transport``; anything unrecognised means auto."""
    config = config or {}
    section = config.get("hub") if isinstance(config.get("hub"), dict) else config.get("agentd_hub")
    value = section.get("transport") if isinstance(section, dict) else None
    return value if value in TRANSPORT_PREFERENCES else "auto"


def choose_transport(
    preference: str,
    target_is_local: bool,
    clients: Mapping[str, bool],
    capabilities: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if preference not in TRANSPORT_PREFERENCES:
        preference = "auto"
    if target_is_local:
        return {"transport": "local", "fallback": None}
    if preference == "local":
        return {"unavailable": "local_requires_local_target"}
    if preference != "auto":
        if not clients.get(preference):
            return {"unavailable": preference + "_client_missing"}
        transport = preference
    elif clients.get("et") and capabilities and capabilities.get("et") == "available":
        transport = "et"
    elif clients.get("mosh") and (not capabilities or capabilities.get("mosh") != "unavailable"):
        transport = "mosh"
    elif clients.get("ssh"):
        transport = "ssh"
    else:
        return {"unavailable": "no_transport_client"}
    fallback = "ssh" if transport != "ssh" and clients.get("ssh") else None
    return {"transport": transport, "fallback": fallback}


def remote_shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def transport_launch_argv(
    transport: str,
    fallback: str | None,
    host: str,
    remote: str,
    et_port: int | None = None,
    stale_file: str = "",
) -> list[str]:
    """The terminal command for a remote transport, with its ssh fallback."""
    wrapped = "sh -lc " + remote_shell_quote(remote)
    if transport == "ssh":
        return ["ssh", "-tt", "--", host, wrapped]
    if fallback is None:
        if transport == "et":
            port = ["-p", str(et_port)] if et_port else []
            return ["et", *port, "-c", wrapped, "--", host]
        return ["mosh", "--", host, "sh", "-lc", remote]
    # A login shell, so the launcher sees the PATH the client lookup saw.
    return ["sh", "-lc", TRANSPORT_LAUNCH_SCRIPT, "transport-launch", host, remote,
            str(et_port or ""), stale_file, wrapped, transport]


def capability_path(state_directory: Path, host: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*", str(host or "")):
        return None
    host = normalize_host(host)
    return Path(state_directory) / "transport-capabilities" / (host + ".json")


def read_capabilities(
    state_directory: Path, host: str, now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Return recorded capability states for host, or None to re-probe."""
    path = capability_path(state_directory, host)
    try:
        record = json.loads(path.read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    now_ms = _now_unix_ms() if now_ms is None else now_ms
    if (not isinstance(record, dict) or record.get("schema") != CAPABILITY_SCHEMA
            or not isinstance(record.get("observedAtUnixMs"), int)
            or not isinstance(record.get("transports"), dict)):
        return None
    transports = record["transports"]
    result: dict[str, Any] = {}
    for name in ("ssh", "et", "mosh"):
        item = transports.get(name)
        state = item.get("state") if isinstance(item, dict) else None
        result[name] = state if state in {"available", "unavailable", "unknown"} else "unknown"
    limit = (UNKNOWN_CAPABILITY_MAX_AGE_MS
             if all(result[name] == "unknown" for name in ("ssh", "et", "mosh"))
             else CAPABILITY_MAX_AGE_MS)
    if not 0 <= now_ms - record["observedAtUnixMs"] <= limit:
        return None
    port = (transports.get("et") or {}).get("port") if isinstance(transports.get("et"), dict) else None
    result["etPort"] = port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else None
    return result


def record_capabilities(
    state_directory: Path, host: str, response: Mapping[str, Any],
    now_ms: int | None = None,
) -> bool:
    """Persist a resolver transport observation; unreachable is never written."""
    transports = response.get("transports")
    path = capability_path(state_directory, host)
    if (path is None or not isinstance(transports, dict)
            or transports.get("state") not in {"complete", "partial"}):
        return False
    record = {
        "schema": CAPABILITY_SCHEMA,
        "host": normalize_host(host),
        "observedAtUnixMs": _now_unix_ms() if now_ms is None else now_ms,
        "resolverVersion": response.get("resolverVersion"),
        "transports": transports,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def connection_plan(
    machine: str,
    session: str,
    local_machine: str,
    clients: list[dict[str, Any]],
    process_argv: Callable[[int], list[str]] = _default_process_argv,
    which: Callable[[str], str | None] = shutil.which,
    *,
    preference: str = "auto",
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose a terminal transport from the preference and recorded capability.

    Painting the menu only reads recorded capability; nothing is probed here.
    """
    if not _safe_remote_part(machine, host=True):
        return {"available": False, "reason": "invalid_machine"}
    if not _safe_remote_part(session):
        return {"available": False, "reason": "agent_has_no_tmux_session"}
    terminal = which("ghostty")
    tmux = which("tmux")
    if not terminal:
        return {"available": False, "reason": "ghostty_unavailable"}
    if machine_matches(machine, local_machine):
        if not tmux:
            return {"available": False, "reason": "tmux_unavailable"}
        return {"available": True, "transport": "local", "terminal": terminal}
    present = {name: bool(which(name)) for name in ("et", "mosh", "ssh")}
    choice = choose_transport(preference, False, present, capabilities)
    if "unavailable" in choice:
        return {"available": False, "reason": choice["unavailable"]}
    return {
        "available": True, "transport": choice["transport"],
        "fallback": choice["fallback"], "terminal": terminal,
        "etPort": (capabilities or {}).get("etPort"),
    }


def build_launch_argv(
    agent: dict[str, Any],
    plan: dict[str, Any],
    local_machine: str,
    stale_file: str = "",
) -> list[str] | None:
    if not plan.get("available"):
        return None
    machine = str(agent.get("machine", ""))
    location = agent.get("tmux") or {}
    session = str(location.get("session", ""))
    if not _safe_remote_part(session) or not _safe_remote_part(machine, host=True):
        return None
    if machine.startswith("-"):
        return None
    terminal = str(plan.get("terminal", "ghostty"))
    tmux_command = ["tmux", "attach-session", "-t", "=" + session]
    remote_command = "exec " + " ".join(shlex.quote(part) for part in tmux_command)
    transport = plan.get("transport")
    if transport == "local":
        return [terminal, "-e", *tmux_command]
    if transport not in {"et", "mosh", "ssh"}:
        return None
    return [terminal, "-e", *transport_launch_argv(
        transport, plan.get("fallback"), machine, remote_command,
        plan.get("etPort"), stale_file,
    )]


def verify_connection(
    agent: dict[str, Any],
    plan: dict[str, Any],
    local_machine: str,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any] | None:
    """Verify the target before opening a terminal.

    This is intentionally called only after a user selects a row. et and mosh
    need a PTY, so every remote transport is preflighted over a PTY-free SSH
    command; a transport that then fails to start falls back to SSH inside
    the launched terminal (see TRANSPORT_LAUNCH_SCRIPT).
    """
    machine = str(agent.get("machine", ""))
    session = str((agent.get("tmux") or {}).get("session", ""))
    if not _safe_remote_part(machine, host=True) or not _safe_remote_part(session):
        return None
    transport = str(plan.get("transport", ""))
    has_session = ["tmux", "has-session", "-t", "=" + session]
    if transport == "local":
        argv = has_session
    elif transport in {"et", "mosh", "ssh"} and which("ssh"):
        remote = "exec " + " ".join(shlex.quote(part) for part in has_session)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", machine,
                "sh -lc " + shlex.quote(remote)]
    else:
        return None
    try:
        runner(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return {**plan, "verified": True}


class AgentdHub:
    """Persistent SSE subscriber with a same-identity acknowledgement ledger."""

    def __init__(
        self,
        config: dict[str, Any],
        directory: Path,
        on_snapshot: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None,
        status_owner: bool = True,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.config = hub_config(config)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.directory / "hub-snapshot.json"
        self.pending_path = self.directory / "hub-pending.json"
        self.launch_path = self.directory / "hub-launches.json"
        self.status_path = self.directory / "hub-status.json"
        self.ack_path = self.directory / "hub-ack.json"
        self.lock_path = self.directory / "hub.lock"
        self.on_snapshot = on_snapshot
        self.on_disconnect = on_disconnect
        self.status_owner = status_owner
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._response: Any = None
        self._response_socket: Any = None
        self._connection_generation = 0
        self._thread: threading.Thread | None = None
        self._sleep_thread: threading.Thread | None = None
        self._sleep_context: Any = None
        self._sleep_loop: Any = None
        self._sleep_bus: Any = None
        self._sleep_glib: Any = None
        self._sleep_cancel: threading.Event | None = None
        self._sleep_ready: threading.Event | None = None
        self._suspended = False
        self._status = STATUS_DISABLED if not self.enabled else STATUS_CONNECTING
        self._error: str | None = None
        self._last_snapshot_at_unix_ms: int | None = None
        self._last_seen_at_unix_ms: int | None = None
        self._last_seen_boot: float | None = None
        self._connection_started_boot: float | None = None
        self._snapshot: dict[str, Any] | None = self._load_snapshot()
        self._previous: dict[str, tuple[str, int | None]] = {}
        self._pending: dict[str, dict[str, Any]] = self._load_pending()
        if self._snapshot:
            for _agent in self._snapshot.get("agents", []):
                _activity = _agent.get("activity", {})
                _stamp = _activity.get("observedAtUnixMs")
                self._previous[agent_identity(_agent)] = (
                    str(_activity.get("state", "unknown")),
                    _stamp if isinstance(_stamp, int) else None,
                )
                if (_agent.get("activity", {}).get("state") == "needs_attention"
                        and _agent.get("presence", {}).get("state") == "present"
                        and not self.is_acknowledged(_agent)):
                    self._pending[agent_identity(_agent)] = copy.deepcopy(_agent)
        # A cached active snapshot is meaningful transition history: if the
        # first fresh frame after a restart is idle, that is a completion and
        # should alert. A cached idle frame, by contrast, remains a silent
        # baseline. With no cached frame at all, the first idle frame is also
        # the silent baseline.
        self._initialized = bool(self._snapshot)
        status = self._read_status()
        # A newly-created owner must receive a valid snapshot before it can
        # report live.  A non-owner (the short-lived list/open process) reads
        # the persisted status through status()/connected instead.
        if self.status_owner:
            cached_snapshot_at = self._strict_unix_ms(status.get("lastSnapshotAtUnixMs"))
            cached_seen_at = self._strict_unix_ms(status.get("lastSeenAtUnixMs"))
            # These are display-only receipt times from the prior owner.  A
            # snapshot file must exist, and contradictory ordering is not a
            # trustworthy cache-age signal.
            if self._snapshot is None or (
                cached_snapshot_at is not None
                and cached_seen_at is not None
                and cached_snapshot_at > cached_seen_at
            ):
                cached_snapshot_at = None
            self._last_snapshot_at_unix_ms = cached_snapshot_at
            self._last_seen_at_unix_ms = cached_seen_at
            self._error = "" if self._snapshot else None
        else:
            self._error = str(status.get("error") or "") or None

    @property
    def error(self) -> str | None:
        with self._lock:
            if self.status_owner:
                return self._error
        return self.status().get("error")

    @error.setter
    def error(self, value: str | None) -> None:
        with self._lock:
            self._error = value

    @property
    def connected(self) -> bool:
        """Compatibility boolean backed by the liveness state machine."""
        return bool(self.status().get("connected"))

    @connected.setter
    def connected(self, value: bool) -> None:
        # Keep old test doubles/callers functional, but never let a setter
        # claim live without a recent timestamp.
        with self._lock:
            if value:
                now_ms = _now_unix_ms()
                self._status = STATUS_LIVE
                self._last_seen_at_unix_ms = now_ms
                self._last_seen_boot = _now_boottime()
                self._error = None
            elif self._status == STATUS_LIVE:
                self._status = STATUS_STALE
                self._error = "hub_disconnected"

    @property
    def enabled(self) -> bool:
        return bool(self.config["enabled"])

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._wake.clear()
        with self._lock:
            self._status = STATUS_SUSPENDED if self._suspended else STATUS_CONNECTING
            self._error = None
        if self.status_owner:
            self._write_status(self._status, None)
        self._start_sleep_watcher()
        self._thread = threading.Thread(target=self._run, name="agentd-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._connection_generation += 1
        self._stop_sleep_watcher()
        # Do not close a BufferedReader from this thread while the reader
        # thread may hold its read lock.  Shutting down the owned socket wakes
        # its blocking readline; the reader thread performs final close().
        response_socket = self._response_socket
        if response_socket is not None:
            try:
                response_socket.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError, ValueError):
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self._lock:
            if self._status != STATUS_DISABLED:
                self._status = STATUS_RECONNECTING
            self._error = "stopped"
        if self.status_owner:
            self._write_status(STATUS_RECONNECTING, "stopped")

    def _mark_disconnected(self, error: str, *, generation: int | None = None) -> bool:
        with self._lock:
            if generation is not None and generation != self._connection_generation:
                return False
            was_connected = self._status == STATUS_LIVE
            if self._suspended:
                self._status = STATUS_SUSPENDED
            else:
                # Keep stale visible while cached roster data is retained;
                # the next connection attempt transitions to reconnecting.
                self._status = STATUS_STALE if was_connected else STATUS_RECONNECTING
            self._error = error
            status = self._status
            if self.status_owner:
                self._write_status(status, error, generation=generation)
        # External callbacks must never run under the Hub lock.  The
        # attention service takes its own worker lock before reading Hub
        # state, so the inverse order here would deadlock both services.
        if was_connected and self.on_disconnect is not None:
            self.on_disconnect()
        return True

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return self._status_payload(STATUS_DISABLED, self._error)
        if not self.status_owner:
            return self._persisted_status()
        notify_disconnect = False
        with self._lock:
            previous = self._status
            status = self._effective_status_locked()
            error = self._error
            if previous == STATUS_LIVE and status == STATUS_STALE:
                # Persist and notify while the transition is still current;
                # a same-generation fresh snapshot must not be overwritten
                # by a delayed stale write or followed by a stale callback.
                if self.status_owner:
                    self._write_status(status, error)
                notify_disconnect = self.on_disconnect is not None
            payload = self._status_payload(status, error)
        if notify_disconnect and self.on_disconnect is not None:
            self.on_disconnect()
        return payload

    def _status_payload(self, status: str, error: str | None) -> dict[str, Any]:
        with self._lock:
            snapshot_at = self._last_snapshot_at_unix_ms
            seen_at = self._last_seen_at_unix_ms
        return {
            "enabled": bool(self.config["configured"]),
            "status": status,
            "connected": status == STATUS_LIVE,
            "error": error,
            "lastSnapshotAtUnixMs": snapshot_at,
            "lastSeenAtUnixMs": seen_at,
            "machine": self.config["machine"],
        }

    def _effective_status_locked(self) -> str:
        if not self.enabled:
            return STATUS_DISABLED
        if self._suspended:
            return STATUS_SUSPENDED
        if self._status == STATUS_LIVE and self._liveness_expired_locked():
            self._status = STATUS_STALE
            self._error = "hub_liveness_deadline"
        return self._status

    def _liveness_expired_locked(self) -> bool:
        if self._status != STATUS_LIVE:
            return False
        anchor = self._last_seen_boot or self._connection_started_boot
        return anchor is None or _now_boottime() - anchor >= LIVENESS_DEADLINE_SECONDS

    @staticmethod
    def _owner_alive(status: Mapping[str, Any]) -> bool:
        try:
            owner_pid = int(status.get("ownerPid") or 0)
        except (TypeError, ValueError):
            return False
        if owner_pid <= 0 or not (owner_pid == os.getpid() or _pid_alive(owner_pid)):
            return False
        owner_ticks = status.get("ownerStartTimeTicks")
        return type(owner_ticks) is int and _process_start_ticks(owner_pid) == owner_ticks

    @staticmethod
    def _strict_unix_ms(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    def _persisted_status(self) -> dict[str, Any]:
        raw = self._read_status()
        status = raw.get("status")
        # Old bool-only files are intentionally not trusted as live.  This
        # matters after an owner crash or an interrupted pre-health upgrade.
        if not isinstance(status, str) or status not in STATUS_VALUES:
            status = STATUS_STALE if raw.get("connected") else STATUS_RECONNECTING
        elif status == STATUS_DISABLED and self.enabled:
            status = STATUS_RECONNECTING
        last_seen = self._strict_unix_ms(raw.get("lastSeenAtUnixMs"))
        last_snapshot = self._strict_unix_ms(raw.get("lastSnapshotAtUnixMs"))
        at_unix_ms = self._strict_unix_ms(raw.get("atUnixMs"))
        now_ms = _now_unix_ms()
        status_age = (
            now_ms - at_unix_ms
            if at_unix_ms is not None
            else LIVENESS_DEADLINE_SECONDS * 1000 + 1
        )
        seen_age = (
            now_ms - last_seen
            if last_seen is not None
            else LIVENESS_DEADLINE_SECONDS * 1000 + 1
        )
        # Clock jumps into the future are not evidence of health.  Status
        # readers fail closed so the UI cannot turn a bad clock into live.
        future = 0
        owner_alive = self._owner_alive(raw)
        stale_live = status == STATUS_LIVE and (
            raw.get("connected") is not True
            or not owner_alive
            or last_snapshot is None
            or last_seen is None
            or status_age >= LIVENESS_DEADLINE_SECONDS * 1000
            or seen_age >= LIVENESS_DEADLINE_SECONDS * 1000
            or status_age < future
            or seen_age < future
            or last_snapshot > last_seen
        )
        stale_owner_state = status in {
            STATUS_CONNECTING, STATUS_RECONNECTING, STATUS_SUSPENDED,
        } and (
            not owner_alive
            or status_age >= LIVENESS_DEADLINE_SECONDS * 1000
            or status_age < future
        )
        if stale_live:
            status = STATUS_STALE
        if stale_owner_state:
            status = STATUS_STALE
        payload = {
            "enabled": bool(self.config["configured"]),
            "status": status,
            "connected": status == STATUS_LIVE,
            "error": raw.get("error") or (
                "hub_status_stale" if stale_live or stale_owner_state else None
            ),
            "lastSnapshotAtUnixMs": last_snapshot,
            "lastSeenAtUnixMs": last_seen,
            "machine": self.config["machine"],
        }
        return payload

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def _load_snapshot(self) -> dict[str, Any] | None:
        try:
            return validate_snapshot(json.loads(self.snapshot_path.read_text(encoding="utf-8")))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return None

    def _read_status(self) -> dict[str, Any]:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_status(
        self, status: str, error: str | None, *, generation: int | None = None,
    ) -> None:
        if status not in STATUS_VALUES:
            status = STATUS_RECONNECTING
        with self._lock:
            if generation is not None and generation != self._connection_generation:
                return
            last_snapshot = self._last_snapshot_at_unix_ms
            last_seen = self._last_seen_at_unix_ms
            payload = {
                "status": status,
                "connected": status == STATUS_LIVE,
                "error": error,
                "atUnixMs": _now_unix_ms(),
                "lastSnapshotAtUnixMs": last_snapshot,
                "lastSeenAtUnixMs": last_seen,
                "ownerPid": os.getpid(),
                "ownerStartTimeTicks": _process_start_ticks(os.getpid()),
            }
            temporary = self.status_path.with_suffix(".json.tmp")
            try:
                temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                temporary.replace(self.status_path)
            except OSError:
                pass

    def _set_status(
        self,
        status: str,
        error: str | None = None,
        *,
        generation: int | None = None,
        suspended: bool | None = None,
    ) -> bool:
        if status not in STATUS_VALUES:
            status = STATUS_RECONNECTING
        with self._lock:
            if generation is not None and generation != self._connection_generation:
                return False
            if suspended is not None and suspended != self._suspended:
                return False
            self._status = status
            self._error = error
            if self.status_owner:
                self._write_status(status, error, generation=generation)
        return True

    def _close_active_response(self) -> None:
        with self._lock:
            response = self._response
            self._response = None
            self._response_socket = None
        if response is not None:
            # Only shut down the socket here.  The SSE reader owns the
            # BufferedReader and will perform the final response.close(),
            # avoiding a close/read lock race during suspend.
            response_socket = self._socket_for_response(response)
            if response_socket is not None:
                try:
                    response_socket.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError, ValueError):
                    pass

    def prepare_for_sleep(self, sleeping: bool) -> None:
        """Suspend/resume seam used by logind and integration tests.

        Suspending invalidates the current stream and liveness immediately,
        while resume wakes a pending backoff instead of waiting for it to
        expire.  The cached roster is deliberately untouched.
        """
        sleeping = bool(sleeping)
        if not self.enabled:
            return
        if sleeping:
            with self._lock:
                if self._stop.is_set() or self._suspended:
                    return
                was_live = self._status == STATUS_LIVE
                self._connection_generation += 1
                generation = self._connection_generation
                self._suspended = True
                self._status = STATUS_SUSPENDED
                self._error = "suspended"
            if self.status_owner:
                self._write_status(STATUS_SUSPENDED, "suspended", generation=generation)
            self._wake.set()
            self._close_active_response()
            if was_live and self.on_disconnect is not None:
                self.on_disconnect()
            return
        with self._lock:
            if self._stop.is_set() or not self._suspended:
                return
            self._suspended = False
            self._connection_generation += 1
            generation = self._connection_generation
            if self._status == STATUS_SUSPENDED:
                self._status = STATUS_RECONNECTING
                self._error = "resuming"
        if self.status_owner:
            self._write_status(STATUS_RECONNECTING, "resuming", generation=generation)
        self._wake.set()

    def _start_sleep_watcher(self) -> None:
        with self._lock:
            if self._sleep_thread and self._sleep_thread.is_alive():
                return
            cancel = threading.Event()
            ready = threading.Event()
            thread = threading.Thread(
                target=self._sleep_watch,
                args=(cancel, ready),
                name="agentd-hub-sleep",
                daemon=True,
            )
            self._sleep_context = None
            self._sleep_loop = None
            self._sleep_bus = None
            self._sleep_glib = None
            self._sleep_cancel = cancel
            self._sleep_ready = ready
            self._sleep_thread = thread
        thread.start()

    @staticmethod
    def _queue_sleep_quit(context: Any, loop: Any, glib: Any) -> None:
        """Quit through a source owned by the watcher's private context."""
        try:
            source = glib.idle_source_new()
            source.set_priority(glib.PRIORITY_HIGH)

            def quit_loop(*_args: Any) -> bool:
                loop.quit()
                return glib.SOURCE_REMOVE

            source.set_callback(quit_loop)
            source.attach(context)
            context.wakeup()
        except Exception:
            # Optional/fake GLib implementations may lack source helpers.
            # The cancellation check still covers stop-before-run.
            try:
                loop.quit()
            except Exception:
                pass

    def _stop_sleep_watcher(self) -> None:
        with self._lock:
            thread = self._sleep_thread
            cancel = self._sleep_cancel
            ready = self._sleep_ready
            context = self._sleep_context
            loop = self._sleep_loop
            glib = self._sleep_glib
        if cancel is not None:
            cancel.set()
        if thread and thread.is_alive() and context is None and ready is not None:
            # The watcher may still be importing Gio or connecting to the
            # system bus.  Once it publishes its context, cancellation is
            # either observed before run() or queued onto that context.
            ready.wait(timeout=0.5)
            with self._lock:
                if self._sleep_thread is thread:
                    context = self._sleep_context
                    loop = self._sleep_loop
                    glib = self._sleep_glib
        if context is not None and loop is not None and glib is not None:
            self._queue_sleep_quit(context, loop, glib)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            # A timed-out watcher still owns a subscription.  Preserve every
            # reference so start() cannot create a duplicate watcher.
            if self._sleep_thread is thread and (thread is None or not thread.is_alive()):
                self._sleep_thread = None
                self._sleep_context = None
                self._sleep_loop = None
                self._sleep_bus = None
                self._sleep_glib = None
                self._sleep_cancel = None
                self._sleep_ready = None

    def _sleep_watch(self, cancel: threading.Event, ready: threading.Event) -> None:
        """Watch logind on a private GLib context when optional deps exist."""
        context = None
        bus = None
        subscription = None
        try:
            from gi.repository import Gio, GLib

            # Use an explicitly-owned context and Gio's low-level signal API;
            # this avoids pushing events through the process-global default
            # context used by the optional desktop-notification monitor.
            context = GLib.MainContext.new()
            context.push_thread_default()
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

            def received(_connection, _sender, _path, _interface, _signal, parameters):
                if cancel.is_set() or self._stop.is_set():
                    return
                try:
                    values = parameters.unpack()
                    self.prepare_for_sleep(bool(values[0] if isinstance(values, tuple) else values))
                except Exception:
                    return

            subscription = bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                received,
            )
            loop = GLib.MainLoop.new(context, False)
            with self._lock:
                if self._sleep_cancel is not cancel:
                    return
                self._sleep_context = context
                self._sleep_bus = (bus, subscription)
                self._sleep_loop = loop
                self._sleep_glib = GLib
            ready.set()
            if cancel.is_set() or self._stop.is_set():
                return
            loop.run()
        except Exception:
            # Liveness still fails closed without dbus/python-gobject.  This
            # is deliberately not a service-fatal dependency.
            return
        finally:
            ready.set()
            if bus is not None and subscription is not None:
                try:
                    bus.signal_unsubscribe(subscription)
                except Exception:
                    pass
            if context is not None:
                try:
                    context.pop_thread_default()
                except Exception:
                    pass
            with self._lock:
                if self._sleep_cancel is cancel:
                    self._sleep_context = None
                    self._sleep_loop = None
                    self._sleep_bus = None
                    self._sleep_glib = None

    @staticmethod
    def _enable_tcp_keepalive(sock: Any) -> None:
        """Enable kernel TCP keepalive in addition to SSE heartbeats."""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (
                ("TCP_KEEPIDLE", int(HEARTBEAT_INTERVAL_SECONDS)),
                ("TCP_KEEPINTVL", int(HEARTBEAT_INTERVAL_SECONDS)),
                ("TCP_KEEPCNT", 3),
            ):
                option = getattr(socket, name, None)
                if option is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except (OSError, AttributeError, TypeError, ValueError):
            pass

    @staticmethod
    def _reconnect_delay(index: int) -> float:
        base = DEFAULT_RECONNECT_SECONDS[min(max(0, index), len(DEFAULT_RECONNECT_SECONDS) - 1)]
        # Keep jitter bounded and preserve the configured 30-second cap.
        jitter = random.uniform(0.0, min(1.0, base * 0.25))
        return min(DEFAULT_RECONNECT_SECONDS[-1], base + jitter)

    def _load_acks(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.ack_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _load_pending(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            return {
                str(identity): agent
                for identity, agent in value.items()
                if isinstance(agent, dict) and agent_identity(agent) == str(identity)
            }
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _load_launches(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.launch_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            now = time.time()
            return {
                str(identity): item
                for identity, item in value.items()
                if isinstance(item, dict)
                and float(item.get("expiresAt", 0) or 0) > now
            }
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_pending(self) -> None:
        temporary = self.pending_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._pending, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.pending_path)

    def _write_launches(self, launches: dict[str, dict[str, Any]]) -> None:
        temporary = self.launch_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(launches, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.launch_path)

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        with self._lock, self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _write_acks(self, acks: dict[str, dict[str, Any]]) -> None:
        temporary = self.ack_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(acks, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.ack_path)

    def acknowledge(self, identity: str, agent: dict[str, Any] | None = None) -> bool:
        with self._state_lock():
            acks = self._load_acks()
            stamp = (agent or {}).get("activity", {}).get("observedAtUnixMs")
            identity = str(identity)
            pending = self._load_pending()
            existing_ack = acks.get(identity)
            existing_stamp = existing_ack.get("observedAtUnixMs") if isinstance(existing_ack, dict) else None
            pending_agent = pending.get(identity)
            pending_stamp = (
                pending_agent.get("activity", {}).get("observedAtUnixMs")
                if isinstance(pending_agent, dict) else None
            )

            # A click-time process may be acknowledging an older snapshot
            # while the daemon has already observed a newer claim for the
            # same machine/instance/PID/start-ticks identity. Never let the
            # old click regress the acknowledgement or remove that claim.
            if (
                (isinstance(existing_stamp, int) and not isinstance(stamp, int))
                or (isinstance(pending_stamp, int) and not isinstance(stamp, int))
                or (isinstance(existing_stamp, int) and isinstance(stamp, int) and existing_stamp > stamp)
                or (isinstance(pending_stamp, int) and isinstance(stamp, int) and pending_stamp > stamp)
            ):
                self._pending = pending
                return False

            if not isinstance(existing_stamp, int) or not isinstance(stamp, int) or stamp >= existing_stamp:
                acks[identity] = {"observedAtUnixMs": stamp, "at": int(time.time() * 1000)}
                self._write_acks(acks)
            # Only remove the exact captured claim. Do not merge stale
            # self._pending entries: another process may have authoritatively
            # removed them from the complete snapshot in the meantime.
            pending.pop(identity, None)
            self._pending = pending
            self._write_pending()
            return True

    def has_launch_intent(self, identity: str, agent: dict[str, Any] | None = None) -> bool:
        """Return whether this exact claim already has a live launch reservation."""
        with self._state_lock():
            launches = self._load_launches()
            item = launches.get(str(identity))
            if not item:
                self._write_launches(launches)
                return False
            observed = (agent or {}).get("activity", {}).get("observedAtUnixMs")
            return item.get("observedAtUnixMs") == observed

    def reserve_launch(self, identity: str, agent: dict[str, Any], ttl: float = 15.0) -> bool:
        """Atomically reserve one click-to-launch attempt for an exact claim."""
        with self._state_lock():
            launches = self._load_launches()
            current = launches.get(str(identity))
            observed = agent.get("activity", {}).get("observedAtUnixMs")
            if current and current.get("observedAtUnixMs") == observed:
                self._write_launches(launches)
                return False
            launches[str(identity)] = {
                "observedAtUnixMs": observed,
                "reservedAt": time.time(),
                "expiresAt": time.time() + max(1.0, float(ttl)),
            }
            self._write_launches(launches)
            return True

    def clear_launch_intent(self, identity: str) -> None:
        with self._state_lock():
            launches = self._load_launches()
            launches.pop(str(identity), None)
            self._write_launches(launches)

    def is_acknowledged(self, agent: dict[str, Any]) -> bool:
        identity = agent_identity(agent)
        ack = self._load_acks().get(identity)
        if not isinstance(ack, dict):
            return False
        observed = agent.get("activity", {}).get("observedAtUnixMs")
        acknowledged = ack.get("observedAtUnixMs")
        return acknowledged is None or observed is not None and acknowledged >= observed

    def pending_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(agent) for agent in self._pending.values() if not self.is_acknowledged(agent)]

    def _accept(self, snapshot: dict[str, Any], generation: int | None = None) -> bool:
        # Hold the lifecycle lock across every in-memory, disk, and status
        # effect.  Suspend/stop either linearizes before this block (and the
        # old frame is discarded) or after a complete acceptance.  External
        # notification is delivered after releasing the lock.
        with self._lock:
            if generation is not None and (
                generation != self._connection_generation
                or self._stop.is_set()
                or self._suspended
            ):
                return False
            alerts = self._accept_with_notify(snapshot, False, True, True)
        if self.on_snapshot is not None:
            self.on_snapshot(snapshot, alerts)
        return True

    def _accept_with_notify(
        self,
        snapshot: dict[str, Any],
        notify: bool,
        write_status: bool = True,
        persist_snapshot: bool = True,
        *,
        mark_live: bool = True,
    ) -> list[dict[str, Any]]:
        snapshot = validate_snapshot(snapshot)
        received_at = _now_unix_ms()
        received_boot = _now_boottime()
        alerts: list[dict[str, Any]] = []
        current: dict[str, tuple[str, int | None]] = {}
        for agent in snapshot.get("agents", []):
            if not isinstance(agent, dict):
                continue
            identity = agent_identity(agent)
            activity = agent.get("activity", {})
            state = str(activity.get("state", "unknown"))
            stamp = activity.get("observedAtUnixMs")
            current[identity] = (state, stamp if isinstance(stamp, int) else None)
            previous = self._previous.get(identity)
            initial = not self._initialized
            is_new_attention = state == "needs_attention" and (
                previous is None or previous[0] != state or (stamp is not None and stamp > (previous[1] or -1))
            )
            is_new_idle = state == "idle" and previous is not None and previous[0] == "active" and (
                stamp is None or stamp > (previous[1] or -1)
            )
            pending_before = self._pending.get(identity)
            same_pending_claim = bool(
                pending_before
                and pending_before.get("activity", {}).get("observedAtUnixMs") == stamp
            )
            if (is_new_attention or is_new_idle) and not same_pending_claim and not self.is_acknowledged(agent):
                if not initial or state == "needs_attention":
                    alerts.append(agent)
        self._previous = current
        self._initialized = True
        with self._lock:
            self._snapshot = copy.deepcopy(snapshot)
            current_identities = {
                agent_identity(agent)
                for agent in snapshot.get("agents", [])
                if isinstance(agent, dict)
            }
            for identity in list(self._pending):
                if identity not in current_identities:
                    self._pending.pop(identity, None)
            for agent in snapshot.get("agents", []):
                identity = agent_identity(agent)
                state = agent.get("activity", {}).get("state")
                if state not in {"needs_attention", "idle"} or agent.get("presence", {}).get("state") != "present":
                    self._pending.pop(identity, None)
            for agent in alerts:
                self._pending[agent_identity(agent)] = copy.deepcopy(agent)
            if persist_snapshot:
                with self.lock_path.open("a+", encoding="utf-8") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    disk_pending = self._load_pending()
                    current_identities = {
                        agent_identity(agent)
                        for agent in snapshot.get("agents", [])
                        if isinstance(agent, dict)
                    }
                    # A Hub snapshot is complete: claims absent from it (or
                    # no longer pending) must not survive merely because this
                    # process was constructed with an older pending map. A
                    # claim observed after this snapshot was taken is the one
                    # safe exception for a concurrent writer.
                    snapshot_observed = snapshot.get("observedAtUnixMs")
                    snapshot_observed = snapshot_observed if isinstance(snapshot_observed, int) else None
                    for identity, disk_agent in list(disk_pending.items()):
                        current_agent = next(
                            (agent for agent in snapshot.get("agents", [])
                             if isinstance(agent, dict) and agent_identity(agent) == identity),
                            None,
                        )
                        disk_stamp = disk_agent.get("activity", {}).get("observedAtUnixMs") if isinstance(disk_agent, dict) else None
                        newer_concurrent_claim = (
                            isinstance(snapshot_observed, int)
                            and isinstance(disk_stamp, int)
                            and disk_stamp > snapshot_observed
                        )
                        if not newer_concurrent_claim and (identity not in current_identities or (
                            current_agent is not None
                            and current_agent.get("activity", {}).get("state") not in {"needs_attention", "idle"}
                        )):
                            disk_pending.pop(identity, None)
                    for agent in alerts:
                        identity = agent_identity(agent)
                        existing = disk_pending.get(identity)
                        existing_stamp = existing.get("activity", {}).get("observedAtUnixMs") if isinstance(existing, dict) else None
                        alert_stamp = agent.get("activity", {}).get("observedAtUnixMs")
                        if not (isinstance(existing_stamp, int) and isinstance(alert_stamp, int)
                                and existing_stamp > alert_stamp):
                            disk_pending[identity] = copy.deepcopy(agent)
                    self._pending = disk_pending
                    self._write_pending()
            self._last_snapshot_at_unix_ms = received_at
            self._last_seen_at_unix_ms = received_at
            self._last_seen_boot = received_boot
            if mark_live:
                self._status = STATUS_LIVE
                self._error = None
            if write_status and self.status_owner:
                self._write_status(self._status, self._error)
            if persist_snapshot:
                with self.lock_path.open("a+", encoding="utf-8") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    temporary = self.snapshot_path.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                    temporary.replace(self.snapshot_path)
        if notify and self.on_snapshot is not None:
            self.on_snapshot(snapshot, alerts)
        return alerts

    @staticmethod
    def _validate_heartbeat(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("hub heartbeat is not an object")
        if value.get("schema") != HEARTBEAT_SCHEMA:
            raise ValueError("unsupported agentd-hub heartbeat")
        at = value.get("atUnixMs")
        if type(at) is not int or at < 0:
            raise ValueError("hub heartbeat timestamp is invalid")
        return value

    def _accept_heartbeat(self, value: Any, generation: int | None = None) -> bool:
        self._validate_heartbeat(value)
        now_ms = _now_unix_ms()
        now_boot = _now_boottime()
        with self._lock:
            if generation is not None and (
                generation != self._connection_generation
                or self._stop.is_set()
                or self._suspended
            ):
                return False
            # A heartbeat cannot establish a connection.  It only keeps a
            # connection fresh after a validated snapshot has made it live.
            self._last_seen_at_unix_ms = now_ms
            if self._status == STATUS_LIVE:
                self._last_seen_boot = now_boot
                self._error = None
            status = self._status
            if self.status_owner:
                self._write_status(status, self._error, generation=generation)
        return True

    def _sse_lines(self, response: Any) -> Iterator[bytes]:
        """Yield SSE lines from one response; the caller owns the watchdog."""
        while not self._stop.is_set():
            try:
                line = response.readline()
            except (OSError, TimeoutError, socket.timeout, AttributeError):
                if self._stop.is_set():
                    return
                raise
            if not line:
                return
            yield line

    def _read_stream(self, response: Any, output: queue.Queue, generation: int) -> None:
        """Read/parsing worker; the owner thread enforces liveness deadlines."""
        try:
            for item in parse_sse(self._sse_lines(response)):
                with self._lock:
                    if generation != self._connection_generation:
                        return
                while not self._stop.is_set():
                    try:
                        output.put((generation, "event", item), timeout=0.5)
                        break
                    except queue.Full:
                        with self._lock:
                            if generation != self._connection_generation:
                                return
            output.put((generation, "eof", None), timeout=0.5)
        except Exception as error:
            try:
                output.put((generation, "error", error), timeout=0.5)
            except queue.Full:
                return
        finally:
            self._close_response(response)

    @staticmethod
    def _socket_for_response(response: Any) -> Any:
        """Return urllib's owned socket, when the response exposes one."""
        try:
            return response.fp.raw._sock
        except (AttributeError, TypeError):
            return None

    @classmethod
    def _close_response(cls, response: Any) -> None:
        """Wake any reader before closing a response and swallow cleanup errors."""
        response_socket = cls._socket_for_response(response)
        if response_socket is not None:
            try:
                response_socket.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError, ValueError):
                pass
        try:
            response.close()
        except Exception:
            pass

    def fetch_snapshot(self) -> bool:
        """Refresh once for a user click; the daemon still uses persistent SSE."""
        if not self.enabled:
            return False
        response = None
        try:
            # The one-shot validation endpoint is separate from the daemon's
            # persistent /events stream.
            snapshot_url = self.config["url"].rstrip("/")
            if snapshot_url.endswith("/events"):
                snapshot_url = snapshot_url[:-7] + "/snapshot"
            else:
                snapshot_url += "/snapshot"
            response = urllib.request.urlopen(urllib.request.Request(snapshot_url), timeout=3)
            snapshot = validate_snapshot(json.loads(response.read().decode("utf-8")))
            # A one-shot click refresh updates the cached roster and timing
            # fields, but it does not claim that the persistent SSE owner is
            # live.  Only that stream's validated snapshot establishes live.
            self._accept_with_notify(snapshot, False, False, False, mark_live=False)
            return True
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            self._mark_disconnected(str(error) or "hub_unavailable")
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _run(self) -> None:
        index = 0
        while not self._stop.is_set():
            with self._lock:
                suspended = self._suspended
                loop_generation = self._connection_generation
            if suspended:
                self._set_status(
                    STATUS_SUSPENDED,
                    "suspended",
                    generation=loop_generation,
                    suspended=True,
                )
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if index:
                self._set_status(
                    STATUS_RECONNECTING,
                    self._error,
                    generation=loop_generation,
                    suspended=False,
                )
            response = None
            valid_snapshot = False
            reader: threading.Thread | None = None
            invalidated = False
            try:
                with self._lock:
                    attempt_generation = self._connection_generation
                request = urllib.request.Request(
                    event_url(self.config["url"]),
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                )
                response = urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT_SECONDS)
                response_socket = self._socket_for_response(response)
                self._enable_tcp_keepalive(response_socket)
                if response_socket is not None:
                    # The handshake deadline must not become a 10-second
                    # read timeout on a stream whose heartbeat is every 15s.
                    # The owner loop below enforces a separate, bounded
                    # liveness deadline and shuts this socket down on expiry.
                    response_socket.settimeout(None)
                with self._lock:
                    # A suspend/stop may have raced with urlopen().  Do not
                    # install a late response after that cancellation.
                    generation = self._connection_generation
                    discarded = (
                        self._stop.is_set()
                        or self._suspended
                        or generation != attempt_generation
                    )
                    if not discarded:
                        self._response = response
                        self._response_socket = response_socket
                        self._connection_started_boot = _now_boottime()
                        self._last_seen_boot = None
                        self._status = STATUS_CONNECTING
                        self._error = None
                if discarded:
                    continue
                with self._lock:
                    still_current = (
                        not self._stop.is_set()
                        and not self._suspended
                        and generation == self._connection_generation
                    )
                if self.status_owner and still_current:
                    self._write_status(STATUS_CONNECTING, None, generation=generation)

                events: queue.Queue = queue.Queue(maxsize=32)
                reader = threading.Thread(
                    target=self._read_stream,
                    args=(response, events, generation),
                    name="agentd-hub-sse-reader",
                    daemon=True,
                )
                reader.start()
                while not self._stop.is_set():
                    with self._lock:
                        if self._suspended:
                            raise RuntimeError("hub_suspended")
                        if generation != self._connection_generation:
                            invalidated = True
                            raise RuntimeError("hub_stream_invalidated")
                        anchor = self._last_seen_boot if self._status == STATUS_LIVE else self._connection_started_boot
                        expired = anchor is None or _now_boottime() - anchor >= LIVENESS_DEADLINE_SECONDS
                    if expired:
                        raise TimeoutError("hub_liveness_deadline")
                    try:
                        event_generation, kind, value = events.get(timeout=LIVENESS_CHECK_SECONDS)
                    except queue.Empty:
                        continue
                    if event_generation != generation:
                        invalidated = True
                        raise RuntimeError("hub_stream_invalidated")
                    if kind == "error":
                        raise value
                    if kind == "eof":
                        raise ConnectionError("hub_stream_closed")
                    event, _event_id, data = value
                    if event in {"snapshot", "message"}:
                        # Invalid JSON/schema is a stream failure.  In
                        # particular, malformed dribbled bytes never refresh
                        # lastSeen and cannot defeat the deadline watchdog.
                        if not self._accept(json.loads(data), generation):
                            invalidated = True
                            raise RuntimeError("hub_stream_invalidated")
                        valid_snapshot = True
                        index = 0
                    elif event == "heartbeat":
                        if not self._accept_heartbeat(json.loads(data), generation):
                            invalidated = True
                            raise RuntimeError("hub_stream_invalidated")
                if self._stop.is_set():
                    return
            except Exception as error:
                if not self._stop.is_set():
                    self._mark_disconnected(
                        str(error) or "hub_unavailable",
                        generation=attempt_generation,
                    )
            finally:
                with self._lock:
                    if self._response is response:
                        self._response = None
                        self._response_socket = None
                    if reader is not None and generation == self._connection_generation:
                        # Stop a producer blocked on the bounded event queue
                        # once this stream has failed; it will close its own
                        # buffered response in its finally block.
                        self._connection_generation += 1
                if response is not None:
                    response_socket = self._socket_for_response(response)
                    if response_socket is not None:
                        try:
                            response_socket.shutdown(socket.SHUT_RDWR)
                        except (OSError, AttributeError, ValueError):
                            pass
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=1)
                if response is not None and (reader is None or not reader.is_alive()):
                    # The reader normally closes its own BufferedReader.  A
                    # response that failed before its reader was spawned is
                    # still owned by this thread.
                    self._close_response(response)
            if self._stop.is_set():
                return
            if self._suspended:
                continue
            if invalidated:
                # Suspend/resume cancellation is an explicit wake request,
                # not a transport failure: reconnect without backoff.
                continue
            # A stream that never delivered a valid snapshot must continue
            # increasing backoff.  Only a valid snapshot resets it.
            if not valid_snapshot:
                delay_index = index
                index += 1
            else:
                delay_index = 0
            delay = self._reconnect_delay(delay_index)
            self._wake.wait(delay)
            self._wake.clear()


def display_title(agent: dict[str, Any]) -> str:
    name = str(agent.get("name") or "").strip()
    if name:
        return name
    harness = str(agent.get("harness") or "agent")
    location = agent.get("tmux") or {}
    session = str(location.get("session") or "").strip()
    machine = str(agent.get("machine") or "").strip()
    if session and machine:
        return f"{session} · {machine}"
    if session:
        return session
    return f"{harness} · {machine}" if machine else harness


def make_agent_row(
    agent: dict[str, Any],
    match: dict[str, Any] | None,
    plan: dict[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    stamp = agent.get("activity", {}).get("observedAtUnixMs")
    first = float(stamp) / 1000 if isinstance(stamp, int) else now
    workspace = (match or {}).get("workspace") or {}
    row = {
        "kind": "agent",
        "id": agent_identity(agent),
        "address": str((match or {}).get("address", "")),
        "stable_id": agent_identity(agent),
        "class": "agentd-hub",
        "title": display_title(agent),
        "window_title": str((match or {}).get("title", "")),
        "workspace_id": workspace.get("id"),
        "workspace": str(workspace.get("name", "")),
        "first_attention_at": first,
        "last_attention_at": first,
        "count": 1,
        "source": "agentd-hub",
        "machine": str(agent.get("machine", "")),
        "open_on_machine": bool(match),
        "connection_available": bool(plan.get("available")),
        "unavailable_reason": "" if plan.get("available") else str(plan.get("reason", "unavailable")),
        "activity": agent.get("activity", {}).get("state", "unknown"),
        "harness": agent.get("harness", ""),
        "agent": copy.deepcopy(agent),
    }
    return row


def launch_agent(
    agent: dict[str, Any],
    plan: dict[str, Any],
    local_machine: str,
    popen: Callable[..., Any] = subprocess.Popen,
) -> list[str] | None:
    argv = build_launch_argv(agent, plan, local_machine)
    if argv is None:
        return None
    popen(argv, start_new_session=True)
    return argv
