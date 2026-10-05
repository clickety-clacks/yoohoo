#!/usr/bin/env python3
"""Re-vendor agent-window-resolver from a named upstream ref.

    scripts/sync-resolver.py REF [--repo PATH_OR_URL]

Copies the package's Python modules from exactly that commit into this
repository's vendored copy, copies the shared transport-policy vectors, and
writes VENDORED.json beside the copy: upstream URL, commit, sync date, and the
git blob id of every vendored file, so a test can prove the copy is that
commit without network access. Review the diff and run the tests afterwards.
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import subprocess
import tempfile

UPSTREAM = "https://github.com/clickety-clacks/agent-window-resolver"
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "payload/agent_window_resolver"
# Shared vectors and pure regression suites that Yoohoo carries verbatim.
EXTRAS = {
    "fixtures/transport-policy-v1.json": ROOT / "tests/fixtures/transport-policy-v1.json",
    "tests/test_tmux_launch_hints.py": ROOT / "tests/test_tmux_launch_hints.py",
    "tests/test_qualified_tmux_targets.py": ROOT / "tests/test_qualified_tmux_targets.py",
}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref", help="branch, tag or commit in the upstream repository")
    parser.add_argument("--repo", default=UPSTREAM + ".git",
                        help="upstream clone URL or local checkout (default: GitHub)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="resolver-sync-") as temporary:
        clone = Path(temporary) / "upstream"
        subprocess.run(["git", "clone", "--quiet", args.repo, str(clone)], check=True)
        # A fresh clone has only its default branch locally; other branches
        # resolve through origin/. Tags and commit ids resolve directly.
        for candidate in (args.ref, "origin/" + args.ref):
            try:
                commit = git(clone, "rev-parse", "--verify", "--quiet",
                             candidate + "^{commit}").decode().strip()
                break
            except subprocess.CalledProcessError:
                continue
        else:
            raise SystemExit(f"{args.ref} is not a branch, tag or commit upstream")
        listing = git(clone, "ls-tree", "-r", commit, "--", "agent_window_resolver").decode()
        files = {}
        for line in listing.splitlines():
            meta, path = line.split("\t", 1)
            _, kind, blob = meta.split()
            if kind == "blob" and path.endswith(".py") and "/" not in path.split("/", 1)[1]:
                files[path.split("/", 1)[1]] = blob
        if "__init__.py" not in files:
            raise SystemExit(f"{args.ref} has no agent_window_resolver package")
        for stale in PACKAGE.glob("*.py"):
            if stale.name not in files:
                stale.unlink()
        for name in files:
            (PACKAGE / name).write_bytes(git(clone, "show", f"{commit}:agent_window_resolver/{name}"))
        extras = {}
        for source, target in EXTRAS.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(clone, "show", f"{commit}:{source}"))
            extras[source] = {
                "vendoredAt": str(target.relative_to(ROOT)),
                "blob": git(clone, "rev-parse", f"{commit}:{source}").decode().strip(),
            }
    record = {
        "upstream": UPSTREAM,
        "ref": args.ref,
        "commit": commit,
        "syncedOn": datetime.date.today().isoformat(),
        "files": dict(sorted(files.items())),
        "extras": extras,
    }
    (PACKAGE / "VENDORED.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"vendored agent-window-resolver {commit} ({args.ref})")


if __name__ == "__main__":
    main()
