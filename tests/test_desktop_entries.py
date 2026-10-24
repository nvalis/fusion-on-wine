"""Desktop migration keeps one branded shortcut and preserves unrelated entries."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from internal.runtime import APP_ENTRY, CALLBACK_ENTRY, ROOT, Settings, desktop_values, install_entries


class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.apps = self.data / "applications"
        self.apps.mkdir(parents=True)
        home = root / "home"
        self.settings = Settings(home, home / "prefix", home / "wine", home / "cache",
                                 home / "logs", home / "python", root / "config/config.json")
        self.legacy = self.apps / "fusion-wine.desktop"
        self.environment = patch.dict("os.environ", {"XDG_DATA_HOME": str(self.data)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def legacy_entry(self, name="Autodesk Fusion", command=None):
        command = command or f"{ROOT / 'fusion.sh'} launch"
        self.legacy.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={command}\nIcon=fusion-wine\n")
        return self.legacy.read_bytes()

    def install(self):
        result = subprocess.CompletedProcess([], 0, stdout=CALLBACK_ENTRY + "\n")
        with patch("internal.runtime.subprocess.run", return_value=result):
            install_entries(self.settings)

    def test_name_and_installed_fusion_logo(self):
        icon = self.data / "icons/hicolor/256x256/apps/fusion-wine.png"
        icon.parent.mkdir(parents=True)
        icon.touch()
        self.install()
        values = desktop_values(self.apps / APP_ENTRY)
        self.assertEqual(values["Name"], "Autodesk Fusion")
        self.assertEqual(values["Icon"], "fusion-wine")
        self.assertIn(str(ROOT / "fusion.sh"), values["Exec"])

    def test_uses_wine_generated_vendor_icon_if_needed(self):
        entry = self.apps / "wine/Programs/Autodesk/Autodesk Fusion.desktop"
        entry.parent.mkdir(parents=True)
        contents = "[Desktop Entry]\nName=Autodesk Fusion\nNoDisplay=true\nIcon=vendor-fusion-icon\n"
        entry.write_text(contents)
        self.install()
        self.assertEqual(desktop_values(self.apps / APP_ENTRY)["Icon"], "vendor-fusion-icon")
        self.assertEqual(entry.read_text(), contents)

    def test_missing_vendor_icon_has_a_valid_fallback(self):
        self.install()
        self.assertEqual(desktop_values(self.apps / APP_ENTRY)["Icon"], "applications-engineering")

    def test_retires_only_matching_legacy_shortcut_and_keeps_backup(self):
        original = self.legacy_entry()
        self.install()
        self.assertFalse(self.legacy.exists())
        backups = list((self.settings.home / "desktop-backups").glob("*.desktop"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.apps / APP_ENTRY).is_file())

    def test_unknown_launcher_is_preserved(self):
        original = self.legacy_entry(command="/another/installation/fusion.sh launch")
        self.install()
        self.assertEqual(self.legacy.read_bytes(), original)

    def test_custom_named_shortcut_is_preserved(self):
        original = self.legacy_entry(name="My custom CAD launcher")
        self.install()
        self.assertEqual(self.legacy.read_bytes(), original)

    def test_desktop_action_is_not_confused_with_main_exec(self):
        self.legacy_entry(command="/another/application")
        with self.legacy.open("a") as output:
            output.write(f"\n[Desktop Action Fusion]\nExec={ROOT / 'fusion.sh'} launch\n")
        original = self.legacy.read_bytes()
        self.install()
        self.assertEqual(self.legacy.read_bytes(), original)

    def test_migration_is_idempotent(self):
        original = self.legacy_entry()
        self.install()
        self.legacy.write_bytes(original)
        self.install()
        self.install()
        self.assertFalse(self.legacy.exists())
        self.assertEqual(len(list((self.settings.home / "desktop-backups").glob("*.desktop"))), 1)

    def test_conflicting_backup_does_not_delete_legacy_shortcut(self):
        original = self.legacy_entry()
        self.install()
        backup = next((self.settings.home / "desktop-backups").glob("*.desktop"))
        backup.write_bytes(b"unexpected backup contents")
        self.legacy.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "backup differs"):
            self.install()
        self.assertEqual(self.legacy.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
