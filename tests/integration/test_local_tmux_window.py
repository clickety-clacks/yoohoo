"""Opt-in Testbed gate for a real local tmux-backed Ghostty window.

The fixture is deliberately narrow: one private tmux server, one attached
Ghostty window, and one ``sleep`` pane published through a synthetic Hub.  The
action is the production ``AttentionService.open_target`` path with a real
compositor inventory, focus dispatch, and Linux collector.  It must reuse the
existing window; attaching a new terminal or probing through SSH/mosh is a
test failure.
"""

from __future__ import annotations

import os
from pathlib import Path
import json
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_bundled_window import (
    _SyntheticHub,
    agent_identity,
    clients,
    hypr,
    load_daemon,
    process_ticks,
    wait_for,
)


@unittest.skipUnless(
    os.environ.get("YOOHOO_LOCAL_TMUX_WINDOW_TEST") == "1",
    "set YOOHOO_LOCAL_TMUX_WINDOW_TEST=1 for the opt-in Testbed test",
)
class LocalTmuxWindowIntegrationTests(unittest.TestCase):
    def test_existing_local_tmux_window_is_focused_without_attach(self):
        if os.environ.get("YOOHOO_TESTBED") != "1":
            self.skipTest("live desktop integration needs YOOHOO_TESTBED=1 on a test machine")
        if (not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
                or not os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("Hyprland/Wayland environment is unavailable")
        if any(shutil.which(name) is None for name in ("ghostty", "hyprctl", "tmux")):
            self.skipTest("Ghostty, hyprctl, and tmux are required")

        daemon = load_daemon("yoohoo_local_tmux_window_integration")
        previous_workspace = str(hypr("activeworkspace").get("name", ""))
        previous = hypr("activewindow")
        previous_address = str(previous.get("address", ""))
        previous_rows = [row for row in clients()
                         if row.get("address") == previous_address]
        previous_pid = previous_rows[0].get("pid") if len(previous_rows) == 1 else None
        previous_stable = (previous_rows[0].get("stableId", "")
                           if len(previous_rows) == 1 else "")
        if (not isinstance(previous_pid, int) or previous_pid <= 0
                or not isinstance(previous_stable, str) or not previous_stable):
            previous_address = ""
            previous_pid = None
            previous_stable = ""
        try:
            previous_ticks = process_ticks(previous_pid) if previous_pid else None
        except (FileNotFoundError, OSError, ValueError):
            previous_address = ""
            previous_ticks = None

        def prior_is_current() -> bool:
            if not previous_address or previous_pid is None or previous_ticks is None:
                return False
            rows = [row for row in clients()
                    if row.get("address") == previous_address]
            if len(rows) != 1:
                return False
            row = rows[0]
            if (row.get("stableId") != previous_stable
                    or row.get("pid") != previous_pid):
                return False
            try:
                return process_ticks(previous_pid) == previous_ticks
            except (FileNotFoundError, OSError, ValueError):
                return False

        def window_key(row: dict) -> tuple[str, str, int, str]:
            return (str(row.get("address", "")),
                    str(row.get("stableId", "")),
                    int(row.get("pid") or 0), process_ticks(int(row["pid"])))

        def identity_gone(identity: tuple[int, str]) -> bool:
            try:
                return process_ticks(identity[0]) != identity[1]
            except (FileNotFoundError, ProcessLookupError):
                return True

        def argv_for(pid: int) -> list[str]:
            try:
                return Path("/proc", str(pid), "cmdline").read_bytes().decode(
                    "utf-8", "replace").rstrip("\0").split("\0")
            except (FileNotFoundError, OSError, UnicodeError):
                return []

        def dispatch(expression: str) -> None:
            subprocess.run(
                ["hyprctl", "dispatch", expression], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )

        terminal: subprocess.Popen[bytes] | None = None
        pane_pid: int | None = None
        pane_ticks: str | None = None
        terminal_window: dict | None = None
        socket_path: Path | None = None
        tmux_env: dict[str, str] | None = None
        fixture_root: Path | None = None
        server_identity: tuple[int, str] | None = None
        client_identity: tuple[int, str] | None = None
        cleanup_verified = False
        try:
            # Explicit cleanup: never let a finalizer remove a socket whose
            # server/client cleanup could not be verified.
            fixture_root = Path(tempfile.mkdtemp(prefix="yoohoo-local-tmux-"))
            try:
                root = fixture_root
                tmux_tmpdir = root / "tmux"
                tmux_tmpdir.mkdir()
                socket_path = tmux_tmpdir / ("tmux-" + str(os.getuid())) / "default"
                socket_path.parent.mkdir()
                tmux_env = os.environ.copy()
                tmux_env.pop("TMUX", None)
                tmux_env.pop("TMUX_PANE", None)
                tmux_env["TMUX_TMPDIR"] = str(tmux_tmpdir)
                tmux_env["TERM"] = "xterm-256color"
                session = "yoohoo-local-tmux-" + uuid.uuid4().hex[:10]
                generic_title = "Yoohoo local tmux fixture"

                def tmux(*arguments: str, check: bool = True):
                    assert socket_path is not None and tmux_env is not None
                    return subprocess.run(
                        ["tmux", "-f", "/dev/null", "-S", str(socket_path), *arguments],
                        env=tmux_env, check=check, capture_output=True,
                        text=True, timeout=3,
                    )

                tmux("new-session", "-d", "-s", session, "-n", "syntheticHub",
                     "/usr/bin/sleep", "60")
                server_pid = int(tmux("display-message", "-p", "#{pid}").stdout.strip())
                server_identity = (server_pid, process_ticks(server_pid))
                window_index = tmux(
                    "list-windows", "-t", "=" + session, "-F", "#{window_index}"
                ).stdout.strip()
                pane_line = tmux(
                    "display-message", "-p", "-t", f"={session}:{window_index}",
                    "#{pane_id}\t#{pane_pid}",
                ).stdout.strip()
                pane_id, raw_pane_pid = pane_line.split("\t", 1)
                pane_pid = int(raw_pane_pid)
                pane_ticks = process_ticks(pane_pid)
                self.assertTrue(wait_for(lambda:
                    Path("/proc", str(pane_pid), "exe").resolve()
                    == Path("/usr/bin/sleep")), "owned pane did not exec sleep")
                self.assertEqual(argv_for(pane_pid)[-1], "60")

                # No target/session argument: only the test socket is explicit,
                # so terminal environment filtering cannot escape isolation.
                terminal = subprocess.Popen(
                    ["/usr/bin/ghostty", "--config-default-files=false",
                     "--gtk-single-instance=false", "--confirm-close-surface=false",
                     "--title=" + generic_title, "-e", "/usr/bin/tmux",
                     "-f", "/dev/null", "-S", str(socket_path), "attach"],
                    env=tmux_env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

                def owned_window():
                    matches = [row for row in clients()
                               if row.get("pid") == terminal.pid]
                    self.assertLessEqual(len(matches), 1)
                    return matches[0] if matches else None

                terminal_window = wait_for(owned_window)
                self.assertIsNotNone(terminal_window,
                                     "owned Ghostty did not map to Hyprland")
                assert terminal_window is not None
                self.assertNotIn(session, " ".join(argv_for(terminal.pid)))
                title = str(terminal_window.get("title", ""))
                self.assertTrue(title, "Ghostty window has no title")
                self.assertNotIn(session, title)
                client_pids = wait_for(lambda: tmux(
                    "list-clients", "-F", "#{client_pid}"
                ).stdout.split())
                self.assertIsNotNone(client_pids, "owned tmux client did not attach")
                self.assertEqual(len(client_pids), 1)
                self.assertIn(terminal.pid, daemon.process_ancestors(int(client_pids[0])))
                client_pid = int(client_pids[0])
                client_identity = (client_pid, process_ticks(client_pid))

                target_workspace = "yoohoo-tmux-target-" + str(terminal.pid)
                empty_workspace = "yoohoo-tmux-empty-" + str(terminal.pid)
                dispatch(
                    "hl.dsp.window.move({ window = "
                    + json.dumps("address:" + str(terminal_window["address"]))
                    + ", workspace = "
                    + json.dumps("name:" + target_workspace)
                    + ", follow = false })"
                )
                self.assertTrue(wait_for(lambda: next(
                    (row for row in clients()
                     if row.get("address") == terminal_window["address"]
                     and str((row.get("workspace") or {}).get("name", ""))
                     == target_workspace), None)))
                dispatch(
                    "hl.dsp.focus({ workspace = "
                    + json.dumps("name:" + empty_workspace) + " })"
                )
                self.assertTrue(wait_for(lambda: str(
                    hypr("activeworkspace").get("name", "")) == empty_workspace))

                before_action = {window_key(row) for row in clients()}
                target_ticks = pane_ticks
                assert target_ticks is not None
                agent = {
                    "machine": socket.gethostname(),
                    "instanceId": "yoohoo-local-tmux-window-test",
                    "id": {"pid": pane_pid, "startTimeTicks": target_ticks},
                    "harness": "syntheticHub",
                    "presence": {"state": "present"},
                    "activity": {"state": "needs_attention", "observedAtUnixMs": 2_000},
                    "tmux": {
                        "session": session,
                        "windowIndex": window_index,
                        "windowName": "syntheticHub",
                        "paneId": pane_id,
                        "socket": {"kind": "path", "value": str(socket_path)},
                    },
                }

                adapter = daemon.agentd_hub._bundled_resolver_adapter()
                probe_runs: list[tuple[str, ...]] = []

                class NoFullTargetProbe(adapter._linux.DefaultProbeIO):
                    def run(self, argv, **kwargs):
                        values = tuple(str(value) for value in argv)
                        probe_runs.append(values)
                        executable = Path(values[0]).name if values else ""
                        if executable in {"ssh", "mosh", "mosh-client"}:
                            raise AssertionError("local matching attempted remote transport")
                        if executable in {"python", "python3", "python3.12"}:
                            raise AssertionError("local matching ran a full target Python probe")
                        return super().run(argv, **kwargs)

                collector = adapter.LinuxCollector(io=NoFullTargetProbe())
                resolved_response: dict | None = None

                def production_resolve(agent_value, windows, machine, **kwargs):
                    nonlocal resolved_response
                    result = adapter.resolve_agent(
                        agent_value, windows, machine, collector=collector, **kwargs
                    )
                    resolved_response = result.response
                    return result

                forbidden = {"ghostty", "ssh", "mosh", "mosh-client"}
                original_popen = subprocess.Popen

                def guarded_popen(*args, **kwargs):
                    raw = args[0] if args else kwargs.get("args", ())
                    values = (shlex.split(raw) if isinstance(raw, str)
                              else [str(value) for value in raw])
                    executable = Path(values[0]).name if values else ""
                    if executable in forbidden:
                        raise AssertionError(
                            f"existing-window activation spawned forbidden {executable}"
                        )
                    return original_popen(*args, **kwargs)

                runtime_env = dict(tmux_env)
                runtime_env["XDG_STATE_HOME"] = str(root / "state")
                with patch.dict(os.environ, runtime_env, clear=True), \
                        patch.object(daemon, "resolve_agent_window", production_resolve), \
                        patch.object(daemon.subprocess, "Popen", guarded_popen):
                    service = daemon.AttentionService()
                    service.hub = _SyntheticHub(agent, socket.gethostname())
                    service.hub_pending[agent_identity(agent)] = agent
                    self.assertTrue(service.open_target(agent_identity(agent)))

                self.assertIsNotNone(resolved_response)
                assert resolved_response is not None
                self.assertEqual(resolved_response.get("status"), "matched")
                self.assertTrue(probe_runs)
                self.assertTrue(all(Path(argv[0]).name == "tmux"
                                    for argv in probe_runs))
                self.assertTrue(service.hub.acknowledged)
                self.assertTrue(service.hub.launch_cleared)

                active = hypr("activewindow")
                self.assertEqual(active.get("address"), terminal_window["address"])
                self.assertEqual(str(hypr("activeworkspace").get("name", "")),
                                 target_workspace)
                after_action = {window_key(row) for row in clients()}
                self.assertEqual(len(after_action), len(before_action))
                self.assertEqual(after_action, before_action)
            finally:
                # Only terminate the owned Ghostty handle.  The private server
                # is then killed through its explicit socket, never user's tmux.
                if terminal is not None and terminal.poll() is None:
                    terminal.terminate()
                if terminal is not None:
                    try:
                        terminal.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        terminal.kill()
                        terminal.wait(timeout=3)
                if socket_path is not None and tmux_env is not None:
                    stopped = subprocess.run(
                        ["tmux", "-S", str(socket_path), "kill-server"],
                        env=tmux_env, check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=3,
                    )
                    if stopped.returncode != 0 and (server_identity is None
                            or not identity_gone(server_identity)):
                        self.fail(f"private server stop failed; preserved {fixture_root}")
                self.assertTrue(server_identity is not None and wait_for(
                    lambda: identity_gone(server_identity), 3),
                    f"private server cleanup unverified; preserved {fixture_root}")
                self.assertTrue(terminal is None or (client_identity is not None
                    and wait_for(lambda: identity_gone(client_identity), 3)),
                    f"private client cleanup unverified; preserved {fixture_root}")
                cleanup_verified = True
        finally:
            if pane_pid is not None and pane_ticks is not None:
                try:
                    if (process_ticks(pane_pid) == pane_ticks
                            and Path("/proc", str(pane_pid), "exe").resolve()
                            == Path("/usr/bin/sleep")):
                        os.kill(pane_pid, signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError, OSError, ValueError):
                    pass
                self.assertTrue(
                    wait_for(lambda: not Path("/proc", str(pane_pid)).exists(), 3),
                    "owned sleep pane process survived cleanup",
                )
            if terminal_window is not None:
                self.assertTrue(wait_for(lambda: not any(
                    row.get("address") == terminal_window.get("address")
                    for row in clients()), 3),
                    "owned Ghostty window survived cleanup")
            if previous_address and prior_is_current():
                daemon.focus_window(previous_address)
                self.assertTrue(wait_for(lambda: hypr("activewindow").get(
                    "address") == previous_address, 3),
                    "could not restore the prior focused window")
            elif previous_workspace:
                dispatch(
                    "hl.dsp.focus({ workspace = "
                    + json.dumps(previous_workspace if previous_workspace.isdecimal()
                                 else "name:" + previous_workspace) + " })"
                )
            if cleanup_verified and fixture_root is not None:
                shutil.rmtree(fixture_root)


if __name__ == "__main__":
    unittest.main()
