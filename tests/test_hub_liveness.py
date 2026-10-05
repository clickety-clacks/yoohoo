"""Agentd Hub liveness, suspend/resume, and status-reader regressions.

These tests deliberately use a tiny in-process SSE response rather than a
real Hub.  The production integration tests own the real Rust Hub and
logind/private-D-Bus wiring; this file keeps timing and failure-mode coverage
fast and deterministic.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agentd_hub_liveness", ROOT / "payload/agentd_hub.py")
hub = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hub)


def snapshot(*, revision: int = 1) -> dict:
    return {
        "type": "snapshot",
        "schema": "agentd-hub.snapshot.v1",
        "revision": revision,
        "sources": [],
        "agents": [],
    }


def sse(event: str, value: object) -> bytes:
    return (
        f"event: {event}\n".encode()
        + b"data: "
        + json.dumps(value, separators=(",", ":")).encode()
        + b"\n\n"
    )


class FakeSocket:
    def __init__(self, response: "FakeResponse") -> None:
        self.response = response
        self.options: list[tuple[int, int, int]] = []

    def shutdown(self, *_args: object) -> None:
        self.response.release.set()

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def settimeout(self, _value: float | None) -> None:
        return


class _Raw:
    def __init__(self, sock: FakeSocket) -> None:
        self._sock = sock


class _FP:
    def __init__(self, sock: FakeSocket) -> None:
        self.raw = _Raw(sock)


class FakeResponse:
    """Buffered-reader-shaped response with a socket shutdown wakeup."""

    def __init__(self, lines: list[bytes] | None = None, *, flood: bytes | None = None) -> None:
        self.lines = [line for frame in (lines or []) for line in frame.splitlines(keepends=True)]
        self.flood = flood.splitlines(keepends=True) if flood is not None else None
        self.flood_index = 0
        self.release = threading.Event()
        self.socket = FakeSocket(self)
        self.fp = _FP(self.socket)

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.flood is not None and not self.release.is_set():
            time.sleep(0.003)
            line = self.flood[self.flood_index]
            self.flood_index = (self.flood_index + 1) % len(self.flood)
            return line
        self.release.wait(2)
        return b""

    def close(self) -> None:
        self.release.set()


class DribbleResponse(FakeResponse):
    """Never completes an SSE record, simulating a slow/partial peer."""

    def readline(self) -> bytes:
        if not self.release.is_set():
            time.sleep(0.003)
            return b"event: heartbeat"
        return b""


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class HubLivenessTests(unittest.TestCase):
    def make_client(self, directory: str, *, on_snapshot=None, on_disconnect=None):
        return hub.AgentdHub(
            {"agentd_hub": {"enabled": True, "url": "http://hub"}},
            Path(directory),
            on_snapshot=on_snapshot,
            on_disconnect=on_disconnect,
        )

    def run_client(self, client: hub.AgentdHub, opener):
        calls: list[object] = []

        def wrapped(request, **kwargs):
            calls.append(request)
            return opener(len(calls), request, kwargs)

        patches = (
            patch.object(hub.AgentdHub, "_start_sleep_watcher"),
            patch.object(hub.AgentdHub, "_stop_sleep_watcher"),
            patch.object(hub.urllib.request, "urlopen", side_effect=wrapped),
            patch.object(client, "_reconnect_delay", return_value=0.01),
        )
        for item in patches:
            item.start()
        client.start()
        return calls, patches

    @staticmethod
    def stop_patches(patches):
        for item in reversed(patches):
            item.stop()

    def test_initial_heartbeat_is_not_live_and_snapshot_then_heartbeat_is_live(self):
        seen: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory, on_snapshot=lambda value, _alerts: seen.append(value))
            client._accept_heartbeat({"schema": hub.HEARTBEAT_SCHEMA, "atUnixMs": 1})
            self.assertEqual(client.status()["status"], hub.STATUS_CONNECTING)
            self.assertFalse(client.connected)
            self.assertEqual(seen, [])

            first = snapshot()
            second = FakeResponse([sse("snapshot", first), sse("heartbeat", {
                "schema": hub.HEARTBEAT_SCHEMA,
                "atUnixMs": 2,
            })])
            with patch.object(hub.AgentdHub, "_start_sleep_watcher"), \
                 patch.object(hub.AgentdHub, "_stop_sleep_watcher"), \
                 patch.object(hub.urllib.request, "urlopen", return_value=second) as urlopen, \
                 patch.object(hub, "LIVENESS_DEADLINE_SECONDS", 1.0):
                client.start()
                self.assertTrue(wait_for(lambda: client.connected))
                self.assertEqual(client.status()["status"], hub.STATUS_LIVE)
                self.assertEqual(len(seen), 1)
                self.assertIsNotNone(client.status()["lastSeenAtUnixMs"])
                self.assertTrue(all("/snapshot" not in call.args[0].full_url
                                    for call in urlopen.call_args_list))
                self.assertIn((hub.socket.SOL_SOCKET, hub.socket.SO_KEEPALIVE, 1), second.socket.options)
                client.stop()

    def test_heartbeat_only_connection_hits_initial_snapshot_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            response = FakeResponse(flood=sse("heartbeat", {
                "schema": hub.HEARTBEAT_SCHEMA,
                "atUnixMs": 1,
            }))

            def opener(_index, _request, _kwargs):
                return response

            with patch.object(hub, "LIVENESS_DEADLINE_SECONDS", 0.05):
                calls, patches = self.run_client(client, opener)
                try:
                    self.assertTrue(wait_for(lambda: len(calls) >= 2, 1.5))
                    self.assertFalse(client.connected)
                    self.assertIn(client.status()["status"], {
                        hub.STATUS_RECONNECTING, hub.STATUS_STALE,
                    })
                finally:
                    client.stop()
                    self.stop_patches(patches)

    def test_silent_blackhole_becomes_stale_and_retains_cached_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            cached = snapshot()
            client._accept_with_notify(cached, False)
            response = FakeResponse([sse("snapshot", cached)])

            with patch.object(hub, "LIVENESS_DEADLINE_SECONDS", 0.05):
                calls, patches = self.run_client(client, lambda *_args: response)
                try:
                    self.assertTrue(wait_for(lambda: client.status()["status"] == hub.STATUS_STALE, 1.5))
                    self.assertFalse(client.connected)
                    self.assertEqual(client.snapshot(), cached)
                    self.assertGreaterEqual(len(calls), 1)
                finally:
                    client.stop()
                    self.stop_patches(patches)

    def test_malformed_and_unknown_floods_do_not_refresh_deadline(self):
        cases = (
            [b"event: heartbeat\ndata: {}\n\n"],
            None,
            DribbleResponse(),
        )
        with tempfile.TemporaryDirectory() as directory:
            for malformed in cases:
                client = self.make_client(directory)
                if isinstance(malformed, DribbleResponse):
                    response = malformed
                elif malformed is not None:
                    response = FakeResponse(malformed)
                else:
                    response = FakeResponse(flood=b"event: unknown\ndata: ping\n\n")
                with patch.object(hub, "LIVENESS_DEADLINE_SECONDS", 0.05):
                    calls, patches = self.run_client(client, lambda *_args, response=response: response)
                    try:
                        self.assertTrue(wait_for(lambda: len(calls) >= 2, 1.5))
                        self.assertFalse(client.connected)
                    finally:
                        client.stop()
                        self.stop_patches(patches)

    def test_backoff_is_capped_and_resets_only_after_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            completed = FakeResponse([sse("snapshot", snapshot())])
            completed.release.set()
            responses = [OSError("offline"), OSError("offline"), completed, OSError("offline")]
            delays: list[int] = []

            def opener(index, _request, _kwargs):
                result = responses[min(index - 1, len(responses) - 1)]
                if isinstance(result, BaseException):
                    raise result
                return result

            with patch.object(hub.AgentdHub, "_start_sleep_watcher"), \
                 patch.object(hub.AgentdHub, "_stop_sleep_watcher"), \
                 patch.object(client, "_reconnect_delay", side_effect=lambda index: delays.append(index) or 0.01):
                calls: list[object] = []

                def wrapped(request, **kwargs):
                    calls.append(request)
                    return opener(len(calls), request, kwargs)

                urlopen = patch.object(hub.urllib.request, "urlopen", side_effect=wrapped)
                urlopen.start()
                client.start()
                try:
                    self.assertTrue(wait_for(lambda: len(delays) >= 4, 1.5))
                    self.assertEqual(delays[:4], [0, 1, 0, 0])
                finally:
                    client.stop()
                    urlopen.stop()

            with patch.object(hub.random, "uniform", return_value=1.0):
                self.assertEqual(hub.AgentdHub._reconnect_delay(99), hub.DEFAULT_RECONNECT_SECONDS[-1])

    def test_suspend_resume_during_read_reconnects_without_stale_green_state(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            first = FakeResponse([sse("snapshot", snapshot())])
            second = FakeResponse([sse("snapshot", snapshot(revision=2))])

            def opener(index, _request, _kwargs):
                return first if index == 1 else second

            calls, patches = self.run_client(client, opener)
            try:
                self.assertTrue(wait_for(lambda: client.connected))
                client.prepare_for_sleep(True)
                self.assertEqual(client.status()["status"], hub.STATUS_SUSPENDED)
                self.assertFalse(client.connected)
                client.prepare_for_sleep(False)
                self.assertTrue(wait_for(lambda: len(calls) >= 2 and client.connected))
            finally:
                client.stop()
                self.stop_patches(patches)

    def test_suspend_resume_during_blocked_connect_discards_late_response(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            connect_release = threading.Event()
            late = FakeResponse([sse("snapshot", snapshot())])
            fresh = FakeResponse([sse("snapshot", snapshot(revision=2))])

            def opener(index, _request, _kwargs):
                if index == 1:
                    connect_release.wait(2)
                    return late
                return fresh

            calls, patches = self.run_client(client, opener)
            try:
                self.assertTrue(wait_for(lambda: len(calls) == 1))
                client.prepare_for_sleep(True)
                self.assertEqual(client.status()["status"], hub.STATUS_SUSPENDED)
                client.prepare_for_sleep(False)
                connect_release.set()
                self.assertTrue(wait_for(lambda: len(calls) >= 2 and client.connected))
                self.assertNotEqual(client.snapshot().get("revision"), 1)
            finally:
                connect_release.set()
                client.stop()
                self.stop_patches(patches)

    def test_non_owner_status_fails_closed_for_dead_old_future_and_malformed_health(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            now = int(time.time() * 1000)
            owner_ticks = 123
            base = {
                "status": hub.STATUS_LIVE,
                "connected": True,
                "ownerPid": os.getpid(),
                "ownerStartTimeTicks": owner_ticks,
                "atUnixMs": now,
                "lastSnapshotAtUnixMs": now,
                "lastSeenAtUnixMs": now,
            }
            client = hub.AgentdHub(
                {"agentd_hub": {"enabled": True, "url": "http://hub"}},
                path,
                status_owner=False,
            )
            with patch.object(hub, "_process_start_ticks", return_value=owner_ticks), \
                 patch.object(hub, "_pid_alive", side_effect=lambda pid: pid == os.getpid()):
                for update in (
                    {"ownerPid": 99999999},
                    {"atUnixMs": now - 60000, "lastSeenAtUnixMs": now - 60000},
                    {"atUnixMs": now + 1000, "lastSeenAtUnixMs": now + 1000},
                    {"lastSeenAtUnixMs": "not-a-timestamp"},
                    {"status": {"not": "a-string"}},
                ):
                    value = copy.deepcopy(base)
                    value.update(update)
                    path.joinpath("hub-status.json").write_text(json.dumps(value) + "\n")
                    self.assertFalse(client.status()["connected"], update)
                    self.assertNotEqual(client.status()["status"], hub.STATUS_LIVE)

    def test_status_expiry_getter_does_not_stop_reconnect_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(directory)
            client._status = hub.STATUS_LIVE
            client._last_seen_boot = hub._now_boottime() - 100
            value = client.status()
            self.assertEqual(value["status"], hub.STATUS_STALE)
            self.assertFalse(value["connected"])
            self.assertFalse(client._stop.is_set())


if __name__ == "__main__":
    unittest.main()
