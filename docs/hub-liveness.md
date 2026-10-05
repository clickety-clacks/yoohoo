# Hub connection health and sleep recovery

## Incident and requirements

On September 15, 2026, the desktop's last accepted Hub snapshot arrived at
11:15:14 AM PT, immediately before system suspend. The laptop resumed at
1:26 PM PT, but Yoohoo's existing SSE socket never received more bytes.
A separate, bounded subscription received seven snapshots in four seconds.
The server was serving updates; Yoohoo's unbounded reader had no means to
detect that its old connection had stopped carrying them.

The remediation must:

- Distinguish connecting, live, reconnecting, stale, and suspended states.
  A connected socket alone is not proof of a current roster.
- Require a valid snapshot on each new subscription before showing live.
- Keep last-known agents visible during outages, with a last-update age.
- Maintain freshness with additive Hub heartbeat events, not snapshot polling.
- Detect silent loss within a bounded deadline, including incomplete or
  malformed traffic that must not keep the feed falsely live.
- Mark the feed unavailable before sleep, discard the old stream, and attempt
  a new subscription promptly on wake. Retry with bounded, jittered backoff
  while networking is unavailable.
- Make stopped/stale backend state expire in the indicator rather than
  persisting a green status indefinitely.
- Preserve normal attention, acknowledgement, resolver, harness, and tmux
  behavior. Heartbeats do not produce attention or update agent activity.

## Verification and delivery

Automated and fault-injection tests are required to run on the test machine. The desktop is limited to
source work, read-only diagnostics, and the authorized complete installation.
No physical suspend, user terminal manipulation, or synthetic attention is
needed on the desktop.

Acceptance covers a quiet healthy stream, a silent blackhole, EOF and invalid
frames, suspend/resume during reading and retry, cached roster preservation,
UI expiry, bounded CPU use, and the existing relevant Yoohoo regressions.
The Hub's independent server tests must establish that heartbeat emission
does not change roster revision or replace the initial complete snapshot.

Deployment must use the complete Yoohoo installer and a complete, identified
Hub build with rollback material. Production read-only verification must show
fresh heartbeats and live status. This document will record measured results
and any untested scope before completion.

## Validation record

- Hub: 31 Rust tests passed on the test machine, including a delayed first-body-poll
  regression; formatting and Clippy with warnings denied passed.
- Cross-product: the release-built Hub and actual Python subscriber passed a
  quiet-stream check using the real 15-second heartbeat. The complete roster,
  its revision, and last-snapshot timestamp remained unchanged while the
  last-seen timestamp advanced. Synthetic input came from an owned local SSH
  stub; no real agents were queried or changed. Owned processes were cleaned up.
- This gate initially caught the 10-second connection timeout leaking into
  stream reads. The stream now uses the separate liveness watchdog, not the
  handshake timeout.
- Final selected Yoohoo regression suite: 132 tests passed on the test machine,
  including nine liveness tests and six lifecycle tests. The existing worker
  test's fake now filters acknowledged entries like production and waits for
  the locked cache update; no production worker change was needed.
- Actual logind-shaped sleep/wake signals on a private D-Bus reached the
  production Gio listener and produced suspended/reconnecting states; stopping
  the listener terminated it and made later signals inert. No physical suspend
  or system-bus mutation was performed.
- UI policy passed on the test machine. A Quickshell component smoke check also passed
  there using the installed Omarchy QML components and isolated configuration
  and state directories; no popup was opened, and client inventory stayed empty.
- Test-boundary exception: the UI coding subagent ran the isolated
  `tests.test_selection` unittest on the desktop despite explicit instructions.
  This was disclosed to Mike and rerun on the test machine. It did not mutate services,
  the desktop, or terminals. Remaining runtime gates stay on the test machine.

Independent Sol high review found no remaining blockers after lifecycle,
generation, callback-locking, and malformed-state corrections. The final
Quickshell component smoke passed again after the timestamp validation changes.
The final real Hub/subscriber quiet-stream test also passed (16.123 seconds).

These gates do not claim a physical laptop sleep/resume test on the desktop.
## Installed delivery

Installed September 15, 2026, at approximately 9:06 PM PT:

- The remote host Hub: complete standalone binary package, an unreleased local build
  based on v0.1.1 / `8981a292b921d580d5793c5faae8568b5fceeb8a`.
  Installed binary SHA256:
  `ea86b3672827612e267b187b5be1e3da6195f7fd7376f7e42d1c6249e82be9f6`.
  Source, archive/checksums, and previous binary are retained at
  `~/.local/state/agentd-hub-heartbeat.ZdmQwv` on the remote host.
  Only the Hub service was restarted; Agentd and its hooks were untouched.
- The desktop Yoohoo: `python -B install.py install`, the complete normal installer,
  including service activation, Hyprland validation, and shell restart.
  Backup: `~/.local/state/yoohoo/backups/1789531574915587105`.
  All 24 nonpersonal distribution files match source and installer manifest.
  Personal Yoohoo config and shell settings retain their preinstall hashes.
  The bundled resolver is unchanged.
- Read-only verification: both services active, Yoohoo `status=live` with fresh
  advancing receive timestamps, Hyprland config errors empty, shell ping `ok`.
  A bounded 18-second production SSE observation received snapshots and the
  new `agentd.hub.heartbeat.v1` event. Its curl timeout was intentional, to end
  observation of an otherwise unending stream.
- No desktop sleep test, synthetic attention, terminal manipulation, or tmux
  restart was performed. The test machine component smoke left desktop clients empty.

The next physical sleep/wake on the desktop remains a real-world observation, not
something these deployment checks claim to have exercised.

## September 16: false-stale indicator investigation

Ask observed the running indicator alternate live/stale while both the daemon
status and list payload were live with fresh timestamps. Installed Panel.qml
matches source. The timer captures `nowMs` before starting the asynchronous
list command; `applyPayload` was using that older clock to validate timestamps
received later. A valid snapshot newer than the timer tick therefore fails
the future-date guard and its displayed age becomes unknown.

The scoped correction is to sample local `Date.now()` at payload receipt,
before health validation. Genuine future timestamps, malformed timestamps,
and the 45-second expiry must remain rejected. The regression must exercise
the actual extracted QML handler with a response arriving after the timer
tick, not only the standalone health helper.

Runtime verification is pending: the test machine SSH timed out during this
investigation. No desktop tests, installed changes, or restarts are authorized
by this handoff or performed for it.

Source fix prepared in `payload/Panel.qml`: sample `root.nowMs = Date.now()`
at the start of `applyPayload`. `tests/test_selection.py` now includes a
deterministic receipt-after-tick case (tick 100000, receipt 101000, snapshot
100500, last seen 100900), checks derived live status and a 500 ms age, then
checks genuine future and expired timestamps remain unavailable. These are
authored regression cases, not yet passing runtime evidence. Existing handler
fixtures also use a fixed local clock. Selection.js and the daemon are unchanged.

The test machine became available later on September 16. The frozen source
(`Panel.qml` SHA256 `6ffd571055eeb6c1d6bbe47e6ff105356d58169f5bec4eeaebe85169ad69aed5`,
`test_selection.py` SHA256 `5eca249d70c07bea47b6df5c72451ea0fbc42174788633c8370e9f3dabe27bd1`)
passed the complete selection-policy test, including the actual extracted QML
handler's delayed-receipt, future-date, and expiry assertions. Independent
Sol high static review accepted this pair. A separate baseline copy using the
old Panel with the new regression failed at the receipt-clock assertion
(`100000 != 101000`), as expected. Staging:
`/tmp/yoohoo-clock-check.FiSZI6` on the test machine. No desktop test or deployment was
performed; installed desktop files remain unchanged pending authorization.

Mike subsequently authorized installation on the desktop. On September 16 at
8:19 AM PT, the complete `python -B install.py install` workflow succeeded.
Only Panel.qml required a content update; all 24 nonpersonal package files
match source and the installer record. Backup:
`~/.local/state/yoohoo/backups/1789571950939411975`.
Personal config and shell settings hashes are unchanged. Service is active,
Hyprland config errors empty, shell ping succeeds, and read-only running-panel
IPC reports derived `hubHealth=live` with an empty error and fresh timestamps.
No terminals or tmux sessions were restarted and no desktop tests were run.
