"""Opt-in real Eternal Terminal gate, beside test_mosh_windows.py.

Everything runs as the invoking user inside one temporary directory: a private
loopback sshd, a private etserver on a high port, and tmux servers isolated by
TMUX_TMPDIR. It never touches the user's own sshd, etserver or tmux server and
needs no desktop. A pty stands in for Ghostty: it runs the exact argv Yoohoo
would hand to ``ghostty -e`` and answers terminal colour-scheme queries.

It proves, with production Yoohoo code and the bundled resolver:

1. verify-target with probeTransports observes et as reachable;
2. the recorded capability makes ``auto`` choose et, and the launch attaches
   the remote tmux session through et;
3. best-effort matching recognises the et window (exact proof is impossible);
4. an app in the remote tmux pane sees the light reply to its query and then
   the unsolicited light->dark change, through et and tmux.

Enable with YOOHOO_ET_WINDOWS_TEST=1 and YOOHOO_ET_BIN=<dir with et, etserver,
etterminal>. Run it on a test machine (nacelle), never on a live desktop.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SESSION = "et gate"
DARK, LIGHT = b"\x1b[?997;1n", b"\x1b[?997;2n"
QUERY = b"\x1b[?996n"

# Runs in the remote tmux pane: subscribe, query, log every polarity report.
THEME_APP = r"""
import os, sys, termios, tty, select, time
log = open(sys.argv[1], "a", buffering=1)
fd = sys.stdin.fileno()
tty.setraw(fd)
os.write(1, b"\x1b[?2031h\x1b[?996n")
buffer, end = b"", time.monotonic() + 120
while time.monotonic() < end:
    if select.select([fd], [], [], 0.5)[0]:
        buffer += os.read(fd, 1024)
        for code, name in ((b"\x1b[?997;1n", "dark"), (b"\x1b[?997;2n", "light")):
            while code in buffer:
                index = buffer.index(code)
                buffer = buffer[:index] + buffer[index + len(code):]
                log.write(name + "\n")
"""


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ticks(pid: int) -> str:
    raw = Path("/proc", str(pid), "stat").read_text()
    return str(int(raw.rsplit(") ", 1)[1].split()[19]))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout: float, message):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(message() if callable(message) else message)


class TerminalStandIn:
    """A pty that answers colour-scheme queries like Ghostty would."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.mode = LIGHT
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(argv[0], argv, env)
        self.output = b""

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return
                self.output += chunk
                for _ in range(chunk.count(QUERY)):
                    os.write(self.fd, self.mode)
                # tmux also asks for the colours themselves (OSC 10/11) and
                # re-asks after every change report; answer like Ghostty.
                dark = self.mode == DARK
                for number, (on_dark, on_light) in ((b"10", (b"ffff", b"0000")),
                                                    (b"11", (b"1010", b"ffff"))):
                    value = on_dark if dark else on_light
                    for _ in range(chunk.count(b"\x1b]" + number + b";?")):
                        os.write(self.fd, b"\x1b]" + number + b";rgb:" + value + b"/"
                                 + value + b"/" + value + b"\x1b\\")

    def flip(self, mode: bytes) -> None:
        self.mode = mode
        os.write(self.fd, mode)

    def close(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass


@unittest.skipUnless(os.environ.get("YOOHOO_ET_WINDOWS_TEST") == "1",
                     "explicit real-et opt-in required (YOOHOO_ET_WINDOWS_TEST=1)")
class EtWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.et_bin = Path(os.environ["YOOHOO_ET_BIN"]).resolve(strict=True)
        self.temp = tempfile.TemporaryDirectory(prefix="yoohoo-et-gate-")
        self.addCleanup(self.temp.cleanup)
        lab = self.lab = Path(self.temp.name)
        self.cleanups: list = []
        self.addCleanup(lambda: [action() for action in reversed(self.cleanups)])
        (lab / "bin").mkdir()
        (lab / "tmux").mkdir(mode=0o700)
        self.sshd_port, self.et_port = free_port(), free_port()
        for name in ("host_key", "client_key"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                            str(lab / name)], check=True)
        shutil.copy(lab / "client_key.pub", lab / "authorized_keys")
        remote_path = f"{lab}/bin:{self.et_bin}:/usr/local/bin:/usr/bin:/bin"
        (lab / "sshd_config").write_text(
            f"Port {self.sshd_port}\nListenAddress 127.0.0.1\nHostKey {lab}/host_key\n"
            f"AuthorizedKeysFile {lab}/authorized_keys\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\n"
            f"PidFile {lab}/sshd.pid\nSetEnv PATH={remote_path} TMUX_TMPDIR={lab}/tmux"
            # etterminal finds a non-root etserver's fifo through this.
            f" XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '/tmp')}\n")
        (lab / "ssh_config").write_text(
            f"Host 127.0.0.1\n  Port {self.sshd_port}\n  IdentityFile {lab}/client_key\n"
            f"  IdentitiesOnly yes\n  UserKnownHostsFile {lab}/known_hosts\n"
            "  StrictHostKeyChecking accept-new\n  BatchMode yes\n")
        wrapper = lab / "bin/ssh"
        wrapper.write_text(f"#!/bin/sh\nexec /usr/bin/ssh -F {lab}/ssh_config \"$@\"\n")
        wrapper.chmod(0o755)
        for name in ("et", "etserver", "etterminal"):
            (lab / "bin" / name).symlink_to(self.et_bin / name)
        subprocess.run(["/usr/bin/sshd", "-f", str(lab / "sshd_config"),
                        "-E", str(lab / "sshd.log")], check=True)
        self.cleanups.append(lambda: os.kill(int((lab / "sshd.pid").read_text()), signal.SIGTERM))
        etserver = subprocess.Popen(
            [str(self.et_bin / "etserver"), "--port", str(self.et_port),
             "-l", str(lab / "etlog"), "--pidfile", str(lab / "etserver.pid")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.cleanups.append(lambda: (etserver.kill(), etserver.wait()))
        # TERM as a terminal sets it; tmux refuses to attach without one.
        self.env = {**os.environ, "PATH": f"{lab}/bin:{os.environ['PATH']}",
                    "TMUX_TMPDIR": str(lab / "tmux"), "TERM": "xterm-256color"}
        self.env.pop("TMUX", None)
        self.cleanups.append(lambda: subprocess.run(
            ["tmux", "kill-server"], env=self.env, stderr=subprocess.DEVNULL))
        wait_for(lambda: subprocess.run(
            ["ssh", "127.0.0.1", "true"], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0,
            10, "private sshd did not accept the test key")

    def test_et_launch_identification_and_theme_follow(self) -> None:
        os.environ.update({"PATH": self.env["PATH"], "TMUX_TMPDIR": self.env["TMUX_TMPDIR"]})
        hub = load("yoohoo_et_gate_hub", ROOT / "payload/agentd_hub.py")
        adapter = hub._bundled_resolver_adapter()
        log = self.lab / "theme.log"
        (self.lab / "theme_app.py").write_text(THEME_APP)
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-x", "100", "-y", "30",
                        sys.executable, str(self.lab / "theme_app.py"), str(log)],
                       env=self.env, check=True)
        pane = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + SESSION,
             "-F", "#{window_index} #{pane_id} #{pane_pid}"],
            env=self.env, check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(len(pane), 3, "remote tmux pane did not start")
        app_pid = int(pane[2])
        local = socket.gethostname()
        agent = {"machine": "127.0.0.1", "instanceId": "et-gate",
                 "id": {"pid": app_pid, "startTimeTicks": int(ticks(app_pid))},
                 "tmux": {"session": SESSION, "windowIndex": pane[0], "paneId": pane[1]}}

        verified = adapter.verify_target(agent, local, probe_transports=True)
        response = verified.response
        self.assertEqual(response["status"], "verified", response)
        self.assertEqual(response["transports"]["et"], {
            "state": "available", "code": "et_reachable", "port": self.et_port,
        }, response["transports"])
        state = self.lab / "state"
        self.assertTrue(hub.record_capabilities(state, "127.0.0.1", response))

        # The pty stands in for Ghostty; every other executable is real.
        which = lambda name: "ghostty" if name == "ghostty" else shutil.which(name)
        plan = hub.connection_plan("127.0.0.1", SESSION, local, [], which=which,
                                   capabilities=hub.read_capabilities(state, "127.0.0.1"))
        self.assertEqual((plan["transport"], plan["fallback"], plan["etPort"]),
                         ("et", "ssh", self.et_port))
        stale = hub.capability_path(state, "127.0.0.1")
        argv = hub.build_launch_argv(agent, plan, local, str(stale))
        self.assertEqual(argv[:2], ["ghostty", "-e"])

        terminal = TerminalStandIn(argv[2:], self.env)
        self.cleanups.append(terminal.close)
        client = wait_for(lambda: (terminal.pump(0.3), subprocess.run(
            ["tmux", "list-clients", "-t", "=" + SESSION, "-F", "#{client_pid}"],
            env=self.env, capture_output=True, text=True).stdout.split())[1],
            20, lambda: "et did not attach the remote tmux session: "
            + terminal.output[-3000:].decode("utf-8", "replace"))
        self.assertTrue(client, terminal.output[-2000:])
        self.assertTrue(client)

        # The window's process subtree, as the compositor would report it.
        window = {"stableId": "et-gate", "address": "0x1", "pid": terminal.pid,
                  "startTimeTicks": ticks(terminal.pid), "title": "et"}
        request = adapter.request_for_agent(agent, [window], local, operation="match")
        match = adapter.Resolver().resolve(request, adapter.LinuxCollector())
        self.assertEqual(match["status"], "matched", match)
        evidence = [item["code"] for item in match["candidates"][0]["match"]["evidence"]]
        uncertainty = [item["code"] for item in match["candidates"][0]["match"]["uncertainty"]]
        self.assertIn("transport_host_session_hint", evidence)
        self.assertIn("et_hint_not_exact_proof", uncertainty)

        wait_for(lambda: (terminal.pump(0.3), log.exists() and "light" in log.read_text())[1],
                 20, "remote app never saw the light reply")
        terminal.flip(DARK)
        wait_for(lambda: (terminal.pump(0.3), log.read_text().splitlines()[-1:] == ["dark"])[1],
                 20, lambda: "remote app never saw the light->dark change: log="
             + repr(log.read_text()) + " 2031h=" + str(terminal.output.count(b"\x1b[?2031h"))
             + " queries=" + str(terminal.output.count(QUERY)))
        print(json.dumps({"theme": log.read_text().split(), "transport": plan["transport"],
                          "etPort": self.et_port}))


    def test_failed_et_start_falls_back_to_ssh_and_forgets_the_record(self) -> None:
        os.environ.update({"PATH": self.env["PATH"], "TMUX_TMPDIR": self.env["TMUX_TMPDIR"]})
        hub = load("yoohoo_et_gate_hub_fallback", ROOT / "payload/agentd_hub.py")
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "sleep 120"],
                       env=self.env, check=True)
        state = self.lab / "state"
        closed = free_port()
        self.assertTrue(hub.record_capabilities(state, "127.0.0.1", {"transports": {
            "state": "complete",
            "ssh": {"state": "available", "code": "ssh_probe_succeeded"},
            "et": {"state": "available", "code": "et_reachable", "port": closed},
            "mosh": {"state": "unavailable", "code": "mosh_server_missing"},
        }}))
        which = lambda name: "ghostty" if name == "ghostty" else shutil.which(name)
        agent = {"machine": "127.0.0.1", "tmux": {"session": SESSION}}
        plan = hub.connection_plan("127.0.0.1", SESSION, socket.gethostname(), [], which=which,
                                   capabilities=hub.read_capabilities(state, "127.0.0.1"))
        self.assertEqual((plan["transport"], plan["etPort"]), ("et", closed))
        stale = hub.capability_path(state, "127.0.0.1")
        argv = hub.build_launch_argv(agent, plan, socket.gethostname(), str(stale))
        terminal = TerminalStandIn(argv[2:], self.env)
        self.cleanups.append(terminal.close)
        wait_for(lambda: (terminal.pump(0.3), subprocess.run(
            ["tmux", "list-clients", "-t", "=" + SESSION, "-F", "#{client_pid}"],
            env=self.env, capture_output=True, text=True).stdout.split())[1],
            30, lambda: "ssh fallback did not attach: "
            + terminal.output[-2000:].decode("utf-8", "replace"))
        self.assertFalse(stale.exists(), "a reachable host's stale record was kept")


if __name__ == "__main__":
    unittest.main()
