# Unopenable Agentd rows and dismissal

Observed on osanwe, September 17, 2026: the menu repeatedly listed
`claude · osanwe` entries whose selection failed with
`agent_has_no_tmux_session`. The claims were Claude processes started by other
applications (Omarchy Ask and the Tightbeam decision-request window). Agentd
reported their turns; the Hub forwarded them; Yoohoo had no window match and
no tmux session, so the only possible action was a failure. A user had no way
to clear the entry.

## Presentation rule

`AttentionService.hub_rows` now applies one general rule to every Hub claim:

- Matched to an existing local window: shown; a native alert for the same
  window merges into it (see [merged rows](merged-attention-rows.md)).
- No window match but a tmux session: shown as a connect target.
- Neither: not shown as a standalone Agentd entry, and no sound is played for
  it. Native window attention is unaffected.

The rule is not specific to any harness or launcher. A claim that later gains
a window match through the background resolver appears at that point.

## Failure card and dismissal

When an entry that was listed cannot be opened, the panel replaces the raw
code with a plain-language sentence (`Selection.plainReason`) and shows a card
with **Dismiss alert** (`D`) and **Keep** (`Esc`). Enter retries. Dismissal
runs `window-attention dismiss <key>`: a native address is cleared with reason
`dismissed`; a Hub identity is acknowledged at the pending claim exactly as a
successful open would be, so the agent is not touched and a newer claim still
surfaces. Unknown identities dismiss successfully because there is nothing
left to clear.

## Validation

Regression tests were added in `tests/test_hub.py` (projection rule, alert
sound gating, dismiss for Hub and native rows, CLI failure payload) and
`tests/test_selection.py` (plain-language reasons). Automated tests for this
change run on Plumbus, not osanwe. Plumbus was unreachable while the change
was written; record the Plumbus run here before installing.
