#!/usr/bin/env python3
"""Exercise the published Agentd + Hub wire path with one disposable fixture.

This runner is intended for a Testbed testbed, not the normal unit suite.  It
starts one synthetic process named ``codex`` in a uniquely named tmux session,
marks that exact Agentd identity ``needs_attention``, and starts agentd-hub on
the requested loopback port.  A temporary ``tailscale`` stub returns ``{}``
so Hub uses the explicit hosts-file fallback.  The temporary ``ssh`` shim
executes the local Agentd CLI for that one fixture source because this test
machine may not have SSH-to-self configured.  No real agent process, desktop
configuration, or service is changed.

Run on Testbed after installing the published binaries:

    python3 tests/integration/testbed_hub_wire.py

Override paths/port with ``--agentd``, ``--hub``, ``--listen-port``, or
``--keep-tmux`` when debugging.  The default cleanup kills only the uniquely
named fixture session and the Hub child started by this process.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[2]
HUB_CLIENT = ROOT / "payload" / "agentd_hub.py"


def command(path: str, *args: str, **kwargs):
    return subprocess.run([path, *args], check=True, text=True, **kwargs)


def agent_snapshot(agentd: str) -> dict:
    return json.loads(subprocess.check_output([agentd, "list", "--json"], text=True))


def wait_for_fixture(agentd: str, deadline: float) -> dict:
    while time.monotonic() < deadline:
        frame = agent_snapshot(agentd)
        if any(item.get("harness") == "codex" for item in frame.get("agents", [])):
            return frame
        time.sleep(0.1)
    raise RuntimeError("Agentd did not discover the synthetic codex fixture")


def wait_http(url: str, deadline: float) -> dict:
    error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except Exception as exc:  # noqa: BLE001 - report one final bounded error
            error = str(exc)
            time.sleep(0.1)
    raise RuntimeError(f"Hub did not serve {url}: {error}")


def write_shims(directory: Path, agentd: str) -> None:
    # Invalid/empty Tailscale JSON is intentional: Hub's documented fallback
    # then reads only the explicit hosts file, never the user's tailnet.
    (directory / "tailscale").write_text("#!/bin/sh\nprintf '%s\\n' '{}'")
    (directory / "ssh").write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        f"  *' agentd list --json'*) exec {agentd!s} list --json ;;\n"
        f"  *' agentd watch --json'*) exec {agentd!s} watch --json ;;\n"
        "  *) echo 'fixture ssh: unexpected command' >&2; exit 2 ;;\n"
        "esac\n"
    )
    for path in (directory / "tailscale", directory / "ssh"):
        path.chmod(0o755)


def run(args: argparse.Namespace) -> None:
    agentd = str(Path(args.agentd).expanduser())
    hub = str(Path(args.hub).expanduser())
    session = f"yoohoo-it-{uuid.uuid4().hex[:10]}"
    fixture_script: Path | None = None
    hub_process: subprocess.Popen[str] | None = None
    tmux_created = False
    try:
        with tempfile.TemporaryDirectory(prefix="yoohoo-testbed-wire-") as temp:
            temp_path = Path(temp)
            bin_path = temp_path / "bin"
            bin_path.mkdir()
            write_shims(bin_path, agentd)
            hosts = temp_path / "hosts.txt"
            hosts.write_text("fixture-local\n")

            # prctl(PR_SET_NAME) makes Agentd's proc_comm detector see a Codex
            # root while the process remains an ordinary disposable Python
            # fixture.  It receives no prompt, transcript, or agent input.
            fixture_script = temp_path / "codex_fixture.py"
            fixture_script.write_text(
                "import ctypes, time\n"
                "ctypes.CDLL(None).prctl(15, b'codex', 0, 0, 0)\n"
                "time.sleep(3600)\n"
            )
            command("tmux", "new-session", "-d", "-s", session, sys.executable, str(fixture_script))
            tmux_created = True

            frame = wait_for_fixture(agentd, time.monotonic() + 5)
            fixture = next(item for item in frame["agents"] if item.get("harness") == "codex")
            command(agentd, "activity", "--pid", str(fixture["id"]["pid"]), "--state", "needs_attention")

            env = dict(os.environ)
            env["PATH"] = f"{bin_path}:{env.get('PATH', '')}"
            hub_process = subprocess.Popen(
                [hub, "--listen", f"127.0.0.1:{args.listen_port}", "--hosts-file", str(hosts)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            snapshot = wait_http(
                f"http://127.0.0.1:{args.listen_port}/snapshot", time.monotonic() + 15
            )
            assert snapshot["schema"] == "agentd-hub.snapshot.v1", snapshot
            assert len(snapshot["sources"]) == 1, snapshot
            assert snapshot["sources"][0]["machine"] == "fixture-local", snapshot
            assert snapshot["sources"][0]["health"]["state"] == "reporting", snapshot
            assert any(
                item["id"] == fixture["id"] and item["activity"]["state"] == "needs_attention"
                for item in snapshot["agents"]
            ), snapshot
            print(json.dumps({
                "status": "PASS",
                "hub_revision": snapshot["revision"],
                "source_health": snapshot["sources"][0]["health"],
                "agent_identity": fixture["id"],
                "tmux_session": session,
                "scope": "local Agentd CLI fixture via temporary ssh shim; no cross-host SSH",
            }, sort_keys=True))
    finally:
        if hub_process is not None:
            hub_process.terminate()
            try:
                hub_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                hub_process.kill()
                hub_process.wait(timeout=5)
        if tmux_created and not args.keep_tmux:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentd", default="agentd")
    parser.add_argument("--hub", default="agentd-hub")
    parser.add_argument("--listen-port", type=int, default=8788)
    parser.add_argument("--keep-tmux", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except (AssertionError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
