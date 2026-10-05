"""Regression tests for shared attention-tag ownership.

All compositor and Hub I/O is replaced with stdlib mocks.
"""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


DAEMON = Path(__file__).resolve().parents[1] / "payload/window-attention"
LOADER = importlib.machinery.SourceFileLoader("pulse_ownership_attention", str(DAEMON))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
attention = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(attention)


def candidate(address):
    return {"window": {"address": address}}


class PulseOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.object(attention, "load_config", return_value={
            "sound_enabled": False,
            "history_enabled": False,
        })
        self.config.start()
        self.addCleanup(self.config.stop)

    def service(self, directory):
        state = patch.object(attention, "state_dir", return_value=Path(directory))
        state.start()
        self.addCleanup(state.stop)
        return attention.AttentionService()

    def test_hub_removal_preserves_local_owner_then_last_owner_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            address = "0xabc"
            service.windows[address] = {"address": address}
            service.hub_matches = {"agent-a": address}
            operations = []
            with patch.object(service, "_write_hub_match_cache"), \
                 patch.object(attention, "active_address", return_value=""), \
                 patch.object(attention, "tag_window_with_name",
                              side_effect=lambda *args: operations.append(("named", args))), \
                 patch.object(attention, "tag_window",
                              side_effect=lambda *args: operations.append(("base", args))):
                self.assertTrue(service._apply_hub_matches({}, service._resolver_generation))
                self.assertEqual(operations, [])

                service.windows.clear()
                service.hub_matches = {"agent-a": address}
                self.assertTrue(service._apply_hub_matches({}, service._resolver_generation))

            self.assertEqual(operations, [
                ("named", (address, attention.PULSE_TAG, False)),
                ("base", (address, False)),
            ])

    def test_base_cleanup_survives_pulse_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            with patch.object(attention, "tag_window_with_name",
                              side_effect=OSError("pulse cleanup failed")), \
                 patch.object(attention, "tag_window") as base:
                self.assertTrue(service._clear_unowned_tags("0xabc"))

            base.assert_called_once_with("0xabc", False)

    def test_shared_hub_address_removal_is_owned_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            address = "0xabc"
            service.hub_matches = {"agent-a": address, "agent-b": address}
            cleared = []
            with patch.object(service, "_write_hub_match_cache"), \
                 patch.object(attention, "active_address", return_value=""), \
                 patch.object(attention, "tag_window_with_name",
                              side_effect=lambda *args: cleared.append(args)), \
                 patch.object(attention, "tag_window") as base:
                self.assertTrue(service._apply_hub_matches(
                    {"agent-b": candidate(address)}, service._resolver_generation,
                ))

                self.assertEqual(cleared, [])
                base.assert_called_once_with(address, True)

                cleared.clear()
                base.reset_mock()
                service.hub_matches = {"agent-a": address, "agent-b": address}
                self.assertTrue(service._apply_hub_matches(
                    {}, service._resolver_generation,
                ))

            self.assertEqual(cleared, [(address, attention.PULSE_TAG, False)])
            base.assert_called_once_with(address, False)
            self.assertEqual(service.hub_matches, {})

    def test_shared_hub_address_survives_one_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            address = "0xabc"
            agent = {"activity": {"state": "needs_attention", "observedAtUnixMs": 1},
                     "presence": {"state": "present"}}
            proof = candidate(address)
            service.hub_matches = {"agent-a": address, "agent-b": address}
            service.hub_proofs = {"agent-a": proof, "agent-b": proof}
            service.hub.acknowledge = lambda *_args: True
            service.hub.clear_launch_intent = lambda *_args: None
            cleared = []
            result = type("Result", (), {"response": {"status": "matched"}})()
            with patch.object(service, "_current_hub_state", return_value=(agent, True)), \
                 patch.object(service, "_same_agent_claim", return_value=True), \
                 patch.object(service, "_candidate_matches_agent", return_value=True), \
                 patch.object(service, "_candidate_current", return_value=True), \
                 patch.object(attention, "get_clients", return_value=[]), \
                 patch.object(attention, "active_address", return_value=address), \
                 patch.object(attention, "resolve_agent_window", return_value=result), \
                 patch.object(attention, "resolver_candidate_record_for_window",
                              return_value=proof), \
                 patch.object(attention, "tag_window_with_name",
                              side_effect=lambda *args: cleared.append(args)), \
                 patch.object(attention, "tag_window") as base:
                service._run_focus_revalidation({
                    "identity": "agent-a",
                    "address": address,
                    "agent": agent,
                    "generation": service._resolver_generation,
                })

            self.assertEqual(cleared, [])
            base.assert_not_called()
            self.assertEqual(service.hub_matches, {"agent-b": address})

    def test_disconnect_clears_only_addresses_without_local_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            local_address = "0xaaa"
            hub_only_address = "0xbbb"
            service.windows[local_address] = {"address": local_address}
            service.hub_matches = {
                "source-a:agent": local_address,
                "source-b:agent": hub_only_address,
            }
            operations = []
            with patch.object(attention, "tag_window_with_name",
                              side_effect=lambda *args: operations.append(("named", args))), \
                 patch.object(attention, "tag_window",
                              side_effect=lambda *args: operations.append(("base", args))):
                service.clear_hub_matches()

            self.assertEqual(operations, [
                ("named", (hub_only_address, attention.PULSE_TAG, False)),
                ("base", (hub_only_address, False)),
            ])
            self.assertEqual(service.hub_matches, {})

    def test_local_clear_preserves_hub_owned_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            address = "0xabc"
            service.windows[address] = {
                "address": address, "class": "test", "title": "test",
                "last_attention_at": 1,
            }
            service.write_state()
            service.hub_matches = {"agent-a": address}
            with patch.object(service, "_queue_focus_revalidation"), \
                 patch.object(attention, "client_for", return_value={"address": address}), \
                 patch.object(attention, "tag_window_with_name") as named, \
                 patch.object(attention, "tag_window") as base:
                service.clear(address, "focused")

            named.assert_not_called()
            base.assert_not_called()
            self.assertEqual(service.hub_matches, {"agent-a": address})
            self.assertEqual(service.windows, {})

    def test_native_cli_focus_defers_cleanup_to_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            with patch.object(attention, "client_for", return_value={"address": "0xabc"}), \
                 patch.object(attention, "focus_window") as focus, \
                 patch.object(service, "clear") as clear:
                self.assertTrue(service.open_target("0xabc", native=True))

            focus.assert_called_once_with("0xabc")
            clear.assert_not_called()

    def test_pulse_repairs_each_missing_base_tag_without_redundant_ipc(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            address = "0xabc"
            service.windows[address] = {"address": address, "last_attention_at": 1}
            service.write_state()
            delays = []

            def sleep(delay):
                delays.append(delay)
                if len(delays) == 3:
                    service.running = False

            inventories = [
                [{"address": address, "tags": []}],
                [{"address": address, "tags": [attention.TAG]}],
                [{"address": address, "tags": [attention.PULSE_TAG]}],
            ]
            with patch.object(attention, "get_clients", side_effect=inventories), \
                 patch.object(attention, "tag_window") as base, \
                 patch.object(attention, "tag_window_with_name") as named, \
                 patch.object(attention.time, "sleep", side_effect=sleep):
                service.pulse()

            self.assertEqual(base.call_args_list, [
                unittest.mock.call(address, True),
                unittest.mock.call(address, True),
            ])
            self.assertEqual(named.call_args_list, [
                unittest.mock.call(address, attention.PULSE_TAG, True),
                unittest.mock.call(address, attention.PULSE_TAG, False),
                unittest.mock.call(address, attention.PULSE_TAG, True),
            ])
            self.assertEqual(delays, [1.6, 2.0, 1.6])


if __name__ == "__main__":
    unittest.main()
