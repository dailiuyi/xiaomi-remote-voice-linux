import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from floating_preview import status_url, get_window_class

ROOT = Path(__file__).resolve().parent


class FloatingPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_status_url_rejects_non_loopback_or_malformed_urls(self):
        self.assertEqual(
            status_url({'preview_url': 'http://127.0.0.1:18767/test/'}),
            'http://127.0.0.1:18767/test/status'
        )
        for bad_url in [
            'http://example.com/test/',
            'http://192.168.1.1:18767/test/',
            'https://127.0.0.1:18767/test/',
            'http://user:pass@127.0.0.1:18767/test/',
            'http://127.0.0.1:18767/test?query=1',
        ]:
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(ValueError):
                    status_url({'preview_url': bad_url})

    def test_dynamic_island_lifecycle_expand_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            WindowCls = get_window_class()
            win = WindowCls(runtime_dir=tmp_path, service_meta_path=tmp_path/'nonexistent.json')
            win.poll_timer.stop()
            win.show()
            self.app.processEvents()
            
            # Initial compact state
            self.assertEqual(win.width(), win.COMPACT_W)
            self.assertEqual(win.height(), win.COMPACT_H)
            self.assertFalse(win.is_expanded)

            # Ready phase verification
            win.display({'phase': 'ready', 'session': 100})
            self.assertFalse(win.is_expanded)
            self.assertEqual(win.header_text, 'MI 语音 · 就绪')

            # Trigger recording state
            win.display({
                'phase': 'recording',
                'session': 101,
                'preview': '我想让这个悬浮窗实现类似苹果灵动岛的效果',
                'final': ''
            })
            self.assertTrue(win.is_expanded)

            # Advance animation to completion
            start = time.monotonic()
            while win.animating and (time.monotonic() - start < 1.0):
                self.app.processEvents()
                time.sleep(0.02)

            self.assertEqual(win.width(), win.EXPANDED_W)
            self.assertEqual(win.height(), win.EXPANDED_H)
            
            # Evidence file saved
            curr_file = tmp_path/'current.json'
            self.assertTrue(curr_file.exists())
            curr_data = json.loads(curr_file.read_text())
            self.assertEqual(curr_data['phase'], 'recording')
            self.assertTrue(curr_data['expanded'])
            self.assertEqual(curr_data['geometry'][2], win.EXPANDED_W)
            self.assertEqual(curr_data['geometry'][3], win.EXPANDED_H)

            # Preview grab saved
            preview_img = tmp_path/'last-preview.png'
            self.assertTrue(preview_img.exists())
            self.assertGreater(preview_img.stat().st_size, 1000)

            # Trigger paste_shortcut_sent and verify quick recoil
            win.display({
                'phase': 'paste_shortcut_sent',
                'session': 102,
                'preview': '',
                'final': '测试完成'
            })
            self.assertTrue(win.collapse_timer.isActive())
            # Wait for 0.8s collapse timer to fire
            time.sleep(0.85)
            self.app.processEvents()
            start = time.monotonic()
            while win.animating and (time.monotonic() - start < 1.0):
                self.app.processEvents()
                time.sleep(0.02)
            self.assertFalse(win.is_expanded)
            self.assertEqual(win.width(), win.COMPACT_W)

            # Double click reset
            from PyQt6.QtCore import Qt, QPointF
            from PyQt6.QtGui import QMouseEvent
            event = QMouseEvent(
                QMouseEvent.Type.MouseButtonDblClick,
                QPointF(20, 20),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier
            )
            win.mouseDoubleClickEvent(event)
            pos_file = tmp_path/'position.json'
            self.assertTrue(pos_file.exists())
            pos_data = json.loads(pos_file.read_text())
            self.assertIn('center_x', pos_data)
            self.assertIn('top_y', pos_data)

            win.close()

    def test_mouse_drag_and_save(self):
        from PyQt6.QtCore import Qt, QPointF
        from PyQt6.QtGui import QMouseEvent
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            WindowCls = get_window_class()
            win = WindowCls(runtime_dir=tmp_path, service_meta_path=tmp_path/'nonexistent.json')
            win.poll_timer.stop()
            win.show()
            self.app.processEvents()

            pos_file = tmp_path/'position.json'
            # Window show should not automatically write position.json
            self.assertFalse(pos_file.exists())

            # Simulate mouse press at global (100, 20)
            press = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(20, 10),
                QPointF(100, 20),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier
            )
            win.mousePressEvent(press)
            self.assertTrue(win.is_dragging)

            # Simulate mouse move by +50, +30
            move = QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(70, 40),
                QPointF(150, 50),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier
            )
            win.mouseMoveEvent(move)
            self.app.processEvents()

            # Mouse release should finish drag and save position
            release = QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(70, 40),
                QPointF(150, 50),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier
            )
            win.mouseReleaseEvent(release)
            self.assertFalse(win.is_dragging)
            self.assertTrue(pos_file.exists())

            data = json.loads(pos_file.read_text())
            self.assertIn('rel_x', data)
            self.assertIn('offset_y', data)
            self.assertTrue(data.get('is_primary', False))
            win.close()

    def test_stale_non_primary_position_auto_corrected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # Write a stale position far outside primary screen
            pos_file = tmp_path/'position.json'
            pos_file.write_text(json.dumps({'center_x': -9999, 'top_y': -9999}) + '\n')

            WindowCls = get_window_class()
            win = WindowCls(runtime_dir=tmp_path, service_meta_path=tmp_path/'nonexistent.json')
            win.poll_timer.stop()

            # The anchor should be corrected to primary screen center
            from PyQt6.QtGui import QGuiApplication
            primary_rect = QGuiApplication.primaryScreen().availableGeometry()
            self.assertEqual(win.anchor_cx, primary_rect.center().x())
            self.assertEqual(win.anchor_top, primary_rect.top() + 14)

            # The corrected position should be updated in position.json
            data = json.loads(pos_file.read_text())
            self.assertEqual(data['center_x'], primary_rect.center().x())
            self.assertEqual(data['top_y'], primary_rect.top() + 14)
            win.close()


if __name__ == '__main__':
    unittest.main()
