"""Plumbus-only background-worker benchmark with real owned /proc targets.

Usage: python3 benchmark_hub_worker.py /path/to/payload/window-attention

Compositor rows and Hub updates are synthetic; the resolver and Linux
collection are real. No desktop dispatch, SSH, mosh, or tmux is used.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

assert socket.gethostname() == "plumbus"
loader = importlib.machinery.SourceFileLoader("perf_daemon", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
daemon = importlib.util.module_from_spec(spec)
loader.exec_module(daemon)
owned = []
service = None
calls = []
inventories = []
real_resolve = daemon.resolve_agent_window

def resolve(*args, **kwargs):
    calls.append(time.monotonic())
    return real_resolve(*args, **kwargs)

def inventory():
    inventories.append(1)
    return windows

class Hub:
    enabled = True
    connected = True
    config = {"machine": "plumbus"}
    revision = 0

    def __init__(self, agents):
        self.agents = agents

    def pending_agents(self):
        return self.agents

    def snapshot(self):
        return {"revision": self.revision, "agents": self.agents,
                "sources": [{"machine": "fixture.invalid",
                             "health": {"state": "reporting"}}]}

try:
    for _index in range(12):
        owned.append(subprocess.Popen(["/usr/bin/sleep", "60"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL))
    windows = [{"address": hex(100 + index), "stableId": "fixture-" + str(index),
                "pid": process.pid, "class": "com.mitchellh.ghostty",
                "title": "fixture.invalid perf-session-" + str(index),
                "workspace": {"id": 1, "name": "1"}, "focusHistoryID": index}
               for index, process in enumerate(owned)]
    agents = [{"machine": "fixture.invalid", "instanceId": "fixture",
               "id": {"pid": 40000 + index, "startTimeTicks": "1"},
               "name": "perf-session-" + str(index), "harness": "fixture",
               "presence": {"state": "present"},
               "activity": {"state": "needs_attention", "observedAtUnixMs": 1000},
               "tmux": {"session": "perf-session-" + str(index),
                        "windowIndex": "0", "paneId": "%1"}}
              for index in range(2)]
    with tempfile.TemporaryDirectory(prefix="yoohoo-worker-perf-") as state, \
            patch.object(daemon, "load_config", return_value={}), \
            patch.object(daemon, "state_dir", return_value=Path(state)), \
            patch.object(daemon, "get_clients", side_effect=inventory), \
            patch.object(daemon, "active_address", return_value=""), \
            patch.object(daemon, "tag_window"), \
            patch.object(daemon, "tag_window_with_name"), \
            patch.object(daemon, "resolve_agent_window", side_effect=resolve):
        service = daemon.AttentionService()
        service.hub = Hub(agents)
        service._start_resolver_worker()
        worker = service._resolver_thread
        try:
            cpu_start, wall_start = time.process_time(), time.monotonic()
            snapshots = 0
            while time.monotonic() - wall_start < 12:
                service.hub.revision += 1
                service.on_hub_snapshot(service.hub.snapshot(), [])
                snapshots += 1
                time.sleep(0.05)
            cpu = time.process_time() - cpu_start
            wall = time.monotonic() - wall_start
            print(json.dumps({"one_core_percent": 100 * cpu / wall,
                              "cpu_seconds": cpu, "wall_seconds": wall,
                              "resolver_calls": len(calls), "updates": snapshots,
                              "inventory_calls": len(inventories),
                              "matched_windows": len(service.hub_matches)}), flush=True)
        finally:
            service._stop_resolver_worker()
            worker.join(5)
            assert not worker.is_alive(), "owned worker did not stop"
finally:
    for process in owned:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)
