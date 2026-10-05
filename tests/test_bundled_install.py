"""Installer smoke tests for Yoohoo's bundled resolver library."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("yoohoo_installer_smoke", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(installer)

EXPECTED_RESOLVER = {
    "__init__.py", "__main__.py", "cli.py", "collector.py",
    "linux.py", "model.py", "resolver.py", "transports.py",
}


def git_blob_id(data: bytes) -> str:
    """The id git gives this content, as recorded by scripts/sync-resolver.py."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class BundledInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yoohoo-bundled-install-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        hypr = self.home / ".config/hypr/hyprland.lua"
        shell = self.home / ".config/omarchy/shell.json"
        hypr.parent.mkdir(parents=True)
        shell.parent.mkdir(parents=True)
        hypr.write_text("-- test configuration\n")
        shell.write_text(json.dumps({"bar": {"layout": {"left": [], "right": []}}}))
        installer.install(self.home, False)

    def test_exact_resolver_package_is_installed_byte_for_byte(self):
        source = ROOT / "payload/agent_window_resolver"
        installed = self.home / ".local/share/window-attention/agent_window_resolver"
        self.assertEqual(
            {path.name for path in installed.glob("*.py")}, EXPECTED_RESOLVER
        )
        self.assertEqual(
            {path.name for path in installed.iterdir()}, EXPECTED_RESOLVER
        )
        for name in EXPECTED_RESOLVER:
            self.assertEqual((installed / name).read_bytes(), (source / name).read_bytes())
        self.assertTrue(
            (self.home / ".local/share/window-attention/agent_window_adapter.py").is_file()
        )

    def test_vendored_copy_is_the_recorded_upstream_commit(self):
        source = ROOT / "payload/agent_window_resolver"
        record = json.loads((source / "VENDORED.json").read_text())
        self.assertEqual(record["upstream"],
                         "https://github.com/clickety-clacks/agent-window-resolver")
        self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(record["syncedOn"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(set(record["files"]), EXPECTED_RESOLVER)
        self.assertEqual({path.name for path in source.glob("*.py")}, EXPECTED_RESOLVER)
        for name, blob in record["files"].items():
            with self.subTest(file=name):
                self.assertEqual(git_blob_id((source / name).read_bytes()), blob,
                                 "vendored file differs from the recorded commit; "
                                 "re-run scripts/sync-resolver.py")
        for upstream_path, extra in record["extras"].items():
            with self.subTest(file=upstream_path):
                self.assertEqual(
                    git_blob_id((ROOT / extra["vendoredAt"]).read_bytes()), extra["blob"])

    def test_installed_adapter_resolves_and_revalidates_owned_python_pid(self):
        share = self.home / ".local/share/window-attention"
        script = r'''
import json
import os
import socket
import agent_window_adapter as adapter

ticks = adapter.process_start_ticks(os.getpid())
if ticks is None:
    raise SystemExit("could not read test process start ticks")
machine = socket.gethostname()
agent = {
    "machine": machine,
    "instanceId": "installer-smoke",
    "id": {"pid": os.getpid(), "startTimeTicks": ticks},
}
clients = [{"address": "0x123", "pid": os.getpid()}]
first = adapter.resolve_agent(agent, clients, machine)
candidate = adapter.candidate_window(first.response)
if candidate is None:
    raise SystemExit(json.dumps({"resolve": first.response}))
second = adapter.resolve_agent(
    agent, clients, machine, operation="revalidate", prior={
        "window": candidate,
        "target": first.response["candidates"][0]["target"],
        "proof": first.response["candidates"][0]["proof"],
    }
)
if adapter.candidate_window(second.response) is None:
    raise SystemExit(json.dumps({"revalidate": second.response}))
print(json.dumps({"resolve": first.response["status"], "revalidate": second.response["status"]}))
'''
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=share,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(
            json.loads(result.stdout), {"resolve": "matched", "revalidate": "matched"}
        )

    def test_installed_daemon_prefers_share_helper_over_adjacent_bin_file(self):
        poison = self.home / ".local/bin/agentd_hub.py"
        poison.write_text("raise RuntimeError('unrelated adjacent helper loaded')\n")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        result = subprocess.run(
            [str(self.home / ".local/bin/window-attention"), "--help"],
            cwd=self.home / ".local/share/window-attention",
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
