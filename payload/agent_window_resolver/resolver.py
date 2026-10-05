"""Deterministic proof and cardinality engine for protocol v1."""
from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
from pathlib import PurePath
import re
import unicodedata
from typing import Any, Iterable, Literal, Mapping, Sequence

from .collector import (
    Collector,
    Deadline,
    Endpoint,
    ObservationError,
    ProcessNode,
    TargetObservation,
    TmuxClient,
    TmuxTarget,
    TopologySnapshot,
    WindowObservation,
    parse_tmux_command as _parse_tmux_command_shared,
    split_tmux_command as _split_tmux_command_shared,
    transport_command_hint as _transport_command_hint_shared,
    transport_socket_eligible,
)
from .model import (
    MAX_PID,
    MAX_TICKS,
    PriorCandidate,
    ProcessIdentity,
    Request,
    RequestError,
    SocketSelector,
    Target,
    TmuxLocation,
    Window,
    canonical_machine,
    identity_json,
    machine_matches,
    parse_request,
    socket_json,
    window_json,
)

_REASON_SOURCES = {
    "request", "caller", "compositor", "proc", "argv", "tmux", "transport",
    "socket", "ssh_environment", "roster", "dependency", "internal",
}


@dataclass(frozen=True)
class _TransportHint:
    process: ProcessIdentity
    kind: str
    host: str
    target: TmuxTarget
    socket: SocketSelector | None

    @property
    def session(self) -> str:
        """Compatibility for internal callers that only need bare sessions."""
        return self.target.session or ""


@dataclass(frozen=True)
class _MatchHint:
    process: ProcessIdentity
    kind: Literal["transport", "tmux"]
    transport_kind: str
    host: str
    target: TmuxTarget
    socket: SocketSelector | None


def _reason(code: str, source: str, message: str,
            retryable: bool = False) -> dict[str, Any]:
    return {
        "code": code,
        "source": source if source in _REASON_SOURCES else "internal",
        "message": message[:512] or code,
        "retryable": retryable,
    }


def _observation_reasons(errors: Iterable[ObservationError]) -> list[dict[str, Any]]:
    return [_reason(item.code, item.source, item.message, item.retryable)
            for item in errors]


def _response(
    request: Request | None,
    status: str,
    *,
    candidates: Sequence[dict[str, Any]] = (),
    verified_target: dict[str, Any] | None = None,
    evidence: Sequence[dict[str, Any]] = (),
    reasons: Sequence[dict[str, Any]] = (),
    request_id: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "agent-window-resolver.response.v1",
        "requestId": request.request_id if request is not None else request_id,
        "operation": request.operation if request is not None else operation,
        "status": status,
        "candidates": list(candidates),
        "evidence": list(evidence),
        "reasons": list(reasons),
    }
    if request is not None and request.requested_relation is not None:
        result["requestedRelation"] = request.requested_relation
    if verified_target is not None:
        result["verifiedTarget"] = verified_target
    return result
def _identity_equivalent(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return (
        machine_matches(left.machine, right.machine)
        and left.pid == right.pid
        and left.start_time_ticks == right.start_time_ticks
    )



def _identity_key(value: ProcessIdentity) -> tuple[str, int, str]:
    return canonical_machine(value.machine), value.pid, value.start_time_ticks


def _valid_identity(value: ProcessIdentity) -> bool:
    ticks = value.start_time_ticks
    return (
        isinstance(value.machine, str) and bool(canonical_machine(value.machine))
        and isinstance(value.pid, int) and not isinstance(value.pid, bool)
        and 1 <= value.pid <= MAX_PID
        and isinstance(ticks, str) and ticks.isascii() and ticks.isdigit()
        and (ticks == "0" or not ticks.startswith("0"))
        and len(ticks) <= 20 and int(ticks) <= MAX_TICKS
    )


def _graph_index(
    nodes: Iterable[ProcessNode],
    machine: str,
) -> tuple[dict[tuple[str, int, str], ProcessNode], bool]:
    expected = canonical_machine(machine)
    index: dict[tuple[str, int, str], ProcessNode] = {}
    pid_ticks: dict[tuple[str, int], str] = {}
    complete = True
    for node in nodes:
        if not isinstance(node, ProcessNode) or not _valid_identity(node.identity):
            complete = False
            continue
        key = _identity_key(node.identity)
        if key[0] != expected:
            complete = False
            continue
        pid_key = key[:2]
        prior_ticks = pid_ticks.get(pid_key)
        if prior_ticks is not None and prior_ticks != key[2]:
            complete = False
            continue
        pid_ticks[pid_key] = key[2]
        existing = index.get(key)
        if existing is not None and existing != node:
            complete = False
            continue
        index[key] = node
        if node.parent is not None:
            if (not _valid_identity(node.parent)
                    or canonical_machine(node.parent.machine) != expected):
                complete = False
    finished: set[tuple[str, int, str]] = set()
    for origin in tuple(index):
        current = origin
        path: set[tuple[str, int, str]] = set()
        while current in index and current not in finished:
            if current in path:
                complete = False
                break
            path.add(current)
            parent = index[current].parent
            if parent is None:
                break
            current = _identity_key(parent)
        finished.update(path)
    return index, complete


def _ancestor_contains(
    index: Mapping[tuple[str, int, str], ProcessNode],
    child: ProcessIdentity,
    ancestor: ProcessIdentity,
    *,
    max_depth: int = 64,
) -> bool:
    current = child
    seen: set[tuple[str, int, str]] = set()
    wanted = _identity_key(ancestor)
    if wanted not in index:
        return False
    for _ in range(max_depth):
        key = _identity_key(current)
        if key == wanted:
            return True
        if key in seen:
            return False
        seen.add(key)
        node = index.get(key)
        if node is None or node.parent is None:
            return False
        current = node.parent
    return False


def _window_subtree(
    observation: WindowObservation,
    local_machine: str,
) -> tuple[tuple[ProcessNode, ...], bool]:
    index, complete = _graph_index(observation.processes, local_machine)
    root = ProcessIdentity(
        canonical_machine(local_machine),
        observation.window.pid,
        observation.window.start_time_ticks,
    )
    if _identity_key(root) not in index:
        return (), False
    nodes = tuple(
        node for node in index.values()
        if _ancestor_contains(index, node.identity, root)
    )
    return nodes, complete


def _split_tmux_command(command: Sequence[str]) -> tuple[str, SocketSelector | None] | None:
    return _split_tmux_command_shared(command)


def _transport_hint(node: ProcessNode) -> _TransportHint | None:
    parsed = _transport_command_hint_shared(node.argv)
    if parsed is None:
        return None
    kind, host, command = parsed
    return _TransportHint(
        node.identity, kind, host, command.target, command.socket
    )


def _address_key(family: str, address: str) -> tuple[str, bytes] | None:
    if "%" in address:
        return None
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return None
    if value.is_unspecified:
        return None
    if family == "ipv4" and isinstance(value, ipaddress.IPv4Address):
        return "ipv4", value.packed
    if family == "ipv6" and isinstance(value, ipaddress.IPv6Address):
        if value.ipv4_mapped is not None:
            return "ipv4", value.ipv4_mapped.packed
        return "ipv6", value.packed
    return None


def endpoint_linked(left: Endpoint, right: Endpoint) -> bool:
    if left.protocol != right.protocol:
        return False
    if not all(1 <= port <= 65535 for port in (
        left.local_port, left.remote_port, right.local_port, right.remote_port
    )):
        return False
    return (
        _address_key(left.address_family, left.local_address)
        == _address_key(right.address_family, right.remote_address)
        and _address_key(left.address_family, left.remote_address)
        == _address_key(right.address_family, right.local_address)
        and _address_key(left.address_family, left.local_address) is not None
        and _address_key(left.address_family, left.remote_address) is not None
        and left.local_port == right.remote_port
        and left.remote_port == right.local_port
    )


def _endpoint_json(value: Endpoint) -> dict[str, Any]:
    return {
        "protocol": value.protocol,
        "addressFamily": value.address_family,
        "local": {
            "address": value.local_address, "port": value.local_port,
        },
        "remote": {
            "address": value.remote_address, "port": value.remote_port,
        },
    }


def _target_json(target: Target) -> dict[str, Any]:
    location: dict[str, Any]
    if target.tmux is None:
        location = {"kind": "local"}
    else:
        tmux: dict[str, Any] = {
            "session": target.tmux.session,
            "windowIndex": target.tmux.window_index,
            "paneId": target.tmux.pane_id,
        }
        if target.tmux.socket is not None:
            tmux["socket"] = socket_json(target.tmux.socket)
        location = {"kind": "tmux", "tmux": tmux}
    return {
        "identity": identity_json(target.identity, instance_id=target.instance_id),
        "location": location,
    }


def _match_text(value: str) -> str:
    """Bounded, case-insensitive display-name normalization for heuristics."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _match_aliases(value: str) -> tuple[str, ...]:
    normalized = _match_text(value)
    if not normalized:
        return ()
    aliases = {normalized}
    # Agent/session displays commonly use ``session (description)``.  The
    # leading session is useful, but never treat a numeric prefix as a loose
    # substring (0_1_9 must not match 0_1_90).
    if " (" in normalized:
        leading = normalized.split(" (", 1)[0].strip()
        if leading:
            aliases.add(leading)
    return tuple(sorted(aliases, key=lambda item: (len(item), item), reverse=True))


def _name_matches(value: str, wanted: str) -> tuple[bool, bool]:
    """Return (matched, exact) using whole-name boundaries."""
    text = _match_text(value)
    if not text:
        return False, False
    aliases = _match_aliases(wanted)
    for alias in aliases:
        if text == alias:
            return True, True
    for alias in aliases:
        if re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(alias)}(?![A-Za-z0-9_.-])", text
        ):
            return True, False
    return False, False


def _session_matches(value: str, wanted: str) -> bool:
    """Session identifiers compare by bounded aliases, never loose substrings."""
    return bool(set(_match_aliases(value)) & set(_match_aliases(wanted)))


def _tmux_target_relation(
    observed: TmuxTarget,
    wanted: TmuxLocation,
) -> tuple[Literal["matches", "conflicts", "uncertain"], tuple[str, ...]]:
    """Compare only like tmux selectors; never infer missing ID mappings."""
    uncertain: list[str] = []
    wanted_session_kind = (
        "id" if wanted.session.startswith("$") and wanted.session[1:].isdigit()
        else "name"
    )
    if observed.session is None or observed.session_kind is None:
        return "uncertain", ("session",)
    if observed.session_kind != wanted_session_kind:
        return "uncertain", ("session-kind",)
    if observed.session_exact:
        session_match = observed.session == wanted.session
    elif observed.session_kind == "name":
        session_match = _session_matches(observed.session, wanted.session)
    else:
        session_match = observed.session == wanted.session
    if not session_match:
        return "conflicts", ("session",)

    comparisons = (
        ("window", observed.window, observed.window_kind,
         wanted.window_index, "index"),
        ("pane", observed.pane, observed.pane_kind, wanted.pane_id, "id"),
    )
    for label, value, kind, requested, requested_kind in comparisons:
        if value is None or kind is None:
            uncertain.append(label)
        elif kind == "relative" or kind != requested_kind:
            uncertain.append(label + "-kind")
        elif value != requested:
            return "conflicts", (label,)

    return ("uncertain", tuple(uncertain)) if uncertain else ("matches", ())


def _tmux_command_relation(
    observed: TmuxTarget,
    observed_socket: SocketSelector | None,
    wanted: TmuxLocation,
) -> tuple[Literal["matches", "conflicts", "uncertain"], tuple[str, ...]]:
    relation, fields = _tmux_target_relation(observed, wanted)
    if relation == "conflicts":
        return relation, fields
    uncertain = list(fields)
    wanted_socket = wanted.socket
    if observed_socket is None or wanted_socket is None:
        uncertain.append("socket")
    elif observed_socket.kind != wanted_socket.kind:
        uncertain.append("socket-kind")
    elif observed_socket.value != wanted_socket.value:
        return "conflicts", ("socket",)
    return ("uncertain", tuple(uncertain)) if uncertain else ("matches", ())


def _tmux_hint_details(hint: _MatchHint) -> dict[str, Any]:
    details: dict[str, Any] = {
        "kind": hint.transport_kind,
        "session": hint.target.session or "",
        "sessionKind": hint.target.session_kind or "unknown",
        "sessionExact": hint.target.session_exact,
    }
    if hint.target.window is not None:
        details["window"] = hint.target.window
        details["windowKind"] = hint.target.window_kind or "unknown"
    if hint.target.pane is not None:
        details["pane"] = hint.target.pane
        details["paneKind"] = hint.target.pane_kind or "unknown"
    if hint.socket is not None:
        details["socketKind"] = hint.socket.kind
        details["socketValue"] = hint.socket.value
    return details


def _match_evidence(
    code: str,
    source: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    result: str = "supports",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "source": source,
        "result": result,
    }
    if details:
        item["details"] = {
            key: ("".join(char for char in value if char.isprintable())[:512]
                  if isinstance(value, str) else value)
            for key, value in details.items()
        }
    return item


def _append_unique_reason(
    reasons: list[dict[str, Any]], reason: dict[str, Any]
) -> None:
    if not any(item.get("code") == reason.get("code") for item in reasons):
        reasons.append(reason)


def _match_hints(
    nodes: Sequence[ProcessNode],
) -> tuple[_MatchHint, ...]:
    """Extract only normalized transport/tmux hints; never expose raw argv."""
    result: list[_MatchHint] = []
    for node in nodes:
        hint = _transport_hint(node)
        if hint is not None:
            result.append(_MatchHint(
                node.identity, "transport", hint.kind, hint.host,
                hint.target, hint.socket,
            ))
        parsed = _parse_tmux_command_shared(node.argv)
        if parsed is not None:
            result.append(_MatchHint(
                node.identity, "tmux", "", "", parsed.target, parsed.socket,
            ))
    return tuple(result)


def _match_candidate(
    request: Request,
    observation: WindowObservation,
    target_observation: TargetObservation,
) -> dict[str, Any] | None:
    """Build a ranked, explicitly heuristic candidate for ``operation=match``."""
    nodes, graph_complete = _window_subtree(observation, request.local_machine)
    graph_complete = (graph_complete and observation.collection_state == "complete"
                      and not observation.errors)
    # A partial collector may have retained useful argv/title observations but
    # omitted the window root.  They remain hints, never ancestry proof.
    if not nodes:
        nodes = tuple(observation.processes)
    target = request.target
    evidence: list[dict[str, Any]] = []
    uncertainty: list[dict[str, Any]] = []
    scores: list[int] = []
    strong = False
    weak = False
    contradictory_machine = False
    contradictory_session = False
    conflicting_launch_hint = False
    current_title_match = False

    # Existing strict topology proof is an excellent ranking signal, but it is
    # optional for match.  In particular, a readable title/argv match must
    # survive a failed remote probe.  Reuse the canonical strict matcher when
    # it happens to be available so local tmux client visibility strengthens a
    # candidate without becoming a prerequisite for best-effort matching.
    exact_request = replace(
        request, operation="resolve", requested_relation="visible_exact"
    )
    exact_target, exact_target_evidence, _ = _target_complete(
        exact_request, target_observation
    )
    exact_candidate = None
    if exact_target is not None and graph_complete:
        exact_candidate = _candidate_for_window(
            exact_request, observation, exact_target, target_observation
        )
    if exact_candidate is not None:
        evidence.extend(exact_candidate["proof"]["evidence"])
        scores.append(100)
        strong = True

    local_target = machine_matches(target.identity.machine, request.local_machine)
    if local_target and graph_complete and any(
        _identity_equivalent(target.identity, node.identity) for node in nodes
    ):
        # _window_subtree only returns descendants of the window root when its
        # graph is complete, so identity presence is a closed local ancestry
        # hint while still being reported without the strict proof shape.
        evidence.append(_match_evidence(
            "local_process_ancestry_hint", "proc",
            "the live window process graph contains the target identity",
            details={"pid": target.identity.pid},
        ))
        scores.append(100)
        strong = True

    if (local_target and target.tmux is not None
            and machine_matches(
                target_observation.machine, request.local_machine
            )):
        local_clients = _local_match_clients(nodes, target, target_observation)
        if local_clients:
            # A current tmux client row plus PID membership in the supplied
            # window subtree is a strong ranking signal, but remains a match
            # hint: the local probe intentionally did not collect exact
            # target/client ancestry or transport evidence.
            for client in local_clients:
                evidence.append(_match_evidence(
                    "tmux_client_window_subtree_hint", "tmux",
                    "current tmux client location matches the requested pane "
                    "and its PID is in the supplied window process subtree",
                    details={
                        "pid": client.process.pid,
                        "startTimeTicks": client.process.start_time_ticks,
                        "session": client.current_session,
                        "windowIndex": client.current_window_index,
                        "paneId": client.current_pane_id,
                    },
                ))
                scores.append(92)
            strong = True
            _append_unique_reason(uncertainty, _reason(
                "tmux_client_not_exact_proof", "tmux",
                "current tmux client location and PID membership are "
                "best-effort evidence, not exact target ancestry proof",
            ))

    hints = _match_hints(nodes)
    for hint in hints:
        kind = hint.kind
        transport_kind = hint.transport_kind
        host = hint.host
        session = hint.target.session or ""
        hint_machine = host.rsplit("@", 1)[-1] if host else ""
        host_match = bool(hint_machine) and machine_matches(
            hint_machine, target.identity.machine
        )
        relation: Literal["matches", "conflicts", "uncertain"] = "uncertain"
        relation_fields: tuple[str, ...] = ()
        if target.tmux is not None:
            relation, relation_fields = _tmux_command_relation(
                hint.target, hint.socket, target.tmux
            )
        session_comparable = (
            bool(session)
            and "session" not in relation_fields
            and "session-kind" not in relation_fields
        )
        target_compatible = (
            target.tmux is not None
            and session_comparable
            and relation != "conflicts"
        )
        qualifier_conflict = (
            relation == "conflicts" and relation_fields != ("session",)
        )
        if kind == "transport":
            if host and not host_match:
                contradictory_machine = True
                evidence.append(_match_evidence(
                    "transport_machine_mismatch", "transport",
                    "transport host hint conflicts with the requested machine",
                    details={"machine": host}, result="contradicts",
                ))
            if (target.tmux is not None and session
                    and relation == "conflicts"):
                contradictory_session = True
                conflicting_launch_hint = True
                evidence.append(_match_evidence(
                    (
                        "transport_tmux_target_mismatch"
                        if qualifier_conflict else "transport_session_mismatch"
                    ),
                    "tmux",
                    "transport tmux target conflicts with the requested location",
                    details={
                        "session": session,
                        "conflictingField": relation_fields[0],
                    },
                    result="contradicts",
                ))
            if host_match and target_compatible:
                details = _tmux_hint_details(hint)
                evidence.append(_match_evidence(
                    "transport_host_session_hint", "argv",
                    "argv identifies the requested host and a compatible tmux target",
                    details=details,
                ))
                scores.append(90)
                strong = True
            elif host_match:
                evidence.append(_match_evidence(
                    "transport_host_hint", "argv",
                    "argv identifies the requested host",
                    details={"kind": transport_kind},
                ))
            elif target_compatible and not host:
                evidence.append(_match_evidence(
                    "transport_session_hint", "argv",
                    "argv identifies the requested tmux session",
                    details={"kind": transport_kind, "session": session},
                ))
                scores.append(62)
                weak = True
        elif target.tmux is not None:
            if relation == "conflicts":
                contradictory_session = True
                conflicting_launch_hint = True
                evidence.append(_match_evidence(
                    (
                        "tmux_target_mismatch"
                        if qualifier_conflict else "tmux_session_mismatch"
                    ),
                    "tmux", "local tmux command conflicts with the requested location",
                    details={
                        "session": session,
                        "conflictingField": relation_fields[0],
                    },
                    result="contradicts",
                ))
            elif target_compatible:
                details = _tmux_hint_details(hint)
                evidence.append(_match_evidence(
                    "tmux_session_hint", "tmux",
                    "argv names a compatible requested tmux target",
                    details=details,
                ))
                scores.append(72)
                weak = True
        if (relation == "uncertain"
                and any(field.endswith("-kind") for field in relation_fields)):
            _append_unique_reason(uncertainty, _reason(
                "tmux_target_selector_incomparable", "tmux",
                "a launch target selector uses a different identifier kind; "
                "no equality or mismatch was inferred",
            ))
        elif (relation == "uncertain"
              and any(field in {"window", "pane", "socket"}
                      for field in relation_fields)):
            _append_unique_reason(uncertainty, _reason(
                "tmux_target_qualifier_unobserved", "tmux",
                "the launch hint omits one or more requested tmux qualifiers; "
                "the bare session hint remains best effort",
            ))

    # Current titles remain useful even when historical launch arguments name
    # another connection. Report that disagreement, rather than veto the title.
    title = observation.window.title
    if title:
        if target.name:
            matched, exact = _name_matches(title, target.name)
            if matched:
                current_title_match = True
                evidence.append(_match_evidence(
                    "window_title_target_name", "compositor",
                    "window title names the requested agent",
                    details={"exact": exact},
                ))
                scores.append(65 if exact else 55)
                weak = True
        if target.tmux is not None:
            matched, exact = _name_matches(title, target.tmux.session)
            if matched:
                current_title_match = True
                evidence.append(_match_evidence(
                    "window_title_tmux_session", "compositor",
                    "window title names the requested tmux session",
                    details={"exact": exact},
                ))
                scores.append(58 if exact else 50)
                weak = True

    if target.name and not conflicting_launch_hint:
        launch_processes = {_identity_key(hint.process) for hint in hints}
        for node in nodes:
            if _identity_key(node.identity) in launch_processes:
                # A recognized tmux launch is evaluated structurally above.
                # Its raw target text must not reappear as independent name
                # evidence after a qualifier or socket conflict.
                continue
            if any(_name_matches(value, target.name)[0] for value in node.argv):
                evidence.append(_match_evidence(
                    "process_agent_name_hint", "argv",
                    "a process argument names the requested agent",
                ))
                scores.append(42)
                weak = True
                break

    if not scores:
        return None
    if weak and not strong and contradictory_machine and not current_title_match:
        return None

    if not strong:
        _append_unique_reason(uncertainty, _reason(
            "heuristic_match_not_process_proof", "caller",
            "matching evidence is heuristic and does not prove attachment",
        ))
    if hints:
        _append_unique_reason(uncertainty, _reason(
            "transport_hint_not_exact_proof", "transport",
            "transport and session hints were not upgraded to endpoint proof",
        ))
        if any(item.transport_kind == "mosh" for item in hints):
            _append_unique_reason(uncertainty, _reason(
                "mosh_hint_not_exact_proof", "transport",
                "mosh argv is useful matching evidence without exact UDP proof",
            ))
        if weak and not strong and (contradictory_machine or contradictory_session):
            _append_unique_reason(uncertainty, _reason(
                "stale_transport_hint", "argv",
                "a launch hint conflicts with current display/name evidence",
            ))
    if target_observation.collection_state != "complete" or target_observation.errors:
        target_reasons = _observation_reasons(target_observation.errors)
        if not target_reasons:
            target_reasons = [_reason(
                "target_collection_incomplete", "proc",
                "target collection was incomplete; retained hints are best effort",
                True,
            )]
        for reason in target_reasons:
            _append_unique_reason(uncertainty, reason)
    if observation.collection_state != "complete" or observation.errors:
        window_reasons = _observation_reasons(observation.errors)
        if not window_reasons:
            window_reasons = [_reason(
                "window_collection_incomplete", "proc",
                "window collection was incomplete; retained hints are best effort",
                True,
            )]
        for reason in window_reasons:
            _append_unique_reason(uncertainty, reason)

    if not local_target:
        _append_unique_reason(uncertainty, _reason(
            "target_process_not_local", "transport",
            "target machine differs from the local compositor machine",
        ))
    score = max(0, min(100, max(scores)))
    confidence = "high" if score >= 80 else "medium" if score >= 50 else "low"
    return {
        "window": window_json(observation.window),
        "target": _target_json(target),
        "match": {
            "confidence": confidence,
            "score": score,
            "evidence": evidence,
            "uncertainty": uncertainty,
        },
    }


def _match_request(
    request: Request,
    snapshot: TopologySnapshot,
) -> dict[str, Any]:
    observations: dict[tuple[str, str, int, str], list[WindowObservation]] = {}
    for item in snapshot.windows:
        observations.setdefault(_window_key(item.window), []).append(item)
    candidates: list[dict[str, Any]] = []
    local_incomplete = False
    for window in request.windows:
        items = observations.get(_window_key(window), [])
        if len(items) != 1 or items[0].window != window:
            local_incomplete = True
            continue
        if items[0].collection_state != "complete" or items[0].errors:
            local_incomplete = True
        candidate = _match_candidate(request, items[0], snapshot.target)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (
        -int(item["match"]["score"]),
        item["window"]["stableId"], item["window"]["address"],
        item["window"]["pid"], int(item["window"]["startTimeTicks"]),
    ))
    if candidates:
        return _response(request, "matched", candidates=candidates)
    target_incomplete = any(error.retryable for error in snapshot.target.errors)
    if local_incomplete or target_incomplete:
        return _response(request, "unresolved", reasons=[_reason(
            "local_collection_incomplete", "proc",
            "some local window or tmux observations could not be inspected; "
            "absence is unknown", True,
        )])
    reasons = [_reason(
        "candidate_count", "caller", "no supplied window has usable match evidence",
    )]
    if snapshot.target.collection_state != "complete" or snapshot.target.errors:
        for reason in _observation_reasons(snapshot.target.errors):
            _append_unique_reason(reasons, reason)
    return _response(request, "unresolved", reasons=reasons)


def _normalize_target(request: Request, observation: TargetObservation) -> Target | None:
    target = request.target
    if canonical_machine(observation.machine) != target.identity.machine:
        return None
    if target.tmux is None:
        return target if observation.pane is None else None
    location = target.tmux
    if location.socket is None:
        selector = observation.socket
        if (selector is None or selector.kind != "path"
                or not observation.actual_socket_path
                or selector.value != observation.actual_socket_path):
            return None
    else:
        selector = location.socket
        if observation.socket != selector or not observation.actual_socket_path:
            return None
    return Target(
        target.identity,
        target.instance_id,
        TmuxLocation(location.session, location.window_index,
                     location.pane_id, selector),
        target.name,
    )


def _target_complete(
    request: Request,
    observation: TargetObservation,
) -> tuple[Target | None, list[dict[str, Any]], list[dict[str, Any]]]:
    reasons = _observation_reasons(observation.errors)
    if observation.collection_state != "complete" or observation.errors:
        if not reasons:
            reasons.append(_reason("target_collection_incomplete", "proc",
                                   "target collection was incomplete", True))
        return None, [], reasons
    normalized = _normalize_target(request, observation)
    if normalized is None:
        return None, [], [_reason(
            "target_location_mismatch", "tmux",
            "target machine or normalized location did not match",
        )]
    index, graph_complete = _graph_index(
        observation.processes, request.target.identity.machine
    )
    target_present = _identity_key(request.target.identity) in index
    boundary_valid = (
        observation.target_boundary is not None
        and observation.pane is not None
        and observation.target_boundary == observation.pane.process
        and target_present
        and _ancestor_contains(
            index, request.target.identity, observation.target_boundary
        )
    )
    if (not target_present
            or not graph_complete
            or (observation.target_chain_complete is not True and not boundary_valid)
            or (observation.target_boundary is not None and not boundary_valid)):
        return None, [], [_reason(
            "target_process_mismatch", "proc",
            "target PID/start identity is not live",
        )]
    evidence = [{
        "code": "target_process_live",
        "source": "proc",
        "result": "supports",
        "details": {
            "pid": request.target.identity.pid,
            "startTimeTicks": request.target.identity.start_time_ticks,
        },
    }]
    if request.target.tmux is not None:
        pane = observation.pane
        wanted = request.target.tmux
        if (pane is None or pane.session != wanted.session
                or pane.window_index != wanted.window_index
                or pane.pane_id != wanted.pane_id
                or not _ancestor_contains(
                    index, request.target.identity, pane.process
                )):
            return None, evidence, [_reason(
                "pane_membership_mismatch", "tmux",
                "target identity is not a live member of the requested pane",
            )]
        evidence.append({
            "code": "pane_membership_exact",
            "source": "tmux",
            "result": "supports",
            "details": {
                "session": pane.session,
                "windowIndex": pane.window_index,
                "paneId": pane.pane_id,
            },
        })
    return normalized, evidence, []


def _client_endpoints(client: TmuxClient, machine: str) -> tuple[Endpoint, ...]:
    index, complete = _graph_index(client.processes, machine)
    if not complete or _identity_key(client.process) not in index:
        return ()
    related = (
        node for node in index.values()
        if _ancestor_contains(index, client.process, node.identity)
        or _ancestor_contains(index, node.identity, client.process)
    )
    return tuple(
        endpoint
        for node in related
        for endpoint in node.endpoints + node.ssh_endpoints
    )


def _remote_linked_clients(
    nodes: Sequence[ProcessNode],
    target: Target,
    observation: TargetObservation,
) -> tuple[tuple[TmuxClient, Endpoint, Endpoint], ...]:
    linked: dict[
        tuple[tuple[str, int, str], Endpoint, Endpoint],
        tuple[TmuxClient, Endpoint, Endpoint],
    ] = {}
    transport_endpoints: list[Endpoint] = []
    for hint_node in nodes:
        if not transport_socket_eligible(hint_node.argv):
            continue
        hint = _transport_hint(hint_node)
        executable = PurePath(hint_node.argv[0]).name if hint_node.argv else ""
        if (executable == "ssh"
                or (hint is not None and hint.kind == "ssh")):
            transport_endpoints.extend(hint_node.endpoints)
            transport_endpoints.extend(hint_node.ssh_endpoints)
        if (hint_node.argv
                and PurePath(hint_node.argv[0]).name == "mosh-client"):
            transport_endpoints.extend(
                endpoint for endpoint in hint_node.endpoints
                if endpoint.protocol == "udp"
            )
    for client in observation.clients:
        remote_endpoints = _client_endpoints(client, target.identity.machine)
        for left in sorted(transport_endpoints, key=repr):
            for right in sorted(remote_endpoints, key=repr):
                if endpoint_linked(left, right):
                    key = _identity_key(client.process), left, right
                    linked.setdefault(key, (client, left, right))
    return tuple(
        linked[key] for key in sorted(linked, key=repr)
    )


def _local_linked_clients(
    nodes: Sequence[ProcessNode],
    target: Target,
    observation: TargetObservation,
) -> tuple[TmuxClient, ...]:
    return tuple(client for client in observation.clients
                 if any(_identity_equivalent(client.process, node.identity)
                        for node in nodes))


def _local_match_clients(
    nodes: Sequence[ProcessNode],
    target: Target,
    observation: TargetObservation,
) -> tuple[TmuxClient, ...]:
    """Find current tmux clients in a supplied local window subtree.

    Best-effort local matching compares the client PID/start identity and
    current tmux location. The collector does not collect client ancestry or
    transport proof, so this helper must never feed the strict proof path.
    """
    if target.tmux is None:
        return ()
    wanted = target.tmux
    return tuple(
        client for client in observation.clients
        if any(_identity_equivalent(client.process, node.identity)
               for node in nodes)
        and client.current_session == wanted.session
        and client.current_window_index == wanted.window_index
        and client.current_pane_id == wanted.pane_id
    )


def _candidate_for_window(
    request: Request,
    observation: WindowObservation,
    target: Target,
    target_observation: TargetObservation,
) -> dict[str, Any] | None:
    nodes, complete = _window_subtree(observation, request.local_machine)
    client_keys = [
        _identity_key(client.process) for client in target_observation.clients
    ]
    if len(client_keys) != len(set(client_keys)):
        return None
    if not complete:
        return None
    if target.tmux is None:
        if (request.requested_relation != "visible_exact"
                or not machine_matches(target.identity.machine, request.local_machine)
                or not any(_identity_equivalent(target.identity, node.identity)
                           for node in nodes)):
            return None
        evidence = [{
            "code": "window_process_tree_contains_target",
            "source": "proc",
            "result": "supports",
            "details": {"pid": observation.window.pid},
        }]
    else:
        remote_pair: tuple[Endpoint, Endpoint] | None = None
        if machine_matches(target.identity.machine, request.local_machine):
            linked = _local_linked_clients(nodes, target, target_observation)
            if len(linked) != 1:
                return None
            client = linked[0]
        else:
            remote_links = _remote_linked_clients(nodes, target, target_observation)
            if len(remote_links) != 1:
                return None
            client, local_endpoint, target_endpoint = remote_links[0]
            remote_pair = local_endpoint, target_endpoint
        if request.requested_relation == "visible_exact" and (
            client.current_session != target.tmux.session
            or client.current_window_index != target.tmux.window_index
            or client.current_pane_id != target.tmux.pane_id
        ):
            return None
        details: dict[str, Any] = {
            "pid": client.process.pid,
            "startTimeTicks": client.process.start_time_ticks,
        }
        if remote_pair is not None:
            details["localEndpoint"] = _endpoint_json(remote_pair[0])
            details["targetEndpoint"] = _endpoint_json(remote_pair[1])
        evidence = [{
            "code": (
                "tmux_client_visible_exact"
                if request.requested_relation == "visible_exact"
                else "tmux_client_linked"
            ),
            "source": "socket" if not machine_matches(
                target.identity.machine, request.local_machine
            ) else "tmux",
            "result": "supports",
            "details": details,
        }]
    return {
        "window": window_json(observation.window),
        "target": _target_json(target),
        "proof": {
            "state": "complete",
            "relation": request.requested_relation,
            "evidence": evidence,
        },
    }


def _window_key(window: Window) -> tuple[str, str, int, str]:
    return window.stable_id, window.address, window.pid, window.start_time_ticks


def _target_equal(left: Target, right: Target) -> bool:
    # Display metadata is not part of the strict identity/location contract,
    # and is intentionally absent from serialized prior target records.
    return (left.identity == right.identity
            and left.instance_id == right.instance_id
            and left.tmux == right.tmux)


class Resolver:
    """Resolve only from live observations; never performs an action."""

    def __init__(self, now: Any = None) -> None:
        if now is None:
            import time
            now = time.monotonic
        self._now = now

    def resolve(
        self,
        value: Mapping[str, Any] | Request,
        collector: Collector,
    ) -> dict[str, Any]:
        if isinstance(value, Request):
            request = value
        else:
            try:
                request = parse_request(value)
            except (RequestError, TypeError, ValueError) as error:
                request_id = value.get("requestId") if isinstance(value, Mapping) else None
                operation = value.get("operation") if isinstance(value, Mapping) else None
                if not isinstance(request_id, str):
                    request_id = None
                if (not isinstance(operation, str)
                        or operation not in {
                            "resolve", "revalidate", "verify-target", "match"
                        }):
                    operation = None
                code = error.code if isinstance(error, RequestError) else "invalid_request"
                return _response(
                    None, "invalid", request_id=request_id, operation=operation,
                    reasons=[_reason(code, "request", str(error))],
                )
        if request.operation == "revalidate":
            assert request.prior is not None
            if request.prior.relation != request.requested_relation:
                return _response(request, "invalid", reasons=[_reason(
                    "prior_relation_mismatch", "request",
                    "prior relation does not equal requestedRelation",
                )])
            matching = [window for window in request.windows
                        if _window_key(window) == _window_key(request.prior.window)]
            if len(matching) > 1:
                return _response(request, "invalid", reasons=[_reason(
                    "duplicate_window_identity", "request",
                    "prior window identity occurs more than once",
                )])
            if not matching:
                return _response(request, "unresolved", reasons=[_reason(
                    "prior_window_missing_or_changed", "caller",
                    "prior window identity is absent from the supplied snapshot",
                )])
            if (request.prior.target.identity != request.target.identity
                    or request.prior.target.instance_id != request.target.instance_id):
                return _response(request, "unresolved", reasons=[_reason(
                    "prior_target_changed", "roster",
                    "target identity changed since the prior candidate",
                )])
        deadline = Deadline(request.limits.deadline_ms, self._now)
        try:
            snapshot = collector.collect(request, deadline)
        except Exception as error:
            return _response(request, "unreachable", reasons=[_reason(
                "collector_failure", "internal", str(error), True,
            )])
        if deadline.expired() and request.operation != "match":
            return _response(request, "unreachable", reasons=[_reason(
                "deadline_exceeded", "internal",
                "the overall collection deadline expired", True,
            )])
        if not isinstance(snapshot, TopologySnapshot):
            return _response(request, "invalid", reasons=[_reason(
                "collector_contract_violation", "internal",
                "collector returned an invalid topology snapshot",
            )])
        if request.operation == "match":
            return _match_request(request, snapshot)
        target, target_evidence, target_reasons = _target_complete(
            request, snapshot.target
        )
        if target is None:
            state = snapshot.target.collection_state
            status = "unreachable" if state == "unreachable" else "unresolved"
            return _response(
                request, status, evidence=target_evidence,
                reasons=target_reasons or [_reason(
                    "target_unresolved", "proc", "target could not be verified"
                )],
            )
        if request.operation == "verify-target":
            return _response(
                request, "verified", evidence=target_evidence,
                verified_target={
                    **_target_json(target),
                    "evidence": target_evidence,
                },
            )
        if request.operation == "revalidate":
            assert request.prior is not None
            if not _target_equal(request.prior.target, target):
                return _response(request, "unresolved", evidence=target_evidence,
                                 reasons=[_reason(
                    "prior_target_changed", "roster",
                    "normalized target identity or location changed",
                )])
            considered_keys = {_window_key(request.prior.window)}
        else:
            considered_keys = {_window_key(window) for window in request.windows}
        observations: dict[tuple[str, str, int, str], list[WindowObservation]] = {}
        for item in snapshot.windows:
            observations.setdefault(_window_key(item.window), []).append(item)
        selected: list[WindowObservation] = []
        for window in request.windows:
            key = _window_key(window)
            if key not in considered_keys:
                continue
            items = observations.get(key, [])
            if len(items) != 1 or items[0].window != window:
                return _response(request, "unresolved", evidence=target_evidence,
                                 reasons=[_reason(
                    "window_collection_incomplete", "compositor",
                    "window observation is missing or duplicated",
                )])
            selected.append(items[0])
        incomplete = [item for item in selected
                      if item.collection_state != "complete" or item.errors]
        malformed_graphs = [
            item for item in selected
            if not _window_subtree(item, request.local_machine)[1]
        ]
        if incomplete or malformed_graphs:
            errors = [error for item in incomplete for error in item.errors]
            status = "unreachable" if any(
                item.collection_state == "unreachable" for item in incomplete
            ) else "unresolved"
            return _response(
                request, status, evidence=target_evidence,
                reasons=_observation_reasons(errors) or [_reason(
                    "window_collection_incomplete", "proc",
                    "a considered window process scan was incomplete or inconsistent",
                    True,
                )],
            )
        candidates = [
            candidate for item in selected
            if (candidate := _candidate_for_window(
                request, item, target, snapshot.target
            )) is not None
        ]
        for candidate in candidates:
            candidate["proof"]["evidence"] = [
                *target_evidence, *candidate["proof"]["evidence"]
            ]
        candidates.sort(key=lambda item: (
            item["window"]["stableId"], item["window"]["address"],
            item["window"]["pid"], int(item["window"]["startTimeTicks"]),
        ))
        if request.operation == "revalidate":
            if len(candidates) == 1 and (
                _window_key(selected[0].window)
                == _window_key(request.prior.window)  # type: ignore[union-attr]
            ):
                return _response(request, "matched", candidates=candidates)
            return _response(request, "unresolved", evidence=target_evidence,
                             reasons=[_reason(
                "prior_relation_no_longer_proven", "transport",
                "the prior window no longer has the requested relation",
            )])
        if len(candidates) == 1:
            return _response(request, "matched", candidates=candidates)
        if len(candidates) > 1:
            return _response(request, "ambiguous", candidates=candidates,
                             reasons=[_reason(
                "candidate_count", "caller",
                "more than one supplied window qualifies",
            )])
        return _response(request, "unresolved", evidence=target_evidence,
                         reasons=[_reason(
            "candidate_count", "caller", "no supplied window qualifies",
        )])
