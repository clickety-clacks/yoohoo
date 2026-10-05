"""Deterministic regressions for Hub lifecycle cancellation boundaries."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agentd_hub_lifecycle", ROOT / "payload/agentd_hub.py",
)
hub = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hub)


def snapshot(revision: int = 1) -> dict:
    return {
        "type": "snapshot",
        "schema": hub.HUB_SCHEMA,
        "revision": revision,
        "sources": [],
        "agents": [],
    }


class _FakeSource:
    def __init__(self, context: "_FakeContext", attached: threading.Event) -> None:
        self.context = context
        self.attached = attached
        self.callback = None

    def set_priority(self, _priority: int) -> None:
        return

    def set_callback(self, callback) -> None:
        self.callback = callback

    def attach(self, context: "_FakeContext") -> int:
        context.sources.append(self)
        self.attached.set()
        return 1


class _FakeContext:
    def __init__(self) -> None:
        self.sources: list[_FakeSource] = []

    def push_thread_default(self) -> None:
        return

    def pop_thread_default(self) -> None:
        return

    def wakeup(self) -> None:
        return


class _FakeLoop:
    def __init__(
        self,
        context: _FakeContext,
        entered: threading.Event,
        allow_run: threading.Event,
    ) -> None:
        self.context = context
        self.entered = entered
        self.allow_run = allow_run
        self.running = False
        self.stopped = threading.Event()

    def run(self) -> None:
        # This barrier is after AgentdHub's final cancellation check.  A
        # direct quit() before the loop is running is deliberately lost,
        # reproducing GLib's quit-before-run race.
        self.entered.set()
        self.allow_run.wait(2)
        self.running = True
        for source in list(self.context.sources):
            if source.callback is not None:
                source.callback()
        self.stopped.wait(2)
        self.running = False

    def quit(self) -> None:
        if self.running:
            self.stopped.set()


class _FakeBus:
    def __init__(self) -> None:
        self.callback = None
        self.unsubscribed = threading.Event()

    def signal_subscribe(self, *args):
        self.callback = args[-1]
        return 7

    def signal_unsubscribe(self, subscription: int) -> None:
        if subscription == 7:
            self.unsubscribed.set()


class HubLifecycleTests(unittest.TestCase):
    def make_client(self, directory: str, **kwargs):
        return hub.AgentdHub(
            {"agentd_hub": {"enabled": True, "url": "http://hub"}},
            Path(directory),
            **kwargs,
        )

    def test_old_generation_snapshot_has_no_commit_after_suspend_or_stop(self):
        for transition in ("suspend", "stop"):
            with self.subTest(transition=transition), tempfile.TemporaryDirectory() as directory:
                notifications: list[dict] = []
                client = self.make_client(
                    directory,
                    on_snapshot=lambda value, _alerts: notifications.append(value),
                )
                generation = client._connection_generation
                gate = threading.Event()
                result: list[bool] = []

                def accept_after_barrier() -> None:
                    gate.wait(2)
                    result.append(client._accept(snapshot(), generation))

                worker = threading.Thread(target=accept_after_barrier)
                worker.start()
                if transition == "suspend":
                    client.prepare_for_sleep(True)
                else:
                    client._stop.set()
                    with client._lock:
                        client._connection_generation += 1
                gate.set()
                worker.join(2)

                self.assertEqual(result, [False])
                self.assertIsNone(client.snapshot())
                self.assertEqual(notifications, [])
                self.assertFalse(client.snapshot_path.exists())

    def test_sleep_watcher_stop_after_check_before_run_uses_context_source(self):
        entered = threading.Event()
        allow_run = threading.Event()
        attached = threading.Event()
        context = _FakeContext()
        bus = _FakeBus()
        loop = _FakeLoop(context, entered, allow_run)

        glib = types.SimpleNamespace(
            PRIORITY_HIGH=-100,
            SOURCE_REMOVE=False,
            MainContext=types.SimpleNamespace(new=lambda: context),
            MainLoop=types.SimpleNamespace(new=lambda _context, _running: loop),
            idle_source_new=lambda: _FakeSource(context, attached),
        )
        gio = types.SimpleNamespace(
            BusType=types.SimpleNamespace(SYSTEM=1),
            DBusSignalFlags=types.SimpleNamespace(NONE=0),
            bus_get_sync=lambda _bus_type, _cancel: bus,
        )
        repository = types.ModuleType("gi.repository")
        repository.Gio = gio
        repository.GLib = glib
        gi = types.ModuleType("gi")
        gi.repository = repository

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            sys.modules,
            {"gi": gi, "gi.repository": repository},
        ):
            client = self.make_client(directory)
            client._start_sleep_watcher()
            self.assertTrue(entered.wait(2))

            stopped = threading.Event()

            def stop_watcher() -> None:
                client._stop_sleep_watcher()
                stopped.set()

            stopper = threading.Thread(target=stop_watcher)
            stopper.start()
            self.assertTrue(attached.wait(2))
            allow_run.set()
            self.assertTrue(stopped.wait(2))
            stopper.join(2)

            self.assertIsNone(client._sleep_thread)
            self.assertTrue(bus.unsubscribed.is_set())

    def test_persisted_live_requires_snapshot_timestamp_and_connected_true(self):
        now = int(time.time() * 1000)
        cases = (
            ("missing", None, True),
            ("string", "123", True),
            ("boolean", True, True),
            ("disconnected", now, False),
        )
        for name, snapshot_at, connected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                client = self.make_client(directory, status_owner=False)
                raw = {
                    "status": hub.STATUS_LIVE,
                    "connected": connected,
                    "atUnixMs": now,
                    "lastSeenAtUnixMs": now,
                    "ownerPid": 1,
                    "ownerStartTimeTicks": 1,
                }
                if snapshot_at is not None:
                    raw["lastSnapshotAtUnixMs"] = snapshot_at
                client.status_path.write_text(json.dumps(raw), encoding="utf-8")
                with patch.object(hub.AgentdHub, "_owner_alive", return_value=True):
                    status = client.status()
                self.assertEqual(status["status"], hub.STATUS_STALE)
                self.assertFalse(status["connected"])

    def test_duplicate_sleep_edges_do_not_invalidate_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            initial = client._connection_generation
            client.prepare_for_sleep(False)
            self.assertEqual(client._connection_generation, initial)
            client.prepare_for_sleep(True)
            suspended = client._connection_generation
            client.prepare_for_sleep(True)
            self.assertEqual(client._connection_generation, suspended)

    def test_external_callbacks_can_read_hub_from_another_thread(self):
        for callback_kind in ("snapshot", "disconnect"):
            with self.subTest(callback=callback_kind), tempfile.TemporaryDirectory() as directory:
                completed: list[bool] = []
                client_box: list[hub.AgentdHub] = []

                def callback(*_args) -> None:
                    done = threading.Event()

                    def read_hub() -> None:
                        client_box[0].status()
                        client_box[0].snapshot()
                        done.set()

                    reader = threading.Thread(target=read_hub)
                    reader.start()
                    completed.append(done.wait(1))
                    reader.join(1)

                client = self.make_client(
                    directory,
                    on_snapshot=callback if callback_kind == "snapshot" else None,
                    on_disconnect=callback if callback_kind == "disconnect" else None,
                )
                client_box.append(client)
                if callback_kind == "snapshot":
                    self.assertTrue(client._accept(snapshot()))
                else:
                    client.connected = True
                    self.assertTrue(client._mark_disconnected("test_disconnect"))

                self.assertEqual(completed, [True])

    def test_cached_snapshot_age_is_restored_only_for_valid_cache(self):
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "hub-status.json").write_text(json.dumps({
                "lastSnapshotAtUnixMs": now - 10,
                "lastSeenAtUnixMs": now,
            }), encoding="utf-8")
            without_snapshot = self.make_client(directory)
            self.assertIsNone(without_snapshot.status()["lastSnapshotAtUnixMs"])

            (path / "hub-snapshot.json").write_text(
                json.dumps(snapshot()), encoding="utf-8",
            )
            with_snapshot = self.make_client(directory)
            self.assertEqual(
                with_snapshot.status()["lastSnapshotAtUnixMs"], now - 10,
            )


if __name__ == "__main__":
    unittest.main()
