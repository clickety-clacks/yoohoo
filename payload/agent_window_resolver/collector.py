"""Injected observation boundary for resolver collection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
import shlex
import time
from typing import Callable, Literal, Mapping, Protocol, Sequence

from .model import ProcessIdentity, Request, SocketSelector, Window


def _unwrap_tmux_command(
    command: Sequence[str], *, allow_flattened_shell: bool = False,
) -> list[str] | None:
    """Unwrap bounded shell forms without interpreting or executing them."""
    values = list(command)
    # SSH commonly supplies one quoted shell command; mosh may retain its
    # already-tokenized form. Unwrap either representation without executing it.
    for _ in range(8):
        if values and values[0] == "exec":
            values.pop(0)
        if values[:2] == ["sh", "-lc"] and len(values) == 3:
            wrapped = values[2]
        elif values[:2] == ["sh", "-lc"]:
            # A few launchers retain the shell wrapper as separate argv
            # fields (`sh -lc exec tmux ...`) rather than one command string.
            if not allow_flattened_shell:
                return None
            values = values[2:]
            continue
        elif len(values) == 1:
            wrapped = values[0]
        else:
            return values
        try:
            unpacked = shlex.split(wrapped)
        except ValueError:
            return None
        if unpacked == values:
            return values
        values = unpacked
    return None


@dataclass(frozen=True)
class TmuxTarget:
    """Structured, bounded subset of tmux's target-pane grammar."""

    session: str | None
    session_kind: Literal["name", "id"] | None = None
    session_exact: bool = False
    window: str | None = None
    window_kind: Literal["name", "id", "index", "relative"] | None = None
    pane: str | None = None
    pane_kind: Literal["name", "id", "index", "relative"] | None = None
    context_relative: bool = False


@dataclass(frozen=True)
class TmuxCommand:
    target: TmuxTarget
    socket: SocketSelector | None
    operation: Literal["attach", "new"]


def _relative_tmux_component(value: str) -> bool:
    return value in {"!", "+", "-"} or (
        len(value) > 1 and value[0] in "+-" and value[1:].isdigit()
    )


def _tmux_window_kind(
    value: str,
) -> Literal["name", "id", "index", "relative"] | None:
    if value.isdigit() and (value == "0" or not value.startswith("0")):
        return "index"
    if value.startswith("@"):
        return "id" if value[1:].isdigit() else None
    if _relative_tmux_component(value):
        return "relative"
    return "name"


def _tmux_pane_kind(
    value: str,
) -> Literal["name", "id", "index", "relative"] | None:
    if value.isdigit() and (value == "0" or not value.startswith("0")):
        return "index"
    if value.startswith("%"):
        return "id" if value[1:].isdigit() else None
    if _relative_tmux_component(value):
        return "relative"
    return "name"


def _qualify_tmux_target(value: str) -> TmuxTarget | None:
    """Parse explicit target-pane fields without inventing context."""
    if not value:
        return None
    exact_session = value.startswith("=")
    raw = value[1:] if exact_session else value
    if not raw:
        return None
    context_relative = False
    if ":" in raw:
        session, remainder = raw.split(":", 1)
        if not session:
            session = None
            session_kind = None
            context_relative = True
        else:
            session_kind = (
                "id" if session.startswith("$") and session[1:].isdigit()
                else "name"
            )
            if session.startswith("$") and session_kind != "id":
                return None
    elif "." in raw and not exact_session:
        # Without a session separator this is resolved against tmux's current
        # context. Preserve the visible window/pane fields, but never relabel
        # the window as a session.
        session = None
        session_kind = None
        remainder = raw
        context_relative = True
    elif raw.startswith("@"):
        kind = _tmux_window_kind(raw)
        return (
            TmuxTarget(
                None, window=raw, window_kind=kind, context_relative=True,
            )
            if kind == "id" else None
        )
    elif raw.startswith("%"):
        kind = _tmux_pane_kind(raw)
        return (
            TmuxTarget(None, pane=raw, pane_kind=kind, context_relative=True)
            if kind == "id" else None
        )
    elif _relative_tmux_component(raw):
        return TmuxTarget(None, context_relative=True)
    else:
        session_kind: Literal["name", "id"] = (
            "id" if raw.startswith("$") and raw[1:].isdigit() else "name"
        )
        if raw.startswith("$") and session_kind != "id":
            return None
        return TmuxTarget(raw, session_kind, exact_session)
    if not remainder:
        return TmuxTarget(
            session, session_kind, exact_session, context_relative=True,
        )
    if remainder.startswith(":") or remainder.count(".") > 1:
        return None
    if "." in remainder:
        window, pane = remainder.split(".", 1)
        if not pane:
            return None
    else:
        window, pane = remainder, None
    if window:
        window_kind = _tmux_window_kind(window)
        if window_kind is None:
            return None
        context_relative = context_relative or window_kind == "relative"
    else:
        window = None
        window_kind = None
        context_relative = True
    pane_kind: Literal["name", "id", "index", "relative"] | None = None
    if pane is not None:
        pane_kind = _tmux_pane_kind(pane)
        if pane_kind is None:
            return None
        context_relative = context_relative or pane_kind == "relative"
    return TmuxTarget(
        session, session_kind, exact_session, window, window_kind, pane,
        pane_kind, context_relative,
    )


def _parse_tmux_tokens_structured(
    command: Sequence[str], *, allow_spaced_display_session: bool = False,
) -> TmuxCommand | None:
    """Parse the small tmux command grammar used for transport hints.

    Intact argv is deliberately exact. The mosh display string historically
    prints an unquoted multiword session description as separate argv words,
    so that one recovery path may join only that bounded suffix.
    """
    values = list(command)
    if not values or PurePath(values.pop(0)).name != "tmux":
        return None
    selector = None
    if values and values[0] in {"-L", "-S"}:
        if len(values) < 2 or not values[1]:
            return None
        flag, value = values.pop(0), values.pop(0)
        selector = SocketSelector("name" if flag == "-L" else "path", value)
    if not values:
        return None
    operation = values.pop(0)
    if operation not in {"attach", "attach-session", "new", "new-session"}:
        return None
    if operation.startswith("attach"):
        if not values or values.pop(0) != "-t":
            return None
    else:
        # Preserve the existing `new[-session] -s name` form and add tmux's
        # common “attach if present” form, including its -As spelling.
        if not values:
            return None
        option = values.pop(0)
        if option == "-A":
            if not values or values.pop(0) != "-s":
                return None
        elif option not in {"-As", "-s"}:
            return None
    if not values or values[0].startswith("-"):
        return None
    if len(values) == 1:
        target_value = values[0]
    elif allow_spaced_display_session:
        # Mosh display strings flatten ordinary multiword names into separate
        # words. Keep this recovery scoped to display text: option-like words,
        # shell operators, and redirections cannot become session names.
        shell_tokens = {";", "&", "&&", "|", "||", "<", ">", ">>", "2>", "2>>"}
        if any(
            value.startswith("-") or value in shell_tokens
            or any(operator in value for operator in (";", "&", "|", "<", ">"))
            for value in values
        ):
            return None
        target_value = " ".join(values)
    else:
        return None
    if operation.startswith("attach"):
        target = _qualify_tmux_target(target_value)
    else:
        # `new[-session] -s` accepts a literal session name. Colons, periods,
        # a leading equals sign, and spaces are not attach-target syntax here.
        target = TmuxTarget(target_value, "name") if target_value else None
    if target is None:
        return None
    return TmuxCommand(
        target, selector, "attach" if operation.startswith("attach") else "new"
    )


def _parse_tmux_tokens(
    command: Sequence[str], *, allow_spaced_display_session: bool = False,
) -> tuple[str, SocketSelector | None] | None:
    parsed = _parse_tmux_tokens_structured(
        command, allow_spaced_display_session=allow_spaced_display_session,
    )
    if parsed is None or parsed.target.session is None:
        return None
    return parsed.target.session, parsed.socket


def _split_tmux_command(
    command: Sequence[str], *, allow_spaced_display_session: bool = False,
) -> tuple[str, SocketSelector | None] | None:
    values = _unwrap_tmux_command(
        command, allow_flattened_shell=allow_spaced_display_session,
    )
    if values is None:
        return None
    return _parse_tmux_tokens(
        values, allow_spaced_display_session=allow_spaced_display_session,
    )


def parse_tmux_command(
    command: Sequence[str], *, allow_spaced_display_session: bool = False,
) -> TmuxCommand | None:
    values = _unwrap_tmux_command(
        command, allow_flattened_shell=allow_spaced_display_session,
    )
    if values is None:
        return None
    return _parse_tmux_tokens_structured(
        values, allow_spaced_display_session=allow_spaced_display_session,
    )


def split_tmux_command(
    command: Sequence[str],
) -> tuple[str, SocketSelector | None] | None:
    return _split_tmux_command(command)


_TRANSPORTS = {"ssh", "mosh", "et"}
# Eternal Terminal client options that consume the following argument. et
# places the remote command in ``-c`` rather than positionally after the host.
_ET_VALUE_OPTIONS = {
    "-u", "--username", "-p", "--port", "-c", "--command", "--terminal-path",
    "-t", "--tunnel", "-r", "--reversetunnel", "--jumphost", "--jport",
    "--jserverfifo", "-v", "--verbose", "-k", "--keepalive", "-l", "--logdir",
    "--ssh-socket", "--serverfifo", "--ssh-option",
}
_ET_FLAGS = {
    "-e", "--noexit", "-x", "--kill-other-sessions", "--macserver",
    "--logtostdout", "--silent", "-N", "--no-terminal", "-f",
    "--forward-ssh-agent", "--telemetry",
}


def _et_host(value: str) -> str | None:
    """Strip et's optional ``:port`` suffix; a bare IPv6 address has none."""
    if not value or value.startswith("-"):
        return None
    if value.startswith("["):
        host, separator, port = value[1:].partition("]")
        if not separator or (port and not (port[:1] == ":" and port[1:].isdigit())):
            return None
        return host or None
    if value.count(":") == 1:
        host, port = value.split(":")
        return host if host and port.isdigit() else None
    return value


def _et_command_hint(
    command: Sequence[str],
) -> tuple[str, str, TmuxCommand] | None:
    """Parse ``et [options] [--] host`` with its tmux command in ``-c``."""
    values = list(command)
    remote: str | None = None
    hosts: list[str] = []
    while values:
        value = values.pop(0)
        if value == "--":
            hosts.extend(values)
            break
        name, equals, inline = value.partition("=")
        if value.startswith("--") and equals and name in _ET_VALUE_OPTIONS:
            if name == "--command":
                remote = inline
            continue
        if value.startswith("--") and equals and name in _ET_FLAGS:
            continue
        if value in _ET_VALUE_OPTIONS:
            if not values:
                return None
            argument = values.pop(0)
            if value in {"-c", "--command"}:
                remote = argument
            continue
        if value in _ET_FLAGS:
            continue
        if value.startswith("-c") and len(value) > 2:
            remote = value[2:]
            continue
        if value.startswith("-"):
            return None
        hosts.append(value)
    if remote is None or len(hosts) != 1:
        return None
    host = _et_host(hosts[0])
    if host is None:
        return None
    parsed = parse_tmux_command([remote])
    if parsed is None:
        return None
    return "et", host, parsed


def transport_command_hint(
    argv: Sequence[str],
) -> tuple[str, str, TmuxCommand] | None:
    values = list(argv)
    # mosh-client retains the display command in its ``-#`` argument.  It is
    # a useful host/session hint even though it is not transport proof.  Keep
    # parsing bounded and only accept the same tmux command grammar below.
    if values and PurePath(values[0]).name == "mosh-client":
        display: list[str] = []
        for index, value in enumerate(values[1:]):
            if value == "-#":
                display = values[index + 2:]
                break
            if value.startswith("-#"):
                display = [value[2:].lstrip(), *values[index + 2:]]
                break
        if display:
            try:
                display_text = " ".join(display)
                lexer = shlex.shlex(
                    display_text, posix=True, punctuation_chars=";&|<>",
                )
                lexer.whitespace_split = True
                lexer.commenters = ""
                display_tokens = list(lexer)
                operators = [
                    value for value in display_tokens
                    if value and all(character in ";&|<>" for character in value)
                ]
                parsed_display = (
                    display_tokens[:display_tokens.index("|")]
                    if operators == ["|"] else []
                )
            except ValueError:
                parsed_display = []
            if "--" in parsed_display:
                marker = parsed_display.index("--")
                command = parsed_display[marker + 1:]
                if command:
                    host = command.pop(0)
                    parsed = parse_tmux_command(command)
                    if parsed is None:
                        # Some mosh builds flatten a session description
                        # before placing it in ``-#``. Reuse the same grammar,
                        # allowing only that bounded display form.
                        parsed = parse_tmux_command(
                            command, allow_spaced_display_session=True,
                        )
                    if parsed is not None and host and not host.startswith("-"):
                        return "mosh", host, parsed
    start = 0 if values and PurePath(values[0]).name in _TRANSPORTS else None
    if start is None:
        for index, value in enumerate(values[:-1]):
            if value == "-e" and PurePath(values[index + 1]).name in _TRANSPORTS:
                start = index + 1
                break
    if start is None:
        return None
    command = values[start:]
    kind = PurePath(command.pop(0)).name
    if kind == "et":
        return _et_command_hint(command)
    options = {
        "ssh": {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J",
                "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"},
        "mosh": {"-p", "--port", "--ssh"},
    }[kind]
    flags = {"-4", "-6", "-A", "-a", "-C", "-f", "-g", "-K", "-k", "-M", "-N",
             "-n", "-q", "-s", "-T", "-t", "-V", "-v", "-X", "-x", "-Y", "-y"}
    while command and command[0] != "--" and command[0].startswith("-"):
        option = command.pop(0)
        if option in options:
            if not command:
                return None
            command.pop(0)
        elif any(option.startswith(prefix + "=") for prefix in options):
            continue
        elif option in flags or (
            kind == "ssh" and len(option) > 2
            and all("-" + flag in flags for flag in option[1:])
        ):
            continue
        else:
            return None
    if command and command[0] == "--":
        command.pop(0)
    if not command or command[0].startswith("-"):
        return None
    host = command.pop(0)
    parsed = parse_tmux_command(command)
    if parsed is None:
        return None
    return kind, host, parsed


def transport_hint(
    argv: Sequence[str],
) -> tuple[str, str, str, SocketSelector | None] | None:
    """Compatibility view for callers that only consume bare sessions."""
    parsed = transport_command_hint(argv)
    if parsed is None or parsed[2].target.session is None:
        return None
    kind, host, command = parsed
    return kind, host, command.target.session, command.socket


def transport_socket_eligible(argv: Sequence[str]) -> bool:
    executable = PurePath(argv[0]).name if argv else ""
    hint = transport_hint(argv)
    return (
        executable == "ssh"
        or (hint is not None and hint[0] == "ssh")
        or executable == "mosh-client"
    )

CollectionState = Literal["complete", "partial", "unreachable"]


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    address_family: str
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int


@dataclass(frozen=True)
class ProcessNode:
    identity: ProcessIdentity
    parent: ProcessIdentity | None
    argv: tuple[str, ...] = ()
    endpoints: tuple[Endpoint, ...] = ()
    ssh_endpoints: tuple[Endpoint, ...] = ()
    tty: str = ""


@dataclass(frozen=True)
class TmuxPane:
    session: str
    window_index: str
    pane_id: str
    process: ProcessIdentity


@dataclass(frozen=True)
class TmuxClient:
    name: str
    process: ProcessIdentity
    current_session: str
    current_window_index: str
    current_pane_id: str
    processes: tuple[ProcessNode, ...] = ()


@dataclass(frozen=True)
class ObservationError:
    code: str
    source: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class WindowObservation:
    window: Window
    processes: tuple[ProcessNode, ...]
    collection_state: CollectionState = "complete"
    errors: tuple[ObservationError, ...] = ()


@dataclass(frozen=True)
class TargetObservation:
    machine: str
    socket: SocketSelector | None
    actual_socket_path: str | None
    processes: tuple[ProcessNode, ...]
    pane: TmuxPane | None
    clients: tuple[TmuxClient, ...]
    collection_state: CollectionState = "complete"
    errors: tuple[ObservationError, ...] = ()
    # A producer may deliberately stop target ancestry at the separately
    # verified tmux pane process.  This is never inferred from a missing
    # parent; it must be explicitly bound by the producer and parser.
    target_boundary: ProcessIdentity | None = None
    target_chain_complete: bool = True


@dataclass(frozen=True)
class TopologySnapshot:
    windows: tuple[WindowObservation, ...]
    target: TargetObservation


class Deadline:
    def __init__(self, timeout_ms: int, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._end = now() + timeout_ms / 1000.0

    def remaining(self) -> float:
        return max(0.0, self._end - self._now())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


class Collector(Protocol):
    def collect(self, request: Request, deadline: Deadline) -> TopologySnapshot:
        ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""
    timed_out: bool = False
    truncated: bool = False


class ProbeIO(Protocol):
    def read_bytes(self, path: str, max_bytes: int) -> bytes:
        ...

    def readlink(self, path: str, max_chars: int) -> str:
        ...

    def listdir(self, path: str, max_entries: int) -> tuple[str, ...]:
        ...

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_stdout: int,
        max_stderr: int,
        env: Mapping[str, str],
    ) -> CommandResult:
        ...

    def monotonic(self) -> float:
        ...


class StaticCollector:
    """Return raw normalized observations to the real deterministic matcher."""

    def __init__(
        self, snapshot: TopologySnapshot,
        transports: Mapping[str, object] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.transports = transports
        self.calls: list[Request] = []

    def collect(self, request: Request, deadline: Deadline) -> TopologySnapshot:
        self.calls.append(request)
        return self.snapshot

    def observe_transports(
        self, request: Request, deadline: Deadline
    ) -> Mapping[str, object] | None:
        return self.transports
