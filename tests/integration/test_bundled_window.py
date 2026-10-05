"""Opt-in live Yoohoo activation test for the bundled window resolver.

This test is deliberately disabled by default.  When enabled on the isolated
Testbed testbed it creates one Ghostty window containing one bounded ``sleep``
child, publishes only that child as a synthetic Hub claim, and exercises the
real AttentionService action path.  It never searches for or closes an
unrelated window, tmux session, or process.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_daemon(name: str):
    loader = importlib.machinery.SourceFileLoader(
        name, str(ROOT / "payload/window-attention")
    )
    module_spec = importlib.util.spec_from_loader(name, loader)
    daemon = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(daemon)
    return daemon


def hypr(command: str) -> object:
    result = subprocess.run(
        ["hyprctl", "-j", command], check=True, text=True,
        capture_output=True, timeout=3,
    )
    return json.loads(result.stdout)


def clients() -> list[dict]:
    value = hypr("clients")
    return value if isinstance(value, list) else []


def process_ticks(pid: int) -> str:
    raw = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    return str(int(raw.rsplit(") ", 1)[1].split()[19]))


def children(pid: int) -> list[int]:
    result: list[int] = []
    for task in Path("/proc", str(pid), "task").iterdir():
        try:
            raw = (task / "children").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError):
            continue
        result.extend(int(item) for item in raw.split() if item.isdigit())
    return result


def find_sleep(root_pid: int) -> tuple[int, str] | None:
    queue = [root_pid]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        if len(seen) > 128:
            raise AssertionError("owned Ghostty process tree exceeded bound")
        if pid != root_pid:
            try:
                argv = Path("/proc", str(pid), "cmdline").read_bytes()
                executable = os.readlink(f"/proc/{pid}/exe")
                if argv in {b"/usr/bin/sleep\x0025\x00", b"sleep\x0025\x00"} \
                        and executable == "/usr/bin/sleep":
                    return pid, process_ticks(pid)
            except (FileNotFoundError, ProcessLookupError, OSError, ValueError):
                pass
        try:
            queue.extend(children(pid))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return None


def wait_for(predicate, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


class _SyntheticHub:
    enabled = True
    connected = True

    def __init__(self, agent: dict, machine: str):
        self.config = {"machine": machine}
        self.agent = agent
        self.acknowledged = False
        self.launch_cleared = False

    def fetch_snapshot(self) -> bool:
        return True

    def snapshot(self) -> dict:
        return {
            "agents": [self.agent],
            "sources": [{
                "machine": self.agent["machine"],
                "health": {"state": "reporting"},
            }],
        }

    def pending_agents(self) -> list[dict]:
        return [] if self.acknowledged else [self.agent]

    def acknowledge(self, identity: str, agent: dict) -> bool:
        self.acknowledged = identity == agent_identity(agent)
        return self.acknowledged

    def clear_launch_intent(self, _identity: str) -> None:
        self.launch_cleared = True


def agent_identity(agent: dict) -> str:
    identity = agent["id"]
    return "|".join((agent["machine"], agent["instanceId"],
                      str(identity["pid"]), str(identity["startTimeTicks"])))


@unittest.skipUnless(
    os.environ.get("YOOHOO_RESOLVER_WINDOW_TEST") == "1",
    "set YOOHOO_RESOLVER_WINDOW_TEST=1 for the opt-in desktop test",
)
class BundledWindowIntegrationTests(unittest.TestCase):
    def test_owned_ghostty_window_is_focused_without_duplicate_spawn(self):
        if os.environ.get("YOOHOO_TESTBED") != "1":
            self.skipTest("live desktop integration needs YOOHOO_TESTBED=1 on a test machine")
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") \
                or not os.environ.get("WAYLAND_DISPLAY"):
            self.skipTest("Hyprland/Wayland environment is unavailable")
        if shutil.which("ghostty") is None or shutil.which("hyprctl") is None:
            self.skipTest("Ghostty and hyprctl are required")

        daemon = load_daemon("yoohoo_bundled_window_integration")
        previous_active = hypr("activewindow")
        previous_address = (
            str(previous_active.get("address", ""))
            if isinstance(previous_active, dict) else ""
        )
        previous_clients = [item for item in clients()
                            if item.get("address") == previous_address]
        if not previous_address or len(previous_clients) != 1:
            self.skipTest("no existing window is available for focus restoration")
        previous_window = previous_clients[0]
        previous_pid = previous_window.get("pid")
        previous_stable_id = previous_window.get("stableId")
        if (not isinstance(previous_pid, int) or previous_pid <= 0
                or not isinstance(previous_stable_id, str)
                or not previous_stable_id):
            self.skipTest("active window lacks a complete identity")
        try:
            previous_ticks = process_ticks(previous_pid)
        except (FileNotFoundError, OSError, ValueError):
            self.skipTest("active window process identity is unavailable")

        def prior_is_current() -> bool:
            rows = [item for item in clients()
                    if item.get("address") == previous_address]
            if len(rows) != 1:
                return False
            row = rows[0]
            if (row.get("stableId") != previous_stable_id
                    or row.get("pid") != previous_pid):
                return False
            try:
                return process_ticks(previous_pid) == previous_ticks
            except (FileNotFoundError, OSError, ValueError):
                return False

        def focus(address: str) -> None:
            # Exercise Yoohoo's production focus adapter.  The current
            # Hyprland integration dispatches through hl.dsp rather than the
            # legacy ``hyprctl dispatch focuswindow`` command.
            daemon.focus_window(address)

        terminal = subprocess.Popen([
            "/usr/bin/ghostty", "--config-default-files=false",
            "--gtk-single-instance=false", "--confirm-close-surface=false",
            "-e", "/usr/bin/sleep", "25",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL, start_new_session=True)
        child: tuple[int, str] | None = None
        window: dict | None = None
        try:
            def owned_window():
                matches = [item for item in clients()
                           if item.get("pid") == terminal.pid]
                self.assertLessEqual(len(matches), 1)
                return matches[0] if matches else None

            window = wait_for(owned_window)
            self.assertIsNotNone(window, "owned Ghostty did not map to Hyprland")
            assert window is not None
            child = wait_for(lambda: find_sleep(terminal.pid))
            self.assertIsNotNone(child, "owned sleep child did not start")
            assert child is not None
            child_pid, child_ticks = child
            terminal_ticks = process_ticks(terminal.pid)
            address = str(window["address"])
            stable_id = str(window.get("stableId") or ("hypr:" + address))
            self.assertTrue(prior_is_current(), "prior window changed during setup")
            focus(previous_address)
            self.assertEqual(
                wait_for(lambda: hypr("activewindow").get("address")
                         == previous_address),
                True,
                "could not return focus to the pre-existing window",
            )
            self.assertNotEqual(hypr("activewindow").get("address"), address)
            agent = {
                "machine": socket.gethostname(),
                "instanceId": "yoohoo-owned-window-test",
                "id": {"pid": child_pid, "startTimeTicks": child_ticks},
                "presence": {"state": "present"},
                "activity": {"state": "needs_attention"},
            }

            with tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False):
                service = daemon.AttentionService()
                service.hub = _SyntheticHub(agent, socket.gethostname())
                service.hub_pending[agent_identity(agent)] = agent
                original_popen = subprocess.Popen

                def guarded_popen(*args, **kwargs):
                    argv = args[0] if args else kwargs.get("args", ())
                    values = list(argv) if not isinstance(argv, str) else [argv]
                    if values and os.path.basename(str(values[0])) == "ghostty":
                        raise AssertionError(
                            "existing-window activation spawned a terminal"
                        )
                    # hyprctl and any bounded non-terminal helper remain
                    # available to the real activation path.
                    return original_popen(*args, **kwargs)

                with patch.object(daemon.subprocess, "Popen", guarded_popen):
                    self.assertTrue(service.open_target(agent_identity(agent)))

            self.assertTrue(service.hub.acknowledged)
            self.assertTrue(service.hub.launch_cleared)
            active = hypr("activewindow")
            self.assertIsInstance(active, dict)
            self.assertEqual(active.get("address"), address)
            live = [item for item in clients() if item.get("pid") == terminal.pid]
            self.assertEqual(len(live), 1)
            self.assertEqual(live[0].get("address"), address)
            self.assertEqual(live[0].get("stableId") or ("hypr:" + address), stable_id)
            self.assertEqual(process_ticks(terminal.pid), terminal_ticks)
            self.assertEqual(process_ticks(child_pid), child_ticks)
        finally:
            # The Popen handle owns this one terminal.  If its child survives,
            # signal it only after rechecking the captured start-time identity.
            if terminal.poll() is None:
                terminal.terminate()
            try:
                terminal.wait(timeout=3)
            except subprocess.TimeoutExpired:
                terminal.kill()
                terminal.wait(timeout=3)
            if child is not None:
                child_pid, child_ticks = child
                try:
                    if process_ticks(child_pid) == child_ticks:
                        os.kill(child_pid, signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError, OSError, ValueError):
                    pass
                self.assertTrue(
                    wait_for(lambda: not Path("/proc", str(child_pid)).exists(), 3.0),
                    "owned sleep child survived cleanup",
                )
            if window is not None:
                self.assertTrue(
                    wait_for(lambda: not any(
                        item.get("address") == window.get("address")
                        for item in clients()
                    ), 3.0),
                    "owned Ghostty window survived cleanup",
                )
            if previous_address and prior_is_current():
                focus(previous_address)
                self.assertTrue(
                    wait_for(lambda: hypr("activewindow").get("address")
                             == previous_address, 3.0),
                    "could not restore the prior focused window",
                )


if __name__ == "__main__":
    unittest.main()
