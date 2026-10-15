#!/usr/bin/env python3
"""Tests for desktop launcher dialog."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

import launcher_dialog

app = QApplication.instance() or QApplication([])


class LauncherDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_dir = Path(self.temp_dir.name) / 'config'
        self.key_file = self.config_dir / 'api.key'
        self.runtime_dir = Path(self.temp_dir.name) / 'runtime'
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.patch_config = patch.object(launcher_dialog, 'CONFIG_DIR', self.config_dir)
        self.patch_key = patch.object(launcher_dialog, 'KEY_FILE', self.key_file)
        self.patch_runtime = patch.object(launcher_dialog, 'RUNTIME', self.runtime_dir)
        self.patch_config.start()
        self.patch_key.start()
        self.patch_runtime.start()
        self.addCleanup(self.patch_config.stop)
        self.addCleanup(self.patch_key.stop)
        self.addCleanup(self.patch_runtime.stop)

    def test_get_api_key_env_precedence(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'env_secret_key'}):
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self.key_file.write_text('file_secret_key\n')
            self.assertEqual(launcher_dialog.get_api_key(), 'env_secret_key')

    def test_get_api_key_from_file_when_env_empty(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self.key_file.write_text('file_secret_key\n')
            self.key_file.chmod(0o600)
            self.assertEqual(launcher_dialog.get_api_key(), 'file_secret_key')

    def test_get_api_key_rejects_insecure_mode(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self.key_file.write_text('insecure_key\n')
            self.key_file.chmod(0o644)
            self.assertEqual(launcher_dialog.get_api_key(), '')

    def test_get_api_key_missing_file_returns_empty(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            self.assertEqual(launcher_dialog.get_api_key(), '')

    def test_save_api_key_creates_mode_600_file(self):
        launcher_dialog.save_api_key('my-test-api-key')
        self.assertTrue(self.key_file.is_file())
        self.assertEqual(self.key_file.read_text().strip(), 'my-test-api-key')
        mode = stat.S_IMODE(self.key_file.stat().st_mode)
        self.assertEqual(mode, 0o600)
        dir_mode = stat.S_IMODE(self.config_dir.stat().st_mode)
        self.assertEqual(dir_mode, 0o700)

    def test_check_running_service_false_when_no_meta(self):
        status = launcher_dialog.check_running_service()
        self.assertFalse(status['running'])

    def test_check_running_service_false_when_pid_not_matching(self):
        meta = self.runtime_dir / 'current.json'
        meta.write_text(json.dumps({'pid': 999999, 'backend': 'local'}))
        status = launcher_dialog.check_running_service()
        self.assertFalse(status['running'])

    def test_dialog_renders_stopped_ui(self):
        with patch.object(launcher_dialog, 'check_running_service', return_value={'running': False}):
            dialog = launcher_dialog.LauncherDialog()
            self.assertIsNotNone(dialog.btn_cloud)
            self.assertIsNotNone(dialog.btn_local)

    def test_dialog_renders_running_ui(self):
        with patch.object(launcher_dialog, 'check_running_service', return_value={'running': True, 'pid': 1234, 'backend': 'qwen-api'}):
            dialog = launcher_dialog.LauncherDialog()
            self.assertTrue(dialog.status['running'])


if __name__ == '__main__':
    unittest.main()
