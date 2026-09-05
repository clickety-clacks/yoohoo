#!/usr/bin/env python3
"""Per-user Yoohoo installer. No sudo, network access, or package-manager writes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PLUGIN = "window-attention.indicator"
INCLUDE = 'require("hypr.attention")'
FILES = {
    "payload/window-attention": ".local/bin/window-attention",
    "payload/attention.lua": ".config/hypr/attention.lua",
    "payload/window-attention.service": ".config/systemd/user/window-attention.service",
    "payload/config.toml": ".config/window-attention/config.toml",
    "payload/Panel.qml": f".config/omarchy/plugins/{PLUGIN}/Panel.qml",
    "payload/manifest.json": f".config/omarchy/plugins/{PLUGIN}/manifest.json",
    "payload/soft-ui-pop.mp3": ".local/share/sounds/window-attention/soft-ui-pop.mp3",
    "README.md": ".local/share/window-attention/README.md",
    "LICENSE": ".local/share/window-attention/LICENSE",
    "THIRD_PARTY.md": ".local/share/window-attention/THIRD_PARTY.md",
    "tests/test_attention.py": ".local/share/window-attention/test_attention.py",
    "payload/notification_probe.py": ".local/share/window-attention/notification_probe.py",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args):
    subprocess.run(args, check=True, timeout=30)


def validate(home, activate):
    hypr = home / ".config/hypr/hyprland.lua"
    shell = home / ".config/omarchy/shell.json"
    if not hypr.is_file() or not shell.is_file():
        raise ValueError("Requires an existing Lua-based Omarchy configuration (hyprland.lua and shell.json).")
    layout = json.loads(shell.read_text()).get("bar", {}).get("layout")
    if not isinstance(layout, dict) or not isinstance(layout.get("right"), list):
        raise ValueError("Unsupported shell.json: expected bar.layout.right list; nothing changed.")
    if activate:
        for tool in ("hyprctl", "omarchy", "systemctl", "pw-play"):
            if not shutil.which(tool):
                raise ValueError(f"Missing dependency: {tool}; see README.")
        for module in ("dbus", "gi"):
            if importlib.util.find_spec(module) is None:
                raise ValueError(f"Missing Python module: {module}; see README.")
    return hypr, shell


def install(home, activate=True):
    hypr, shell = validate(home, activate)
    # Read and validate every input before writing anything.
    for source in FILES:
        if not (ROOT / source).is_file():
            raise ValueError(f"Missing distribution file: {source}")
    state = home / ".local/state/yoohoo"
    manifest = state / "install.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    backup = state / "backups" / str(time.time_ns())

    def save(path):
        if path.exists():
            dest = backup / path.relative_to(home)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    installed = dict(previous.get("files", {}))
    for source, relative in FILES.items():
        target = home / relative
        if source == "payload/config.toml" and target.exists():
            continue  # Personal settings always win, including on reinstall.
        if target.exists() and target.read_bytes() == (ROOT / source).read_bytes():
            installed[relative] = digest(target)
            continue
        save(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)
        if source == "payload/window-attention":
            target.chmod(0o755)
        installed[relative] = digest(target)

    text = hypr.read_text()
    # Preserve an existing include regardless of quote style or spacing.
    import re
    has_include = re.search(r'^\s*require\s*\(\s*[\"\']hypr\.attention[\"\']\s*\)', text, re.M)
    added_include = previous.get("added_include", False)
    if not has_include:
        save(hypr)
        hypr.write_text(text.rstrip() + "\n" + INCLUDE + "\n")
        added_include = True
    data = json.loads(shell.read_text())
    layout = data["bar"]["layout"]
    has_widget = any(isinstance(items, list) and any(
        (item.get("id") if isinstance(item, dict) else item) == PLUGIN for item in items
    ) for items in layout.values())
    added_widget = previous.get("added_widget", False)
    if not has_widget:
        save(shell)
        layout["right"].append({"id": PLUGIN})
        shell.write_text(json.dumps(data, indent=2) + "\n")
        added_widget = True
    state.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"files": installed, "added_include": added_include,
                                   "added_widget": added_widget}, indent=2) + "\n")
    print(f"Installed Yoohoo under {home}. Changed files backed up under {backup}.")
    if activate:
        print("Activating Yoohoo; an existing service restart clears pending attention.")
        run("systemctl", "--user", "daemon-reload")
        run("hyprctl", "reload")
        errors = subprocess.check_output(["hyprctl", "configerrors"], text=True, timeout=10).strip()
        if errors:
            raise ValueError(f"Hyprland reports errors; inspect config and backups before continuing: {errors}")
        run("systemctl", "--user", "enable", "window-attention.service")
        run("systemctl", "--user", "restart", "window-attention.service")
        run("omarchy", "restart", "shell")


def uninstall(home, activate=True):
    state = home / ".local/state/yoohoo"
    manifest = state / "install.json"
    if not manifest.exists():
        raise ValueError("No Yoohoo installer record; refusing to guess what to remove.")
    record = json.loads(manifest.read_text())
    if activate:
        run("systemctl", "--user", "disable", "--now", "window-attention.service")
    # Remove this subsystem's integrations, including a pre-existing manual install.
    hypr = home / ".config/hypr/hyprland.lua"
    if hypr.exists():
        import re
        hypr.write_text("".join(line for line in hypr.read_text().splitlines(True)
            if not re.match(r'^\s*require\s*\(\s*[\"\']hypr\.attention[\"\']\s*\)\s*;?\s*$', line)))
    shell = home / ".config/omarchy/shell.json"
    if shell.exists():
        data = json.loads(shell.read_text())
        for key, items in data["bar"]["layout"].items():
            if isinstance(items, list):
                data["bar"]["layout"][key] = [item for item in items
                    if (item.get("id") if isinstance(item, dict) else item) != PLUGIN]
        shell.write_text(json.dumps(data, indent=2) + "\n")
    for relative, expected in record["files"].items():
        if relative not in FILES.values():
            raise ValueError(f"Unexpected installer record path: {relative}")
        path = home / relative
        # Personal settings and history survive uninstall; modified files do too.
        if relative == ".config/window-attention/config.toml":
            continue
        if path.is_file() and digest(path) == expected:
            path.unlink()
        elif path.exists():
            print(f"Preserved modified file: {path}")
    manifest.unlink()
    if activate:
        run("systemctl", "--user", "daemon-reload")
        run("hyprctl", "reload")
        run("omarchy", "restart", "shell")
    print("Removed unchanged package files and attention integrations. Settings, history and backups retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "uninstall"])
    parser.add_argument("--home", type=Path, default=Path.home(), help="Target home (staging requires --no-activate)")
    parser.add_argument("--no-activate", action="store_true", help="Only manage files; do not contact the desktop")
    args = parser.parse_args()
    home = args.home.resolve()
    if home != Path.home().resolve() and not args.no_activate:
        parser.error("An alternate home requires --no-activate")
    if not args.no_activate:
        for name, relative in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                               ("XDG_STATE_HOME", ".local/state")):
            if os.environ.get(name) and Path(os.environ[name]).resolve() != home / relative:
                parser.error("Custom XDG paths are not supported by this installer yet")
    try:
        (install if args.command == "install" else uninstall)(home, not args.no_activate)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Yoohoo: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
