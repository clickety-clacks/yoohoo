# Best-effort existing-window matching

Mike's specification correction, September 14, 2026: identifying an existing
window must not require exact SSH or reversed UDP transport proof. Useful
window titles, agent names, tmux session names, and SSH/mosh command hints
should be considered, with uncertainty made explicit.

The canonical source is `../agent-window-resolver/agent_window_resolver` in
the development workspace. Its seven Python modules are bundled unchanged in
`payload/agent_window_resolver`; Ask consumes the same maintained source.
There is no separate resolver installation, PATH discovery, registration,
or harness wrapper requirement.

Yoohoo uses the shared `match` operation. Host plus session command hints rank
above title/name hints; optional exact evidence ranks highest. Multiple matching
windows remain candidates. Yoohoo selects the highest-ranked existing window,
breaking ties by active window, then recent focus. It checks fresh compositor
identity and process start time before focusing and acknowledges the attention
event only afterward. A failed scan is not confirmation that no window exists.

The old strict operations remain available for compatibility and target
verification before a genuinely new attachment. They are not the prerequisite
for selecting an existing SSH/mosh window.

## Verification and limits

Automated tests for this change run on Plumbus, not osanwe:

Final source snapshot: 81 shared-core tests and 80 Yoohoo tests passed, plus
the opt-in live two-window test. Luna implemented the initial changes; Sol
performed independent file-only review, and the owner addressed its findings
and ran the Plumbus checks.

- Shared regression fixtures exercise all three sanitized command forms from
  the reported `0_1_9` windows, including mosh-client's display argument, with
  no remote process or UDP endpoint proof. All three are returned as candidates.
- The SSH regression uses Yoohoo's actual single-argument quoted shell form.
- A live test creates two owned Ghostty windows with the same title, invokes
  production `open_target`, and checks existing-window focus, acknowledgement,
  an unchanged window count, and cleanup of only its own processes.
  It includes synthetic remote tmux metadata and forbids SSH/mosh execution.

Remote `match` does not probe the target host. It reports
`remote_target_not_probed` uncertainty while using local window hints. Local
tmux matching uses current client/session/window/pane data and client PID/start
identity in the terminal subtree, without full target probing. Strict
verification before a new attachment is unchanged. A real unnamed tmux-client
regression failed before this correction and passed afterward on Plumbus.

The original equal-title live test alone did not prove real mosh behavior.
Follow-up tests now pass through production Yoohoo activation with two real
mosh connections, generic titles, and unchanged window/client identities, and
with a local Ghostty/tmux window whose argv contains no session name. Both
verify workspace navigation and acknowledgement. See
[the activation proof](plumbus-activation-proof.md) for scope, hashes, and
cleanup evidence. The installed osanwe `0_1_9` popup click was not retested.

Mike subsequently authorized fixing the installed local `ask` failure. The
corrected bundle was installed on osanwe at 6:30 PM PT on September 14, and only
`window-attention.service` was restarted. No automated tests, test clicks, or
tmux/session changes were performed there. Read-only inspection confirmed the
installed hashes, connected tracker, and an existing `stalls` mosh-window match
from the normal background scan. `ask` was active rather than pending at this
point; its menu click was not fabricated or reported as tested. Previous files
are recoverable under `.local/state/yoohoo/backups/1789435856356097506`.

Matching is deliberately heuristic. A stale session launch argument can be
misleading; current-title disagreement is recorded as uncertainty. Host-only
hints do not qualify. When a window cannot be inspected and no usable match
remains, Yoohoo does not silently treat that failure as permission to duplicate
the connection.
