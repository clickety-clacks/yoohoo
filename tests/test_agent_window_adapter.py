"""Yoohoo's in-process resolver adapter contract tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_window_adapter_test_module", ROOT / "payload/agent_window_adapter.py"
)
adapter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


def agent(*, machine="gibson", instance="hub-1", pid=42, ticks=99, session="ask", name=None):
    value = {
        "machine": machine,
        "instanceId": instance,
        "id": {"pid": pid, "startTimeTicks": ticks},
        "tmux": {"session": session, "windowIndex": 1, "paneId": "%7"},
    }
    if name is not None:
        value["name"] = name
    return value


class CapturingResolver:
    def __init__(self):
        self.request = None
        self.collector = None

    def resolve(self, request, collector):
        self.request = request
        self.collector = collector
        return {"status": "unresolved", "candidates": [], "reasons": []}


class AdapterTests(unittest.TestCase):
    def test_request_keeps_instance_id_inside_target_identity_and_canonical_ticks(self):
        request = adapter.request_for_agent(
            agent(pid=7, ticks=123),
            [{
                "stableId": "hypr:0xabc", "address": "0xabc", "pid": 100,
                "startTimeTicks": "55",
            }],
            "osanwe",
        )
        self.assertIsNotNone(request)
        self.assertEqual(request["target"]["identity"], {
            "machine": "gibson", "instanceId": "hub-1", "pid": 7,
            "startTimeTicks": "123",
        })
        self.assertEqual(request["target"]["tmux"]["windowIndex"], "1")
        self.assertEqual(request["target"]["tmux"]["paneId"], "%7")

    def test_resolve_uses_bundled_core_in_process(self):
        resolver = CapturingResolver()
        sentinel_collector = object()
        result = adapter.resolve_agent(
            agent(), [], "osanwe", resolver=resolver, collector=sentinel_collector
        )
        self.assertEqual(result.response["status"], "unresolved")
        self.assertIs(resolver.collector, sentinel_collector)
        self.assertEqual(resolver.request["schema"], "agent-window-resolver.request.v1")
        self.assertEqual(resolver.request["target"]["identity"]["instanceId"], "hub-1")

    def test_candidate_window_requires_one_complete_visible_exact_proof(self):
        candidate = {
            "window": {"address": "0xabc"},
            "proof": {"state": "complete", "relation": "visible_exact"},
        }
        self.assertEqual(adapter.candidate_window({
            "status": "matched", "candidates": [candidate]
        }), {"address": "0xabc"})
        self.assertIsNone(adapter.candidate_window({
            "status": "matched", "candidates": [
                {**candidate, "proof": {"state": "complete", "relation": "linked_client"}}
            ]
        }))
        self.assertIsNone(adapter.candidate_window({
            "status": "ambiguous", "candidates": [candidate, candidate]
        }))

    def test_match_request_keeps_display_names_and_titles_without_relation(self):
        request = adapter.request_for_agent(
            agent(name="0_1_9 (ticket patrol)"),
            [{
                "stableId": "hypr:0xabc", "address": "0xabc", "pid": 100,
                "startTimeTicks": "55", "title": "0_1_9 (ticket patrol)",
            }],
            "osanwe", operation="match",
        )
        self.assertEqual(request["operation"], "match")
        self.assertEqual(request["target"]["name"], "0_1_9 (ticket patrol)")
        self.assertNotIn("requestedRelation", request)
        self.assertEqual(request["windows"][0]["title"], "0_1_9 (ticket patrol)")

    def test_match_candidates_allow_many_and_select_active_mru_tie(self):
        def candidate(address, stable):
            return {
                "window": {"address": address, "stableId": stable,
                           "pid": 100, "startTimeTicks": "55"},
                "target": {"identity": {}, "location": {"kind": "tmux"}},
                "match": {"confidence": "medium", "score": 65,
                          "evidence": [{"code": "title", "source": "compositor",
                                        "result": "supports"}],
                          "uncertainty": []},
            }
        response = {"operation": "match", "status": "matched", "candidates": [
            candidate("0xdef", "z"), candidate("0xabc", "a")
        ]}
        chosen = adapter.candidate_record(
            response,
            clients=[{"address": "0xdef", "focusHistoryID": 2}],
            active_address="0xdef",
        )
        self.assertEqual(chosen["window"]["address"], "0xdef")
        self.assertEqual(adapter.candidate_window(response)["address"], "0xdef")
        self.assertEqual(
            adapter.candidate_record_for_window(response, {"window": candidate("0xabc", "a")["window"]})["window"]["address"],
            "0xabc",
        )

    def test_adapter_loads_sibling_bundle_even_with_conflicting_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            conflict = Path(directory) / "agent_window_resolver"
            conflict.mkdir()
            (conflict / "__init__.py").write_text("raise RuntimeError('shadowed')\n")
            env = dict(__import__("os").environ)
            env["PYTHONPATH"] = str(conflict.parent) + ":" + str(ROOT / "payload")
            result = subprocess.run(
                [sys.executable, "-c", "import agent_window_adapter; print(agent_window_adapter.resolver_package.__file__)"],
                cwd=ROOT / "payload", env=env, text=True,
                capture_output=True, check=True,
            )
            self.assertEqual(
                Path(result.stdout.strip()).resolve(),
                (ROOT / "payload/agent_window_resolver/__init__.py").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
