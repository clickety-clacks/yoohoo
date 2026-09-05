"""Fresh-home installation, upgrade and removal tests; no live desktop writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yoohoo-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.hypr = self.home / ".config/hypr/hyprland.lua"
        self.shell = self.home / ".config/omarchy/shell.json"
        self.hypr.parent.mkdir(parents=True)
        self.shell.parent.mkdir(parents=True)
        self.hypr.write_text('-- Existing user configuration\n')
        self.shell.write_text(json.dumps({"idle": {"lock": 789}, "bar": {"layout": {
            "left": [{"id": "personal.widget"}], "right": []}}}))

    def test_fresh_reinstall_and_remove(self):
        installer.install(self.home, False)
        for source, target in installer.FILES.items():
            self.assertEqual((installer.ROOT / source).read_bytes(), (self.home / target).read_bytes())
        config = self.home / ".config/window-attention/config.toml"
        config.write_text("sound_enabled = false\n")
        installer.install(self.home, False)
        self.assertEqual(config.read_text(), "sound_enabled = false\n")
        self.assertEqual(self.hypr.read_text().count(installer.INCLUDE), 1)
        data = json.loads(self.shell.read_text())
        self.assertEqual(data["idle"]["lock"], 789)
        self.assertEqual(data["bar"]["layout"]["right"], [{"id": installer.PLUGIN}])
        installer.uninstall(self.home, False)
        self.assertNotIn(installer.INCLUDE, self.hypr.read_text())
        self.assertFalse((self.home / ".local/bin/window-attention").exists())
        self.assertTrue(config.exists())
        self.assertEqual(json.loads(self.shell.read_text())["bar"]["layout"]["left"], [{"id": "personal.widget"}])

    def test_adopt_manual_layout_without_duplicate(self):
        self.hypr.write_text("require('hypr.attention')\n")
        data = json.loads(self.shell.read_text())
        data["bar"]["layout"]["left"].append({"id": installer.PLUGIN})
        self.shell.write_text(json.dumps(data))
        before = self.shell.read_bytes()
        installer.install(self.home, False)
        self.assertEqual(before, self.shell.read_bytes())
        self.assertEqual(self.hypr.read_text(), "require('hypr.attention')\n")
        installer.uninstall(self.home, False)
        self.assertEqual(self.hypr.read_text(), "")

    def test_invalid_layout_writes_nothing(self):
        self.shell.write_text('{}')
        with self.assertRaises(ValueError):
            installer.install(self.home, False)
        self.assertFalse((self.home / ".local/bin/window-attention").exists())

    def test_backups_and_modified_file_preservation(self):
        target = self.home / ".local/bin/window-attention"
        target.parent.mkdir(parents=True)
        target.write_text("old user version")
        installer.install(self.home, False)
        backups = list((self.home / ".local/state/yoohoo/backups").glob("*/.local/bin/window-attention"))
        self.assertEqual(backups[0].read_text(), "old user version")
        target.write_text("modified after installation")
        installer.uninstall(self.home, False)
        self.assertEqual(target.read_text(), "modified after installation")


if __name__ == "__main__":
    unittest.main()
