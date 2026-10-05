# Testbed activation proof — September 14, 2026

Scope: the local `ask · lumen` lookup failure and creation of duplicate
terminals when an SSH/mosh agent already has a window. Tests ran on Testbed;
no test clicks or automated suites ran on lumen.

## Observed results

| Case | Production behavior observed on Testbed |
| --- | --- |
| Local tmux client without session name in argv | The regression failed on the full-probe implementation and passed with current-client matching. |
| Real Ghostty with local tmux, generic title, no session in argv | `AttentionService.open_target` focused the existing window from another workspace, acknowledged the alert, and preserved the window set. Full target probes and new-terminal launches were forbidden. |
| Two real Ghostty/mosh connections, both titled `mosh` | The actual collector returned both windows. Production activation selected the most recently used window, switched to its workspace, acknowledged the alert, and preserved both the full window identity set and the two tmux clients. Zero connection launches were attempted. |

The real-mosh test uses a private loopback SSH/mosh/tmux fixture owned by Ask.
It starts real mosh-server/client processes and supplies normal `-#` session
hints. The agent roster is synthetic and names the actual private pane process;
matching, process collection, focus dispatch, workspace navigation, and client
counts are real. It does not claim to test the popup's mouse event or the entire
ordinary mosh bootstrap command. Separate captured-command regressions cover
the three observed `0_1_9` command forms, including the session description.

Both fixtures clean up only their own windows, processes, and private servers.
After the final runs, Hyprland reported `[]` and no Ghostty, mosh, or tmux
processes remained on the isolated test desktop.

Core suite: 81 passed. Yoohoo suite: 80 passed. Both additional live activation
tests passed. The local desktop fixture needed startup waits and an explicit
private client socket; these were fixture corrections, not product fixes.

Following independent Sol review, the local fixture disables user tmux config
for server and client startup, records server/client PID and start ticks, and
verifies termination before deleting its private socket root. Cleanup failure
preserves that root. Window identity comparisons include process start ticks.
The mosh test checks the shared fixture's frozen hash before importing it.
Both live tests passed again after these safety corrections. A fresh Sol high
reviewer independently read and hash-verified the frozen pair and accepted all
five corrections with no remaining blocker in the requested static scope.
The reviewer did not import code, edit files, or rerun tests; the runtime
results above are the test owner's observations.

## Reproduction artifacts

- `tests/integration/test_local_tmux_window.py`:
  `9454b95e03b0864e46b1975557feccc80380502dfbf5372cf690a468e8063526`
- `tests/integration/test_mosh_windows.py`:
  `77ecb316ac855ea1393a9527d0f246f270c8af934c269ece58105dfa87059752`
- Ask's shared `tests/bundled-resolver-mosh-windows.py` fixture:
  `12bd8e2a5c1f8bbea2545baed34a3f38304257d7c02d596a464cd436cb572e21`

The external fixture path is test-only (`YOOHOO_MOSH_FIXTURE`), not a Yoohoo
installation dependency. The resolver remains bundled with each product.

## Installed-code correspondence

Read-only SHA256 comparison confirmed identical files in the source payload,
the Testbed test payload, and the lumen installation:

```
a066409ee03216098b6375245531ad948415a0f9e0ce97caed1668686695cda2  window-attention
c966950328cc94e1ee2a58da3dc1ef04d39de084edeb4023da9292869ebc172a  agent_window_adapter.py
f2911ba1d4cca826542ed8c1a8be571a6688ac7f37443b7cd996775d7663af45  agent_window_resolver/linux.py
24174377501e53434c8786f75641be5be9e71baa9b0e96274e9e1aca72d40d42  agent_window_resolver/resolver.py
```

Lumen received the fix on Mike's explicit instruction; only Yoohoo's tracker
was restarted. The actual user sessions and terminal windows were not altered.
