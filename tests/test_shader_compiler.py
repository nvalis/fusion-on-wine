"""Stage and verify the bundled compiler without modifying Wine's runtime files."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from internal.runtime import Settings, check_shader_compiler
from internal.setup import setup_shader_compiler


class ShaderCompilerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = Settings(root, root / "prefix", root / "runtime", root / "cache",
                                 root / "logs", root / "python", root / "config")
        self.settings.prefix.mkdir()
        self.marker = self.settings.prefix / ".fusion-on-wine-prefix.json"
        self.marker.write_text('{"schema": 1}')
        application = self.settings.prefix / "drive_c/application"
        application.mkdir(parents=True)
        self.executable = application / "Fusion360.exe"
        self.executable.touch()
        self.source = application / "d3dcompiler_47.dll"
        self.source.write_bytes(b"bundled native compiler")
        self.target = self.settings.prefix / "drive_c/windows/system32/d3dcompiler_47.dll"
        self.target.parent.mkdir(parents=True)
        self.backup = self.target.with_name(self.target.name + ".before-fusion-native-compiler")

    def stage(self):
        with patch("internal.setup.active_prefix_processes", return_value=[]):
            setup_shader_compiler(self.settings, self.executable)

    def test_stages_bundled_compiler_and_preserves_original(self):
        self.target.write_bytes(b"Wine compiler")
        self.stage()
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.backup.read_bytes(), b"Wine compiler")
        self.assertEqual(check_shader_compiler(self.settings, self.executable), self.source)

    def test_replaces_symlink_without_overwriting_runtime(self):
        runtime = self.settings.runtime / "compiler.dll"
        runtime.parent.mkdir()
        runtime.write_bytes(b"private Wine runtime compiler")
        self.target.symlink_to(runtime)
        self.stage()
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(runtime.read_bytes(), b"private Wine runtime compiler")
        self.assertEqual(self.backup.read_bytes(), runtime.read_bytes())

    def test_rerun_is_idempotent_and_preserves_first_backup(self):
        self.target.write_bytes(b"original")
        self.stage()
        before = self.target.stat().st_mtime_ns
        with patch("internal.setup.active_prefix_processes", return_value=[123]):
            setup_shader_compiler(self.settings, self.executable)
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.source.write_bytes(b"updated bundled compiler")
        self.stage()
        self.assertEqual(self.backup.read_bytes(), b"original")
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_refuses_unmanaged_prefix(self):
        self.marker.unlink()
        with self.assertRaisesRegex(RuntimeError, "unmanaged prefix"):
            self.stage()
        self.assertFalse(self.target.exists())

    def test_refuses_to_change_running_prefix(self):
        self.target.write_bytes(b"original")
        with patch("internal.setup.active_prefix_processes", return_value=[123]):
            with self.assertRaisesRegex(RuntimeError, "Save and close"):
                setup_shader_compiler(self.settings, self.executable)
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertFalse(self.backup.exists())

    def test_copy_failure_keeps_original_and_removes_temporary(self):
        self.target.write_bytes(b"original")
        copy = shutil.copy2

        def fail_staging(source, target):
            if target.name.endswith(".new"):
                target.write_bytes(b"partial copy")
                raise OSError("disk full")
            return copy(source, target)

        with patch("internal.setup.shutil.copy2", side_effect=fail_staging):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.stage()
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertFalse(self.target.with_name(self.target.name + ".new").exists())

    def test_launch_check_rejects_missing_or_wrong_compiler(self):
        with self.assertRaisesRegex(RuntimeError, "not staged"):
            check_shader_compiler(self.settings, self.executable)
        self.target.write_bytes(b"Wine compiler")
        with self.assertRaisesRegex(RuntimeError, "not staged"):
            check_shader_compiler(self.settings, self.executable)

    def test_launch_check_detects_changed_autodesk_compiler(self):
        self.stage()
        self.source.write_bytes(b"new vendor compiler")
        with self.assertRaisesRegex(RuntimeError, "not staged"):
            check_shader_compiler(self.settings, self.executable)

    def test_missing_bundle_is_not_replaced_with_wine(self):
        self.source.unlink()
        self.target.write_bytes(b"Wine compiler")
        with self.assertRaisesRegex(RuntimeError, "bundled d3dcompiler_47.dll is missing"):
            self.stage()
        self.assertEqual(self.target.read_bytes(), b"Wine compiler")

    def test_error_logging_uses_the_wine_error_class(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.settings.env()["WINEDEBUG"], "-all,err+all")
        with patch.dict("os.environ", {"WINEDEBUG": "-all"}, clear=True):
            self.assertEqual(self.settings.env()["WINEDEBUG"], "-all")


if __name__ == "__main__":
    unittest.main()
