"""Non-graphical coverage for the opt-in Wine presentation workaround."""
from dataclasses import replace
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from internal.runtime import Settings, check_runtime, manifest
from internal.setup import fingerprint


class PresentationTests(unittest.TestCase):
    def setUp(self):
        root = Path("/private/fusion")
        self.settings = Settings(root, root / "prefix", root / "wine", root / "cache",
                                 root / "logs", root / "python", root / "config")

    def test_fusion_enables_sync_on_all_compositors(self):
        for session in ({}, {"SWAYSOCK": "/sway/socket"}, {"HYPRLAND_INSTANCE_SIGNATURE": "test"}):
            with self.subTest(session=session), patch.dict(os.environ, session, clear=True):
                self.assertEqual(self.settings.env(fusion=True)["FUSION_WINE_SYNC_PRESENT"], "1")

    def test_explicit_disable_for_comparison(self):
        with patch.dict(os.environ, {"FUSION_WINE_SYNC_PRESENT": "0"}, clear=True):
            self.assertEqual(self.settings.env(fusion=True)["FUSION_WINE_SYNC_PRESENT"], "0")

    def test_bootstrap_and_callback_do_not_inherit_sync(self):
        with patch.dict(os.environ, {"FUSION_WINE_SYNC_PRESENT": "1",
                                    "FUSION_WINE_READBACK_PRESENT": "1"}, clear=True):
            env = self.settings.env()
        self.assertNotIn("FUSION_WINE_SYNC_PRESENT", env)
        self.assertNotIn("FUSION_WINE_READBACK_PRESENT", env)

    def test_old_diagnostic_switch_is_not_needed_or_inherited(self):
        with patch.dict(os.environ, {"FUSION_WINE_READBACK_PRESENT": "1"}, clear=True):
            env = self.settings.env(fusion=True)
        self.assertEqual(env["FUSION_WINE_SYNC_PRESENT"], "1")
        self.assertNotIn("FUSION_WINE_READBACK_PRESENT", env)

    def test_old_patch_runtime_can_be_stopped_but_not_launched(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "bin").mkdir()
            (runtime / "bin/wine").touch()
            (runtime / "bin/wineserver").touch()
            (runtime / ".fusion-on-wine-build.json").write_text("{}")
            settings = replace(self.settings, runtime=runtime)
            with patch("internal.runtime.subprocess.check_output",
                       return_value="wine-" + manifest()["wine"]["version"] + "\n"):
                with self.assertRaisesRegex(RuntimeError, "patches differ"):
                    check_runtime(settings)
                check_runtime(settings, allow_old_patches=True)
            with patch("internal.runtime.subprocess.check_output", return_value="wine-wrong\n"):
                with self.assertRaisesRegex(RuntimeError, "Expected"):
                    check_runtime(settings, allow_old_patches=True)

    def test_patch_is_ordered_and_fingerprinted(self):
        spec = manifest()
        path = "internal/patches/wine-present.patch"
        self.assertGreater(spec["wine"]["patches"].index(path),
                           spec["wine"]["patches"].index("internal/patches/wine-palettes.patch"))
        original = fingerprint(spec)
        self.assertIn(path, original["patches"])
        with patch("internal.setup.sha256", return_value="changed"):
            self.assertNotEqual(fingerprint(spec), original)


if __name__ == "__main__":
    unittest.main()
