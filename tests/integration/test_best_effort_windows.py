"""Opt-in Plumbus desktop gate: two real windows, no exact transport proof.

The windows have the same discoverable name but contain ordinary sleep
processes. The synthetic remote roster deliberately cannot be verified through
SSH. This tests best-effort title matching and existing-window activation, not
a real mosh connection. Mosh command-line cases live in the shared core tests.
"""
import os
from pathlib import Path
import socket
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bundled_window import (
    _SyntheticHub, agent_identity, clients, hypr, load_daemon,
    process_ticks, wait_for, find_sleep,
)


@unittest.skipUnless(os.environ.get("YOOHOO_RESOLVER_WINDOW_TEST") == "1",
                     "explicit Plumbus desktop opt-in required")
class BestEffortWindowsTests(unittest.TestCase):
    def test_two_named_windows_focus_existing_without_third_terminal(self):
        if socket.gethostname() != "plumbus":
            self.skipTest("Plumbus only")
        daemon = load_daemon("yoohoo_best_effort_desktop")
        previous = hypr("activewindow")
        previous_pid = previous.get("pid")
        try:
            previous_ticks = process_ticks(previous_pid) if previous_pid else None
        except OSError:
            previous_ticks = None
        name = "yoohoo-name-match-" + str(os.getpid())
        terminals = []
        mapped = []
        owned_children = []
        try:
            for _ in range(2):
                terminal = subprocess.Popen([
                    "/usr/bin/ghostty", "--config-default-files=false",
                    "--gtk-single-instance=false", "--confirm-close-surface=false",
                    "--title=" + name, "-e", "/usr/bin/sleep", "25",
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, start_new_session=True)
                terminals.append(terminal)
                window = wait_for(lambda: next(
                    (w for w in clients() if w.get("pid") == terminal.pid), None))
                self.assertIsNotNone(window, "owned window did not appear")
                mapped.append(window)
                child = wait_for(lambda: find_sleep(terminal.pid))
                self.assertIsNotNone(child, "owned sleep child did not appear")
                owned_children.append(child)
            chosen = mapped[-1]
            daemon.focus_window(chosen["address"])
            self.assertTrue(wait_for(lambda:
                hypr("activewindow").get("address") == chosen["address"]))
            # This host identity is deliberately remote relative to Plumbus.
            # No remote process proof is needed to use the matching titles.
            agent = {
                "machine": "127.0.0.1", "instanceId": "owned-title-gate",
                "id": {"pid": terminals[0].pid,
                       "startTimeTicks": process_ticks(terminals[0].pid)},
                "name": name, "presence": {"state": "present"},
                "activity": {"state": "needs_attention"},
                "tmux": {"session": name, "windowIndex": "0", "paneId": "%1"},
            }
            real_popen = subprocess.Popen

            def forbid_new_terminal(*args, **kwargs):
                argv = args[0] if args else kwargs.get("args", ())
                if argv and Path(str(argv[0])).name == "ghostty":
                    raise AssertionError("opened a third terminal")
                if argv and Path(str(argv[0])).name in {"ssh", "mosh", "mosh-client"}:
                    raise AssertionError("existing-window match attempted a remote probe")
                return real_popen(*args, **kwargs)

            with tempfile.TemporaryDirectory(prefix="yoohoo-best-effort-state-") as state, \
                    patch.dict(os.environ, {"XDG_STATE_HOME": state}):
                service = daemon.AttentionService()
                service.hub = _SyntheticHub(agent, "plumbus")
                with patch.object(daemon.subprocess, "Popen", forbid_new_terminal):
                    self.assertTrue(service.open_target(agent_identity(agent)))
                self.assertTrue(service.hub.acknowledged)
            self.assertEqual(hypr("activewindow").get("address"), chosen["address"])
            self.assertEqual(len([w for w in clients()
                                  if w.get("pid") in {p.pid for p in terminals}]), 2)
        finally:
            # Only Popen handles created above are terminated. No tmux is used.
            for terminal in terminals:
                if terminal.poll() is None:
                    terminal.terminate()
                try:
                    terminal.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    terminal.kill()
                    terminal.wait(timeout=3)
            owned_pids = {p.pid for p in terminals}
            for pid, ticks in owned_children:
                try:
                    if process_ticks(pid) == ticks:
                        os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass
                self.assertTrue(wait_for(lambda: not Path("/proc", str(pid)).exists(), 3))
            self.assertTrue(wait_for(lambda: not any(
                w.get("pid") in owned_pids for w in clients()), 3))
            if previous_ticks is not None:
                try:
                    still_present = any(w.get("address") == previous.get("address")
                                        and w.get("pid") == previous_pid
                                        and w.get("stableId") == previous.get("stableId")
                                        for w in clients())
                    if still_present and process_ticks(previous_pid) == previous_ticks:
                        daemon.focus_window(previous["address"])
                except OSError:
                    pass
