#!/usr/bin/env python3
"""Build deterministic, version-checked release assets from a committed Git tree."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def build(repo, output, tag):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Expected a stable-format version tag such as v0.1.0")
    version = git(repo, "show", "HEAD:VERSION").decode().strip()
    manifest = json.loads(git(repo, "show", "HEAD:payload/manifest.json"))
    if tag != f"v{version}" or manifest["version"] != version:
        raise ValueError("Tag, VERSION, and plugin manifest version must agree")
    if git(repo, "rev-parse", f"{tag}^{{commit}}") != git(repo, "rev-parse", "HEAD"):
        raise ValueError("Release tag must point at the checked-out commit")
    if git(repo, "status", "--porcelain").strip():
        raise ValueError("Build from a clean tree; commit changes first")
    git(repo, "show", f"HEAD:releases/{tag}.md")
    # git archive uses committed content and timestamps, never the working tree.
    archive = git(repo, "archive", "--format=tar", f"--prefix=yoohoo-{version}/", "HEAD")
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"yoohoo-{version}.tar.gz"
    artifact.write_bytes(gzip.compress(archive, compresslevel=9, mtime=0))
    checksum = output / "SHA256SUMS"
    checksum.write_text(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n")
    return artifact, checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    for artifact in build(ROOT, args.output, args.tag):
        print(artifact)


if __name__ == "__main__":
    main()
