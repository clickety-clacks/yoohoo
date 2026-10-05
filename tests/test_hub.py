"""Agentd Hub contract, matching, acknowledgement, and SSE tests."""

from __future__ import annotations

import copy
import http.server
import io
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
spec = importlib.util.spec_from_file_location("agentd_hub", ROOT / "payload/agentd_hub.py")
hub = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hub)


def load_daemon(name):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "payload/window-attention"))
    module_spec = importlib.util.spec_from_loader(name, loader)
    daemon = importlib.util.module_from_spec(module_spec)
    loader.exec_module(daemon)
    return daemon


def agent(
    *,
    machine="osanwe",
    instance="instance-a",
    pid=42,
    ticks=99,
    state="idle",
    observed=1000,
    session="ask",
    pane="%21",
):
    return {
        "machine": machine,
        "instanceId": instance,
        "id": {"pid": pid, "startTimeTicks": ticks},
        "harness": "codex",
        "detectedBy": "proc_comm",
        "presence": {"state": "present", "cause": None},
        "cwd": {"state": "known", "value": "/home/mike", "cause": None},
        "activity": {"state": state, "source": "hook", "observedAtUnixMs": observed},
        "tty": "pts/8",
        "tmux": {"session": session, "windowIndex": 1, "windowName": "mike", "paneId": pane},
        "name": None,
        "startedAtUnixMs": 900,
    }


def snapshot(*agents, revision=1, source_machine="osanwe", source_health="reporting"):
    return {
        "type": "snapshot",
        "schema": "agentd-hub.snapshot.v1",
        "revision": revision,
        "observedAtUnixMs": 1100,
        "sources": [{
            "machine": source_machine,
            "health": {"state": source_health, "observedAtUnixMs": 1100},
            "instanceId": "instance-a",
            "sourceRevision": revision,
            "sourceObservedAtUnixMs": 1100,
            "scan": {"state": "complete", "issues": []},
        }],
        "agents": list(agents),
    }


class HubTests(unittest.TestCase):
    def test_machine_matching_allows_short_fqdn_but_not_unrelated_domains(self):
        self.assertTrue(hub.machine_matches("gibson", "gibson.tailnet.ts.net"))
        self.assertTrue(hub.machine_matches("gibson.tailnet.ts.net", "gibson"))
        self.assertFalse(hub.machine_matches("gibson.one.example", "gibson.two.example"))

    def test_contract_schema_and_identity_include_machine_instance_and_exact_process(self):
        value = snapshot(agent())
        self.assertEqual(hub.validate_snapshot(value), value)
        self.assertEqual(hub.agent_identity(value["agents"][0]), "osanwe|instance-a|42|99")
        with self.assertRaises(ValueError):
            hub.validate_snapshot({"type": "snapshot", "schema": "agentd.snapshot.v0"})

    def test_sse_parser_handles_comments_multiline_data_and_final_record(self):
        records = list(hub.parse_sse([
            b": heartbeat\n", b"event: snapshot\n", b"id: 7\n",
            b"data: {\n", b"data: \"ok\"\n", b"\n",
            b"data: final\n",
        ]))
        self.assertEqual(records, [("snapshot", "7", '{\n"ok"'), ("message", "", "final")])

    def test_initial_idle_is_silent_but_initial_attention_and_new_idle_are_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}},
                                    Path(directory), lambda _snapshot, alerts: seen.extend(alerts))
            idle = agent(state="idle")
            client._accept_with_notify(snapshot(idle), True)
            self.assertEqual(seen, [])
            attention = agent(state="needs_attention", observed=2000)
            client._accept_with_notify(snapshot(attention, revision=2), True)
            self.assertEqual(len(seen), 1)
            client._accept_with_notify(snapshot(attention, revision=3), True)
            self.assertEqual(len(seen), 1)
            active = agent(state="active", observed=3000)
            client._accept_with_notify(snapshot(active, revision=4), True)
            finished = agent(state="idle", observed=4000)
            client._accept_with_notify(snapshot(finished, revision=5), True)
            self.assertEqual(len(seen), 2)

    def test_cached_active_snapshot_rearms_completion_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            first._accept_with_notify(snapshot(agent(state="active", observed=2000)), False)
            seen = []
            restored = hub.AgentdHub(
                {"agentd_hub": {"enabled": True, "url": "http://hub"}},
                path,
                lambda _snapshot, alerts: seen.extend(alerts),
            )
            restored._accept_with_notify(snapshot(agent(state="idle", observed=3000), revision=2), True)
            self.assertEqual(len(seen), 1)

    def test_acknowledgement_survives_reconstruction_and_new_claim_rearms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            waiting = agent(state="needs_attention", observed=2000)
            client._accept_with_notify(snapshot(waiting), False)
            identity = hub.agent_identity(waiting)
            self.assertIn(identity, {hub.agent_identity(item) for item in client.pending_agents()})
            client.acknowledge(identity, waiting)
            self.assertEqual(client.pending_agents(), [])
            restored = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            restored._accept_with_notify(snapshot(waiting), False)
            self.assertEqual(restored.pending_agents(), [])
            newer = agent(state="needs_attention", observed=3000)
            restored._accept_with_notify(snapshot(newer, revision=2), False)
            self.assertEqual(len(restored.pending_agents()), 1)

    def test_finished_idle_claim_is_persisted_for_the_list_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            client._accept_with_notify(snapshot(agent(state="active", observed=2000)), False)
            finished = agent(state="idle", observed=3000)
            client._accept_with_notify(snapshot(finished, revision=2), False)
            identity = hub.agent_identity(finished)
            restored = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            self.assertIn(identity, {hub.agent_identity(item) for item in restored.pending_agents()})

    def test_concurrent_instances_do_not_erase_unrelated_pending_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            second = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            claim_a = agent(pid=101, state="needs_attention", observed=2000)
            claim_b = agent(pid=202, state="needs_attention", observed=2100)
            first._accept_with_notify(snapshot(claim_a), False)
            second._accept_with_notify(snapshot(claim_b, revision=2), False)
            first.acknowledge(hub.agent_identity(claim_a), claim_a)
            restored = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            self.assertEqual(
                {hub.agent_identity(item) for item in restored.pending_agents()},
                {hub.agent_identity(claim_b)},
            )

    def test_old_ack_cannot_remove_newer_same_identity_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            old_client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            new_client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            old_claim = agent(pid=404, state="needs_attention", observed=2000)
            new_claim = agent(pid=404, state="needs_attention", observed=3000)
            old_client._accept_with_notify(snapshot(old_claim), False)
            new_client._accept_with_notify(snapshot(new_claim, revision=2), False)
            self.assertFalse(old_client.acknowledge(hub.agent_identity(old_claim), old_claim))
            restored = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            self.assertEqual(
                {hub.agent_identity(item) for item in restored.pending_agents()},
                {hub.agent_identity(new_claim)},
            )
            self.assertFalse(restored.is_acknowledged(new_claim))

    def test_ack_does_not_resurrect_pending_removed_by_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stale_cli = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            daemon = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            claim_a = agent(pid=505, state="needs_attention", observed=2000)
            claim_b = agent(pid=606, state="needs_attention", observed=2100)
            stale_cli._accept_with_notify(snapshot(claim_a, claim_b), False)
            complete = snapshot(revision=2)
            complete["observedAtUnixMs"] = 3000
            daemon._accept_with_notify(complete, False)
            self.assertTrue(stale_cli.acknowledge(hub.agent_identity(claim_a), claim_a))
            restored = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            self.assertEqual(restored.pending_agents(), [])

    def test_launch_reservation_is_persistent_and_exact_claim_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            second = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, path)
            claim = agent(pid=303, state="needs_attention", observed=2000)
            identity = hub.agent_identity(claim)
            self.assertTrue(first.reserve_launch(identity, claim))
            self.assertTrue(second.has_launch_intent(identity, claim))
            self.assertFalse(second.reserve_launch(identity, claim))
            newer = agent(pid=303, state="needs_attention", observed=3000)
            self.assertTrue(second.reserve_launch(identity, newer))
            second.clear_launch_intent(identity)
            self.assertFalse(first.has_launch_intent(identity, newer))

    def test_complete_snapshot_removal_clears_pending_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, Path(directory))
            waiting = agent(state="needs_attention", observed=2000)
            client._accept_with_notify(snapshot(waiting), False)
            self.assertEqual(len(client.pending_agents()), 1)
            complete = snapshot(revision=2)
            complete["observedAtUnixMs"] = 3000
            client._accept_with_notify(complete, False)
            self.assertEqual(client.pending_agents(), [])

    def test_source_disconnect_is_not_a_clickable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://hub"}}, Path(directory))
            waiting = agent(state="needs_attention", observed=2000)
            client._accept_with_notify(snapshot(waiting), False)
            self.assertEqual(len(client.pending_agents()), 1)
            offline = snapshot(waiting, revision=2, source_health="not_reached")
            client._accept_with_notify(offline, False)
            # The snapshot keeps the agent as stale data, but the daemon's
            # projection filters it by source health and connection state.
            self.assertEqual(offline["sources"][0]["health"]["state"], "not_reached")

    def test_tmux_pane_and_client_bridge_matches_exact_local_agent(self):
        a = agent(state="needs_attention")
        clients = [{"address": "0x1", "pid": 700, "title": "osanwe:mike",
                    "workspace": {"id": 1, "name": "1"}}]
        panes = [{"session": "ask", "windowIndex": "1", "paneId": "%21", "panePid": 600}]
        tmux_clients = [{"session": "ask", "clientPid": 500, "clientTty": "/dev/pts/8"}]
        ancestry = {42: {42, 600}, 500: {500, 700}}
        with patch.object(hub, "_process_start_ticks", return_value=99):
            matches = hub.match_agent_windows([a], clients, "osanwe",
                                              lambda pid: ancestry.get(pid, set()), lambda _pid: [],
                                              panes, tmux_clients)
        self.assertEqual(matches[hub.agent_identity(a)]["address"], "0x1")

    def test_real_tmux_exact_pane_target_and_parent_topology(self):
        import os
        import subprocess

        socket_name = f"yoohoo-test-{os.getpid()}"
        base = ["tmux", "-L", socket_name, "-f", "/dev/null"]
        try:
            subprocess.run(base + ["new-session", "-d", "-s", "ask", "sh"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pane_id = subprocess.check_output(base + ["display-message", "-p", "#{pane_id}"], text=True).strip()
            subprocess.run(base + ["select-pane", "-t", pane_id], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            selected = subprocess.check_output(base + ["display-message", "-p", "#{pane_id}"], text=True).strip()
            self.assertEqual(selected, pane_id)
            panes = subprocess.check_output(
                base + ["list-panes", "-a", "-F", "#{pane_pid}\t#{pane_id}"], text=True
            ).strip().splitlines()
            self.assertTrue(any(line.endswith("\t" + pane_id) for line in panes))
        finally:
            subprocess.run(base + ["kill-server"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_ambiguous_remote_match_is_not_marked_open(self):
        a = agent(machine="gibson", state="needs_attention")
        clients = [
            {"address": "0x1", "pid": 700},
            {"address": "0x2", "pid": 701},
        ]
        argv = ["ghostty", "-e", "mosh", "--", "gibson", "tmux", "attach", "-t", "ask"]
        self.assertEqual(hub.parse_remote_launch(argv), {"transport": "mosh", "host": "gibson", "session": "ask"})
        self.assertEqual(hub.match_agent_windows([a], clients, "osanwe",
                                                  lambda _pid: set(), lambda _pid: argv), {})

    def test_remote_launch_round_trip_is_matchable(self):
        a = agent(machine="gibson", state="needs_attention", session="ask room")
        plan = {"available": True, "transport": "ssh", "terminal": "/bin/ghostty"}
        launch = hub.build_launch_argv(a, plan, "osanwe")
        self.assertEqual(launch[:6], ["/bin/ghostty", "-e", "ssh", "-tt", "--", "gibson"])
        self.assertIn("sh -lc", launch[-1])
        self.assertIn("=ask room", launch[-1])
        self.assertEqual(hub.parse_remote_launch(launch), {
            "transport": "ssh", "host": "gibson", "session": "ask room"
        })

    def test_mosh_preflight_uses_pty_free_ssh_probe(self):
        a = agent(machine="gibson", state="needs_attention", session="ask room")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("Result", (), {})()

        plan = {"available": True, "transport": "mosh", "terminal": "/bin/ghostty"}
        verified = hub.verify_connection(
            a, plan, "osanwe", runner=runner,
            which=lambda name: "/bin/" + name,
        )
        self.assertEqual(verified["transport"], "mosh")
        self.assertEqual(calls[0][0], "ssh")
        self.assertIn("sh -lc", calls[0][-1])
        self.assertIn("=ask room", calls[0][-1])

    def test_remote_tmux_selector_verifies_exact_pane(self):
        daemon = load_daemon("window_attention_remote_selector_test")
        remote = agent(machine="gibson", session="ask room", state="needs_attention")
        remote["tmux"]["windowIndex"] = "3"
        remote["tmux"]["paneId"] = "%21"
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("Result", (), {"stdout": "%21\n"})()

        self.assertTrue(daemon.select_remote_tmux_pane(
            remote, "ssh", runner=runner, which=lambda name: "/bin/" + name,
        ))
        self.assertEqual(calls[0][:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--"])
        self.assertIn("=ask room:3", calls[0][-1])
        self.assertIn("%21", calls[0][-1])

    def test_manual_focus_proves_visible_local_pane_before_hub_ack(self):
        daemon = load_daemon("window_attention_visibility_test")
        waiting = agent(state="needs_attention", session="ask", pane="%21")
        client = {"address": "0x1", "pid": 700}
        panes = [{"session": "ask", "windowIndex": "1", "paneId": "%21",
                  "paneActive": True, "windowActive": True}]
        tmux_clients = [{"session": "ask", "clientPid": 500}]
        with patch.object(daemon, "process_ancestors", side_effect=lambda pid: {pid, 700} if pid == 500 else {pid}):
            self.assertTrue(daemon.hub_agent_is_visible(
                waiting, client, panes, tmux_clients, "osanwe",
            ))
        panes[0]["paneActive"] = False
        self.assertFalse(daemon.hub_agent_is_visible(
            waiting, client, panes, tmux_clients, "osanwe",
        ))

    def test_connection_prefers_mosh_and_launch_uses_exact_target_and_shell_quoting(self):
        a = agent(machine="gibson", state="needs_attention", session="ask room")
        plan = hub.connection_plan("gibson", "ask room", "osanwe", [],
                                   which=lambda name: "/bin/" + name)
        self.assertEqual((plan["transport"], plan["fallback"]), ("mosh", "ssh"))
        argv = hub.build_launch_argv(a, plan, "osanwe")
        # mosh starts inside the shared fallback launcher; the host and the
        # remote command travel as positional arguments, never as script text.
        self.assertEqual(argv[:4], ["/bin/ghostty", "-e", "sh", "-lc"])
        self.assertEqual(argv[4], hub.TRANSPORT_LAUNCH_SCRIPT)
        self.assertEqual(argv[5:7], ["transport-launch", "gibson"])
        self.assertEqual(argv[-1], "mosh")
        self.assertIn("=ask room", argv[7])
        self.assertNotIn(";", argv[7])
        bad = copy.deepcopy(a)
        bad["machine"] = "gibson;touch /tmp/pwned"
        self.assertIsNone(hub.build_launch_argv(bad, plan, "osanwe"))

    def test_local_sse_fixture_delivers_initial_and_followup_frames(self):
        first = snapshot(agent(state="idle"), revision=1)
        second = snapshot(agent(state="needs_attention", observed=2000), revision=2)
        payload = (
            "event: snapshot\nid: 1\ndata: " + json.dumps(first) + "\n\n"
            "event: snapshot\nid: 2\ndata: " + json.dumps(second) + "\n\n"
        ).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                time.sleep(0.1)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                seen = []
                client = hub.AgentdHub({"agentd_hub": {"enabled": True,
                                                        "url": f"http://127.0.0.1:{server.server_port}"}},
                                        Path(directory), lambda _snapshot, alerts: seen.extend(alerts))
                client.start()
                deadline = time.time() + 2
                while len(seen) < 1 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(seen), 1)
                self.assertTrue(client.snapshot())
                client.stop()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_quiet_sse_stream_stays_connected_until_stop(self):
        first = snapshot(agent(state="idle"), revision=1)

        class QuietHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(("event: snapshot\ndata: " + json.dumps(first) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(2)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = hub.AgentdHub({"agentd_hub": {"enabled": True,
                                                        "url": f"http://127.0.0.1:{server.server_port}"}},
                                        Path(directory))
                client.start()
                deadline = time.time() + 1
                while not client.connected and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(client.connected)
                started = time.monotonic()
                client.stop()
                self.assertLess(time.monotonic() - started, 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_remote_click_does_not_ack_when_pre_attach_verification_fails(self):
        daemon = load_daemon("window_attention_hub_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            def fetch_snapshot(self): return True
            def snapshot(self): return snapshot(waiting)
            def pending_agents(self): return [waiting]
            def acknowledge(self, *_args): self.acknowledged = True
            def has_launch_intent(self, *_args): return False
            def clear_launch_intent(self, *_args): pass

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": {
                     "status": "unresolved",
                     "reasons": [{"code": "candidate_count"}],
                 }}
             )()), \
             patch.object(daemon, "verify_agent_target", return_value=type(
                 "Result", (), {"response": {"status": "unresolved"}}
             )()), \
             patch.object(daemon, "connection_plan", return_value={"available": True, "transport": "ssh"}), \
             patch.object(daemon, "verify_connection", return_value=None), \
             patch.object(daemon.subprocess, "Popen") as popen:
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            self.assertFalse(hasattr(service.hub, "acknowledged"))
            popen.assert_not_called()

    def test_unresolved_resolver_reason_is_retained_for_action_feedback(self):
        daemon = load_daemon("window_attention_action_reason_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}

            def fetch_snapshot(self): return True
            def snapshot(self): return snapshot(waiting)
            def pending_agents(self): return [waiting]

        response = {
            "status": "unresolved",
            "reasons": [{
                "code": "window_collection_incomplete",
                "source": "compositor",
                "message": "the compositor inventory could not be completed",
                "retryable": True,
            }],
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": response}
             )()):
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            self.assertEqual(service.last_action_failure, {
                "status": "unresolved",
                "reason": {
                    "code": "window_collection_incomplete",
                    "source": "compositor",
                    "message": "the compositor inventory could not be completed",
                    "retryable": True,
                },
            })

    def test_cli_open_emits_bounded_structured_action_failure(self):
        daemon = load_daemon("window_attention_cli_reason_test")

        class FakeService:
            last_action_failure = {
                "status": "unresolved",
                "reason": {
                    "code": "window_collection_incomplete",
                    "source": "compositor",
                    "message": "x" * 2000,
                    "retryable": True,
                },
            }

            def open_target(self, target):
                self.target = target
                return False

        service = FakeService()
        stderr = io.StringIO()
        with patch.object(daemon, "AttentionService", return_value=service), \
             patch.object(daemon.sys, "argv", ["window-attention", "open", "agent-1"]), \
             patch.object(daemon.sys, "stderr", stderr):
            self.assertEqual(daemon.main(), 1)
        self.assertEqual(service.target, "agent-1")
        diagnostic = json.loads(stderr.getvalue())
        self.assertEqual(diagnostic["error"], "action_failed")
        self.assertEqual(diagnostic["operation"], "open")
        self.assertEqual(diagnostic["target"], "agent-1")
        self.assertEqual(diagnostic["status"], "unresolved")
        self.assertEqual(diagnostic["reason"]["code"], "window_collection_incomplete")
        self.assertEqual(len(diagnostic["reason"]["message"]), 512)
        self.assertTrue(diagnostic["reason"]["retryable"])

    def test_unreadable_compositor_inventory_cannot_start_attach(self):
        daemon = load_daemon("window_attention_inventory_failure_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            def fetch_snapshot(self): return True
            def snapshot(self): return snapshot(waiting, source_machine="gibson")
            def pending_agents(self): return [waiting]
            def acknowledge(self, *_args): self.acknowledged = True
            def has_launch_intent(self, *_args): return False
            def clear_launch_intent(self, *_args): pass

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", side_effect=OSError("hypr unavailable")) as clients, \
             patch.object(daemon, "resolve_agent_window") as resolve, \
             patch.object(daemon, "verify_agent_target") as verify, \
             patch.object(daemon, "connection_plan") as plan, \
             patch.object(daemon, "verify_connection") as verify_connection, \
             patch.object(daemon.subprocess, "Popen") as popen:
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            clients.assert_called_once_with()
            resolve.assert_not_called()
            verify.assert_not_called()
            plan.assert_not_called()
            verify_connection.assert_not_called()
            popen.assert_not_called()

    def test_inventory_failure_after_target_verification_cannot_start_attach(self):
        daemon = load_daemon("window_attention_inventory_recheck_failure_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            def fetch_snapshot(self): return True
            def snapshot(self): return snapshot(waiting, source_machine="gibson")
            def pending_agents(self): return [waiting]
            def acknowledge(self, *_args): self.acknowledged = True
            def has_launch_intent(self, *_args): return False
            def clear_launch_intent(self, *_args): pass

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", side_effect=[[], OSError("hypr unavailable")]), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": {
                     "status": "unresolved",
                     "reasons": [{"code": "candidate_count"}],
                 }}
             )()), \
             patch.object(daemon, "verify_agent_target", return_value=type(
                 "Result", (), {"response": {"status": "verified"}}
             )()), \
             patch.object(daemon, "connection_plan") as plan, \
             patch.object(daemon, "verify_connection") as verify_connection, \
             patch.object(daemon.subprocess, "Popen") as popen:
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            plan.assert_not_called()
            verify_connection.assert_not_called()
            popen.assert_not_called()

    def test_click_does_not_ack_newer_same_process_attention_epoch(self):
        daemon = load_daemon("window_attention_click_epoch_test")
        active_patch = patch.object(daemon, "active_address", return_value="0xabc")
        active_patch.start()
        self.addCleanup(active_patch.stop)
        waiting = agent(machine="gibson", state="needs_attention", observed=1000)
        newer = copy.deepcopy(waiting)
        newer["activity"] = {"state": "needs_attention", "observedAtUnixMs": 2000}
        proof = {
            "window": {
                "stableId": "hypr:0xabc", "address": "0xabc", "pid": 700,
                "startTimeTicks": "55",
            },
            "target": {
                "identity": {
                    "machine": "gibson", "instanceId": "instance-a",
                    "pid": 42, "startTimeTicks": "99",
                },
                "location": {"kind": "tmux", "tmux": {
                    "session": "ask", "windowIndex": "1", "paneId": "%21",
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
            config = {"machine": "osanwe"}

            def __init__(self):
                self.current = waiting
                self.fetches = 0
                self.acknowledged = False
                self.captured = None

            def fetch_snapshot(self):
                self.fetches += 1
                if self.fetches >= 2:
                    self.current = newer
                return True

            def snapshot(self):
                return snapshot(self.current, source_machine="gibson")

            def pending_agents(self):
                return [self.current]

            def acknowledge(self, _identity, captured):
                self.captured = captured
                if (captured.get("activity", {}).get("observedAtUnixMs")
                        != self.current.get("activity", {}).get("observedAtUnixMs")):
                    return False
                self.acknowledged = True
                return True

            def clear_launch_intent(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[
                 {"address": "0xabc", "stableId": "hypr:0xabc", "pid": 700}
             ]), \
             patch.object(daemon, "resolver_process_start_ticks", return_value="55"), \
             patch.object(daemon, "resolve_agent_window", return_value=type(
                 "Result", (), {"response": {"status": "matched"}}
             )()), \
             patch.object(daemon, "resolver_candidate_record", return_value=proof), \
             patch.object(daemon, "focus_window") as focus, \
             patch.object(daemon, "tag_window_with_name") as tag:
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            self.assertIs(service.hub.captured, waiting)
            self.assertFalse(service.hub.acknowledged)
            focus.assert_called_once_with("0xabc")
            tag.assert_not_called()

    def test_source_disconnect_rejects_click_before_ack(self):
        daemon = load_daemon("window_attention_disconnect_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)

        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            def fetch_snapshot(self): return True
            def snapshot(self): return snapshot(waiting, source_machine="gibson", source_health="not_reached")
            def pending_agents(self): return [waiting]
            def acknowledge(self, *_args): self.acknowledged = True

        with tempfile.TemporaryDirectory() as directory, patch.object(daemon, "state_dir", return_value=Path(directory)):
            service = daemon.AttentionService()
            service.hub = FakeHub()
            self.assertFalse(service.open_target(hub.agent_identity(waiting)))
            self.assertFalse(hasattr(service.hub, "acknowledged"))

    def test_cli_projection_keeps_disconnected_rows_visible_but_not_clickable(self):
        daemon = load_daemon("window_attention_projection_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)
        source_snapshot = snapshot(waiting, source_machine="gibson", source_health="reporting")

        class FakeHub:
            enabled = True
            connected = False
            config = {"machine": "osanwe"}
            def snapshot(self): return source_snapshot
            def pending_agents(self): return [waiting]

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "local_tmux_metadata", return_value=([], [])):
            service = daemon.AttentionService()
            service.hub = FakeHub()
            rows = service.hub_rows({"0x1"})
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["machine"], "gibson")
            self.assertFalse(rows[0]["connection_available"])
            self.assertEqual(rows[0]["address"], "")
            self.assertEqual(rows[0]["unavailable_reason"], "hub_disconnected")

    def test_matched_window_stays_activatable_without_launch_tools(self):
        daemon = load_daemon("window_attention_match_projection_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)
        source_snapshot = snapshot(waiting, source_machine="gibson", source_health="reporting")
        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            def snapshot(self): return source_snapshot
            def pending_agents(self): return [waiting]

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[{
                 "address": "0x1", "pid": 700, "title": "mosh",
                 "workspace": {"id": 2, "name": "2"},
             }]), \
             patch.object(daemon, "connection_plan", return_value={"available": False, "reason": "ghostty_unavailable"}):
            service = daemon.AttentionService()
            service.hub = FakeHub()
            service._write_hub_match_cache({hub.agent_identity(waiting): {
                "window": {
                    "stableId": "hypr:0x1", "address": "0x1", "pid": 700,
                    "startTimeTicks": "55", "class": "ghostty",
                },
                "target": {
                    "identity": {
                        "machine": "gibson", "instanceId": "instance-a",
                        "pid": 42, "startTimeTicks": "99",
                    },
                    "location": {"kind": "tmux", "tmux": {
                        "session": "ask", "windowIndex": "1", "paneId": "%21",
                        "socket": {"kind": "path", "value": "/tmp/tmux"},
                    }},
                },
                "proof": {"state": "complete", "relation": "visible_exact",
                          "evidence": [{"code": "test", "source": "caller",
                                        "result": "informational"}]},
            }})
            rows = service.hub_rows({"0x1"})
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["open_on_machine"])
            self.assertTrue(rows[0]["connection_available"])
            self.assertEqual(rows[0]["address"], "0x1")

    @staticmethod
    def _native_menu_row(address="0x1", **overrides):
        row = {
            "address": address,
            "stable_id": "hypr:" + address,
            "class": "ghostty",
            "title": "review",
            "window_title": "review · terminal",
            "workspace_id": 2,
            "workspace": "2",
            "first_attention_at": 10.0,
            "last_attention_at": 20.0,
            "count": 3,
            "source": "native-urgency",
        }
        row.update(overrides)
        return row

    @staticmethod
    def _hub_menu_row(identity="gibson|instance-a|42|99", address="0x1", **overrides):
        row = {
            "kind": "agent",
            "id": identity,
            "address": address,
            "stable_id": identity,
            "class": "agentd-hub",
            "title": "agent review",
            "window_title": "hub window",
            "workspace_id": None,
            "workspace": "",
            "first_attention_at": 30.0,
            "last_attention_at": 30.0,
            "count": 1,
            "source": "agentd-hub",
            "machine": "gibson",
            "open_on_machine": True,
            "connection_available": True,
            "activity": "needs_attention",
            "harness": "codex",
            "agent": {"id": identity},
        }
        row.update(overrides)
        return row

    def test_list_path_merges_exact_duplicate_and_keeps_hub_activation_identity(self):
        daemon = load_daemon("window_attention_row_merge_list_test")
        native = self._native_menu_row()
        hub_row = self._hub_menu_row()

        class FakeHub:
            def status(self):
                return {"status": "live"}

        class FakeService:
            def __init__(self):
                self.hub = FakeHub()

            def hub_rows(self, _addresses):
                return [hub_row]

        with patch.object(daemon, "AttentionService", FakeService), \
             patch.object(daemon, "read_state", return_value={"windows": [native]}), \
             patch.object(daemon, "enriched_state", side_effect=lambda value: value), \
             patch.object(daemon.sys, "argv", ["window-attention", "list"]), \
             patch("builtins.print") as output:
            self.assertEqual(daemon.main(), 0)

        payload = json.loads(output.call_args.args[0])
        self.assertEqual(len(payload["windows"]), 1)
        row = payload["windows"][0]
        self.assertEqual(row["kind"], "agent")
        self.assertEqual(row["id"], hub_row["id"])
        self.assertEqual(row["workspace"], native["workspace"])
        self.assertEqual(row["workspace_id"], native["workspace_id"])
        self.assertEqual(row["first_attention_at"], native["first_attention_at"])
        self.assertEqual(row["last_attention_at"], native["last_attention_at"])
        self.assertEqual(row["count"], native["count"])
        self.assertEqual(row["window_title"], native["window_title"])
        self.assertTrue(row["open_on_machine"])

    def test_merge_does_not_match_distinct_addresses_with_same_title(self):
        daemon = load_daemon("window_attention_row_merge_title_test")
        rows = daemon.merge_attention_rows(
            [self._native_menu_row("0x1", title="same title")],
            [self._hub_menu_row(address="0x2", title="same title")],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["address"] for row in rows}, {"0x1", "0x2"})

    def test_merge_does_not_use_empty_addresses_as_duplicate_keys(self):
        daemon = load_daemon("window_attention_row_merge_empty_test")
        native = self._native_menu_row("0x1")
        rows = daemon.merge_attention_rows(
            [native],
            [self._hub_menu_row(address="", title="stale agent")],
        )
        self.assertEqual(len(rows), 2)
        self.assertIn(native, rows)
        self.assertEqual(rows[-1]["address"], "")

    def test_merge_keeps_distinct_agents_that_share_one_window(self):
        daemon = load_daemon("window_attention_row_merge_agents_test")
        rows = daemon.merge_attention_rows(
            [self._native_menu_row("0x1")],
            [
                self._hub_menu_row("gibson|instance-a|42|99", "0x1"),
                self._hub_menu_row("gibson|instance-a|43|100", "0x1"),
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["id"] for row in rows], [
            "gibson|instance-a|42|99", "gibson|instance-a|43|100",
        ])
        self.assertTrue(all(row["kind"] == "agent" for row in rows))

    def test_merge_keeps_native_when_hub_match_is_not_connection_available(self):
        daemon = load_daemon("window_attention_row_merge_unavailable_test")
        native = self._native_menu_row("0x1")
        for unavailable in (
            {"connection_available": False},
            {"open_on_machine": False},
        ):
            rows = daemon.merge_attention_rows(
                [native], [self._hub_menu_row(address="0x1", **unavailable)],
            )
            self.assertEqual(len(rows), 2)
            self.assertIn(native, rows)

    def test_merge_does_not_replace_useful_hub_metadata_with_blank_native_fields(self):
        daemon = load_daemon("window_attention_row_merge_metadata_test")
        native = self._native_menu_row("0x1", workspace_id=None, workspace="", window_title="")
        hub_row = self._hub_menu_row(
            address="0x1", workspace_id=7, workspace="cached", window_title="agent terminal",
        )
        rows = daemon.merge_attention_rows([native], [hub_row])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace_id"], 7)
        self.assertEqual(rows[0]["workspace"], "cached")
        self.assertEqual(rows[0]["window_title"], "agent terminal")

    def test_native_focus_dispatch_failure_preserves_attention(self):
        daemon = load_daemon("window_attention_focus_test")
        with tempfile.TemporaryDirectory() as directory, patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "client_for", return_value={"address": "0x1"}), \
             patch.object(daemon, "focus_window", side_effect=OSError("focus failed")), \
             patch.object(daemon.AttentionService, "clear") as clear:
            service = daemon.AttentionService()
            self.assertFalse(service.open_target("0x1", native=True))
            clear.assert_not_called()

    def _projection_service(self, daemon, waiting, *, connected=True, clients=None):
        source_snapshot = snapshot(waiting, source_machine=waiting["machine"], source_health="reporting")
        class FakeHub:
            enabled = True
            config = {"machine": "osanwe"}
            def __init__(self, connected): self.connected = connected
            def snapshot(self): return source_snapshot
            def pending_agents(self): return [waiting]
        service = daemon.AttentionService()
        service.hub = FakeHub(connected)
        return service

    def test_projection_hides_agent_without_window_or_tmux_session(self):
        daemon = load_daemon("window_attention_projection_sessionless_test")
        waiting = agent(machine="osanwe", state="needs_attention", observed=2000)
        waiting["tmux"] = None
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]):
            service = self._projection_service(daemon, waiting)
            self.assertEqual(service.hub_rows({"0x1"}), [])
            # The same claim stays hidden while the Hub is unreachable: there
            # is still nothing a user could do with it.
            service.hub.connected = False
            self.assertEqual(service.hub_rows({"0x1"}), [])

    def test_projection_keeps_agent_with_tmux_session_but_no_window(self):
        daemon = load_daemon("window_attention_projection_session_only_test")
        waiting = agent(machine="gibson", state="needs_attention", observed=2000)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[]), \
             patch.object(daemon, "connection_plan", return_value={"available": True, "transport": "mosh", "terminal": "ghostty"}):
            service = self._projection_service(daemon, waiting)
            rows = service.hub_rows({"0x1"})
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["open_on_machine"])
            self.assertTrue(rows[0]["connection_available"])

    def test_projection_keeps_sessionless_agent_with_matched_window(self):
        daemon = load_daemon("window_attention_projection_sessionless_match_test")
        waiting = agent(machine="osanwe", state="needs_attention", observed=2000)
        waiting["tmux"] = None
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "get_clients", return_value=[{
                 "address": "0x1", "pid": 700, "title": "Omarchy Ask #17",
                 "workspace": {"id": 2, "name": "2"},
             }]):
            service = self._projection_service(daemon, waiting)
            service._write_hub_match_cache({hub.agent_identity(waiting): {
                "window": {
                    "stableId": "hypr:0x1", "address": "0x1", "pid": 700,
                    "startTimeTicks": "55", "class": "org.quickshell",
                },
                "target": {
                    "identity": {
                        "machine": "osanwe", "instanceId": "instance-a",
                        "pid": 42, "startTimeTicks": "99",
                    },
                    "location": {"kind": "local"},
                },
                "proof": {"state": "complete", "relation": "visible_exact",
                          "evidence": [{"code": "test", "source": "caller",
                                        "result": "informational"}]},
            }})
            rows = service.hub_rows({"0x1"})
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["open_on_machine"])
            self.assertEqual(rows[0]["address"], "0x1")

    def test_hub_alert_sound_is_silent_for_unpresentable_claim(self):
        daemon = load_daemon("window_attention_alert_sound_test")
        silent = agent(machine="osanwe", state="needs_attention", observed=2000)
        silent["tmux"] = None
        audible = agent(machine="gibson", instance="instance-b", pid=43, state="needs_attention", observed=2000)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)):
            service = daemon.AttentionService()
            with patch.object(service, "play_sound") as play, \
                 patch.object(service, "_queue_resolver_refresh"):
                service.on_hub_snapshot({}, [silent])
                play.assert_not_called()
                service.on_hub_snapshot({}, [audible])
                play.assert_called_once()
                play.reset_mock()
                service.hub_matches[hub.agent_identity(silent)] = "0x1"
                service.on_hub_snapshot({}, [silent])
                play.assert_called_once()

    def test_dismiss_acknowledges_pending_hub_claim_without_opening(self):
        daemon = load_daemon("window_attention_dismiss_hub_test")
        waiting = agent(machine="osanwe", state="idle", observed=2000)
        waiting["tmux"] = None
        identity = hub.agent_identity(waiting)
        class FakeHub:
            enabled = True
            connected = True
            config = {"machine": "osanwe"}
            acknowledged = None
            cleared = None
            def pending_agents(self): return [waiting]
            def acknowledge(self, target, agent_claim):
                self.acknowledged = (target, agent_claim); return True
            def clear_launch_intent(self, target): self.cleared = target
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "focus_window") as focus, \
             patch.object(daemon, "launch_agent") as launch:
            service = daemon.AttentionService()
            service.hub = FakeHub()
            service.hub_pending[identity] = waiting
            self.assertTrue(service.dismiss(identity))
            self.assertEqual(service.hub.acknowledged, (identity, waiting))
            self.assertEqual(service.hub.cleared, identity)
            self.assertNotIn(identity, service.hub_pending)
            focus.assert_not_called()
            launch.assert_not_called()
            # An identity that is no longer pending is already dismissed.
            service.hub.acknowledged = None
            self.assertTrue(service.dismiss("osanwe|gone|1|2"))
            self.assertIsNone(service.hub.acknowledged)
            self.assertFalse(service.dismiss(""))

    def test_dismiss_clears_native_row_by_address_without_focus(self):
        daemon = load_daemon("window_attention_dismiss_native_test")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daemon, "state_dir", return_value=Path(directory)), \
             patch.object(daemon, "focus_window") as focus, \
             patch.object(daemon.AttentionService, "clear") as clear:
            service = daemon.AttentionService()
            self.assertTrue(service.dismiss("0x1"))
            clear.assert_called_once_with("0x1", "dismissed")
            focus.assert_not_called()

    def test_dismiss_cli_reports_failure_on_stderr(self):
        daemon = load_daemon("window_attention_dismiss_cli_test")
        with patch.object(daemon.AttentionService, "dismiss", return_value=False), \
             patch.object(daemon.AttentionService, "__init__", return_value=None), \
             patch.object(daemon.sys, "argv", ["window-attention", "dismiss", "osanwe|x|1|2"]), \
             patch("builtins.print") as output:
            self.assertEqual(daemon.main(), 1)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["operation"], "dismiss")
        self.assertEqual(payload["error"], "action_failed")


if __name__ == "__main__":
    unittest.main()
