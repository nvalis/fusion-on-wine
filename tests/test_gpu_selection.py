"""Per-install GPU selection must not change defaults or sign-in environments."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from internal.runtime import Settings, load_settings, save_settings


class GpuSelectionTests(unittest.TestCase):
    def setUp(self):
        root = Path('/private/fusion')
        self.settings = Settings(root, root / 'prefix', root / 'wine', root / 'cache',
                                 root / 'logs', root / 'python', root / 'config')

    def test_default_does_not_choose_a_gpu(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn('DXVK_FILTER_DEVICE_NAME', self.settings.env(fusion=True))

    def test_saved_choice_applies_to_fusion_only(self):
        settings = replace(self.settings, dxvk_device_filter='AMD Radeon Graphics')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.env(fusion=True)['DXVK_FILTER_DEVICE_NAME'], 'AMD Radeon Graphics')
            self.assertNotIn('DXVK_FILTER_DEVICE_NAME', settings.env())

    def test_environment_overrides_saved_choice(self):
        settings = replace(self.settings, dxvk_device_filter='AMD Radeon Graphics')
        for value in ('NVIDIA', ''):
            with self.subTest(value=value), patch.dict(os.environ, {'DXVK_FILTER_DEVICE_NAME': value}, clear=True):
                self.assertEqual(settings.env(fusion=True)['DXVK_FILTER_DEVICE_NAME'], value)

    def test_desktop_configuration_round_trip_preserves_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            config_home = Path(directory)
            settings = replace(self.settings, config=config_home / 'fusion-on-wine/config.json',
                               dxvk_device_filter='AMD Radeon Graphics')
            save_settings(settings)
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}, clear=True):
                loaded = load_settings()
                self.assertEqual(loaded.dxvk_device_filter, 'AMD Radeon Graphics')
                save_settings(loaded, 'previous-handler.desktop')
                self.assertEqual(load_settings().dxvk_device_filter, 'AMD Radeon Graphics')

    def test_older_configuration_without_choice_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'fusion-on-wine/config.json'
            config.parent.mkdir()
            config.write_text('{"home": "/private/fusion"}')
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}, clear=True):
                self.assertEqual(load_settings().dxvk_device_filter, '')


if __name__ == '__main__':
    unittest.main()
