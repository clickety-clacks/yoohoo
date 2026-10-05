"""Protocol models and strict v1 request parsing."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

# The library version travels in every response envelope.  A caller that
# needs a newer request field can then tell "this copy is too old" apart from
# "my request is invalid": an older copy rejects the field without a version.
VERSION = "0.2.0"
MAX_PID = 4_194_304
MAX_TICKS = (1 << 64) - 1
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,19})\Z")
_WINDOW_INDEX = re.compile(r"^(0|[1-9][0-9]{0,8})\Z")
_PANE = re.compile(r"^%[0-9]{1,12}\Z")
_ADDRESS = re.compile(r"^0x[0-9a-f]{1,32}\Z")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
_DETAIL_KEY = re.compile(r"^[a-z][A-Za-z0-9]{0,63}\Z")
_EVIDENCE_SOURCES = {
    "caller", "compositor", "proc", "argv", "tmux", "transport", "socket",
    "ssh_environment", "roster",
}
_EVIDENCE_RESULTS = {"supports", "contradicts", "unavailable", "informational"}


class RequestError(ValueError):
    """A stable protocol validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SocketSelector:
    kind: str
    value: str


@dataclass(frozen=True)
class ProcessIdentity:
    machine: str
    pid: int
    start_time_ticks: str


@dataclass(frozen=True)
class TmuxLocation:
    session: str
    window_index: str
    pane_id: str
    socket: SocketSelector | None = None


@dataclass(frozen=True)
class Target:
    identity: ProcessIdentity
    instance_id: str
    tmux: TmuxLocation | None = None
    name: str | None = None


@dataclass(frozen=True)
class Window:
    stable_id: str
    address: str
    pid: int
    start_time_ticks: str
    window_class: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class PriorCandidate:
    window: Window
    target: Target
    relation: str


@dataclass(frozen=True)
class Limits:
    deadline_ms: int = 20_000
    max_request_bytes: int = 262_144
    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 16_384


@dataclass(frozen=True)
class Request:
    request_id: str
    operation: str
    requested_relation: str | None
    target: Target
    local_machine: str
    windows: tuple[Window, ...]
    prior: PriorCandidate | None
    limits: Limits
    probe_transports: bool = False


def canonical_machine(value: str) -> str:
    value = value.lower()
    return value[:-1] if value.endswith(".") else value


def machine_matches(left: str, right: str) -> bool:
    left, right = canonical_machine(left), canonical_machine(right)
    if not left or not right:
        return False
    if left == right:
        return True
    if "." not in left:
        return right.split(".", 1)[0] == left
    if "." not in right:
        return left.split(".", 1)[0] == right
    return False


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestError("invalid_" + name, f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str],
                name: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise RequestError("unknown_field", f"{name} has unknown fields")
    if missing:
        raise RequestError("missing_field", f"{name} is missing required fields")


def _plain(value: Any, name: str, *, minimum: int = 1,
           maximum: int = 512) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise RequestError("invalid_" + name, f"{name} has invalid length")
    if _CONTROL.search(value):
        raise RequestError("invalid_" + name, f"{name} contains control characters")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise RequestError(
            "invalid_" + name, f"{name} is not valid Unicode"
        ) from error
    return value


def _machine(value: Any, name: str = "machine") -> str:
    value = _plain(value, name, maximum=255)
    if _MACHINE.fullmatch(value) is None:
        raise RequestError("invalid_" + name, f"{name} is invalid")
    return value


def _pid(value: Any, name: str = "pid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PID:
        raise RequestError("invalid_" + name, f"{name} is invalid")
    return value


def _ticks(value: Any, name: str = "startTimeTicks") -> str:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise RequestError("invalid_" + name, f"{name} is not canonical uint64")
    if int(value) > MAX_TICKS:
        raise RequestError("invalid_" + name, f"{name} exceeds uint64")
    return value


def parse_socket(value: Any) -> SocketSelector:
    raw = _mapping(value, "socket")
    _exact_keys(raw, {"kind", "value"}, {"kind", "value"}, "socket")
    kind = raw.get("kind")
    socket_value = _plain(raw.get("value"), "socket_value", maximum=4096)
    if kind == "name":
        if "/" in socket_value or len(socket_value) > 128:
            raise RequestError("invalid_socket", "tmux socket name is invalid")
    elif kind == "path":
        if not socket_value.startswith("/") or len(socket_value) < 2:
            raise RequestError("invalid_socket", "tmux socket path must be absolute")
    else:
        raise RequestError("invalid_socket", "tmux socket kind is invalid")
    return SocketSelector(kind, socket_value)


def parse_identity(value: Any) -> ProcessIdentity:
    raw = _mapping(value, "identity")
    _exact_keys(raw, {"machine", "instanceId", "pid", "startTimeTicks"},
                {"machine", "instanceId", "pid", "startTimeTicks"}, "identity")
    return ProcessIdentity(
        canonical_machine(_machine(raw["machine"])),
        _pid(raw["pid"]),
        _ticks(raw["startTimeTicks"]),
    )


def parse_tmux(value: Any) -> TmuxLocation:
    raw = _mapping(value, "tmux")
    _exact_keys(raw, {"session", "windowIndex", "paneId", "socket"},
                {"session", "windowIndex", "paneId"}, "tmux")
    session = _plain(raw["session"], "session", maximum=256)
    window = raw["windowIndex"]
    pane = raw["paneId"]
    if not isinstance(window, str) or _WINDOW_INDEX.fullmatch(window) is None:
        raise RequestError("invalid_window_index", "windowIndex is invalid")
    if not isinstance(pane, str) or _PANE.fullmatch(pane) is None:
        raise RequestError("invalid_pane_id", "paneId is invalid")
    return TmuxLocation(
        session, window, pane,
        parse_socket(raw["socket"]) if "socket" in raw else None,
    )


def parse_target(value: Any) -> Target:
    raw = _mapping(value, "target")
    _exact_keys(raw, {"identity", "tmux", "name"}, {"identity"}, "target")
    identity_raw = _mapping(raw["identity"], "identity")
    identity = parse_identity(identity_raw)
    return Target(
        identity,
        _plain(identity_raw["instanceId"], "instance_id", maximum=512),
        parse_tmux(raw["tmux"]) if "tmux" in raw else None,
        _plain(raw["name"], "target_name", maximum=512)
        if "name" in raw else None,
    )


def parse_window(value: Any) -> Window:
    raw = _mapping(value, "window")
    _exact_keys(raw, {"stableId", "address", "pid", "startTimeTicks", "class", "title"},
                {"stableId", "address", "pid", "startTimeTicks"}, "window")
    stable = _plain(raw["stableId"], "stable_id", maximum=256)
    address = raw["address"]
    if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
        raise RequestError("invalid_address", "window address is invalid")
    window_class = None
    if "class" in raw:
        window_class = _plain(raw["class"], "class", minimum=0, maximum=256)
    title = None
    if "title" in raw:
        title = _plain(raw["title"], "title", minimum=0, maximum=512)
    return Window(stable, address, _pid(raw["pid"]),
                  _ticks(raw["startTimeTicks"]), window_class, title)


def _valid_evidence_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        if len(value) > 512 or _CONTROL.search(value) is not None:
            return False
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            return False
        return True
    if not isinstance(value, Mapping):
        return False
    keys = set(value)
    if keys == {"kind", "value"}:
        try:
            parse_socket(value)
            return True
        except RequestError:
            return False
    if keys != {"protocol", "addressFamily", "local", "remote"}:
        return False
    if value.get("protocol") not in {"tcp", "udp"}:
        return False
    if value.get("addressFamily") not in {"ipv4", "ipv6"}:
        return False
    for side_name in ("local", "remote"):
        side = value.get(side_name)
        if not isinstance(side, Mapping) or set(side) != {"address", "port"}:
            return False
        address, port = side.get("address"), side.get("port")
        if (not isinstance(address, str) or not 2 <= len(address) <= 128
                or _CONTROL.search(address) is not None
                or isinstance(port, bool) or not isinstance(port, int)
                or not 1 <= port <= 65535):
            return False
    return True


def _validate_prior_evidence(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise RequestError("invalid_prior_proof", "prior evidence is invalid")
    for raw_item in value:
        item = _mapping(raw_item, "prior_evidence")
        _exact_keys(item, {"code", "source", "result", "details"},
                    {"code", "source", "result"}, "prior_evidence")
        code = item.get("code")
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            raise RequestError("invalid_prior_proof", "prior evidence code is invalid")
        if item.get("source") not in _EVIDENCE_SOURCES:
            raise RequestError("invalid_prior_proof", "prior evidence source is invalid")
        if item.get("result") not in _EVIDENCE_RESULTS:
            raise RequestError("invalid_prior_proof", "prior evidence result is invalid")
        if "details" in item:
            details = item["details"]
            if not isinstance(details, Mapping) or len(details) > 16:
                raise RequestError("invalid_prior_proof", "prior evidence details are invalid")
            for key, detail in details.items():
                if (not isinstance(key, str) or _DETAIL_KEY.fullmatch(key) is None
                        or not _valid_evidence_value(detail)):
                    raise RequestError(
                        "invalid_prior_proof", "prior evidence details are invalid"
                    )


def _parse_prior(value: Any) -> PriorCandidate:
    raw = _mapping(value, "prior")
    _exact_keys(raw, {"window", "target", "proof"},
                {"window", "target", "proof"}, "prior")
    target_raw = _mapping(raw["target"], "prior_target")
    _exact_keys(target_raw, {"identity", "location"},
                {"identity", "location"}, "prior_target")
    location = _mapping(target_raw["location"], "prior_location")
    kind = location.get("kind")
    identity_raw = _mapping(target_raw["identity"], "prior_identity")
    identity = parse_identity(identity_raw)
    instance_id = _plain(identity_raw["instanceId"], "instance_id", maximum=512)
    if kind == "local":
        _exact_keys(location, {"kind"}, {"kind"}, "prior_location")
        target = Target(identity, instance_id)
    elif kind == "tmux":
        _exact_keys(location, {"kind", "tmux"}, {"kind", "tmux"}, "prior_location")
        target = Target(identity, instance_id, parse_tmux(location["tmux"]))
    else:
        raise RequestError("invalid_prior_location", "prior location is invalid")
    proof = _mapping(raw["proof"], "prior_proof")
    _exact_keys(proof, {"state", "relation", "evidence"},
                {"state", "relation", "evidence"}, "prior_proof")
    relation = proof.get("relation")
    if (not isinstance(relation, str)
            or proof.get("state") != "complete"
            or relation not in {"visible_exact", "linked_client"}):
        raise RequestError("invalid_prior_proof", "prior proof is not complete")
    _validate_prior_evidence(proof.get("evidence"))
    return PriorCandidate(parse_window(raw["window"]), target, relation)


def _parse_limits(value: Any) -> Limits:
    raw = _mapping(value, "limits")
    _exact_keys(raw, {"deadlineMs", "maxRequestBytes", "maxStdoutBytes",
                      "maxStderrBytes"}, set(), "limits")
    if not raw:
        raise RequestError("invalid_limits", "limits must not be empty")
    values = {
        "deadline_ms": (raw.get("deadlineMs", 20_000), 1, 20_000),
        "max_request_bytes": (raw.get("maxRequestBytes", 262_144), 1024, 262_144),
        "max_stdout_bytes": (raw.get("maxStdoutBytes", 1_048_576), 4096, 1_048_576),
        "max_stderr_bytes": (raw.get("maxStderrBytes", 16_384), 0, 16_384),
    }
    parsed: dict[str, int] = {}
    for key, (item, low, high) in values.items():
        if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
            raise RequestError("invalid_limits", f"{key} is outside its hard ceiling")
        parsed[key] = item
    return Limits(**parsed)


def parse_request(value: Any) -> Request:
    raw = _mapping(value, "request")
    allowed = {"schema", "requestId", "operation", "requestedRelation",
               "target", "local", "windows", "prior", "limits",
               "probeTransports"}
    required = {"schema", "requestId", "operation", "target", "local", "windows"}
    _exact_keys(raw, allowed, required, "request")
    if raw["schema"] != "agent-window-resolver.request.v1":
        raise RequestError("unsupported_schema", "request schema is unsupported")
    request_id = _plain(raw["requestId"], "request_id", maximum=128)
    operation = raw["operation"]
    if (not isinstance(operation, str)
            or operation not in {"resolve", "revalidate", "verify-target", "match"}):
        raise RequestError("unsupported_operation", "operation is unsupported")
    relation = raw.get("requestedRelation")
    if operation in {"resolve", "revalidate"}:
        if (not isinstance(relation, str)
                or relation not in {"visible_exact", "linked_client"}):
            raise RequestError("invalid_relation", "requestedRelation is required")
    elif "requestedRelation" in raw:
        raise RequestError(
            "invalid_relation",
            f"{operation} forbids requestedRelation",
        )
    local = _mapping(raw["local"], "local")
    _exact_keys(local, {"machine"}, {"machine"}, "local")
    windows_value = raw["windows"]
    if not isinstance(windows_value, list) or len(windows_value) > 4096:
        raise RequestError("invalid_windows", "windows must be a bounded array")
    windows = tuple(parse_window(item) for item in windows_value)
    window_keys = {
        (item.stable_id, item.address, item.pid, item.start_time_ticks)
        for item in windows
    }
    if len(window_keys) != len(windows):
        raise RequestError("duplicate_window_identity",
                           "window identities must be unique")
    prior = _parse_prior(raw["prior"]) if "prior" in raw else None
    if operation in {"resolve", "match"} and prior is not None:
        raise RequestError("invalid_prior", f"{operation} forbids prior")
    if operation == "revalidate" and prior is None:
        raise RequestError("invalid_prior", "revalidate requires prior")
    if operation == "verify-target" and (windows or prior is not None):
        raise RequestError("invalid_verify_target", "verify-target requires empty windows")
    probe_transports = raw.get("probeTransports", False)
    if not isinstance(probe_transports, bool):
        raise RequestError("invalid_probe_transports", "probeTransports must be a boolean")
    if "probeTransports" in raw and operation != "verify-target":
        raise RequestError(
            "invalid_probe_transports", f"{operation} forbids probeTransports",
        )
    return Request(request_id, operation, relation, parse_target(raw["target"]),
                   canonical_machine(_machine(local["machine"], "local_machine")),
                   windows, prior,
                   _parse_limits(raw["limits"]) if "limits" in raw else Limits(),
                   probe_transports)


def socket_json(value: SocketSelector) -> dict[str, str]:
    return {"kind": value.kind, "value": value.value}


def identity_json(value: ProcessIdentity, *, instance_id: str) -> dict[str, Any]:
    return {
        "machine": canonical_machine(value.machine),
        "instanceId": instance_id,
        "pid": value.pid,
        "startTimeTicks": value.start_time_ticks,
    }


def window_json(value: Window) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stableId": value.stable_id,
        "address": value.address,
        "pid": value.pid,
        "startTimeTicks": value.start_time_ticks,
    }
    if value.window_class is not None:
        result["class"] = value.window_class
    if value.title is not None:
        result["title"] = value.title
    return result
