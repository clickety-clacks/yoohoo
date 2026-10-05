#!/usr/bin/env python3
"""Plumbus-only cross-product check: real Hub binary and Yoohoo subscriber.

HUB_HEALTH_BINARY selects an already built Hub. All roster input is synthetic;
an owned SSH stub replaces transport, so no real agents or network hosts are
contacted. No compositor, terminal, installed state, or system service changes.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("health_integration_hub", ROOT / "payload/agentd_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not satisfied within bounded wait")


def cleanup_stub(identity):
    """Signal only the captured child identity, even if Hub exited first."""
    pid, ticks = identity
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return
        if fields[19] != ticks:
            raise AssertionError("stub PID identity changed; refusing signal")
        if fields[0] == "Z":
            return  # Already exited; its parent/guardian owns reaping.
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


@unittest.skipUnless(os.environ.get("HUB_HEALTH_BINARY"), "requires built Hub on Plumbus")
class RealHubHealthTests(unittest.TestCase):
    def test_quiet_real_hub_heartbeat_preserves_roster_and_activity(self):
        binary = Path(os.environ["HUB_HEALTH_BINARY"]).resolve(strict=True)
        directory = Path(tempfile.mkdtemp(prefix="yoohoo-real-hub-health-"))
        # Do not remove failed-fixture evidence automatically.
        self.addCleanup(lambda: print("fixture evidence:", directory))
        tools = directory / "bin"
        tools.mkdir()
        roster = {
            "type": "snapshot", "schema": "agentd.snapshot.v1", "reason": "initial",
            "instanceId": "health-fixture", "revision": 1, "observedAtUnixMs": 1000,
            "scan": {"state": "complete", "issues": []},
            "agents": [{
                "id": {"pid": 424242, "startTimeTicks": 12345},
                "harness": "codex", "detectedBy": "proc_comm",
                "presence": {"state": "present", "cause": None},
                "cwd": {"state": "known", "value": "/fixture", "cause": None},
                "activity": {"state": "needs_attention", "source": "hook", "observedAtUnixMs": 1000},
            }],
        }
        (directory / "roster.json").write_text(json.dumps(roster))
        (directory / "sources.txt").write_text("health-fixture\n")
        ssh = tools / "ssh"
        ssh.write_text("#!/usr/bin/python3\n"
                       "import os, pathlib, signal, sys\n"
                       "root = pathlib.Path(__file__).resolve().parents[1]\n"
                       "ticks = pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]\n"
                       "with (root / 'children.txt').open('a') as log: log.write(str(os.getpid()) + ' ' + ticks + '\\n')\n"
                       "print((root / 'roster.json').read_text(), flush=True)\n"
                       "if 'watch' in sys.argv[-1]: signal.pause()\n")
        ssh.chmod(0o700)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ.get("PATH", "")}
        client = hub.AgentdHub(
            {"agentd_hub": {"enabled": True, "url": f"http://127.0.0.1:{port}"}},
            directory / "client",
        )
        with (directory / "server.log").open("w") as log:
            server = subprocess.Popen(
                [str(binary), "--listen", f"127.0.0.1:{port}", "--sources-file", str(directory / "sources.txt")],
                env=env, stdout=log, stderr=log, start_new_session=True,
            )
            try:
                client.start()
                wait_until(lambda: client.status().get("status") == "live"
                           and client.snapshot()["revision"] >= 2, 10)
                first = client.snapshot()
                first_health = client.status()
                self.assertEqual(len(first["agents"]), 1)
                # Real server default interval: do not speed up the protocol.
                wait_until(lambda: client.status().get("lastSeenAtUnixMs", 0)
                           > first_health["lastSeenAtUnixMs"], 20)
                second_health = client.status()
                self.assertEqual(second_health["status"], "live")
                self.assertEqual(client.snapshot(), first)
                self.assertEqual(second_health["lastSnapshotAtUnixMs"], first_health["lastSnapshotAtUnixMs"])
                self.assertEqual(server.poll(), None)
            finally:
                try:
                    client.stop()
                finally:
                    forced = False
                    if server.poll() is None:
                        try:
                            server.terminate()  # Only our unreaped Popen child.
                        except ProcessLookupError:
                            pass
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Root remains our unreaped direct child. Its new
                        # session/group contains only this fixture and stubs.
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=5)
                        forced = True
                    child_log = directory / "children.txt"
                    if child_log.exists():
                        children = [(int(line.split()[0]), line.split()[1])
                                    for line in child_log.read_text().splitlines()]
                        for identity in children:
                            cleanup_stub(identity)
                        wait_until(lambda: all(not Path("/proc", str(pid)).exists()
                                               for pid, _ticks in children), 3)
                    self.assertFalse(forced, "owned Hub required forced termination; evidence retained")


if __name__ == "__main__":
    unittest.main()
