# Native and Hub attention rows

On September 16, the live menu and `window-attention list` both contained
two `drift · atlas` entries with address `0x556efaa2f590`: one native window
attention record and one Hub agent record. The list command concatenated
the two sources without reconciling their shared window.

The presentation fix merges an actionable, addressed Hub row with its native
window counterpart. The agent identity and activation/acknowledgement path
remain intact, while native workspace details fill gaps in the Hub row.
Distinct agents remain distinct; matching titles or empty addresses do not
establish a shared window. An unavailable Hub row must not hide usable native
attention. Source attention records are not deleted by rendering the list.

## Validation and delivery

- Luna implemented; independent Sol high static review accepted final daemon
  `b2ecdf4bc1bc09c66155133d2717061b637f96b5dac89315bfac2144018a0183`
  and tests `ff11baeb5ee1937fd04535ef71b79349990af54e9027e7d5cbe066604dc4abe2`.
- Testbed stage `/tmp/yoohoo-row-merge.iMaS4M`: all 138 selected regressions
  passed, including six new merge cases. The production CLI-list regression
  fails against the old daemon with `2 != 1`, and passes with the correction.
- Complete normal installer ran on Lumen September 16, 2026, at approximately
  8:35 AM PT. Backup: `~/.local/state/yoohoo/backups/1789572946174201176`.
  All 24 package files match source and installer manifest. User config and
  shell settings retain their preinstall hashes; service and shell are healthy.
- Read-only post-install list and running-panel IPC show one `drift · atlas`
  agent entry, live Hub health, and no error. The restart clears native pending
  state, so this observation alone is not the merge regression proof; that is
  supplied by the Testbed production-list test with both sources present.

No harness, resolver, transport, or tmux changes; no Lumen test execution or
synthetic attention. Native workspace metadata is available to merge only
while a native row is present.
