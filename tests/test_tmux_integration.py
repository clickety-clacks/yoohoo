"""Live tmux/process-topology checks for the production matching path.

These tests use a private tmux socket directory and uniquely named fixtures.
They never attach to, kill, or update the user's normal tmux server or Agentd
state.  CI environments without tmux skip the module.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pty
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DAEMON_PATH = ROOT / "payload" / "window-attention"
loader = importlib.machinery.SourceFileLoader("window_attention_tmux_integration", str(DAEMON_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
daemon = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(daemon)


def _agent(*, pane: str, state: str = "needs_attention") -> dict:
    return {
        "machine": "tmux-fixture",
        "instanceId": "fixture-instance",
        "id": {"pid": os.getpid(), "startTimeTicks": 0},
        "harness": "codex",
        "presence": {"state": "present", "cause": None},
        "activity": {"state": state, "observedAtUnixMs": 2_000},
        "tmux": {
            "session": "",
            "windowIndex": "0",
            "windowName": "fixture window",
            "paneId": pane,
        },
    }


def _resolver_diagnostics(response: dict) -> str:
    """Keep failure output focused on proof state and collection evidence."""
    return json.dumps({
        "status": response.get("status"),
        "reasons": response.get("reasons"),
        "evidence": response.get("evidence"),
        "candidates": response.get("candidates"),
    }, sort_keys=True)


def _probe_diagnostics(probe_io) -> str:
    return json.dumps([
        {
            "argv": list(argv),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "stderr": result.stderr.decode("utf-8", "replace"),
        }
        for argv, result in probe_io.runs
    ], sort_keys=True)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxIntegrationTests(unittest.TestCase):
    """Exercise the actual tmux commands used by Yoohoo."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="yoohoo-tmux-integration-")
        self.tmux_tmpdir = Path(self.temp.name) / "socket"
        self.tmux_tmpdir.mkdir()
        self.env = os.environ.copy()
        # Never let a test launched from a user's tmux client escape into that
        # server.  TMUX_TMPDIR gives the production commands a private default
        # socket without adding a -L test-only argument to them.
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env["TMUX_TMPDIR"] = str(self.tmux_tmpdir)
        self.env["TERM"] = "xterm-256color"
        self.session = f"yoohoo tmux {uuid.uuid4().hex[:10]}"
        self._tmux("new-session", "-d", "-s", self.session, "-n", "fixture window", "sh")
        self.window_index = self._tmux(
            "list-windows", "-t", f"={self.session}", "-F", "#{window_index}"
        ).stdout.strip()
        self.assertTrue(self.window_index)
        self._tmux("split-window", "-h", "-t", f"={self.session}:{self.window_index}", "sh")
        self.client_master, client_slave = pty.openpty()
        self.tmux_client = subprocess.Popen(
            ["tmux", "attach-session", "-t", "=" + self.session],
            env=self.env,
            stdin=client_slave,
            stdout=client_slave,
            stderr=client_slave,
            close_fds=True,
        )
        os.close(client_slave)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.tmux_client.poll() is not None:
                break
            clients = self._tmux("list-clients", "-F", "#{client_pid}", check=False)
            if clients.returncode == 0 and clients.stdout.strip():
                break
            time.sleep(0.02)
        self.assertIsNone(self.tmux_client.poll())

    def tearDown(self) -> None:
        if getattr(self, "tmux_client", None) is not None:
            self.tmux_client.terminate()
            try:
                self.tmux_client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.tmux_client.kill()
                self.tmux_client.wait(timeout=3)
        if getattr(self, "client_master", None) is not None:
            os.close(self.client_master)
        self._tmux("kill-server", check=False)
        self.temp.cleanup()

    def _tmux(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *arguments],
            env=self.env,
            check=check,
            text=True,
            capture_output=True,
            timeout=3,
        )

    def _panes(self) -> list[dict[str, str]]:
        result = self._tmux(
            "list-panes",
            "-t",
            f"={self.session}:{self.window_index}",
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_active}",
        )
        panes = []
        for line in result.stdout.splitlines():
            pane_id, pane_pid, active = line.split("\t")
            panes.append({"paneId": pane_id, "panePid": pane_pid, "active": active})
        self.assertEqual(len(panes), 2)
        return panes

    def test_production_select_targets_percent_pane_and_space_named_session(self):
        panes = self._panes()
        current = next(item for item in panes if item["active"] == "1")
        target = next(item for item in panes if item["paneId"] != current["paneId"])
        self.assertTrue(target["paneId"].startswith("%"))

        agent = _agent(pane=target["paneId"])
        agent["tmux"].update(session=self.session, windowIndex=self.window_index)
        # select_tmux_pane uses the process environment, just as it does in
        # the installed daemon.  Keep this production call on our private
        # server while preserving the real tmux command/target syntax.
        with patch.dict(os.environ, self.env, clear=True):
            daemon.select_tmux_pane(agent, "tmux-fixture")

        selected = self._tmux(
            "display-message", "-p", "-t", f"={self.session}:{self.window_index}", "#{pane_id}"
        ).stdout.strip()
        self.assertEqual(selected, target["paneId"])

    def test_selection_failure_does_not_acknowledge_existing_alert(self):
        """A failed pane selection must remain visible and unacknowledged."""
        panes = self._panes()
        selected_before = next(item for item in panes if item["active"] == "1")["paneId"]
        missing_pane = "%999999"
        direct_failure = self._tmux("select-pane", "-t", missing_pane, check=False)
        self.assertNotEqual(direct_failure.returncode, 0)

        agent = _agent(pane=missing_pane)
        agent["tmux"].update(session=self.session, windowIndex=self.window_index)
        identity = "tmux-fixture|fixture-instance|" + str(os.getpid()) + "|0"

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "tmux-fixture"}

            def fetch_snapshot(self):
                return True

            def snapshot(self):
                return {
                    "sources": [{"machine": "tmux-fixture", "health": {"state": "reporting"}}],
                    "agents": [agent],
                }

            def pending_agents(self):
                return [agent]

            def acknowledge(self, *_args):
                self.acknowledged = True

            def has_launch_intent(self, *_args):
                return False

            def clear_launch_intent(self, *_args):
                pass

        clients = [{"address": "0xf17", "pid": self.tmux_client.pid}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=clients), \
             patch.object(daemon, "resolve_agent_window", return_value=SimpleNamespace(
                 response={"status": "unresolved", "candidates": [], "reasons": [{
                     "code": "pane_membership_mismatch", "source": "tmux",
                     "message": "fixture pane is gone", "retryable": False,
                 }]}
             )), \
             patch.object(daemon, "connection_plan", return_value={
                 "available": False, "reason": "fixture_no_launch"
             }), \
             patch.object(daemon, "focus_window"):
            service = daemon.AttentionService()
            service.hub = FakeHub()
            with patch.dict(os.environ, self.env, clear=True):
                result = service.open_target(identity)

        # This is intentionally an end-to-end assertion: a pane failure must
        # propagate through open_target instead of becoming an acknowledgement.
        self.assertFalse(result)
        self.assertFalse(hasattr(service.hub, "acknowledged"))
        selected_after = self._tmux(
            "display-message", "-p", "-t", f"={self.session}:{self.window_index}", "#{pane_id}"
        ).stdout.strip()
        self.assertEqual(selected_after, selected_before)

    def test_process_ancestors_maps_real_fixture_process(self):
        fixture = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=False,
        )
        try:
            deadline = time.monotonic() + 2
            ancestors = set()
            while time.monotonic() < deadline:
                ancestors = daemon.process_ancestors(fixture.pid)
                if os.getpid() in ancestors:
                    break
                time.sleep(0.01)
            self.assertIn(fixture.pid, ancestors)
            self.assertIn(os.getpid(), ancestors)
        finally:
            fixture.terminate()
            fixture.wait(timeout=3)

    def test_bundled_resolver_resolves_and_revalidates_actual_tmux_pane(self):
        panes = self._panes()
        target = next(item for item in panes if item["active"] == "1")
        command = "exec " + shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])
        self._tmux("send-keys", "-t", target["paneId"], command, "Enter")

        deadline = time.monotonic() + 2
        pane_pid = int(target["panePid"])
        while time.monotonic() < deadline:
            current = next(item for item in self._panes() if item["paneId"] == target["paneId"])
            pane_pid = int(current["panePid"])
            if "-c" in daemon.process_argv(pane_pid):
                break
            time.sleep(0.02)
        self.assertIn("-c", daemon.process_argv(pane_pid))

        stat = Path(f"/proc/{pane_pid}/stat").read_text(encoding="utf-8")
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        agent = _agent(pane=target["paneId"])
        agent["id"].update(pid=pane_pid, startTimeTicks=ticks)
        agent["tmux"].update(session=self.session, windowIndex=self.window_index)
        clients = [{"address": "0xf17", "pid": self.tmux_client.pid}]
        adapter = daemon.agentd_hub._bundled_resolver_adapter()

        class CapturingProbeIO(adapter._linux.DefaultProbeIO):
            def __init__(self):
                super().__init__()
                self.runs = []

            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                self.runs.append((argv, result))
                return result

        probe_io = CapturingProbeIO()
        collector = adapter.LinuxCollector(io=probe_io)
        with patch.dict(os.environ, self.env, clear=True):
            resolved = adapter.resolve_agent(
                agent, clients, "tmux-fixture", collector=collector,
            )
            self.assertEqual(
                resolved.response["status"], "matched",
                _resolver_diagnostics(resolved.response)
                + " probe=" + _probe_diagnostics(probe_io),
            )
            candidate = daemon.resolver_candidate_record(resolved.response)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["window"]["address"], "0xf17")
            probe_io = CapturingProbeIO()
            collector = adapter.LinuxCollector(io=probe_io)
            revalidated = adapter.resolve_agent(
                agent, clients, "tmux-fixture",
                operation="revalidate", prior=resolved.response["candidates"][0],
                collector=collector,
            )
        self.assertEqual(
            revalidated.response["status"], "matched",
            _resolver_diagnostics(revalidated.response)
            + " probe=" + _probe_diagnostics(probe_io),
        )

    def test_best_effort_local_tmux_uses_current_client_without_name_in_argv(self):
        master, slave = pty.openpty()
        client = subprocess.Popen(
            ["tmux", "attach"], env=self.env,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
        os.close(slave)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                listed = self._tmux("list-clients", "-F", "#{client_pid}").stdout.splitlines()
                if str(client.pid) in listed:
                    break
                time.sleep(0.02)
            self.assertIn(str(client.pid), listed)
            target = next(pane for pane in self._panes() if pane["active"] == "1")
            pane_pid = int(target["panePid"])
            ticks = Path(f"/proc/{pane_pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            agent = _agent(pane=target["paneId"])
            agent["id"].update(pid=pane_pid, startTimeTicks=ticks)
            agent["tmux"].update(session=self.session, windowIndex=self.window_index)
            adapter = daemon.agentd_hub._bundled_resolver_adapter()

            class NoFullTargetProbe(adapter._linux.DefaultProbeIO):
                def run(self, argv, **kwargs):
                    if Path(argv[0]).name != "tmux":
                        raise AssertionError("best-effort local lookup ran a full target probe")
                    return super().run(argv, **kwargs)

            with patch.dict(os.environ, self.env, clear=True):
                result = adapter.resolve_agent(
                    agent, [{"address": "0xf18", "pid": client.pid, "title": "host:shell"}],
                    "tmux-fixture", operation="match",
                    collector=adapter.LinuxCollector(io=NoFullTargetProbe()),
                )
            self.assertEqual(result.response["status"], "matched", _resolver_diagnostics(result.response))
            self.assertEqual(result.response["candidates"][0]["window"]["pid"], client.pid)
        finally:
            if client.poll() is None:
                client.terminate()
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=3)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
