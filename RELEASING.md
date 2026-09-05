# Cutting a Yoohoo release

Versions use `MAJOR.MINOR.PATCH`. The `VERSION` file, plugin manifest, and
Git tag must agree. The integer `VERSION` inside the daemon describes its
JSON state format, not the package release; do not bump it for normal releases.

1. Update `VERSION` and `payload/manifest.json`.
2. Write `releases/vX.Y.Z.md` with changes, compatibility caveats, and install instructions.
3. Run `python -B -m unittest discover -s tests -v` and `git diff --check`.
4. Commit and push the release preparation to `main`.
5. Tag that commit and push the tag:

```bash
git tag -a vX.Y.Z -m "Yoohoo vX.Y.Z"
git push origin vX.Y.Z
```

The Release workflow tests the code, validates the versions and exact tag
target, builds `yoohoo-X.Y.Z.tar.gz` from committed files, writes `SHA256SUMS`,
verifies the checksum, and reruns the suite from the extracted distribution.
Only then does it create a draft with both assets and publish it as latest.
Release archives include the installer, default sound, docs, tests and licenses.
Checksums detect file corruption; they are not cryptographic signatures.

Watch with `gh run list --workflow release.yml` and inspect the release assets
with `gh release view vX.Y.Z`. A release is not done until the workflow succeeds
and the public release has both downloadable files. Desktop compatibility still
requires live testing; CI does not run Hyprland.

For a local packaging check after tagging a clean tree:

```bash
python -B scripts/build-release.py --tag vX.Y.Z
cd dist
sha256sum -c SHA256SUMS
```

Never move a published tag or silently replace a published artifact. Fix it in
a new patch release. If the workflow fails after draft creation, inspect that
draft and the run before recovery; blind reruns intentionally do not overwrite
an existing release. Fix or remove an unpublished draft explicitly before retrying.
