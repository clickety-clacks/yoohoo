"""Opt-in Yoohoo adapter gate using Ask's shared real-mosh desktop fixture.

YOOHOO_MOSH_FIXTURE must point to the owner-reviewed, frozen fixture file.
The fixture owns its loopback SSH/mosh/tmux lifecycle and cleanup. This file
adds no resolver or transport implementation: it calls production Yoohoo.
"""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_bundled_window import _SyntheticHub, agent_identity, load_daemon


@unittest.skipUnless(os.environ.get("YOOHOO_MOSH_WINDOWS_TEST") == "1",
                     "explicit real-mosh Testbed opt-in required")
class MoshWindowsTests(unittest.TestCase):
    def test_actual_mosh_connections_focus_existing_window_and_workspace(self):
        self.assertEqual(os.environ.get("YOOHOO_TESTBED"), "1", "test machine only")
        fixture_path = Path(os.environ["YOOHOO_MOSH_FIXTURE"]).resolve(strict=True)
        self.assertEqual(hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                         "12bd8e2a5c1f8bbea2545baed34a3f38304257d7c02d596a464cd436cb572e21",
                         "shared fixture differs from the reviewed snapshot")
        spec = importlib.util.spec_from_file_location("yoohoo_shared_mosh_gate", fixture_path)
        self.assertIsNotNone(spec)
        fixture = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = fixture
        spec.loader.exec_module(fixture)
        daemon = load_daemon("yoohoo_real_mosh_activation")

        def activate(raw_agent):
            agent = copy.deepcopy(raw_agent)
            agent["activity"] = {"state": "needs_attention", "observedAtUnixMs": 1}
            attempts = []
            responses = []
            real_popen = daemon.subprocess.Popen
            real_resolve = daemon.resolve_agent_window

            def capture_resolve(*args, **kwargs):
                result = real_resolve(*args, **kwargs)
                responses.append(result.response)
                return result

            def no_connection_launch(*args, **kwargs):
                argv = args[0] if args else kwargs.get("args", ())
                if argv and Path(str(argv[0])).name in {
                    "ghostty", "ssh", "mosh", "mosh-client", "mosh-server"
                }:
                    attempts.append(Path(str(argv[0])).name)
                    raise AssertionError("existing-window action attempted a connection launch")
                return real_popen(*args, **kwargs)

            with tempfile.TemporaryDirectory(prefix="yoohoo-mosh-state-") as state, \
                    patch.dict(os.environ, {"XDG_STATE_HOME": state}):
                service = daemon.AttentionService()
                service.hub = _SyntheticHub(agent, "testbed")
                with patch.object(daemon, "resolve_agent_window", capture_resolve), \
                        patch.object(daemon.subprocess, "Popen", no_connection_launch):
                    result = service.open_target(agent_identity(agent))
                self.assertTrue(result, service.last_action_failure)
                self.assertTrue(service.hub.acknowledged)
            self.assertEqual(attempts, [])
            self.assertEqual(len(responses), 1)
            candidates = responses[0].get("candidates", [])
            self.assertEqual(len(candidates), 2, responses[0])
            self.assertTrue(all(c["window"]["title"] == "mosh" for c in candidates))
            self.assertTrue(all(any(e["code"] == "transport_host_session_hint"
                                   for e in c["match"]["evidence"])
                                for c in candidates))
            return {"ok": result, "existing": bool(candidates),
                    "newAttachmentAttempts": len(attempts)}

        # The fixture separately asserts actual focus, workspace navigation,
        # unchanged full window identity set and tmux client set, then cleanup.
        outcome = fixture.run_gate(activation_callback=activate)
        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(outcome["windowCount"], 2)
        self.assertEqual(outcome["newAttachmentAttempts"], 0)


if __name__ == "__main__":
    unittest.main()
