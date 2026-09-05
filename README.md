<div align="center">

# Yoohoo! ✨

### Darling, your window would like a word.

**A little drama. Zero focus theft.**

Theme-aware window attention for Omarchy: a soft pulse, a tiny pop,
and a guest list of windows waiting for their moment.

[![Tests](https://github.com/clickety-clacks/yoohoo/actions/workflows/tests.yml/badge.svg)](https://github.com/clickety-clacks/yoohoo/actions/workflows/tests.yml)
[![Code: MIT](https://img.shields.io/badge/code-MIT-f5a9b8)](LICENSE)

**Make an entrance. Don't interrupt the number.**

</div>

---

Your terminal finishes. Your application needs you. Your cursor is halfway
through something important. This is not the moment for a hostile takeover.

Yoohoo listens for applications requesting your attention and marks the window
that needs you without switching your workspace or taking focus away from what
you're doing. Its border softly pulses in your theme's colors, a gentle pop
plays, and the window joins a list in your bar's bell menu. When you're ready,
select it from the menu to go there, or focus it yourself; either way, the
attention marker clears and its normal border returns. Closing the window
clears it too. A local history keeps a record of the requests, so the drama
doesn't have to disappear without receipts.

## Agents, meet your stage manager

Yoohoo is especially handy when you're working with coding agents such as
**Codex CLI and Claude Code** across several terminal windows. Let them work
while you do something else; when they request attention through a supported
terminal's bell or desktop notification, Yoohoo gives their window a little
wave and a place in the bell menu. No agent-specific hooks required. The
agent and terminal must actually emit one of those signals—Yoohoo doesn't
infer that a task has finished by reading the conversation.

And the included bell menu is only one way to use it. You can build your own
**Quickshell customizations** around Yoohoo's shared attention list: a panel
of waiting agent windows, a workspace overview with attention badges, or an
entirely different way to choose what gets your attention next. Read
`window-attention list` for structured JSON, and use
`window-attention focus ADDRESS` when the user selects a window. Yoohoo
handles the tracking and acknowledgement; you get to design the experience.

**Ready?** Start with [requirements](#requirements--before-the-entrance) and
[installation](#installation--get-in-darling). [Settings](#the-dressing-room),
[verification](#receipts-please), and [uninstallation](#take-a-bow) are below too.

## She's got range

- **A tasteful pulse.** A 5px border breathes between your theme's inactive
  and active border colors. No hardcoded pink. Fabulous is theme-independent.
- **A little pop.** A soft notification sound on first entry, with a shared
  cooldown. Repeated requests don't keep playing it.
- **A guest list.** A bell-menu entry shows the window, workspace, age, and
  signal count. Select it to go there.
- **An exit cue.** Focusing or closing the window clears its entry and
  attention styling. Acknowledged, not held hostage.
- **Receipts.** A local JSON list for other UIs, plus optional JSONL history.
- **Manners.** No automatic notification clicks. No agent hooks. No tmux
  inference. No pretending every desktop notification identifies a window.

## Requirements — before the entrance

This is an **early per-user integration for Lua-based Omarchy**, not a
universal Hyprland extension or an official Omarchy component. The desktop
integration has been exercised on Hyprland **0.56.2** with Omarchy's
Quickshell shell. Other versions need validation; older `.conf`-based
Hyprland setups are not supported by this installer.

Required: Python **3.11+**, Hyprland's `hyprctl`, Omarchy/Quickshell, a systemd
user session, and PipeWire's `pw-play`. Standard desktop-notification capture
also needs `python-dbus` and `python-gobject` and a session bus that allows
`BecomeMonitor`. There is no pip environment, web server, cloud service,
Codex, Claude, Ghostty, or tmux dependency.

On an otherwise supported Omarchy install, install any missing Python bindings:

```bash
omarchy pkg add python-dbus python-gobject
```

## Installation — get in, darling

Run as your desktop user, **not root**:

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
If activation fails, inspect the reported error and backups; the installer
does not claim transactional rollback.

Reinstall/update with the same command after `git pull --ff-only`. A service
restart clears current pending attention; history remains. Shell restart is
intentional: plugin hot reload has previously left duplicate IPC handlers.

**Chezmoi users:** these are ordinary user files at stable paths. Chezmoi can
continue tracking them. After an update, inspect `chezmoi diff` and capture
the intended changes; otherwise a later apply may restore older versions.

## The dressing room

Yoohoo retains the established `window-attention` technical filenames so
existing installations and dotfile tracking continue to work.

| Location | What lives there |
| --- | --- |
| `~/.local/bin/window-attention` | Daemon and CLI |
| `~/.config/window-attention/config.toml` | Your settings |
| `~/.config/hypr/attention.lua` | Focus policy and theme-aware rules |
| `~/.config/omarchy/plugins/window-attention.indicator/` | Yoohoo bell menu |
| `~/.config/systemd/user/window-attention.service` | Session service |
| `~/.local/share/sounds/window-attention/soft-ui-pop.mp3` | Default sound |
| `~/.local/share/window-attention/` | Docs, licenses, tests, diagnostic utility |
| `~/.local/state/window-attention/` | Current list and history |
| `~/.local/state/yoohoo/` | Installer record and backups |

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
```

Restart `window-attention.service` after changing daemon settings. For
rendering changes, edit `attention.lua`, run `hyprctl reload`, and check
`hyprctl configerrors`. The border alternates every 900ms; Hyprland's border
animation supplies the interpolation. Disable sound if you prefer a silent
entrance; disabling desktop-notification capture leaves native urgency active.

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

- A helper like `notify-send`, a proxy, a disconnected sender, or a process
  owning several windows may not resolve. These notifications are skipped,
  not guessed. Native urgency still works if that application emits it.
- A finished job that emits neither signal cannot be detected. Yoohoo does
  not watch agent lifecycles or infer completion from terminal text.
- If an app emits both inputs, the count may increase twice. It counts
  **signals**, not tasks; repeated signals do not replay the sound.
- Hyprland's foreign-toplevel activation can bypass its ordinary activation
  policy (window switchers need this). Yoohoo never invokes it automatically;
  this is not a universal firewall against every possible focus change.
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
reinstallation, config preservation, and removal. They do **not** simulate
a complete compositor or prove every Omarchy release compatible. Live checks
on the original deployment exercised both native urgency and real terminal
desktop notifications, unchanged focus, sound playback, interpolated border
pixels, menu selection, acknowledgement, and shutdown cleanup.

For a native terminal check, arrange a delayed BEL in a terminal that supports
compositor attention, switch away before it fires, then acknowledge it through
the bell menu. A passive diagnostic is included at
`~/.local/share/window-attention/notification_probe.py`; it logs sender identity,
not notification content. Never automatically invoke notification actions to
try to discover their target windows.

## Take a bow

```bash
python install.py uninstall
```

Stops/disables the service, removes attention integrations and unchanged
package files, and retains personal settings, event history, backups, and
modified files. It refuses to guess without its install record. Empty parent
directories may remain. Chezmoi users should also update their tracked state
if they don't want a later restore to reinstall Yoohoo.

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

**Your windows may crave attention. Your focus is still yours.** 💅
