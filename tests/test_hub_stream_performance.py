"""Regression tests for quiet Agentd Hub SSE streams."""

from __future__ import annotations

import http.server
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agentd_hub_stream_performance", ROOT / "payload/agentd_hub.py")
hub = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hub)


def _agent(state: str = "idle", observed: int = 1000) -> dict:
    return {
        "machine": "osanwe",
        "instanceId": "instance-a",
        "id": {"pid": 42, "startTimeTicks": 99},
        "harness": "codex",
        "detectedBy": "proc_comm",
        "presence": {"state": "present", "cause": None},
        "cwd": {"state": "known", "value": "/home/mike", "cause": None},
        "activity": {"state": state, "source": "hook", "observedAtUnixMs": observed},
        "tty": "pts/8",
        "tmux": {"session": "ask", "windowIndex": 1, "windowName": "mike", "paneId": "%21"},
        "name": None,
        "startedAtUnixMs": 900,
    }


def _snapshot(revision: int, state: str = "idle", observed: int = 1000) -> dict:
    return {
        "type": "snapshot",
        "schema": "agentd-hub.snapshot.v1",
        "revision": revision,
        "observedAtUnixMs": observed,
        "sources": [{
            "machine": "osanwe",
            "health": {"state": "reporting", "observedAtUnixMs": observed},
            "instanceId": "instance-a",
            "sourceRevision": revision,
            "sourceObservedAtUnixMs": observed,
            "scan": {"state": "complete", "issues": []},
        }],
        "agents": [_agent(state, observed)],
    }


def _frame(value: dict) -> bytes:
    return ("event: snapshot\ndata: " + json.dumps(value) + "\n\n").encode()


class HubStreamPerformanceTests(unittest.TestCase):
    def _server(self, handler_class):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_quiet_connection_delivers_later_frame_without_reconnect(self):
        first = _frame(_snapshot(1))
        second = _frame(_snapshot(2, "needs_attention", 2000))
        data_start = second.index(b"data: ") + len(b"data: ")
        data_end = second.index(b"\n", data_start)
        split_at = data_start + (data_end - data_start) // 2
        revisions: list[int] = []
        quiet_started = threading.Event()
        second_sent = threading.Event()
        release_handler = threading.Event()
        request_count = 0
        request_lock = threading.Lock()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                nonlocal request_count
                with request_lock:
                    request_count += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                # This is intentionally longer than the old 0.5-second
                # timeout, with the next frame itself split across the quiet
                # interval so the reader must preserve its partial line.
                self.wfile.write(second[:split_at])
                self.wfile.flush()
                quiet_started.set()
                time.sleep(0.8)
                self.wfile.write(second[split_at:])
                self.wfile.flush()
                second_sent.set()
                release_handler.wait(5)

            def log_message(self, *_args):
                pass

        server, thread = self._server(Handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = hub.AgentdHub(
                    {"agentd_hub": {"enabled": True, "url": f"http://127.0.0.1:{server.server_port}"}},
                    Path(directory),
                    lambda snapshot, _alerts: revisions.append(snapshot["revision"]),
                    status_owner=False,
                )
                client.start()
                try:
                    self.assertTrue(quiet_started.wait(2))
                    cpu_started = time.process_time()
                    self.assertTrue(second_sent.wait(3), revisions)
                    cpu_used = time.process_time() - cpu_started
                    self.assertLess(cpu_used, 0.15)
                    self.assertTrue(second_sent.is_set(), revisions)
                    deadline = time.monotonic() + 1
                    while 2 not in revisions and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(revisions[:2], [1, 2])
                    with request_lock:
                        self.assertEqual(request_count, 1)
                finally:
                    client.stop()
        finally:
            release_handler.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_stop_wakes_quiet_reader_promptly(self):
        first = _frame(_snapshot(1))
        connected = threading.Event()
        release_handler = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                connected.set()
                release_handler.wait(5)

            def log_message(self, *_args):
                pass

        server, thread = self._server(Handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = hub.AgentdHub(
                    {"agentd_hub": {"enabled": True, "url": f"http://127.0.0.1:{server.server_port}"}},
                    Path(directory),
                    status_owner=False,
                )
                client.start()
                try:
                    self.assertTrue(connected.wait(2))
                    time.sleep(0.7)
                    started = time.monotonic()
                    client.stop()
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertFalse(client._thread.is_alive())
                finally:
                    if client._thread and client._thread.is_alive():
                        client.stop()
        finally:
            release_handler.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_disconnect_reconnects_and_delivers_next_snapshot(self):
        first = _frame(_snapshot(1))
        second = _frame(_snapshot(2, "needs_attention", 2000))
        revisions: list[int] = []
        second_sent = threading.Event()
        release_handler = threading.Event()
        request_count = 0
        request_lock = threading.Lock()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                nonlocal request_count
                with request_lock:
                    request_count += 1
                    request_number = request_count
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if request_number == 1:
                    self.wfile.write(first)
                    self.wfile.flush()
                    return
                self.wfile.write(second)
                self.wfile.flush()
                second_sent.set()
                release_handler.wait(5)

            def log_message(self, *_args):
                pass

        server, thread = self._server(Handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = hub.AgentdHub(
                    {"agentd_hub": {"enabled": True, "url": f"http://127.0.0.1:{server.server_port}"}},
                    Path(directory),
                    lambda snapshot, _alerts: revisions.append(snapshot["revision"]),
                    status_owner=False,
                )
                client.start()
                try:
                    self.assertTrue(second_sent.wait(4), revisions)
                    deadline = time.monotonic() + 1
                    while 2 not in revisions and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(revisions[:2], [1, 2])
                    with request_lock:
                        self.assertGreaterEqual(request_count, 2)
                finally:
                    client.stop()
        finally:
            release_handler.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_timeout_error_does_not_hot_loop_poisoned_reader(self):
        class PoisonedResponse:
            calls = 0

            def readline(self):
                self.calls += 1
                raise socket.timeout("timed out")

        with tempfile.TemporaryDirectory() as directory:
            client = hub.AgentdHub(
                {"agentd_hub": {"enabled": True, "url": "http://hub"}},
                Path(directory),
                status_owner=False,
            )
            response = PoisonedResponse()
            lines = client._sse_lines(response)
            with self.assertRaises(socket.timeout):
                next(lines)
            self.assertEqual(response.calls, 1)


if __name__ == "__main__":
    unittest.main()
