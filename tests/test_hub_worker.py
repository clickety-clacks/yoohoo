"""Non-blocking resolver-worker regressions for Hub presentation/ack paths."""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_daemon(name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "payload/window-attention"))
    spec = importlib.util.spec_from_loader(name, loader)
    daemon = importlib.util.module_from_spec(spec)
    loader.exec_module(daemon)
    return daemon


def agent(*, machine="atlas", state="needs_attention"):
    return {
        "machine": machine,
        "instanceId": "worker-instance",
        "id": {"pid": 42, "startTimeTicks": 99},
        "presence": {"state": "present"},
        "activity": {"state": state, "observedAtUnixMs": 1000},
        "tmux": {"session": "ask", "windowIndex": 1, "paneId": "%7"},
    }


def candidate():
    return {
        "window": {
            "stableId": "hypr:0xabc", "address": "0xabc", "pid": 700,
            "startTimeTicks": "55",
        },
        "target": {
            "identity": {
                "machine": "atlas", "instanceId": "worker-instance",
                "pid": 42, "startTimeTicks": "99",
            },
            "location": {"kind": "tmux", "tmux": {
                "session": "ask", "windowIndex": "1", "paneId": "%7",
                "socket": {"kind": "path", "value": "/tmp/tmux"},
            }},
        },
        "proof": {
            "state": "complete", "relation": "visible_exact",
            "evidence": [{"code": "test", "source": "proc", "result": "supports"}],
        },
    }


class FakeHub:
    enabled = True
    connected = True
    config = {"machine": "lumen"}

    def __init__(self, item):
        self.item = item
        self.acked = threading.Event()

    def snapshot(self):
        return {"agents": [self.item], "sources": [{
            "machine": self.item["machine"],
            "health": {"state": "reporting"},
        }]}

    def pending_agents(self):
        return [self.item]

    def acknowledge(self, *_args):
        self.acked.set()
        return True

    def clear_launch_intent(self, *_args):
        pass


class MutableHub(FakeHub):
    def __init__(self, item):
        super().__init__(item)
        self.items = [item]
        self.source_state = "reporting"

    def snapshot(self):
        return {"agents": list(self.items), "sources": [{
            "machine": item["machine"],
            "health": {"state": self.source_state},
        } for item in self.items]}

    def pending_agents(self):
        return list(self.items)

    def acknowledge(self, _identity, captured):
        current = self.items[0]
        if (current.get("activity", {}).get("observedAtUnixMs")
                != captured.get("activity", {}).get("observedAtUnixMs")):
            return False
        self.acked.set()
        return True


class WorkerTests(unittest.TestCase):
    def test_repeated_unchanged_snapshots_do_not_rescan_and_claim_change_does(self):
        daemon = load_daemon("window_attention_worker_fingerprint_test")
        item = agent()
        proof = candidate()
        calls = []
        first_probe = threading.Event()
        second_probe = threading.Event()

        def resolve(current, *_args, **_kwargs):
            calls.append(current["activity"]["observedAtUnixMs"])
            (first_probe if len(calls) == 1 else second_probe).set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700,
                  "class": "Ghostty", "title": "ask", "focusHistoryID": 1}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "_candidate_matches_agent", return_value=True), \
             patch.object(daemon.AttentionService, "_candidate_current", return_value=True), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            try:
                service.on_hub_snapshot(service.hub.snapshot(), [])
                self.assertTrue(first_probe.wait(2), calls)
                for _ in range(20):
                    service.on_hub_snapshot(service.hub.snapshot(), [])
                time.sleep(0.2)
                self.assertEqual(calls, [1000])

                changed = copy.deepcopy(item)
                changed["activity"] = {"state": "needs_attention", "observedAtUnixMs": 2000}
                service.hub.items = [changed]
                service.on_hub_snapshot(service.hub.snapshot(), [])
                self.assertTrue(second_probe.wait(2), calls)
                self.assertEqual(calls, [1000, 2000])
            finally:
                service._stop_resolver_worker()

    def test_unchanged_callbacks_skip_compositor_and_dirty_event_requeries(self):
        daemon = load_daemon("window_attention_worker_desktop_dirty_test")
        item = agent()
        proof = candidate()
        resolver_calls = []
        clients_calls = []
        active_calls = []
        first_probe = threading.Event()
        dirty_probe = threading.Event()
        clients = [{
            "address": "0xabc", "stableId": "hypr:0xabc", "pid": 700,
            "class": "Ghostty", "title": "ask", "focusHistoryID": 1,
        }]

        def get_clients_probe():
            clients_calls.append(1)
            return clients

        def active_probe():
            active_calls.append(1)
            return ""

        def resolve(*_args, **_kwargs):
            resolver_calls.append(1)
            (first_probe if len(resolver_calls) == 1 else dirty_probe).set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", side_effect=get_clients_probe), \
             patch.object(daemon, "active_address", side_effect=active_probe), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "_candidate_matches_agent", return_value=True), \
             patch.object(daemon.AttentionService, "_candidate_current", return_value=True), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            try:
                service._queue_resolver_refresh()
                self.assertTrue(first_probe.wait(2))
                time.sleep(0.1)
                baseline_clients = len(clients_calls)
                baseline_active = len(active_calls)
                for _ in range(20):
                    service.on_hub_snapshot(service.hub.snapshot(), [])
                time.sleep(0.2)
                self.assertEqual(len(clients_calls), baseline_clients)
                self.assertEqual(len(active_calls), baseline_active)
                with patch.object(service, "refresh_window"):
                    service.handle("windowtitle>>0xabc")
                self.assertTrue(dirty_probe.wait(2), resolver_calls)
                self.assertGreater(len(clients_calls), baseline_clients)
                self.assertGreater(len(active_calls), baseline_active)
            finally:
                service._stop_resolver_worker()

    def test_fresh_empty_hub_callbacks_skip_inventory_and_resolver(self):
        daemon = load_daemon("window_attention_worker_empty_fast_path_test")
        inventory_calls = []
        resolver_calls = []

        class EmptyHub:
            enabled = True
            connected = True
            config = {"machine": "lumen"}

            def snapshot(self):
                return {"agents": [], "sources": []}

            def pending_agents(self):
                return []

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", side_effect=lambda: inventory_calls.append(1)), \
             patch.object(daemon, "resolve_agent_window", side_effect=lambda *_args, **_kwargs: resolver_calls.append(1)), \
             patch.object(daemon, "active_address", side_effect=lambda: ""):
            service = daemon.AttentionService()
            service.hub = EmptyHub()
            service._start_resolver_worker()
            try:
                for _ in range(30):
                    service.on_hub_snapshot(service.hub.snapshot(), [])
                time.sleep(0.2)
                self.assertEqual(inventory_calls, [])
                self.assertEqual(resolver_calls, [])
            finally:
                service._stop_resolver_worker()

    def test_nine_agent_refresh_finishes_after_one_finite_sweep_and_merges_cache(self):
        daemon = load_daemon("window_attention_worker_finite_sweep_test")
        items = []
        for index in range(9):
            current = agent()
            current["instanceId"] = f"instance-{index}"
            current["id"] = {"pid": 42 + index, "startTimeTicks": 99 + index}
            items.append(current)
        calls = []
        seen_ninth = threading.Event()
        proof = candidate()

        class Hub(MutableHub):
            def __init__(self):
                super().__init__(items[0])
                self.items = items

        def resolve(current, *_args, **_kwargs):
            calls.append(current["instanceId"])
            if current["instanceId"] == "instance-8":
                seen_ninth.set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "_candidate_matches_agent", return_value=True), \
             patch.object(daemon.AttentionService, "_candidate_current", return_value=True), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = Hub()
            service._start_resolver_worker()
            try:
                service._queue_resolver_refresh()
                self.assertTrue(seen_ninth.wait(2), calls)
                deadline = time.monotonic() + 2
                cache = Path(directory) / "hub-match-cache.json"
                while (not cache.exists() or len(json.loads(cache.read_text())["matches"]) < 9) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(cache.exists())
                self.assertEqual(len(json.loads(cache.read_text())["matches"]), 9)
                time.sleep(0.2)
                self.assertEqual(len(calls), 9)
            finally:
                service._stop_resolver_worker()

    def test_explicit_focus_revalidation_bypasses_passive_fingerprint_skip(self):
        daemon = load_daemon("window_attention_worker_focus_bypass_test")
        item = agent(machine="lumen")
        proof = candidate()
        proof["target"]["identity"]["machine"] = "lumen"
        identity = daemon.agent_identity(item)
        address = "0xabc"
        probe_started = threading.Event()

        def resolve(*_args, **_kwargs):
            probe_started.set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": address, "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=address), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record_for_window", return_value=proof), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = FakeHub(item)
            service.hub_matches[identity] = address
            service.hub_proofs[identity] = proof
            service._resolver_agent_fingerprints = {
                identity: service._resolver_agent_fingerprint(item),
            }
            service._resolver_known_agent_ids = frozenset({identity})
            service._resolver_clients_fingerprint = service._resolver_desktop_fingerprint(
                [{"address": address, "stableId": "hypr:0xabc", "pid": 700}], address,
            )
            service._resolver_last_passive_resolve = time.monotonic()
            service._resolver_retry_at = time.monotonic() + 60
            service._start_resolver_worker()
            try:
                service.clear(address, "focused")
                self.assertTrue(probe_started.wait(2))
            finally:
                service._stop_resolver_worker()

    def test_resolver_relevant_hypr_events_queue_background_refresh(self):
        daemon = load_daemon("window_attention_worker_event_queue_test")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)):
            service = daemon.AttentionService()
            with patch.object(service, "_queue_resolver_refresh") as queue:
                for event in (
                    "openwindow", "openwindowv2", "closewindow", "windowtitle",
                    "windowtitlev2", "movewindow", "movewindowv2", "activewindowv2",
                ):
                    service.handle(f"{event}>>0xabc")
                self.assertEqual(queue.call_count, 8)

    def test_remote_proof_tags_window_without_blocking_snapshot_callback(self):
        daemon = load_daemon("window_attention_worker_remote_test")
        item = agent()
        proof = candidate()
        tagged = threading.Event()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": {"status": "matched"}}
             )()), \
            patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "play_sound"), \
             patch.object(daemon, "tag_window", side_effect=lambda _address, _enabled: tagged.set()), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = FakeHub(item)
            service._start_resolver_worker()
            started = time.monotonic()
            service.on_hub_snapshot(service.hub.snapshot(), [item])
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(tagged.wait(2))
            self.assertEqual(service.hub_matches[daemon.agent_identity(item)], "0xabc")
            service._stop_resolver_worker()

    def test_unrelated_snapshot_does_not_cancel_current_probe(self):
        daemon = load_daemon("window_attention_worker_liveness_test")
        item = agent()
        proof = candidate()
        probe_started = threading.Event()
        release_probe = threading.Event()
        tagged = threading.Event()

        def resolve(*_args, **_kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(2))
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window", side_effect=lambda *_args: tagged.set()), \
             patch.object(daemon, "tag_window_with_name"), \
             patch.object(daemon.AttentionService, "play_sound"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            service._queue_resolver_refresh()
            self.assertTrue(probe_started.wait(2))
            # A heartbeat/snapshot update is not a claim change and must not
            # invalidate the in-flight proof.
            service.on_hub_snapshot(service.hub.snapshot(), [])
            release_probe.set()
            self.assertTrue(tagged.wait(2))
            self.assertEqual(service.hub_matches[daemon.agent_identity(item)], "0xabc")
            service._stop_resolver_worker()

    def test_changed_or_unhealthy_claim_cannot_publish_probe(self):
        daemon = load_daemon("window_attention_worker_stale_test")
        item = agent()
        proof = candidate()
        probe_started = threading.Event()
        release_probe = threading.Event()
        tagged = threading.Event()

        def resolve(*_args, **_kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(2))
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window", side_effect=lambda *_args: tagged.set()), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            service._queue_resolver_refresh()
            self.assertTrue(probe_started.wait(2))
            service.hub.source_state = "not_reached"
            release_probe.set()
            time.sleep(0.15)
            self.assertFalse(tagged.is_set())
            cache = Path(directory) / "hub-match-cache.json"
            if cache.exists():
                self.assertEqual(json.loads(cache.read_text())["matches"], {})
            service._stop_resolver_worker()

    def test_disconnect_discards_inflight_probe_and_cache(self):
        daemon = load_daemon("window_attention_worker_disconnect_test")
        item = agent()
        proof = candidate()
        probe_started = threading.Event()
        release_probe = threading.Event()

        def resolve(*_args, **_kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(2))
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            service._queue_resolver_refresh()
            self.assertTrue(probe_started.wait(2))
            service.clear_hub_matches()
            release_probe.set()
            time.sleep(0.15)
            cache = Path(directory) / "hub-match-cache.json"
            if cache.exists():
                self.assertEqual(json.loads(cache.read_text())["matches"], {})
            self.assertEqual(service.hub_matches, {})
            service._stop_resolver_worker()

    def test_reconnect_with_same_inputs_reproves_after_disconnect_clear(self):
        daemon = load_daemon("window_attention_worker_reconnect_test")
        item = agent()
        proof = candidate()
        calls = []
        first_started = threading.Event()
        second_started = threading.Event()

        def resolve(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                first_started.set()
            else:
                second_started.set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "_candidate_matches_agent", return_value=True), \
             patch.object(daemon.AttentionService, "_candidate_current", return_value=True), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            service._start_resolver_worker()
            try:
                service._queue_resolver_refresh()
                self.assertTrue(first_started.wait(2))
                identity = daemon.agent_identity(item)
                deadline = time.monotonic() + 2
                while service.hub_matches.get(identity) != "0xabc" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(service.hub_matches.get(identity), "0xabc")
                service.clear_hub_matches()
                service._queue_resolver_refresh()
                self.assertTrue(second_started.wait(2), calls)
                self.assertGreaterEqual(len(calls), 2)
            finally:
                service._stop_resolver_worker()

    def test_focus_queue_keeps_only_latest_active_window(self):
        daemon = load_daemon("window_attention_worker_focus_queue_test")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)):
            service = daemon.AttentionService()
            service._queue_focus_revalidation("old", "0xold")
            service._queue_focus_revalidation("new", "0xnew")
            self.assertEqual(set(service._resolver_focus_jobs), {"new"})
            self.assertEqual(service._resolver_focus_jobs["new"]["address"], "0xnew")

    def test_final_inventory_rejects_malformed_stable_id(self):
        daemon = load_daemon("window_attention_worker_stable_id_test")
        proof = candidate()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)):
            service = daemon.AttentionService()
            for malformed in (False, 0, ""):
                clients = [{
                    "address": "0xabc", "stableId": malformed, "pid": 700,
                }]
                self.assertFalse(
                    service._candidate_current(
                        proof, clients, ticks_reader=lambda _pid: "55"
                    )
                )

    def test_refresh_round_robin_eventually_considers_after_first_eight(self):
        daemon = load_daemon("window_attention_worker_round_robin_test")
        items = []
        for index in range(9):
            current = agent()
            current["instanceId"] = f"instance-{index}"
            current["id"] = {"pid": 42 + index, "startTimeTicks": 99 + index}
            items.append(current)
        calls = []
        seen_ninth = threading.Event()
        proof = candidate()

        class Hub(MutableHub):
            def __init__(self):
                super().__init__(items[0])
                self.items = items

        def resolve(item, *_args, **_kwargs):
            calls.append(item["instanceId"])
            if item["instanceId"] == "instance-8":
                seen_ninth.set()
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon.AttentionService, "_candidate_matches_agent", return_value=True), \
             patch.object(daemon.AttentionService, "_candidate_current", return_value=True), \
             patch.object(daemon, "active_address", return_value=""), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = Hub()
            service._start_resolver_worker()
            service._queue_resolver_refresh()
            self.assertTrue(seen_ninth.wait(2), calls)
            service._stop_resolver_worker()

    def test_focused_hub_window_revalidates_before_acknowledgement(self):
        daemon = load_daemon("window_attention_worker_focus_test")
        item = agent(machine="lumen")
        proof = candidate()
        proof["target"]["identity"]["machine"] = "lumen"
        address = "0xabc"
        cleared = threading.Event()

        class FocusHub(FakeHub):
            def pending_agents(self):
                # Production excludes acknowledged claims. Leaving this
                # fake pending lets a later background refresh re-add it.
                return [] if self.acked.is_set() else [self.item]

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": address, "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value=address), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": {"status": "matched"}}
             )()), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window", side_effect=lambda *_args: cleared.set()), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = FocusHub(item)
            identity = daemon.agent_identity(item)
            service.hub_matches[identity] = address
            service.hub_proofs[identity] = proof
            service._start_resolver_worker()
            try:
                service.clear(address, "focused")
                self.assertTrue(service.hub.acked.wait(2))
                self.assertTrue(cleared.wait(2))
                # acknowledge() signals before the remainder of the commit;
                # observe the cache only after its owning lock is released.
                with service.thread_lock:
                    self.assertNotIn(identity, service.hub_matches)
            finally:
                service._stop_resolver_worker()

    def test_focus_revalidation_does_not_ack_newer_activity_epoch(self):
        daemon = load_daemon("window_attention_worker_epoch_test")
        item = agent(machine="lumen")
        proof = candidate()
        proof["target"]["identity"]["machine"] = "lumen"
        probe_started = threading.Event()
        release_probe = threading.Event()
        tags = []

        def resolve(*_args, **_kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(2))
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value="0xabc"), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window", side_effect=lambda *args: tags.append(args)), \
             patch.object(daemon, "tag_window_with_name", side_effect=lambda *args: tags.append(args)):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            identity = daemon.agent_identity(item)
            service.hub_matches[identity] = "0xabc"
            service.hub_proofs[identity] = proof
            service._start_resolver_worker()
            service.clear("0xabc", "focused")
            self.assertTrue(probe_started.wait(2))
            newer = copy.deepcopy(item)
            newer["activity"] = {"state": "needs_attention", "observedAtUnixMs": 2000}
            service.hub.items = [newer]
            release_probe.set()
            time.sleep(0.15)
            self.assertFalse(service.hub.acked.is_set())
            self.assertEqual(service.hub_matches[identity], "0xabc")
            self.assertEqual(tags, [])
            service._stop_resolver_worker()

    def test_focus_queue_captures_epoch_before_worker_slot(self):
        daemon = load_daemon("window_attention_worker_queued_epoch_test")
        item = agent(machine="lumen")
        proof = candidate()
        proof["target"]["identity"]["machine"] = "lumen"
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        operations = []

        def resolve(*_args, **_kwargs):
            operations.append(_kwargs.get("operation"))
            refresh_started.set()
            self.assertTrue(release_refresh.wait(2))
            return type("Result", (), {"response": {"status": "matched"}})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "active_address", return_value="0xabc"), \
             patch.object(daemon, "resolve_agent_window", side_effect=resolve), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "tag_window"), \
             patch.object(daemon, "tag_window_with_name"):
            service = daemon.AttentionService()
            service.hub = MutableHub(item)
            identity = daemon.agent_identity(item)
            service.hub_matches[identity] = "0xabc"
            service.hub_proofs[identity] = proof
            service._start_resolver_worker()
            service._queue_resolver_refresh()
            self.assertTrue(refresh_started.wait(2))
            service.clear("0xabc", "focused")
            newer = copy.deepcopy(item)
            newer["activity"] = {"state": "needs_attention", "observedAtUnixMs": 2000}
            service.hub.items = [newer]
            release_refresh.set()
            time.sleep(0.2)
            self.assertFalse(service.hub.acked.is_set())
            self.assertEqual(service.hub_matches[identity], "0xabc")
            self.assertNotIn("revalidate", operations)
            service._stop_resolver_worker()


if __name__ == "__main__":
    unittest.main()
