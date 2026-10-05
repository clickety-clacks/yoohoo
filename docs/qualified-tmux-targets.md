# Qualified tmux launch targets

## Reported defect

Ask's production launcher emits `tmux attach-session -t =SESSION:WINDOW.%PANE`
when roster metadata supplies a window index and pane ID. The shared parser
previously removed the leading `=` but treated the remaining target as a
session name. Comparing that value with the roster's bare session name missed
windows opened by Ask itself. Bare-session fixtures did not exercise this
round trip.

This is a general grammar mismatch, not a session-name allowlist. Source
inspection confirms the mismatch; the original failed window's argv was not
captured, so that inspection alone does not prove the cause of every reported
duplicate.

## Correction requirements

- Parse attach targets into session, window, and pane components. Retain exact
  selectors, index versus ID distinctions, and socket name versus socket path.
- Treat `new-session -s` as a literal session name, not attach-target syntax.
- Keep bare-session matching best effort. Missing qualifiers do not require
  remote proof or prevent otherwise useful matching.
- Comparable conflicting qualifiers must not support the requested location.
  A conflicting launch argument must not reappear as independent agent-name
  evidence. Independent current title/client evidence can still be useful
  when historical launch arguments have become stale.
- Do not invent equality or disagreement between incomparable observations,
  such as a pane index and pane ID without an observed mapping.
- Continue parsing SSH/mosh hints without executing their command text.

The reference grammar is documented in the
[tmux manual](https://man.openbsd.org/tmux), under target syntax and
`attach-session`.

## Ownership and verification

Yoohoo maintains the shared implementation in this checkout. Ask owns its
adapter and the production-launch/repeat-selection regression. Both products
bundle the same maintained source; neither needs a separate resolver install.

Required coverage includes qualified SSH and mosh launch hints, bare targets,
literal new-session names, socket/window/pane distinctions, and conflicting
targets. The adapter regression must use production launch output, then select
the same agent again and verify existing-window focus without another launch.
Pre-created bare-session windows are not evidence for that round trip.

## Source handoff, September 15, 2026

The correction is implemented and passed independent Sol high static review.
Only the following two modules changed; the other five bundled modules remain
unchanged. Copy the complete seven-module package into each product, not a
separately installed executable.

```
06f510e61e598b4378effeb7198b22d5d310e428dbf42396849e5b0c0cb860cc  collector.py
0be324ff3bf9052182c7114b5976a0e6e3ab4696942b6f123da934b1268b65bb  resolver.py
```

The focused regression file is `tests/test_qualified_tmux_targets.py`. Ask's
adapter replay is `tests/agent-launch-roundtrip.test.mjs`, with
`tests/launch-roundtrip-matcher.py` in the Ask repository.

## The test machine verification

The reviewed source hashes above were verified in the fresh staging directory
`/tmp/yoohoo-qualified-validation.MC0F1qrv` on the test machine.

- All 104 shared-core unit tests passed, including 14 new qualified-target
  tests. The focused new/existing launch-hint run also passed 23 tests.
- Yoohoo's selected non-desktop suite passed 117 tests, including bundled
  installation, adapter, menu-selection, pulse-ownership, and Hub regressions.
  Private-tmux and desktop integration fixtures were not part of this run.
- Ask reports all eight production-launch/repeat replay cases passed in
  `/tmp/ask-qualified-roundtrip.hO7xDw`: SSH/mosh with socket name/path, one
  launch followed by two focus requests, distinct/equal agent and session
  names, and qualifier/raw-argv conflict checks. The compositor/process
  graph/focus are injected in this replay; this is not live desktop proof.
- A negative-control SSH replay against the pre-fix core failed the conflict
  assertions, but its initial repeat-selection step still passed through
  session-description aliases and display-name fallback. Ask added a
  variant with a plain randomized session and a different agent display name.
  That stronger replay (`1cfc3241...`) passes with the corrected core. Against
  the pre-fix `42db2f45...` / `24174377...` core, both SSH/path/distinct and
  mosh/path/distinct fail on an attempted second terminal launch, before the
  conflict assertions, in `/tmp/ask-qualified-baseline.ahzHt7` on the test machine.

- Ask's actual SSH launch/repeat gate passed on the test machine at fixture hash
  `948762b3cc561cdf2543012d74695406a18cd3699c1aa503339a3740602c51a9`.
  Production launch arguments opened one real Ghostty/private-SSH/tmux
  connection. Both repeated activations focused the same stable window and
  workspace, with zero new launches and an unchanged single tmux attachment.
  The actual title stayed generic; both resolver passes supported the
  qualified transport hint. The private attach preflight was fixture-supplied;
  collection, matching, compositor state, and focus were real.
- An initial SSH run failed during identity-checked cleanup of an exiting
  process and was not accepted. Ask recovered only its proven owned processes,
  retained diagnostic roots, and added a bounded retry of the same strict
  cleanup helper. The reviewed retry emitted a successful behavior checkpoint
  and `cleanupVerified=true`; a fresh compositor snapshot was empty.

- The final combined live fixture
  `35a728273d9bf45e5c67818d4b3f612db9972963ef7afeff434f929814200c9a`
  passed both SSH and native loopback mosh modes. Each emitted one terminal
  launch, one attachment, two existing-window focuses with no new launches,
  and `cleanupVerified=true`. Mosh used the native client/server and actual
  display arguments, with original server identity/UDP inode checks. Its
  test-only `--local` bootstrap does **not** cover remote SSH bootstrap.
  An earlier mosh run passed behavior but failed cleanup while the server
  exited naturally; it was not accepted. The reviewed fixture-only correction
  retries observations of the same recorded server and retains uncertain state.

## Yoohoo installation on the desktop

At 10:17 AM PT on September 15, the complete Yoohoo installer was run:
`python -B install.py install`. It created backup
`~/.local/state/yoohoo/backups/1789492674492287538`.
All 24 nonpersonal installer-managed files matched the source and the
installer-generated hash record afterward. The personal configuration was
retained. Yoohoo was active, its Hub connection was healthy, the desktop shell
answered its health check, and Hyprland reported no configuration errors.
Tracked Yoohoo paths had no outstanding chezmoi differences.

No tests or live connection fixtures ran on the desktop. No selective installed-file
patches or manual installation-record edits were used for this deployment.
Ask owns its separate complete installation; this record does not claim Ask
was deployed.
