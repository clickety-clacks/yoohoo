"""Isolated Plumbus-only quiet-stream performance reproduction.

Usage: python3 benchmark_hub_stream.py /path/to/agentd_hub.py [quiet_seconds]
"""
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

assert socket.gethostname() == "plumbus"
quiet_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 2
spec = importlib.util.spec_from_file_location("measured_hub", sys.argv[1])
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)
release = threading.Event()
connected = threading.Event()
seen = []
requests = []

def frame(revision):
    return ("event: snapshot\ndata: " + json.dumps({
        "type": "snapshot", "schema": "agentd-hub.snapshot.v1",
        "revision": revision, "agents": [], "sources": [],
    }) + "\n\n").encode()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(frame(1))
            self.wfile.flush()
            connected.set()
            release.wait(quiet_seconds + 5)
            self.wfile.write(frame(2))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
with tempfile.TemporaryDirectory(prefix="yoohoo-perf-state-") as state:
    client = hub.AgentdHub(
        {"agentd_hub": {"enabled": True,
         "url": "http://127.0.0.1:" + str(server.server_port)}},
        Path(state), on_snapshot=lambda snapshot, _alerts: seen.append(snapshot["revision"]),
        status_owner=False,
    )
    try:
        client.start()
        assert connected.wait(3), "no test HTTP connection"
        time.sleep(0.8)
        cpu_start, wall_start = time.process_time(), time.monotonic()
        time.sleep(quiet_seconds)
        cpu_seconds = time.process_time() - cpu_start
        wall_seconds = time.monotonic() - wall_start
        release.set()
        deadline = time.monotonic() + 1
        while 2 not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        print(json.dumps({"cpu_seconds": cpu_seconds,
                          "wall_seconds": wall_seconds,
                          "one_core_percent": 100 * cpu_seconds / wall_seconds,
                          "received_revisions": seen, "connections": len(requests)}))
    finally:
        release.set()
        client.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(2)
