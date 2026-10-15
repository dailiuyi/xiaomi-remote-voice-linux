#!/usr/bin/env python3
"""Tests for unified configuration and diagnostic script configure.py."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import configure


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)

        self.mock_home = self.temp_path / 'home'
        self.mock_home.mkdir(parents=True)
        self.mock_config_dir = self.mock_home / '.config/mi-voice'
        self.mock_key_file = self.mock_config_dir / 'api.key'
        self.mock_remapper = self.mock_home / '.config/input-remapper'
        self.mock_presets = self.mock_remapper / 'presets'
        self.mock_systemd = self.mock_home / '.config/systemd/user'
        self.mock_bin = self.mock_home / '.local/bin'
        self.mock_desktop = self.mock_home / 'Desktop'
        self.mock_app = self.mock_home / '.local/share/applications'

        self.patches = [
            patch.object(configure, 'HOME', self.mock_home),
            patch.object(configure, 'CONFIG_DIR', self.mock_config_dir),
            patch.object(configure, 'KEY_FILE', self.mock_key_file),
            patch.object(configure, 'REMAPPER_DIR', self.mock_remapper),
            patch.object(configure, 'REMAPPER_PRESETS', self.mock_presets),
            patch.object(configure, 'USER_SYSTEMD_DIR', self.mock_systemd),
            patch.object(configure, 'LOCAL_BIN', self.mock_bin),
            patch.object(configure, 'DESKTOP_DIR', self.mock_desktop),
            patch.object(configure, 'APP_DIR', self.mock_app),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_setup_key_writes_mode_600(self):
        res = configure.setup_key('test-key-12345')
        self.assertTrue(res['success'])
        self.assertTrue(self.mock_key_file.is_file())
        self.assertEqual(self.mock_key_file.read_text().strip(), 'test-key-12345')
        mode = stat.S_IMODE(self.mock_key_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_setup_key_rejects_empty_or_newlines(self):
        with self.assertRaises(ValueError):
            configure.setup_key('   ')
        with self.assertRaises(ValueError):
            configure.setup_key('key-with\nnewline')

    def test_inspect_credentials_checks_permissions(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            self.mock_config_dir.mkdir(parents=True, exist_ok=True)
            self.mock_key_file.write_text('valid-token\n')
            self.mock_key_file.chmod(0o644)  # Insecure mode
            info = configure.inspect_credentials()
            self.assertTrue(info['file_exists'])
            self.assertFalse(info['file_mode_secure'])

            self.mock_key_file.chmod(0o600)  # Secure mode
            info_sec = configure.inspect_credentials()
            self.assertTrue(info_sec['file_mode_secure'])
            self.assertTrue(info_sec['configured'])

    def test_setup_desktop_creates_all_files_with_exec_bit(self):
        res = configure.setup_desktop()
        self.assertTrue(res['success'])
        launcher = self.mock_bin / 'mi-voice'
        desktop_start = self.mock_desktop / 'mi-voice.desktop'
        desktop_stop = self.mock_desktop / 'mi-voice-stop.desktop'
        app_start = self.mock_app / 'mi-voice.desktop'
        app_stop = self.mock_app / 'mi-voice-stop.desktop'

        for f in (launcher, desktop_start, desktop_stop, app_start, app_stop):
            self.assertTrue(f.is_file(), f"{f} should exist")
            mode = stat.S_IMODE(f.stat().st_mode)
            self.assertTrue(mode & 0o111, f"{f} should be executable")

        dt_info = configure.inspect_desktop()
        self.assertTrue(dt_info['all_ready'])

    def test_setup_keymap_deploys_dual_device_presets(self):
        res = configure.setup_keymap()
        self.assertTrue(res['success'])
        p1 = self.mock_presets / 'MI RC' / 'vibe_coding.json'
        p2 = self.mock_presets / '小米蓝牙语音遥控器' / 'vibe_coding.json'
        cfg = self.mock_remapper / 'config.json'
        srv = self.mock_systemd / 'input-remapper-autoload.service'

        self.assertTrue(p1.is_file())
        self.assertTrue(p2.is_file())
        self.assertTrue(cfg.is_file())
        self.assertTrue(srv.is_file())

        km_info = configure.inspect_keymap()
        self.assertTrue(km_info['presets_ready'])
        self.assertTrue(km_info['config_ready'])
        self.assertTrue(km_info['service_installed'])

    def test_setup_keymap_merges_existing_autoload_entries(self):
        self.mock_remapper.mkdir(parents=True, exist_ok=True)
        cfg = self.mock_remapper / 'config.json'
        cfg.write_text(json.dumps({'autoload': {'Existing Mouse': 'mouse_preset'}, 'version': '2.2.0'}))

        configure.setup_keymap()

        data = json.loads(cfg.read_text())
        self.assertEqual(data['autoload'].get('Existing Mouse'), 'mouse_preset')
        self.assertEqual(data['autoload'].get('MI RC'), 'vibe_coding')
        self.assertEqual(data['autoload'].get('小米蓝牙语音遥控器'), 'vibe_coding')

    def test_setup_keymap_uses_dynamic_remapper_path(self):
        with patch('shutil.which', return_value='/opt/custom/bin/input-remapper-control'):
            configure.setup_keymap()
            srv = self.mock_systemd / 'input-remapper-autoload.service'
            self.assertIn('/opt/custom/bin/input-remapper-control', srv.read_text())

    def test_inspect_all_returns_structured_dict(self):
        data = configure.inspect_all()
        for key in ('overall_ready', 'bluetooth', 'keymap', 'desktop', 'credentials', 'runtimes'):
            self.assertIn(key, data)
        self.assertIsInstance(data['overall_ready'], bool)


if __name__ == '__main__':
    unittest.main()
