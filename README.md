<div align="center">

# Yoohoo! ✨

### Darling, your window would like a word.

**Know which windows need you. Get to them when you're ready.**

A breathing border, a soft pop, and a bell menu for Omarchy.

![Yoohoo's theme-colored border gently breathing around an agent window awaiting attention](assets/yoohoo-hero.gif)

![Yoohoo's attention menu showing five waiting windows, including local and remote terminal sessions.](assets/release-theme.png)

[![Tests](https://github.com/clickety-clacks/yoohoo/actions/workflows/tests.yml/badge.svg)](https://github.com/clickety-clacks/yoohoo/actions/workflows/tests.yml)
[![Code: MIT](https://img.shields.io/badge/code-MIT-f5a9b8)](LICENSE)

</div>

---

When a window needs you, Yoohoo gives it a gently breathing border, a soft
sound, and a spot in your bar's bell menu. Pick it from the menu when you're
ready. Its entry clears and its border goes back to normal. Switching to it
yourself or closing it clears the entry too. Until then, it waits.

## Agents, meet your stage manager

Running **Codex CLI or Claude Code** in several terminals? Let them work while
you do something else. When an agent asks for you through a supported terminal's
bell or desktop notification, Yoohoo marks that window and adds it to the menu.
You can see who's waiting without checking every terminal. Very considerate
of them, for once.

We recommend **Ghostty**, the terminal used for Yoohoo's live checks. Other
terminals can work if they emit supported attention signals. The agent and
terminal must actually send a bell or notification; Yoohoo doesn't read the
conversation to work out when a task is done. No agent-specific hooks are needed.

Want your own **Quickshell** UI? Use Yoohoo's attention list to build a panel
of waiting agent windows or add attention badges to your workspace overview.
Read `window-attention list` for structured JSON and call
`window-attention focus ADDRESS` when someone picks a window. Yoohoo keeps
the list up to date; your UI decides how to show it.

**Ready?** Start with [requirements](#requirements) and
[installation](#installation). [Settings](#the-dressing-room),
[verification](#receipts-please), and [uninstallation](#take-a-bow) are below too.

## She's got range

- **A tasteful pulse.** A 5px border breathes between your theme's inactive
  and active border colors. No hardcoded pink. Fabulous is theme-independent.
- **A little pop.** One soft sound when a window first joins the list, with a
  shared cooldown. Repeat requests stay quiet.
- **A guest list.** A bell-menu entry shows the window, workspace, age, and
  signal count. Select it to go there.
- **An exit cue.** Focusing or closing the window clears its entry and
  attention styling.
- **Receipts.** A local JSON list for other UIs, plus optional JSONL history.
- **Manners.** Yoohoo waits for you to pick a window before taking you there.
  See [how it works](#how-she-knows) for the detection rules and limits.

## Requirements

Yoohoo is an **early per-user integration for Lua-based Omarchy**. It has been
tested on Hyprland **0.56.2** with Omarchy's Quickshell shell. Other versions
need validation; the installer does not support older `.conf`-based Hyprland
setups. This is an independent project, not an official Omarchy component.

Required: Python **3.11+**, Hyprland's `hyprctl`, Omarchy/Quickshell, a systemd
user session, and PipeWire's `pw-play`. Standard desktop-notification capture
also needs `python-dbus` and `python-gobject` and a session bus that allows
`BecomeMonitor`. Everything runs locally. Coding agents, Ghostty, and tmux
are not required dependencies.

The optional Agentd Hub integration additionally needs a separately installed
`agentd-hub` process serving loopback `/events`. Remote launch hints use
Ghostty plus `mosh` when available and `ssh` as a fallback; these tools are
only needed if you want to open a remote agent from the menu. Yoohoo never
installs Hub, Agentd, SSH keys, or packages for you.

On an otherwise supported Omarchy install, install any missing Python bindings:

```bash
omarchy pkg add python-dbus python-gobject
```

## Installation

For a versioned installation, download the archive and `SHA256SUMS` from the
[latest release](https://github.com/clickety-clacks/yoohoo/releases/latest),
then follow that release's checksum, extraction, and install instructions.
Each archive includes the installer and default sound. No Git checkout required.

Or install the development version from the repository. Run as your desktop
user, **not root**:

```bash
git clone https://github.com/clickety-clacks/yoohoo.git
cd yoohoo
python install.py install
```

The installer checks prerequisites, copies the package, preserves existing
personal settings, adds a Hyprland include and bar entry if absent, enables
the user service, and reloads the desktop integration. It does not install
packages or replace your desktop configuration with somebody else's dotfiles.
Changed existing files are backed up under `~/.local/state/yoohoo/backups/`.
There is no automatic rollback. If activation fails, inspect the reported
error; the backups contain the files from before installation.

To update a Git checkout, run `git pull --ff-only`, then the install command
again. Restarting the service clears pending attention; history stays. The
installer also restarts the shell because hot-reloading the plugin has
previously left duplicate menu handlers running.

**Chezmoi users:** these are ordinary user files at stable paths. Chezmoi can
continue tracking them. After an update, inspect `chezmoi diff` and capture
the intended changes; otherwise a later apply may restore older versions.

## The dressing room

### Keyboard menu controls

Yoohoo provides generic actions; it does not install any global shortcuts.
Call them through `qs ipc -p /usr/share/omarchy/shell call window-attention.indicator ACTION`:

| Action | Behavior |
| --- | --- |
| `next` | Open with the first entry selected, or cycle forward if already open. |
| `previous` | Open with the last entry selected, or cycle backward if already open. |
| `accept` | Open the selected window only during a session started by next/previous. |
| `cancel` | Close without opening a window. |
| `toggle` | Ordinary menu open/close; no release-to-accept session. |

Inside the menu, Tab/Shift+Tab and arrow keys cycle, Enter opens, and Escape
cancels. Selection wraps, follows the same window across refreshes, and scrolls
into view. Clicking the bell still opens an ordinary menu.

For an optional Super+Tab switcher, add this to your personal Hyprland Lua
bindings. **This replaces Omarchy's next/previous workspace shortcuts.** Choose
different keys if you use those shortcuts. The modifier is your choice, not a
Yoohoo requirement.

```lua
hl.unbind("SUPER + TAB")
hl.unbind("SUPER + SHIFT + TAB")
local yoohoo_ipc = "qs ipc -p /usr/share/omarchy/shell call window-attention.indicator ordered "
local yoohoo_epoch = tostring(os.time()) .. "-" .. tostring(math.random(1000000))
local yoohoo_session, yoohoo_sequence = 0, 0
local function yoohoo_send(action)
  if action == "accept" and yoohoo_sequence == 0 then return end
  if yoohoo_sequence == 0 then yoohoo_session = yoohoo_session + 1 end
  yoohoo_sequence = yoohoo_sequence + 1
  hl.exec_cmd(yoohoo_ipc .. action .. " " .. yoohoo_epoch .. "-" .. yoohoo_session .. " " .. yoohoo_sequence)
  if action == "accept" then yoohoo_sequence = 0 end
end
o.bind("SUPER + TAB", "Yoohoo: open or next", function() yoohoo_send("next") end)
o.bind("SUPER + SHIFT + TAB", "Yoohoo: open or previous", function() yoohoo_send("previous") end)
o.bind("Super_L", nil, function() yoohoo_send("accept") end, { release = true, ignore_mods = true, non_consuming = true, transparent = true })
o.bind("Super_R", nil, function() yoohoo_send("accept") end, { release = true, ignore_mods = true, non_consuming = true, transparent = true })
```

Hold Super and tap Tab to cycle; add Shift to reverse; release Super to open
your selection. Escape cancels, and releasing Super after cancellation does
nothing. `transparent` keeps the release binding from being suppressed after
Tab; `ignore_mods` lets it work even if Shift is still held. No shortcuts are
changed by installation or upgrade.

The example uses `ordered ACTION STREAM SEQUENCE`, an optional IPC transport
for bindings that spawn separate processes. Give each held-key session a unique
stream ID and number its commands from 1. Yoohoo buffers out-of-order arrivals
and ignores duplicates, so a quick release cannot reach the menu before its
opening command. Plain `next`, `previous`, `accept`, and `cancel` remain available
for callers that already deliver commands in order. Losing a command or restarting
the shell mid-session can abandon that session; release and press again to start
a new one. The menu itself contains no modifier-key policy.

### Files and settings

Stage name Yoohoo, filename `window-attention`. The files keep the original
name so existing installs and dotfile tracking keep working.

| Location | What lives there |
| --- | --- |
| `~/.local/bin/window-attention` | Daemon and CLI |
| `~/.local/share/window-attention/agentd_hub.py` | Optional Hub SSE client and launch policy |
| `~/.local/share/window-attention/agent_window_adapter.py` and `agent_window_resolver/` | In-process, read-only window identity proofs used by Hub matching |
| `~/.config/window-attention/config.toml` | Your settings |
| `~/.config/hypr/attention.lua` | Focus policy and theme-aware rules |
| `~/.config/omarchy/plugins/window-attention.indicator/` | Yoohoo bell menu |
| `~/.config/systemd/user/window-attention.service` | Session service |
| `~/.local/share/sounds/window-attention/soft-ui-pop.mp3` | Default sound |
| `~/.local/share/window-attention/` | Docs, licenses, tests, diagnostic utility |
| `~/.local/state/window-attention/` | Current list and history |
| `~/.local/state/yoohoo/` | Installer record and backups |

The window resolver ships inside Yoohoo. There is no separate resolver command
to install, service to run, or path to configure. Yoohoo imports its bundled
Python library; Ask bundles the same maintained source for its own adapter.
Updating that shared source is a maintainer task, not an installation step.

The installer currently targets standard home-directory paths. Custom XDG
directory layouts are not yet supported. It appends `require("hypr.attention")`
to `~/.config/hypr/hyprland.lua` and adds `window-attention.indicator` to the
existing bar layout without duplicating it.

Set the mood in `~/.config/window-attention/config.toml`:

```toml
sound_enabled = true
sound_path = "~/.local/share/sounds/window-attention/soft-ui-pop.mp3"
sound_volume = 0.35
sound_cooldown_ms = 1500
history_enabled = true
desktop_notifications_enabled = true

[agentd_hub]
enabled = false
url = "http://127.0.0.1:8787"
```

Restart `window-attention.service` after changing daemon settings. Disable
sound if you prefer a silent entrance. Disabling desktop-notification capture
leaves native urgency active.

For rendering changes, edit `attention.lua`, run `hyprctl reload`, and check
`hyprctl configerrors`. The border breathes over a 3.6-second cycle: a
1.6-second rise, a 1.6-second fall, and a 0.4-second rest. Hyprland renders an
ease-in-out-sine approximation using a cubic Bezier with control points
`(0.37, 0)` and `(0.63, 1)`. Its shared `border` animation leaf also softens
ordinary focus-border color changes. This setting applies to all window borders.

The shape is inspired by Apple's [breathing status LED patent](https://patents.google.com/patent/US6658577B2/en):
a biased sinusoidal brightness envelope with a quiet interval. The patent's
example uses a 1.8-second overall period and a 0.4-second quiet interval;
Yoohoo deliberately uses a slower cycle, adapted to theme colors rather than
LED brightness. It approximates the patent's curve; actual Mac firmware may
use different timing or curves. The corresponding normalized fade is
`f(u) = (1 - cos(pi * u)) / 2` for `u` from 0 to 1.

### Terminal labels

The menu prefers live local tmux session names over generic terminal titles.
For explicit `ghostty -e mosh/ssh HOST tmux ...` launches, it can also show
the original remote session name and host. Remote labels describe the launch;
they cannot track later remote session switches or renames.

Discovery only reads local process metadata and queries the default local tmux
server, with a short timeout. It never connects to another machine or reads
conversation contents. Missing tools, unsupported launch syntax, and ambiguous
shared terminal processes fall back to the window title. The original title is
retained as `window_title` in enriched `window-attention list` output. This is
menu presentation only; attention detection and stored history are unchanged.

### Agents across machines (optional)

Yoohoo can subscribe to a local [Agentd Hub](https://github.com/clickety-clacks/agentd-hub)
instance. Hub is a separate, read-only process: it collects complete Agentd
snapshots over the user's existing SSH access and serves them on loopback. Yoohoo
does not install, discover, or manage Hub, and it never sends commands to Agentd.

Install and start Hub separately, then opt in from
`~/.config/window-attention/config.toml`:

```toml
[agentd_hub]
enabled = false
url = "http://127.0.0.1:8787"
```

Keep `enabled = false` unless that loopback endpoint is available. Hub rows are
shown alongside native window alerts and retain their machine and Agentd
identity. A disconnected source stays visible as unavailable; Yoohoo never
turns stale activity into idle and never lets an unavailable row acknowledge or
open an agent. Reconnects start from the current complete snapshot, so a
previous event is not replayed. Enabling Hub does not require SSH keys or
credentials in Yoohoo's configuration.

The bell reports Hub health separately from the attention count. Its states are
`connecting`, `live`, `reconnecting`, `stale`, `suspended`, and `disabled`.
`live` starts only after a validated snapshot; subsequent fresh heartbeats keep
it live, while a connected socket by itself is not treated as healthy. Yoohoo
expires an abandoned connection after 45 seconds (Hub heartbeats normally
arrive every 15 seconds),
and labels retained remote rows with the snapshot age while the source is
reconnecting or stale. The UI derives this timeout from
`lastSnapshotAtUnixMs` and `lastSeenAtUnixMs`, so an old cached status cannot
silently become green; a quiet but healthy roster remains live when heartbeats
are fresh. Malformed or legacy bool-only health data fails closed
and leaves remote rows unavailable. The bar adds `…` while connecting and `!`
for stale, reconnecting, or suspended Hub state; those outage states use the
urgent theme color even when no attention windows are waiting.

Yoohoo listens for logind's sleep/wake signals. Before sleep it marks the feed
suspended and closes the old stream; on wake it requests a new subscription.
Failed connections retry with capped exponential backoff and jitter. Heartbeat
loss also triggers recovery, so an interrupted connection need not produce an
explicit network error. This uses the existing Python GObject dependency;
there are no new tmux hooks, harness wrappers, or snapshot polling jobs.

When an available remote row has a launch hint, Yoohoo opens it in Ghostty,
preferring `mosh` and falling back to `ssh`. This is a best-effort connection
attempt, not an agent command channel. If the connection fails, the row remains
unacknowledged and the local window keeps its attention state.

A Hub row only appears when there is something a person can do with it: either
Yoohoo has matched the agent to a window on this machine, or the agent has a
tmux session that can be attached. An agent with neither (for example Claude
running inside another application's own window) is not listed as a standalone
Agentd entry and makes no sound; the native attention alert for its window,
if any, still works as usual. When an entry that was listed can no longer be
opened, the menu says why in plain language and offers **Dismiss alert** (or
press `D`). Dismissing clears that one entry only; the agent keeps running and
a genuinely new request for attention appears again. The same action is
available as `window-attention dismiss <target>` for a row id or address.

## How she knows

Two existing desktop signals, one attention list:

1. **Native urgency:** Hyprland emits `urgent` with an exact window identity.
   `misc.focus_on_activate = false` prevents ordinary activation requests
   from moving focus. Yoohoo records and tags the window instead.
2. **Standard desktop notifications:** a passive D-Bus monitor observes
   `org.freedesktop.Notifications.Notify`, obtains the sender PID from bus
   credentials, and accepts it **only if it owns exactly one Hyprland window**.

The daemon owns tags and state. Hyprland owns rendering. The shell plugin
reads the list. Only an explicit menu selection requests focus.

### Even a diva has boundaries

- Some notification senders can't be matched to a window: `notify-send` and
  similar helpers, proxies, disconnected senders, and processes that own
  several windows. Yoohoo skips those notifications. Native urgency still
  works if the application emits it.
- A finished job that emits neither signal cannot be detected. Yoohoo does
  not watch agent lifecycles or infer completion from terminal text.
- If an app emits both inputs, the count may increase twice. It counts
  **signals**, not tasks; repeated signals do not replay the sound.
- Hyprland's foreign-toplevel activation can bypass its ordinary activation
  policy (window switchers need this). Yoohoo never invokes it automatically,
  but cannot stop other tools from doing so.
- Already-focused windows are ignored. Orderly service stops clear pending
  entries. Crash recovery requires matching addresses and stable window IDs.
- Notification payload text is not stored, but history includes **window
  titles**, which can be sensitive. History is local, unbounded, and has no
  automatic rotation yet. Disable it or manage retention yourself.
- Theme colors that are identical will not produce a visible color pulse;
  disabled border animations make the transitions discrete.

## Receipts, please

```bash
window-attention list
window-attention dismiss <row-id-or-address>
window-attention play-sound
systemctl --user status window-attention
journalctl --user -u window-attention
python -B -m unittest discover -s tests -v
```

`~/.local/state/window-attention/current.json` has a versioned `windows` list.
Each entry contains an address, stable ID, title, class, workspace,
timestamps, signal count, and source. Events append to `history.jsonl` when
enabled. Custom UIs can read the list without scraping notifications.

The installed regression suite can also run with:

```bash
python -B ~/.local/share/window-attention/test_attention.py
```

Automated tests cover daemon state/race handling and staged installation,
JavaScript selection policy (the full development suite also requires Node.js),
reinstallation, config preservation, and removal. Desktop behavior and
compatibility with other Omarchy versions require live testing. Live checks
on the original deployment exercised both native urgency and real terminal
desktop notifications, unchanged focus, sound playback, interpolated border
pixels, menu selection, acknowledgement, and shutdown cleanup.

For a disposable Agentd/Hub wire check on a prepared Plumbus testbed, run
`python3 tests/integration/plumbus_hub_wire.py`. It creates one synthetic
Codex process in a uniquely named tmux session, forces Hub's explicit
hosts-file fallback with temporary discovery shims, verifies the reporting
snapshot on loopback port 8788, and cleans up that fixture session. The runner
does not exercise cross-host SSH; it reports that scope explicitly because
self-SSH may not be configured.

For a native terminal check, arrange a delayed BEL in a terminal that supports
compositor attention, switch away before it fires, then acknowledge it through
the bell menu. A passive diagnostic is included at
`~/.local/share/window-attention/notification_probe.py`; it logs sender identity,
not notification content. Neither the diagnostic nor Yoohoo invokes a
notification action to discover its target window.

## Take a bow

```bash
python install.py uninstall
```

Stops/disables the service, removes attention integrations and unchanged
package files, and retains personal settings, event history, backups, and
modified files. Uninstallation requires its install record. Empty parent
directories may remain. Chezmoi users, update your tracked state too, or a
later restore will bring Yoohoo right back.

For file-only staging or testing, use `--home /path/to/staged-home --no-activate`.
That home must contain a supported `hyprland.lua` and `shell.json`; no real
desktop commands run in this mode.

## Credits & couture

Code: [MIT](LICENSE). The soft UI pop is by **humordome**, Pixabay asset
**451232**, used as Yoohoo's notification cue under the separate Pixabay
Content License. See [third-party terms](THIRD_PARTY.md); the sound is **not**
MIT licensed and must not be redistributed as a standalone audio asset.

Made by [clickety-clacks](https://github.com/clickety-clacks).
Independent of the Omarchy project.

Maintainers: [how to cut a release](RELEASING.md).

**They can wait, darling.** 💅
