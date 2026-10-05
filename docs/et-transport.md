# Eternal Terminal transport

Yoohoo can reach a remote agent's tmux session over Eternal Terminal (`et`) as
well as mosh and ssh. The reason is the terminal's light/dark mode: an app
subscribes with `CSI ? 2031 h`, asks with `CSI ? 996 n`, and the terminal
answers `CSI ? 997 ; 1 n` (dark) or `CSI ? 997 ; 2 n` (light), again on every
change. mosh terminates the terminal protocol on the server and never relays
these, so a remote app cannot follow the desktop. et relays them unchanged.
mosh stays available: on cellular its predictive local echo still matters.

The choice is `[agentd_hub] transport` (README, "Choosing et, mosh or ssh"). It
follows agent-window-resolver's `docs/transport-policy-v1.md`, which Omarchy Ask
also implements; `tests/test_transport_policy.py` checks Yoohoo against the
shared vectors in `tests/fixtures/transport-policy-v1.json`.

## What the resolver observes

`verify-target` with `probeTransports` runs one extra bounded SSH command on the
target. et counts as available only when `etserver` is running, `etterminal` is
on SSH's non-interactive PATH, and a TCP connection from this machine to
etserver's port succeeds. mosh counts as available only when `mosh-server` is
present and a nonce sent from here to a UDP port in mosh's range comes back.
Both use the address SSH reached. From gibson to nacelle over the tailnet the
whole probe took 0.44 s (mosh UDP passing, no etserver running).

et windows are recognised from the et client's argv, where the remote command
is in `-c`. That is a host/session hint, graded like mosh: the remote tmux
client descends from `etterminal`, which reaches the shared `etserver` over a
local socket, so no TCP endpoint pair links a window to its session.

## Verified on nacelle, October 5, 2026

nacelle runs et 7.0.0 from `~/opt/et` (built, not installed system-wide).
`tests/integration/test_et_windows.py` builds a private loopback sshd, a
non-root etserver on a free port and a TMUX_TMPDIR-isolated tmux server in one
temporary directory; a pty stands in for Ghostty and answers colour queries
(`CSI ? 996 n` and OSC 10/11, as Ghostty does). Both tests passed:

- The bundled resolver's `verify-target` observed et as reachable on the
  private port. Yoohoo recorded it, `auto` chose et with an ssh fallback, and
  the launch attached the remote tmux session through et.
- The bundled resolver matched the launched window from the et argv with
  `transport_host_session_hint` and `et_hint_not_exact_proof`.
- An app inside the remote tmux pane received light in reply to its query and
  then the unsolicited light-to-dark change sent by the stand-in terminal.
- With the recorded et port closed, the launcher's et start failed, its ssh
  reachability check succeeded, it deleted the stale record and attached the
  same session over `ssh -tt`.

Run it with `YOOHOO_ET_WINDOWS_TEST=1 YOOHOO_ET_BIN=~/opt/et/usr/bin python3
-B -m unittest tests/integration/test_et_windows.py` on a test machine.

Measured separately on nacelle: et exits 0 on a normal tmux detach and when the
remote command fails, and exits 1 at once when etserver is unreachable, so a
nonzero exit is a transport failure and safe to fall back on. tmux 3.7c only
forwards a client's light/dark change to panes after the terminal also answers
its OSC 10/11 colour queries.

Not covered here: a real Ghostty window and Hyprland focus over et, a real
roaming laptop, and etserver installed system-wide. et types its `-c` command
into the remote login shell, so it briefly appears there.
