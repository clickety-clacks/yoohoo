"""Installed regression tests; no compositor, sound, or real window mutations."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

repo_daemon = Path(__file__).resolve().parents[1] / "payload/window-attention"
loader = importlib.machinery.SourceFileLoader(
    "attention", str(repo_daemon if repo_daemon.is_file() else Path.home() / ".local/bin/window-attention"))
spec = importlib.util.spec_from_loader(loader.name, loader)
attention = importlib.util.module_from_spec(spec)
loader.exec_module(attention)


class AttentionTests(unittest.TestCase):
    def test_sound_cooldown_is_shared_between_service_instances(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ), patch.object(attention.subprocess, "Popen") as player, \
             patch.object(attention.threading.Thread, "start"), \
             patch.object(attention.time, "monotonic", return_value=100):
            first, second = attention.AttentionService(), attention.AttentionService()
            for service in (first, second):
                service.config.update(sound_enabled=True, sound_path=__file__, sound_cooldown_ms=1500)
                service.play_sound()
            self.assertEqual(player.call_count, 1)

    def test_shutdown_removes_both_tags_and_pending_entries(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ), patch.object(attention, "tag_window_with_name") as tag:
            service = attention.AttentionService()
            service.windows["0x1"] = {"address": "0x1", "last_attention_at": 1}
            service.write_state()
            service.stop()
            service.cleanup()
            tag.assert_any_call("0x1", attention.TAG, False)
            tag.assert_any_call("0x1", attention.PULSE_TAG, False)
            self.assertEqual(attention.read_state()["windows"], [])

    def setUp(self):
        self.config_patch = patch.object(attention, "load_config", return_value={
            "sound_enabled": False, "history_enabled": True})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_pulse_survives_query_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ):
            service = attention.AttentionService()
            service.windows["0x1"] = {"address": "0x1", "last_attention_at": 1}
            service.write_state()
            cycles = []
            def tick(_):
                cycles.append(True)
                if len(cycles) == 2:
                    service.running = False
            with patch.object(attention, "get_clients", side_effect=[
                OSError("temporary failure"), [{"address": "0x1"}]]), \
                 patch.object(attention, "tag_window_with_name") as tag, \
                 patch.object(attention.time, "sleep", side_effect=tick):
                service.pulse()
                tag.assert_called_once_with("0x1", attention.PULSE_TAG, False)
                self.assertEqual(len(cycles), 2)

    def test_missing_notification_dependency_fails_service(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ), patch.dict("sys.modules", {"dbus": None}):
            service = attention.AttentionService()
            service.notifications()
            self.assertTrue(service.fatal_error)
            self.assertFalse(service.running)

    def test_focused_window_does_not_gain_attention(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ), patch.object(attention, "active_address", return_value="0x1"), \
             patch.object(attention, "tag_window") as tag:
            service = attention.AttentionService()
            service.attend("0x1", "desktop-notification")
            tag.assert_not_called()
            self.assertEqual(attention.read_state()["windows"], [])

    def test_closed_window_is_removed_and_recorded_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ), patch.object(attention, "tag_window_with_name") as tag:
            service = attention.AttentionService()
            service.config["history_enabled"] = True
            service.windows["0x1"] = {"address": "0x1", "class": "test", "title": "test",
                                      "last_attention_at": 1}
            service.write_state()
            service.handle("closewindow>>1")
            self.assertEqual(attention.read_state()["windows"], [])
            self.assertIn('"event":"closed"', service.history_path.read_text())
            tag.assert_not_called()

    def test_restart_rejects_reused_addresses_and_cleans_orphan_tags(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ):
            service = attention.AttentionService()
            service.windows = {"0x1": {"address": "0x1", "stable_id": "old", "last_attention_at": 1},
                               "0x2": {"address": "0x2", "stable_id": "kept", "last_attention_at": 1},
                               "0x3": {"address": "0x3", "stable_id": "focused", "last_attention_at": 1}}
            service.write_state()
            clients = [{"address": "0x1", "stableId": "new", "tags": [attention.PULSE_TAG]},
                       {"address": "0x2", "stableId": "kept", "tags": []},
                       {"address": "0x3", "stableId": "focused", "tags": [attention.TAG]}]
            with patch.object(attention, "get_clients", return_value=clients), \
                 patch.object(attention, "active_address", return_value="0x3"), \
                 patch.object(attention, "tag_window_with_name") as tag:
                service.restore_tags()
                self.assertEqual(set(service.windows), {"0x2"})
                tag.assert_any_call("0x1", attention.PULSE_TAG, False)
                tag.assert_any_call("0x2", attention.TAG, True)
                tag.assert_any_call("0x3", attention.TAG, False)

    def test_sender_mapping_never_guesses_between_windows(self):
        clients = [{"pid": 123, "address": "0x1"},
                   {"pid": 456, "address": "0x2"},
                   {"pid": 456, "address": "0x3"}]
        self.assertEqual(attention.unique_window_for_pid(123, clients), "0x1")
        self.assertEqual(attention.unique_window_for_pid(456, clients), "")
        self.assertEqual(attention.unique_window_for_pid(789, clients), "")
        self.assertEqual(attention.unique_window_for_pid(0, [{"pid": 0, "address": "0x4"}]), "")

    def test_clear_cannot_be_overtaken_by_inflight_pulse(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ):
            service = attention.AttentionService()
            address = "0x123"
            service.windows[address] = {"address": address, "class": "test",
                                        "title": "test", "last_attention_at": 1}
            service.write_state()
            entered = threading.Event()
            release = threading.Event()
            cleared = threading.Event()
            tags = {attention.TAG}

            def tag(addr, name, enabled):
                if enabled:
                    entered.set()
                    self.assertTrue(release.wait(2))
                    tags.add(name)
                else:
                    tags.discard(name)

            def clear():
                service.clear(address, "focused")
                cleared.set()

            def end_pulse(_):
                service.running = False

            with patch.object(attention, "client_for", return_value={"address": address}), \
                 patch.object(attention, "get_clients", return_value=[{"address": address}]), \
                 patch.object(attention, "tag_window_with_name", side_effect=tag), \
                 patch.object(attention.time, "sleep", side_effect=end_pulse):
                pulser = threading.Thread(target=service.pulse)
                pulser.start()
                self.assertTrue(entered.wait(2))
                clearer = threading.Thread(target=clear)
                clearer.start()
                self.assertFalse(cleared.wait(0.05))
                release.set()
                pulser.join(2)
                clearer.join(2)
                self.assertFalse(pulser.is_alive())
                self.assertFalse(clearer.is_alive())
                self.assertTrue(cleared.is_set())
                self.assertEqual(tags, set())
                self.assertEqual(attention.read_state()["windows"], [])


if __name__ == "__main__":
    unittest.main()
