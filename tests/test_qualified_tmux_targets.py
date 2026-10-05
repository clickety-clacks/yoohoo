"""Pure qualified tmux target regressions; performs no I/O or execution."""
from __future__ import annotations

import shlex
from pathlib import Path
import sys
import unittest

# The same regression file is vendored into Yoohoo's release bundle, where
# tests must exercise payload/agent_window_resolver rather than this checkout.
_PAYLOAD_ROOT = Path(__file__).resolve().parents[1] / "payload"
if (_PAYLOAD_ROOT / "agent_window_resolver").is_dir():
    sys.path.insert(0, str(_PAYLOAD_ROOT))

from agent_window_resolver import (
    Limits,
    ObservationError,
    ProcessIdentity,
    ProcessNode,
    Request,
    Resolver,
    SocketSelector,
    StaticCollector,
    Target,
    TargetObservation,
    TmuxLocation,
    TopologySnapshot,
    Window,
    WindowObservation,
)
from agent_window_resolver.collector import (
    parse_tmux_command,
    split_tmux_command,
    transport_command_hint,
    transport_hint,
)


LOCAL = "lumen"
REMOTE = "atlas"
SESSION = "review agent (roundtrip)"
WINDOW = Window("window-1", "0xabc", 100, "20", title="terminal")


def _identity(machine: str, pid: int, ticks: str) -> ProcessIdentity:
    return ProcessIdentity(machine, pid, ticks)


def _response(
    argv: tuple[str, ...],
    *,
    tmux: TmuxLocation | None = None,
    title: str = "terminal",
    root_argv: tuple[str, ...] = (),
) -> dict:
    location = tmux if tmux is not None else TmuxLocation(
        SESSION, "3", "%71", SocketSelector("name", "agents")
    )
    target = Target(
        _identity(REMOTE, 800, "80"), "fixture", location, SESSION
    )
    window = Window(
        WINDOW.stable_id, WINDOW.address, WINDOW.pid,
        WINDOW.start_time_ticks, title=title,
    )
    root = ProcessNode(_identity(LOCAL, 100, "20"), None, root_argv)
    child = ProcessNode(_identity(LOCAL, 101, "21"), root.identity, argv)
    request = Request(
        "qualified-target", "match", None, target, LOCAL, (window,), None,
        Limits(),
    )
    snapshot = TopologySnapshot(
        (WindowObservation(window, (root, child)),),
        TargetObservation(
            REMOTE, None, None, (), None, (), "unreachable",
            (ObservationError(
                "remote_unreachable", "transport", "offline", True,
            ),),
        ),
    )
    return Resolver().resolve(request, StaticCollector(snapshot))


def _codes(response: dict) -> list[str]:
    return [
        item["code"]
        for candidate in response["candidates"]
        for item in candidate["match"]["evidence"]
    ]


class QualifiedTargetParserTests(unittest.TestCase):
    def test_exact_attach_target_retains_every_selector(self) -> None:
        parsed = parse_tmux_command((
            "tmux", "-L", "agents", "attach-session", "-t",
            "=review agent (roundtrip):3.%71",
        ))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.operation, "attach")
        self.assertEqual(parsed.socket, SocketSelector("name", "agents"))
        self.assertEqual(parsed.target.session, SESSION)
        self.assertEqual(parsed.target.session_kind, "name")
        self.assertTrue(parsed.target.session_exact)
        self.assertEqual(parsed.target.window, "3")
        self.assertEqual(parsed.target.window_kind, "index")
        self.assertEqual(parsed.target.pane, "%71")
        self.assertEqual(parsed.target.pane_kind, "id")
        self.assertFalse(parsed.target.context_relative)
        self.assertEqual(
            split_tmux_command((
                "tmux", "-L", "agents", "attach-session", "-t",
                "=review agent (roundtrip):3.%71",
            )),
            (SESSION, SocketSelector("name", "agents")),
        )

    def test_id_index_and_name_kinds_are_distinct(self) -> None:
        parsed = parse_tmux_command((
            "tmux", "attach", "-t", "$4:@8.12",
        ))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            (parsed.target.session_kind, parsed.target.window_kind,
             parsed.target.pane_kind),
            ("id", "id", "index"),
        )
        named = parse_tmux_command((
            "tmux", "attach", "-t", "session:editor.logs",
        ))
        self.assertIsNotNone(named)
        assert named is not None
        self.assertEqual(
            (named.target.window_kind, named.target.pane_kind),
            ("name", "name"),
        )

    def test_new_session_name_is_literal_not_attach_grammar(self) -> None:
        literal = "=review:3.%71"
        for options in (("-s",), ("-As",), ("-A", "-s")):
            with self.subTest(options=options):
                parsed = parse_tmux_command((
                    "tmux", "new-session", *options, literal,
                ))
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.operation, "new")
                self.assertEqual(parsed.target.session, literal)
                self.assertEqual(parsed.target.session_kind, "name")
                self.assertFalse(parsed.target.session_exact)
                self.assertIsNone(parsed.target.window)
                self.assertIsNone(parsed.target.pane)

    def test_context_relative_target_does_not_fabricate_session(self) -> None:
        cases = {
            "session:": ("session", None, None, None, None),
            "session:.%71": ("session", None, None, "%71", "id"),
            ":3.%71": (None, "3", "index", "%71", "id"),
            "window.%71": (None, "window", "name", "%71", "id"),
            "@8": (None, "@8", "id", None, None),
            "%71": (None, None, None, "%71", "id"),
            "+1": (None, None, None, None, None),
            "!": (None, None, None, None, None),
        }
        for target, fields in cases.items():
            with self.subTest(target=target):
                command = ("tmux", "attach-session", "-t", target)
                parsed = parse_tmux_command(command)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.target.session, fields[0])
                self.assertTrue(parsed.target.context_relative)
                self.assertEqual(
                    (parsed.target.window, parsed.target.window_kind,
                     parsed.target.pane, parsed.target.pane_kind),
                    fields[1:],
                )
                legacy = split_tmux_command(command)
                if fields[0] is None:
                    self.assertIsNone(legacy)
                else:
                    self.assertEqual(legacy, (fields[0], None))

    def test_malformed_or_unsupported_attach_targets_are_rejected(self) -> None:
        for target in (
            "", "=", "session::3", "session:3.", "session:3.%71.extra",
            "session:@bad.%71", "session:3.%bad", "@bad", "%bad",
        ):
            with self.subTest(target=target):
                self.assertIsNone(parse_tmux_command((
                    "tmux", "attach-session", "-t", target,
                )))

    def test_ssh_and_mosh_keep_qualified_targets_without_execution(self) -> None:
        remote = (
            "exec tmux -S /tmp/agents.sock attach-session "
            "-t " + shlex.quote("=" + SESSION + ":3.%71")
        )
        ssh = transport_command_hint((
            "ssh", REMOTE, "sh -lc " + shlex.quote(remote),
        ))
        self.assertIsNotNone(ssh)
        assert ssh is not None
        self.assertEqual(ssh[:2], ("ssh", REMOTE))
        self.assertEqual(ssh[2].target.session, SESSION)
        self.assertEqual(ssh[2].target.window, "3")
        self.assertEqual(ssh[2].target.pane, "%71")
        self.assertEqual(
            ssh[2].socket, SocketSelector("path", "/tmp/agents.sock")
        )
        self.assertEqual(
            transport_hint((
                "ssh", REMOTE, "sh -lc " + shlex.quote(remote),
            )),
            ("ssh", REMOTE, SESSION,
             SocketSelector("path", "/tmp/agents.sock")),
        )

        mosh = transport_command_hint((
            "mosh-client",
            "-# -- atlas tmux -L agents attach-session -t "
            f"={SESSION}:3.%71 |",
            "192.0.2.4", "60001",
        ))
        self.assertIsNotNone(mosh)
        assert mosh is not None
        self.assertEqual(mosh[:2], ("mosh", REMOTE))
        self.assertEqual(mosh[2].target.session, SESSION)
        self.assertEqual(mosh[2].target.window, "3")
        self.assertEqual(mosh[2].target.pane, "%71")


class QualifiedTargetMatchTests(unittest.TestCase):
    def test_exact_qualified_ssh_and_mosh_targets_match(self) -> None:
        commands = (
            (
                "ssh", REMOTE, "tmux -L agents attach-session -t "
                + shlex.quote("=" + SESSION + ":3.%71"),
            ),
            (
                "mosh-client", "-# -- atlas tmux -L agents attach-session "
                f"-t ={SESSION}:3.%71 |", "192.0.2.4", "60001",
            ),
        )
        for argv in commands:
            with self.subTest(argv=argv[0]):
                response = _response(argv)
                self.assertEqual(response["status"], "matched")
                self.assertIn("transport_host_session_hint", _codes(response))
                evidence = response["candidates"][0]["match"]["evidence"]
                details = next(
                    item["details"] for item in evidence
                    if item["code"] == "transport_host_session_hint"
                )
                self.assertEqual(details["windowKind"], "index")
                self.assertEqual(details["paneKind"], "id")

    def test_bare_target_stays_best_effort(self) -> None:
        response = _response((
            "ssh", REMOTE, "tmux attach-session -t " + shlex.quote(SESSION),
        ))
        self.assertEqual(response["status"], "matched")
        self.assertIn("transport_host_session_hint", _codes(response))

    def test_exact_session_marker_is_case_sensitive(self) -> None:
        differently_cased = "Review agent (roundtrip)"
        response = _response((
            "ssh", REMOTE, "tmux -L agents attach-session -t "
            + shlex.quote("=" + differently_cased + ":3.%71"),
        ))
        self.assertEqual(response["status"], "unresolved")
        self.assertNotIn("transport_host_session_hint", _codes(response))

    def test_comparable_qualifier_conflicts_do_not_support_location(self) -> None:
        cases = (
            ("=" + SESSION + ":4.%71", SocketSelector("name", "agents")),
            ("=" + SESSION + ":3.%72", SocketSelector("name", "agents")),
            ("=" + SESSION + ":.%72", SocketSelector("name", "agents")),
            ("=" + SESSION + ":3.%71", SocketSelector("name", "other")),
        )
        for target, socket in cases:
            flag = "-L" if socket.kind == "name" else "-S"
            argv = (
                "ssh", REMOTE,
                f"tmux {flag} {shlex.quote(socket.value)} attach-session -t "
                + shlex.quote(target),
            )
            with self.subTest(target=target, socket=socket):
                response = _response(argv)
                self.assertEqual(response["status"], "unresolved")
                self.assertNotIn("transport_host_session_hint", _codes(response))

    def test_conflict_does_not_reenter_through_raw_agent_name(self) -> None:
        argv = (
            "ssh", REMOTE, "tmux -L agents attach-session -t "
            + shlex.quote("=" + SESSION + ":4.%71"),
        )
        root_argv = (
            "ghostty", "-e", "sh", "-lc",
            "fallback script mentions " + SESSION,
        )
        response = _response(argv, root_argv=root_argv)
        self.assertEqual(response["status"], "unresolved")
        self.assertNotIn("process_agent_name_hint", _codes(response))

        current = _response(argv, title=SESSION, root_argv=root_argv)
        self.assertEqual(current["status"], "matched")
        self.assertIn("window_title_target_name", _codes(current))
        self.assertNotIn("transport_host_session_hint", _codes(current))
        self.assertNotIn("process_agent_name_hint", _codes(current))

    def test_incomparable_selector_kinds_do_not_invent_conflict(self) -> None:
        commands = (
            "=" + SESSION + ":@8.%71",
            "=" + SESSION + ":3.71",
        )
        for target in commands:
            with self.subTest(target=target):
                response = _response((
                    "ssh", REMOTE, "tmux -L agents attach-session -t "
                    + shlex.quote(target),
                ))
                self.assertEqual(response["status"], "matched")
                self.assertIn("transport_host_session_hint", _codes(response))
                uncertainty = response["candidates"][0]["match"]["uncertainty"]
                self.assertIn(
                    "tmux_target_selector_incomparable",
                    [item["code"] for item in uncertainty],
                )

    def test_different_socket_selector_kinds_are_incomparable(self) -> None:
        response = _response((
            "ssh", REMOTE, "tmux -S /tmp/agents.sock attach-session -t "
            + shlex.quote("=" + SESSION + ":3.%71"),
        ))
        self.assertEqual(response["status"], "matched")
        self.assertIn("transport_host_session_hint", _codes(response))
        uncertainty = response["candidates"][0]["match"]["uncertainty"]
        self.assertIn(
            "tmux_target_selector_incomparable",
            [item["code"] for item in uncertainty],
        )

    def test_tmux_launch_does_not_match_target_without_tmux_location(self) -> None:
        target = Target(_identity(REMOTE, 800, "80"), "fixture", None, "other")
        window = WINDOW
        root = ProcessNode(_identity(LOCAL, 100, "20"), None)
        child = ProcessNode(
            _identity(LOCAL, 101, "21"), root.identity,
            ("ssh", REMOTE, "tmux attach-session -t unrelated"),
        )
        request = Request(
            "non-tmux", "match", None, target, LOCAL, (window,), None, Limits(),
        )
        snapshot = TopologySnapshot(
            (WindowObservation(window, (root, child)),),
            TargetObservation(
                REMOTE, None, None, (), None, (), "unreachable",
                (ObservationError(
                    "remote_unreachable", "transport", "offline", True,
                ),),
            ),
        )
        response = Resolver().resolve(request, StaticCollector(snapshot))
        self.assertEqual(response["status"], "unresolved")
        self.assertEqual(response["candidates"], [])


if __name__ == "__main__":
    unittest.main()
