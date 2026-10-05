"""Yoohoo's transport choice against the vectors shared with Omarchy Ask.

tests/fixtures/transport-policy-v1.json is agent-window-resolver's
fixtures/transport-policy-v1.json, copied by scripts/sync-resolver.py and
pinned by VENDORED.json. Ask runs the same vectors against its own code.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("yoohoo_transport_policy_hub", ROOT / "payload/agentd_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)
VECTORS = json.loads((ROOT / "tests/fixtures/transport-policy-v1.json").read_text())


class TransportPolicyConformanceTests(unittest.TestCase):
    def test_choice_matches_every_shared_vector(self):
        self.assertEqual(VECTORS["schema"], "transport-policy.v1")
        self.assertEqual(list(hub.TRANSPORT_PREFERENCES), VECTORS["preferences"])
        for case in VECTORS["policy"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(hub.choose_transport(
                    case["preference"], case["targetIsLocal"],
                    case["clients"], case["capabilities"],
                ), case["expect"])

    def test_launch_matches_every_shared_vector(self):
        for case in VECTORS["launch"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(hub.transport_launch_argv(
                    case["transport"], case["fallback"], case["host"],
                    case["remote"], case["etPort"], case["staleFile"],
                ), case["expect"])

    def test_preference_is_read_from_the_hub_section(self):
        self.assertEqual(hub.transport_preference({}), "auto")
        self.assertEqual(hub.transport_preference({"agentd_hub": {"transport": "et"}}), "et")
        self.assertEqual(hub.transport_preference({"agentd_hub": {"transport": "telnet"}}), "auto")


class CapabilityRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yoohoo-capability-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def response(self, state="complete"):
        return {"resolverVersion": "0.2.0", "transports": {
            "state": state,
            "ssh": {"state": "available", "code": "ssh_probe_succeeded"},
            "et": {"state": "available", "code": "et_reachable", "port": 4022},
            "mosh": {"state": "unavailable", "code": "mosh_udp_blocked"},
        }}

    def test_observation_is_recorded_apart_from_the_config_and_read_back(self):
        self.assertTrue(hub.record_capabilities(self.state, "Gibson.", self.response(), now_ms=1000))
        path = self.state / "transport-capabilities/gibson.json"
        self.assertTrue(path.is_file())
        self.assertEqual(hub.read_capabilities(self.state, "gibson", now_ms=2000), {
            "ssh": "available", "et": "available", "mosh": "unavailable", "etPort": 4022,
        })

    def test_unreachable_host_never_overwrites_what_was_learned(self):
        hub.record_capabilities(self.state, "gibson", self.response(), now_ms=1000)
        self.assertFalse(hub.record_capabilities(
            self.state, "gibson", self.response("unreachable"), now_ms=1500))
        self.assertEqual(hub.read_capabilities(self.state, "gibson", now_ms=2000)["et"], "available")

    def test_old_or_missing_record_means_probe_again(self):
        self.assertIsNone(hub.read_capabilities(self.state, "gibson"))
        hub.record_capabilities(self.state, "gibson", self.response(), now_ms=0)
        self.assertIsNone(hub.read_capabilities(
            self.state, "gibson", now_ms=hub.CAPABILITY_MAX_AGE_MS + 1))

    def test_all_unknown_record_lasts_a_day(self):
        response = {"transports": {"state": "partial", **{
            name: {"state": "unknown", "code": "probe_failed"} for name in ("ssh", "et", "mosh")}}}
        hub.record_capabilities(self.state, "gibson", response, now_ms=0)
        self.assertIsNotNone(hub.read_capabilities(self.state, "gibson", now_ms=3600 * 1000))
        self.assertIsNone(hub.read_capabilities(
            self.state, "gibson", now_ms=hub.UNKNOWN_CAPABILITY_MAX_AGE_MS + 1))

    def test_unsafe_host_has_no_record_path(self):
        for host in ("../x", "a/b", "-x;y", ".."):
            with self.subTest(host=host):
                self.assertFalse(hub.record_capabilities(self.state, host, self.response()))

    def test_recorded_et_capability_selects_et_with_its_port(self):
        hub.record_capabilities(self.state, "gibson", self.response())
        plan = hub.connection_plan(
            "gibson", "ask", "osanwe", [], which=lambda name: "/bin/" + name,
            capabilities=hub.read_capabilities(self.state, "gibson"),
        )
        self.assertEqual((plan["transport"], plan["fallback"], plan["etPort"]), ("et", "ssh", 4022))
        argv = hub.build_launch_argv({"machine": "gibson", "tmux": {"session": "ask"}},
                                     plan, "osanwe", "/s/gibson.json")
        self.assertEqual(argv[-1], "et")
        self.assertEqual(hub.parse_remote_launch(argv),
                         {"transport": "et", "host": "gibson", "session": "ask"})

    def test_without_a_record_auto_keeps_mosh_first(self):
        plan = hub.connection_plan("gibson", "ask", "osanwe", [], which=lambda name: "/bin/" + name)
        self.assertEqual(plan["transport"], "mosh")


if __name__ == "__main__":
    unittest.main()
