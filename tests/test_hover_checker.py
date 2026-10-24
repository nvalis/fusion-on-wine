"""Non-graphical safety and RGB checks for the opt-in real Fusion checker."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("fusion_hover_checker", Path(__file__).with_name("check_fusion_hover.py"))
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class HoverCheckerTests(unittest.TestCase):
    def test_point_parser(self):
        self.assertEqual(checker.point("1450,750"), (1450, 750))
        for value in ("-1,0", "1", "1,2,3", "face,2"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                checker.point(value)

    def test_rgb_tolerance_does_not_hide_a_stale_face(self):
        self.assertTrue(checker.close_color((154, 151, 144), (154, 150, 144), 2))
        self.assertFalse(checker.close_color((111, 106, 97), (154, 150, 144), 2))

    def test_configured_prefix_is_always_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / checker.DIAGNOSTIC_MARKER).write_text('{"purpose":"graphical-regression"}')
            with self.assertRaisesRegex(RuntimeError, "configured Fusion prefix"):
                checker.validate_prefix(prefix, prefix, os.getpid())

    def test_symlink_to_configured_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix"
            prefix.mkdir()
            alias = root / "alias"
            alias.symlink_to(prefix, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "configured Fusion prefix"):
                checker.validate_prefix(alias, prefix, os.getpid())

    def test_disposable_marker_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "explicit graphical-regression marker"):
                checker.validate_prefix(root, root / "working", os.getpid())
            (root / checker.DIAGNOSTIC_MARKER).write_text(json.dumps({"purpose": "installation"}))
            with self.assertRaisesRegex(RuntimeError, "explicit graphical-regression marker"):
                checker.validate_prefix(root, root / "working", os.getpid())

    def test_process_must_use_the_disposable_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / checker.DIAGNOSTIC_MARKER).write_text('{"purpose":"graphical-regression"}')
            with self.assertRaisesRegex(RuntimeError, "selected process does not use"):
                checker.validate_prefix(root, root / "working", os.getpid())


if __name__ == "__main__":
    unittest.main()
