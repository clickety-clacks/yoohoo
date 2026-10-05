# Steady border across attention sources

Yoohoo's native-window and Hub inputs can both own attention for one window.
The steady `window-attention` tag must remain until the last owner clears.
Only `window-attention-pulse` should toggle during the breathing cycle. Both
rules use the same border width; dropping the steady tag lets the dark phase
fall back to the ordinary window border and changes the application's size.

## Reproduction (September 14, 2026)

On the test machine, the installed daemon source (`3bb482b983bba6ba8b5b8d8794118f68c52cfd702ac8de566749b1f04c7e2064`)
was loaded from an isolated staging directory. One owned Ghostty window ran
`sleep 15`, with user terminal configuration disabled. No tmux was used.
The fixture supplied both a native-window record and a synthetic Hub match
for that same window, then called the production `_apply_hub_matches({})`
and `pulse()` paths. Configuration and state were injected into a temporary
directory; there was no live Hub connection or installed service change.

After the Hub match disappeared, the native record remained, but the steady
tag was gone. Real Hyprland snapshots during four pulse phases showed the
window content changing between **2370×1294 at (15,41)** and
**2376×1300 at (12,38)**. This reproduces actual layout movement, not merely
a changing color. The owned terminal exited naturally and the desktop
returned to its initial empty inventory.

The breathing curve, colors, and 1.6-second fade / 0.4-second rest settings
are not implicated and should remain unchanged by the ownership fix.

## Fix and regression coverage

Presentation cleanup checks the union of native and Hub owners. Removing
one owner no longer removes and re-adds the tags belonging to another.
Focus actions from short-lived CLI processes leave visual cleanup to the
daemon, which has the Hub ownership state. Each pulse checks the existing
compositor snapshot and repairs a missing steady tag before changing the
pulse tag; an already-present steady tag needs no additional dispatch.

The first seven ownership regressions failed in six cases against the old
installed source. These cover native-plus-Hub overlap, disconnect cleanup,
multiple Hub claims on one window, focus cleanup, and missing-tag repair.
The existing query-failure test now explicitly supplies its assumed steady
tag; missing-tag repair has its own regression.

The corrected live test machine run retained **2370×1294 at (15,41)** across all
four sampled phases, with the steady tag always present and the pulse tag
observed both present and absent. The test terminal exited naturally and
the desktop was empty afterward. No tests or artificial attention signals
were run on the desktop.

## Verification and installation

The final source passed **99 tests on the test machine** and independent Sol high
static review. The live geometry gate used daemon `58029a3a…`; the only
subsequent daemon change deduplicates previous Hub addresses with `set()`.
That final change is covered by the simultaneous-removal regression.

Final SHA-256 values:

- Daemon: `685b786c405ccdb6c651e75ae8b1d4233197972f91eee97c4e0f31ebca26e55d`
- Ownership tests: `8a47a90fc1e34f1ad632f7994b29e62e3782d576d732f64dca6576b9cdf8ebd6`
- Attention tests: `dbe8e2af29eacf52f8d2464c78006e93b1d7d262761fe81ede60fcae93953a5e`

Installed on the desktop at **10:54 PM PT, September 14, 2026**. Only
`window-attention.service` was restarted. Its native and notification
listeners started successfully, and its Hub status became connected without
an error. The installed daemon and tracked chezmoi source match the reviewed
hash. Animation configuration was not modified.

The previous daemon, chezmoi source, and installation manifest are backed up
under `~/.local/state/yoohoo/backups/pulse-border.TbryS4XM/`. The installation
manifest was updated for the daemon only. No release was cut or pushed.
