"""Pure tmux launch-hint regressions; no Linux, tmux, or network access."""
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
from agent_window_resolver.collector import split_tmux_command, transport_hint


LOCAL = "lumen"


def _identity(machine: str, pid: int, ticks: str) -> ProcessIdentity:
    return ProcessIdentity(machine, pid, ticks)


def _unreachable_target() -> TargetObservation:
    return TargetObservation(
        "atlas", None, None, (), None, (), "unreachable",
        (ObservationError("remote_unreachable", "transport", "offline", True),),
    )


class TmuxLaunchHintTests(unittest.TestCase):
    def test_actual_mosh_client_new_session_display(self) -> None:
        argv = (
            "mosh-client",
            "-# -- atlas tmux new-session -A -s pimcamp |",
            "192.0.2.87",
            "60022",
        )
        self.assertEqual(
            transport_hint(argv), ("mosh", "atlas", "pimcamp", None)
        )

    def test_ghostty_mosh_new_session_argv(self) -> None:
        argv = (
            "/usr/bin/ghostty", "-e", "mosh", "--", "atlas", "tmux",
            "new-session", "-A", "-s", "pimcamp",
        )
        self.assertEqual(
            transport_hint(argv), ("mosh", "atlas", "pimcamp", None)
        )

    def test_plain_ssh_quoted_remote_shell(self) -> None:
        remote = "exec tmux new-session -A -s pimcamp"
        argv = ("ssh", "atlas", "sh -lc " + shlex.quote(remote))
        self.assertEqual(
            transport_hint(argv), ("ssh", "atlas", "pimcamp", None)
        )

    def test_spaced_mosh_display_session_recovery(self) -> None:
        for session in (
            "0_1_9 (patrol)", "stalls and churn", "Product Owner",
            "tmux migration", "always true", "echo review",
        ):
            argv = (
                "mosh-client",
                f"-# -- atlas tmux new-session -A -s {session} |",
                "192.0.2.87",
                "60022",
            )
            with self.subTest(session=session):
                self.assertEqual(
                    transport_hint(argv), ("mosh", "atlas", session, None)
                )

    def test_intact_punctuation_session_names_remain_literal(self) -> None:
        for session in ("R&D", "foo;bar"):
            with self.subTest(session=session, form="direct"):
                self.assertEqual(
                    split_tmux_command((
                        "tmux", "new-session", "-A", "-s", session,
                    )),
                    (session, None),
                )
            remote = f"exec tmux new-session -A -s {shlex.quote(session)}"
            with self.subTest(session=session, form="quoted-ssh"):
                self.assertEqual(
                    transport_hint(("ssh", "atlas", "sh -lc " + shlex.quote(remote))),
                    ("ssh", "atlas", session, None),
                )

    def test_flattened_mosh_shell_wrapper_uses_shared_parser(self) -> None:
        argv = (
            "mosh-client",
            "-# -- atlas sh -lc exec tmux new-session -A -s pimcamp |",
            "192.0.2.87",
            "60022",
        )
        self.assertEqual(
            transport_hint(argv), ("mosh", "atlas", "pimcamp", None)
        )

    def test_attach_forms_and_socket_selectors_remain_supported(self) -> None:
        self.assertEqual(
            split_tmux_command(("tmux", "attach", "-t", "pimcamp")),
            ("pimcamp", None),
        )
        self.assertEqual(
            split_tmux_command(("tmux", "attach-session", "-t", "=pimcamp")),
            ("pimcamp", None),
        )
        self.assertEqual(
            split_tmux_command(
                ("tmux", "-L", "agents", "new-session", "-A", "-s", "pimcamp")
            ),
            ("pimcamp", SocketSelector("name", "agents")),
        )
        self.assertEqual(
            split_tmux_command(
                ("tmux", "-S", "/tmp/tmux.sock", "new", "-As", "pimcamp")
            ),
            ("pimcamp", SocketSelector("path", "/tmp/tmux.sock")),
        )

    def test_malformed_intact_argv_is_not_a_session_hint(self) -> None:
        malformed = (
            ("tmux", "new-session", "-A", "pimcamp"),
            ("tmux", "new-session", "-A", "-s"),
            ("tmux", "new-session", "-A", "-s", "pimcamp", "echo", "ready"),
            ("tmux", "new-session", "-d", "-s", "pimcamp"),
        )
        for argv in malformed:
            with self.subTest(argv=argv):
                self.assertIsNone(split_tmux_command(argv))
        self.assertIsNone(transport_hint((
            "ghostty", "-e", "mosh", "--", "atlas", "tmux",
            "new-session", "-A", "-s", "pimcamp", "echo", "ready",
        )))
        self.assertIsNone(transport_hint((
            "ssh", "atlas", "sh", "-lc", "tmux", "attach", "-t", "pimcamp",
        )))
        self.assertIsNone(transport_hint((
            "mosh-client",
            "-# -- atlas tmux new-session -A -s pimcamp -- echo ready |",
            "192.0.2.87", "60022",
        )))
        for display in (
            "-# -- atlas tmux new-session -A -s pimcamp; echo ready |",
            "-# -- atlas tmux new-session -A -s pimcamp& echo ready |",
            "-# -- atlas tmux new-session -A -s pimcamp | echo ready |",
            "-# -- atlas tmux new-session -A -s pimcamp;true |",
            "-# -- atlas tmux new-session -A -s pimcamp&true |",
            "-# -- atlas tmux new-session -A -s pimcamp>file |",
        ):
            with self.subTest(display=display):
                self.assertIsNone(transport_hint((
                    "mosh-client", display, "192.0.2.87", "60022",
                )))

    def test_et_puts_the_remote_command_in_dash_c(self) -> None:
        remote = "exec tmux attach-session -t =pimcamp"
        for argv in (
            ("et", "-c", "sh -lc " + shlex.quote(remote), "--", "atlas"),
            ("/usr/bin/ghostty", "-e", "et", "-p", "2022", "-c",
             "tmux attach -t =pimcamp", "atlas"),
            ("et", "atlas:2022", "--command=tmux new-session -A -s pimcamp"),
            ("et", "--serverfifo", "/tmp/etserver.fifo", "-c",
             "tmux attach -t pimcamp", "mike@atlas"),
        ):
            with self.subTest(argv=argv):
                hint = transport_hint(argv)
                self.assertIsNotNone(hint)
                kind, host, session, socket = hint
                self.assertEqual((kind, session, socket), ("et", "pimcamp", None))
                self.assertEqual(host.rsplit("@", 1)[-1], "atlas")

    def test_et_socket_selector_and_ipv6_host(self) -> None:
        self.assertEqual(
            transport_hint(("et", "-c", "tmux -L work attach -t =a", "[fd7a::1]:2022")),
            ("et", "fd7a::1", "a", SocketSelector("name", "work")),
        )

    def test_et_without_a_tmux_command_or_single_host_is_not_a_hint(self) -> None:
        for argv in (
            ("et", "atlas"),
            ("et", "-c", "vim notes", "atlas"),
            ("et", "-c", "tmux attach -t a", "atlas", "extra"),
            ("et", "--unknown-option", "-c", "tmux attach -t a", "atlas"),
            ("et", "-c"),
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(transport_hint(argv))

    def test_new_and_attach_hints_rank_both_mosh_windows(self) -> None:
        windows = (
            Window("window-old", "0xabc", 100, "20", title="mosh"),
            Window("window-new", "0xdef", 101, "21", title="mosh"),
        )
        old = ProcessNode(
            _identity(LOCAL, 100, "20"), None,
            ("mosh-client",
             "-# -- atlas tmux new-session -A -s pimcamp |",
             "192.0.2.87", "60022"),
        )
        new = ProcessNode(
            _identity(LOCAL, 101, "21"), None,
            ("mosh-client",
             "-# -- atlas sh -lc exec tmux attach-session -t =pimcamp |",
             "192.0.2.87", "60016"),
        )
        request = Request(
            "tmux-hint", "match", None,
            Target(
                _identity("atlas", 42, "99"), "pimcamp-agent",
                TmuxLocation("pimcamp", "0", "%1"),
            ),
            LOCAL, windows, None, Limits(),
        )
        snapshot = TopologySnapshot(
            (
                WindowObservation(windows[0], (old,)),
                WindowObservation(windows[1], (new,)),
            ),
            _unreachable_target(),
        )
        response = Resolver().resolve(request, StaticCollector(snapshot))
        self.assertEqual(response["status"], "matched")
        self.assertEqual(len(response["candidates"]), 2)
        for candidate in response["candidates"]:
            self.assertTrue(any(
                evidence["code"] == "transport_host_session_hint"
                for evidence in candidate["match"]["evidence"]
            ))


if __name__ == "__main__":
    unittest.main()
