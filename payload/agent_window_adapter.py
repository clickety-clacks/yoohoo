"""Yoohoo's in-process adapter for the bundled Agent Window Resolver.

This module translates Agentd Hub records and fresh Hyprland client records
into the resolver's v1 request model.  The resolver remains read-only: this
adapter owns cache policy and the caller owns focus, tmux selection, launch,
and acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


def _load_resolver_package():
    """Load only the package shipped beside this adapter."""
    bundle_name = "_yoohoo_agent_window_resolver"
    root = Path(__file__).resolve().parent
    package = root / "agent_window_resolver"
    init = package / "__init__.py"
    if not init.is_file():
        raise ModuleNotFoundError(
            "bundled agent_window_resolver package is missing"
        )
    spec = importlib.util.spec_from_file_location(
        bundle_name, init,
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("bundled agent_window_resolver package cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[bundle_name] = module
    spec.loader.exec_module(module)
    return module


resolver_package = _load_resolver_package()
_linux = importlib.import_module("_yoohoo_agent_window_resolver.linux")
_resolver = importlib.import_module("_yoohoo_agent_window_resolver.resolver")
LinuxCollector = _linux.LinuxCollector
Resolver = _resolver.Resolver


@dataclass(frozen=True)
class ResolveResult:
    """One resolver response and the window snapshot used to request it."""

    response: dict[str, Any]
    windows: tuple[dict[str, Any], ...]


def _ticks(value: Any) -> str | None:
    """Normalize only an integer or canonical decimal tick value.

    Agentd's JSON identity currently carries ticks as an integer while the v1
    wire model deliberately carries them as a decimal string.  In
    particular, do not use ``int(value)`` here: it would silently accept
    floats, exponents, whitespace, and non-canonical leading zeroes.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 0 <= value <= (1 << 64) - 1:
            return str(value)
        return None
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value) is None:
        return None
    if int(value) > (1 << 64) - 1:
        return None
    return value


def _pid(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 4_194_304 else None


def _address(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if re.fullmatch(r"0x[0-9a-f]{1,32}", text) is not None:
        return text
    if re.fullmatch(r"[0-9a-f]{1,32}", text) is not None:
        return "0x" + text
    return ""


def _display_text(value: Any, maximum: int = 512) -> str | None:
    """Keep optional compositor/roster display metadata bounded and safe."""
    if not isinstance(value, str):
        return None
    text = "".join(char if ord(char) >= 32 and ord(char) != 127 else " "
                   for char in value[:maximum]).strip()
    return text or None


def process_start_ticks(pid: int, proc_root: str = "/proc") -> str | None:
    """Read a process identity without accepting a reused PID."""
    try:
        raw = Path(proc_root, str(pid), "stat").read_text(
            encoding="utf-8", errors="strict"
        )
        fields = raw.rsplit(") ", 1)[1].split()
        ticks = int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return _ticks(ticks)


def window_records(
    clients: Iterable[Mapping[str, Any]],
    *,
    proc_root: str = "/proc",
) -> tuple[dict[str, Any], ...]:
    """Build canonical window identities from one fresh Hypr client scan."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str]] = set()
    seen_addresses: set[str] = set()
    for client in clients:
        if not isinstance(client, Mapping):
            raise ValueError("Hypr client record is not an object")
        address = _address(client.get("address"))
        pid = _pid(client.get("pid"))
        ticks = process_start_ticks(pid, proc_root) if pid is not None else None
        if not address or pid is None or ticks is None:
            raise ValueError("Hypr client identity is incomplete")
        if address in seen_addresses:
            raise ValueError("duplicate Hypr client address")
        seen_addresses.add(address)
        # Hyprland versions without stableId still expose a stable address;
        # retain address/PID/start ticks as the complete identity.
        stable_value = client.get("stableId")
        if stable_value is not None and (not isinstance(stable_value, str) or not stable_value):
            raise ValueError("Hypr client stableId is invalid")
        stable_id = stable_value or ("hypr:" + address)
        item: dict[str, Any] = {
            "stableId": stable_id,
            "address": address,
            "pid": pid,
            "startTimeTicks": ticks,
        }
        window_class = client.get("class")
        if isinstance(window_class, str):
            item["class"] = window_class[:256]
        title = _display_text(client.get("title"))
        if title is not None:
            item["title"] = title
        key = (stable_id, address, pid, ticks)
        if key in seen:
            raise ValueError("duplicate Hypr client identity")
        seen.add(key)
        records.append(item)
    return tuple(records)


def _request_id(agent: Mapping[str, Any], operation: str) -> str:
    raw = "|".join(
        str(value)
        for value in (
            operation,
            agent.get("machine", ""),
            agent.get("instanceId", ""),
            (agent.get("id") or {}).get("pid", ""),
            (agent.get("id") or {}).get("startTimeTicks", ""),
        )
    ).encode("utf-8", "replace")
    return "yoohoo-" + hashlib.sha256(raw).hexdigest()[:32]


def request_for_agent(
    agent: Mapping[str, Any],
    windows: Iterable[Mapping[str, Any]],
    local_machine: str,
    *,
    operation: str = "resolve",
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create a strict v1 request from one Hub agent."""
    machine = agent.get("machine")
    instance_id = agent.get("instanceId")
    identity = agent.get("id")
    if (not isinstance(machine, str) or not machine.strip()
            or not isinstance(instance_id, str) or not instance_id
            or not isinstance(identity, Mapping)):
        return None
    pid = _pid(identity.get("pid"))
    ticks = _ticks(identity.get("startTimeTicks"))
    if pid is None or ticks is None:
        return None
    target_identity = {
        "machine": machine,
        "instanceId": instance_id,
        "pid": pid,
        "startTimeTicks": ticks,
    }
    target: dict[str, Any] = {"identity": target_identity}
    target_name = _display_text(agent.get("name"))
    if target_name is not None:
        target["name"] = target_name
    if "tmux" in agent:
        raw_tmux = agent.get("tmux")
        if not isinstance(raw_tmux, Mapping):
            return None
        session = raw_tmux.get("session")
        window_index = raw_tmux.get("windowIndex")
        pane_id = raw_tmux.get("paneId")
        if (not isinstance(session, str) or not session or len(session) > 256
                or re.search(r"[\x00-\x1f\x7f]", session)):
            return None
        if isinstance(window_index, bool):
            return None
        if isinstance(window_index, int) and 0 <= window_index <= 999_999_999:
            window_index = str(window_index)
        if (not isinstance(window_index, str)
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", window_index) is None):
            return None
        if not isinstance(pane_id, str) or re.fullmatch(r"%[0-9]{1,12}", pane_id) is None:
            return None
        tmux: dict[str, Any] = {
            "session": session,
            "windowIndex": window_index,
            "paneId": pane_id,
        }
        if "socket" in raw_tmux:
            socket = raw_tmux.get("socket")
            if not isinstance(socket, Mapping):
                return None
            kind = socket.get("kind")
            value = socket.get("value")
            if (kind not in {"name", "path"} or not isinstance(value, str)
                    or not value or len(value) > 4096
                    or re.search(r"[\x00-\x1f\x7f]", value)
                    or (kind == "name" and ("/" in value or len(value) > 128))
                    or (kind == "path" and (not value.startswith("/") or len(value) < 2))):
                return None
            tmux["socket"] = {
                "kind": kind, "value": value,
            }
        target["tmux"] = tmux
    if operation not in {"resolve", "revalidate", "verify-target", "match"}:
        return None
    if operation in {"resolve", "match"} and prior is not None:
        return None
    if operation == "revalidate" and not isinstance(prior, Mapping):
        return None
    request: dict[str, Any] = {
        "schema": "agent-window-resolver.request.v1",
        "requestId": _request_id(agent, operation),
        "operation": operation,
        "target": target,
        "local": {"machine": str(local_machine)},
        "windows": [] if operation == "verify-target" else [dict(window) for window in windows],
        "limits": {
            "deadlineMs": 12_000,
            "maxRequestBytes": 262_144,
            "maxStdoutBytes": 1_048_576,
            "maxStderrBytes": 16_384,
        },
    }
    if operation in {"resolve", "revalidate"}:
        request["requestedRelation"] = "visible_exact"
    if prior is not None:
        request["prior"] = dict(prior)
    return request


def resolve_agent(
    agent: Mapping[str, Any],
    clients: Iterable[Mapping[str, Any]],
    local_machine: str,
    *,
    prior: Mapping[str, Any] | None = None,
    operation: str = "resolve",
    proc_root: str = "/proc",
    collector: Any = None,
    resolver: Any = None,
) -> ResolveResult:
    """Perform one bounded, read-only resolver operation in-process."""
    if operation not in ("match", "resolve", "revalidate", "verify-target"):
        return ResolveResult({
            "schema": "agent-window-resolver.response.v1", "requestId": "yoohoo",
            "operation": None, "status": "invalid", "candidates": [], "evidence": [],
            "reasons": [{"code": "invalid_operation", "source": "request",
                         "message": "unsupported resolver operation", "retryable": False}],
        }, ())
    try:
        windows = () if operation == "verify-target" else window_records(clients, proc_root=proc_root)
    except (OSError, ValueError, TypeError) as error:
        return ResolveResult(
            {"schema": "agent-window-resolver.response.v1", "requestId": "yoohoo",
             "operation": operation, "evidence": [],
             "status": "unreachable", "candidates": [], "reasons": [{
                "code": "window_snapshot_incomplete", "source": "compositor",
                "message": str(error)[:512], "retryable": True,
            }]}, (),
        )
    request = request_for_agent(
        agent, windows, local_machine, operation=operation, prior=prior
    )
    if request is None:
        return ResolveResult(
            {"schema": "agent-window-resolver.response.v1", "requestId": "yoohoo",
             "operation": operation, "evidence": [],
             "status": "invalid", "candidates": [], "reasons": [{
                "code": "invalid_agent_identity", "source": "request",
                "message": "Hub agent identity cannot form a v1 request",
                "retryable": False,
            }]}, windows
        )
    active_resolver = resolver if resolver is not None else Resolver()
    active_collector = collector if collector is not None else LinuxCollector()
    response = active_resolver.resolve(request, active_collector)
    return ResolveResult(response, windows)


def verify_target(
    agent: Mapping[str, Any],
    local_machine: str,
    *,
    collector: Any = None,
    resolver: Any = None,
) -> ResolveResult:
    """Verify the roster process/location before a transport attach."""
    return resolve_agent(
        agent, (), local_machine, operation="verify-target",
        collector=collector, resolver=resolver,
    )


def candidate_window(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the top exact or best-effort matched window for presentation."""
    if response.get("status") != "matched":
        return None
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    if response.get("operation") != "match" and len(candidates) != 1:
        return None
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None
    proof = candidate.get("proof")
    if isinstance(proof, Mapping):
        if proof.get("state") != "complete" or proof.get("relation") != "visible_exact":
            return None
    elif not _valid_match_candidate(candidate):
        return None
    window = candidate.get("window")
    return dict(window) if isinstance(window, Mapping) else None


def _valid_match_candidate(candidate: Mapping[str, Any]) -> bool:
    match = candidate.get("match")
    window = candidate.get("window")
    target = candidate.get("target")
    if (not isinstance(match, Mapping) or match.get("confidence") not in {
            "high", "medium", "low"
    } or isinstance(match.get("score"), bool)
            or not isinstance(match.get("score"), int)
            or not 0 <= match["score"] <= 100
            or not isinstance(match.get("evidence"), list)
            or not match["evidence"]
            or not isinstance(match.get("uncertainty"), list)
            or not isinstance(window, Mapping)
            or not isinstance(target, Mapping)):
        return False
    return True


def _window_identity(window: Mapping[str, Any]) -> tuple[str, str, int, str] | None:
    stable = window.get("stableId")
    address = _address(window.get("address"))
    pid = window.get("pid")
    ticks = window.get("startTimeTicks")
    if (not isinstance(stable, str) or not stable or not address
            or isinstance(pid, bool) or not isinstance(pid, int)
            or not isinstance(ticks, str)
            or re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", ticks) is None):
        return None
    return stable, address, pid, ticks


def _ordered_candidates(
    candidates: list[Mapping[str, Any]],
    *,
    clients: Iterable[Mapping[str, Any]] | None = None,
    active_address: str | None = None,
) -> list[Mapping[str, Any]]:
    """Keep resolver score primary, then prefer active/MRU adapter metadata."""
    history: dict[str, int] = {}
    for client in clients or ():
        if not isinstance(client, Mapping):
            continue
        address = _address(client.get("address"))
        value = client.get("focusHistoryID")
        if address and isinstance(value, int) and not isinstance(value, bool):
            history[address] = value
    preferred = _address(active_address) if active_address else ""
    return sorted(candidates, key=lambda candidate: (
        -int((candidate.get("match") or {}).get("score", -1)),
        0 if preferred and _address((candidate.get("window") or {}).get("address")) == preferred else 1,
        history.get(_address((candidate.get("window") or {}).get("address")), 1 << 30),
        str((candidate.get("window") or {}).get("stableId", "")),
        _address((candidate.get("window") or {}).get("address")),
        int((candidate.get("window") or {}).get("pid", 0)),
        int(str((candidate.get("window") or {}).get("startTimeTicks", "0"))),
    ))


def candidate_record(
    response: Mapping[str, Any],
    *,
    clients: Iterable[Mapping[str, Any]] | None = None,
    active_address: str | None = None,
) -> dict[str, Any] | None:
    """Return the top exact proof or ranked best-effort match record."""
    if response.get("status") != "matched":
        return None
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    match_candidates = [candidate for candidate in candidates
                        if isinstance(candidate, Mapping)
                        and _valid_match_candidate(candidate)]
    if match_candidates:
        candidate = _ordered_candidates(
            match_candidates, clients=clients, active_address=active_address
        )[0]
    else:
        candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None
    proof = candidate.get("proof")
    window = candidate.get("window")
    target = candidate.get("target")
    if isinstance(proof, Mapping):
        if (len(candidates) != 1 or proof.get("state") != "complete"
                or proof.get("relation") != "visible_exact"):
            return None
    elif not _valid_match_candidate(candidate):
        return None
    if not isinstance(window, Mapping) or not isinstance(target, Mapping):
        return None
    return dict(candidate)


def candidate_record_for_window(
    response: Mapping[str, Any], prior: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Select a rematch only when it preserves the prior four-field identity."""
    prior_window = prior.get("window") if isinstance(prior, Mapping) else None
    wanted = _window_identity(prior_window) if isinstance(prior_window, Mapping) else None
    if wanted is None or response.get("status") != "matched":
        return None
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        window = candidate.get("window")
        if (isinstance(window, Mapping) and _window_identity(window) == wanted
                and (_valid_match_candidate(candidate)
                     or (isinstance(candidate.get("proof"), Mapping)
                         and candidate["proof"].get("state") == "complete"
                         and candidate["proof"].get("relation") == "visible_exact"))):
            return dict(candidate)
    return None


__all__ = [
    "ResolveResult", "candidate_record", "candidate_record_for_window",
    "candidate_window", "process_start_ticks",
    "verify_target",
    "request_for_agent", "resolve_agent", "window_records",
]
