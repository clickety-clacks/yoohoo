"""Release integrity checks use isolated Git repositories, never a desktop."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[1] / "scripts/build-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yoohoo-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.output = Path(self.temp.name) / "output"
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Release test")
        (self.repo / "payload").mkdir()
        (self.repo / "releases").mkdir()
        (self.repo / "VERSION").write_text("0.1.0\n")
        (self.repo / "payload/manifest.json").write_text(json.dumps({"version": "0.1.0"}))
        (self.repo / "releases/v0.1.0.md").write_text("Debut\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.git("tag", "v0.1.0")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL)

    def test_archive_checksum_and_determinism(self):
        artifact, checksum = release.build(self.repo, self.output, "v0.1.0")
        original = artifact.read_bytes()
        self.assertEqual(checksum.read_text(), f"{hashlib.sha256(original).hexdigest()}  yoohoo-0.1.0.tar.gz\n")
        with tarfile.open(artifact) as archive:
            self.assertEqual(archive.extractfile("yoohoo-0.1.0/VERSION").read(), b"0.1.0\n")
            self.assertFalse(any("/.git/" in name for name in archive.getnames()))
        release.build(self.repo, self.output, "v0.1.0")
        self.assertEqual(artifact.read_bytes(), original)

    def test_mismatched_version_rejected(self):
        with self.assertRaises(ValueError):
            release.build(self.repo, self.output, "v0.2.0")

    def test_dirty_tree_rejected(self):
        (self.repo / "uncommitted").write_text("not shipped")
        with self.assertRaises(ValueError):
            release.build(self.repo, self.output, "v0.1.0")

    def test_tag_must_identify_head(self):
        self.git("commit", "--allow-empty", "-qm", "later")
        with self.assertRaises(ValueError):
            release.build(self.repo, self.output, "v0.1.0")


if __name__ == "__main__":
    unittest.main()
