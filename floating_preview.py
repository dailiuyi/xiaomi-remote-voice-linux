#!/usr/bin/env python3
"""Passive Dynamic Island desktop preview for the existing loopback ASR service."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT/'artifacts/floating-preview'
SERVICE_META = ROOT/'artifacts/streaming-runtime/current.json'


def status_url(metadata):
    url = metadata['preview_url']
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.username or parsed.password or parsed.query
            or parsed.fragment or not parsed.path.endswith('/')):
        raise ValueError('Expected a private loopback preview URL')
    return url + 'status'


def stop_current():
    path = RUNTIME/'current.json'
    if not path.exists():
        return
    try:
        pid = int(json.loads(path.read_text())['pid'])
    except Exception:
        path.unlink(missing_ok=True)
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return
    try:
        proc = Path(f'/proc/{pid}')
        cmd_parts = (proc/'cmdline').read_bytes().split(b'\x00')
        valid_script = str(ROOT/'floating_preview.py').encode() in cmd_parts or b'floating_preview.py' in cmd_parts
        if proc.stat().st_uid != os.getuid() or not valid_script:
            raise RuntimeError('Floating preview PID identity changed; no signal sent')
        signal.pidfd_send_signal(descriptor, signal.SIGINT)
        if not select.select([descriptor], [], [], 5)[0]:
            raise RuntimeError('Preview stop requested but exit not confirmed')
        path.unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def get_window_class():
    if 'QT_QPA_PLATFORM' not in os.environ and (os.environ.get('DISPLAY') or os.path.exists('/tmp/.X11-unix/X0')):
        os.environ.setdefault('DISPLAY', ':0')
        os.environ['QT_QPA_PLATFORM'] = 'xcb'

    from PyQt6.QtCore import Qt, QTimer, QUrl, QPoint, QRectF, QPointF, QVariantAnimation, QEasingCurve
    from PyQt6.QtGui import (QGuiApplication, QPainter, QColor, QPainterPath,
                             QPen, QFont, QBrush)
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest
    from PyQt6.QtWidgets import QWidget

    class DynamicIslandWindow(QWidget):
        COMPACT_W, COMPACT_H, COMPACT_R = 210, 38, 19.0
        EXPANDED_W, EXPANDED_H, EXPANDED_R = 430, 138, 26.0
        HOLD_SUCCESS_SECONDS = 0.8
        COLLAPSE_DURATION_MS = 180

        def __init__(self, runtime_dir=RUNTIME, service_meta_path=SERVICE_META):
            super().__init__()
            self.runtime_dir = Path(runtime_dir)
            self.service_meta_path = Path(service_meta_path)

            flags = (Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setWindowTitle('MI 语音预览 · 灵动岛')
            self.setObjectName('MiVoiceDynamicIsland')

            self.current_w = float(self.COMPACT_W)
            self.current_h = float(self.COMPACT_H)
            self.current_r = float(self.COMPACT_R)
            self.progress = 0.0
            self.is_expanded = False
            self.animating = False

            # Dragging state
            self.is_dragging = False
            self.drag_start_pos = QPoint()
            self.drag_start_cx = 0
            self.drag_start_top = 0
            self.setMouseTracking(True)
            self.setCursor(Qt.CursorShape.SizeAllCursor)

            # Default placement: Apple Island style on primary screen top center
            self.anchor_cx = 0
            self.anchor_top = 14
            self.reset_to_primary_screen(save=False)

            # Restore customized position if available
            self.load_position()

            # Connect dynamic display topology changes
            app_inst = QGuiApplication.instance()
            if app_inst:
                app_inst.primaryScreenChanged.connect(self.on_primary_screen_changed)
                app_inst.screenRemoved.connect(self.on_screen_removed)

            # Elastic transition animation
            self.anim = QVariantAnimation(self)
            self.anim.valueChanged.connect(self.on_anim_step)
            self.anim.finished.connect(self.on_anim_finished)

            # Precision collapse timer
            self.collapse_timer = QTimer(self)
            self.collapse_timer.setSingleShot(True)
            self.collapse_timer.timeout.connect(self.on_collapse_timeout)

            # Waveform & visual pulse state
            self.wave_phase = 0.0
            self.wave_timer = QTimer(self)
            self.wave_timer.timeout.connect(self.on_wave_tick)
            self.wave_timer.start(35)

            # Network polling
            self.net = QNetworkAccessManager(self)
            self.net.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
            self.net.finished.connect(self.received)
            self.pending = False
            self.last_signature = None
            self.changed_at = time.monotonic()
            self.state_data = {'phase': 'starting'}
            self.display_text = ''
            self.header_text = 'MI 语音 · 就绪'
            self.close_hovered = False

            self.sync_geometry()
            self.apply_window_hints()

            self.poll_timer = QTimer(self)
            self.poll_timer.timeout.connect(self.poll)
            self.poll_timer.start(150)
            self.poll()

        def apply_window_hints(self):
            wid = int(self.winId())
            if wid > 0 and os.environ.get('DISPLAY'):
                try:
                    import subprocess
                    subprocess.run(
                        ['xprop', '-id', str(wid), '-f', '_NET_WM_STATE', '32a',
                         '-set', '_NET_WM_STATE',
                         '_NET_WM_STATE_SKIP_TASKBAR, _NET_WM_STATE_SKIP_PAGER, _NET_WM_STATE_ABOVE, _NET_WM_STATE_STAYS_ON_TOP'],
                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except Exception:
                    pass

        def showEvent(self, event):
            super().showEvent(event)
            self.apply_window_hints()

        def on_collapse_timeout(self):
            phase = self.state_data.get('phase', '')
            if phase not in ('recording', 'tail', 'finalizing'):
                self.set_expanded_target(False)

        def reset_to_primary_screen(self, save=False):
            primary = QGuiApplication.primaryScreen()
            if not primary:
                return
            s_rect = primary.availableGeometry()
            self.anchor_cx = s_rect.center().x()
            self.anchor_top = s_rect.top() + 14
            self.sync_geometry()
            if save:
                self.save_position()

        def save_position(self):
            screen = QGuiApplication.screenAt(QPoint(self.anchor_cx, self.anchor_top)) or QGuiApplication.primaryScreen()
            if not screen:
                return
            s_rect = screen.availableGeometry()
            rel_x = (self.anchor_cx - s_rect.left()) / max(1.0, float(s_rect.width()))
            offset_y = self.anchor_top - s_rect.top()
            primary = QGuiApplication.primaryScreen()
            is_pri = (screen == primary)
            data = {
                'center_x': int(self.anchor_cx),
                'top_y': int(self.anchor_top),
                'rel_x': round(rel_x, 4),
                'offset_y': int(offset_y),
                'screen_name': screen.name(),
                'is_primary': is_pri
            }
            try:
                (self.runtime_dir/'position.json').write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + '\n'
                )
            except OSError:
                pass

        def load_position(self):
            pos_file = self.runtime_dir/'position.json'
            if not pos_file.exists():
                return
            try:
                pos_data = json.loads(pos_file.read_text())
                primary = QGuiApplication.primaryScreen()
                if not primary:
                    return
                target_screen = primary
                screen_name = pos_data.get('screen_name')
                if screen_name and not pos_data.get('is_primary', True):
                    for s in QGuiApplication.screens():
                        if s.name() == screen_name:
                            target_screen = s
                            break

                s_rect = target_screen.availableGeometry()

                if 'rel_x' in pos_data and 'offset_y' in pos_data:
                    self.anchor_cx = int(s_rect.left() + s_rect.width() * float(pos_data['rel_x']))
                    self.anchor_top = int(s_rect.top() + int(pos_data['offset_y']))
                    return

                if 'center_x' in pos_data and 'top_y' in pos_data:
                    cx = pos_data['center_x']
                    cy = pos_data['top_y']
                    primary_rect = primary.availableGeometry()
                    if primary_rect.contains(QPoint(cx, cy)):
                        self.anchor_cx = cx
                        self.anchor_top = cy
                    else:
                        self.reset_to_primary_screen(save=True)
            except (OSError, ValueError, KeyError):
                pass

        def on_primary_screen_changed(self, screen):
            self.load_position()
            self.sync_geometry()

        def on_screen_removed(self, screen):
            current_screen = QGuiApplication.screenAt(QPoint(self.anchor_cx, self.anchor_top))
            if not current_screen or current_screen == screen:
                self.reset_to_primary_screen(save=True)
            else:
                self.sync_geometry()

        def sync_geometry(self):
            w = max(int(self.current_w), self.COMPACT_W)
            h = max(int(self.current_h), self.COMPACT_H)
            self.setFixedSize(w, h)
            left = int(self.anchor_cx - w / 2)
            top = int(self.anchor_top)
            screen = QGuiApplication.screenAt(QPoint(self.anchor_cx, self.anchor_top)) or QGuiApplication.primaryScreen()
            s_rect = screen.availableGeometry()
            clamped_left = max(s_rect.left(), min(left, s_rect.right() - w + 1))
            clamped_top = max(s_rect.top(), min(top, s_rect.bottom() - h + 1))
            self.move(clamped_left, clamped_top)

        def set_expanded_target(self, target_expanded: bool):
            if self.is_expanded == target_expanded:
                return
            self.is_expanded = target_expanded
            self.animating = True
            self.anim.stop()
            if target_expanded:
                # Fluid Spring bounce on expand
                self.anim.setDuration(320)
                self.anim.setEasingCurve(QEasingCurve.Type.OutBack)
                self.anim.setStartValue(self.progress)
                self.anim.setEndValue(1.0)
            else:
                # Swift crisp recoil on collapse
                self.anim.setDuration(self.COLLAPSE_DURATION_MS)
                self.anim.setEasingCurve(QEasingCurve.Type.OutQuad)
                self.anim.setStartValue(self.progress)
                self.anim.setEndValue(0.0)
            self.anim.start()

        def on_anim_step(self, val):
            self.progress = float(val)
            self.current_w = self.COMPACT_W + (self.EXPANDED_W - self.COMPACT_W) * self.progress
            self.current_h = self.COMPACT_H + (self.EXPANDED_H - self.COMPACT_H) * self.progress
            self.current_r = self.COMPACT_R + (self.EXPANDED_R - self.COMPACT_R) * self.progress
            self.sync_geometry()
            self.update()

        def on_anim_finished(self):
            self.animating = False
            self.progress = 1.0 if self.is_expanded else 0.0
            self.current_w = self.EXPANDED_W if self.is_expanded else self.COMPACT_W
            self.current_h = self.EXPANDED_H if self.is_expanded else self.COMPACT_H
            self.current_r = self.EXPANDED_R if self.is_expanded else self.COMPACT_R
            self.sync_geometry()
            self.update()
            self.save_evidence(self.state_data, self.is_expanded)

        def on_wave_tick(self):
            phase = self.state_data.get('phase')
            if phase == 'recording' or (phase == 'finalizing' and self.progress > 0.5):
                self.wave_phase = (self.wave_phase + 0.22) % (math.pi * 2)
                self.update()

        def poll(self):
            if self.pending:
                return
            try:
                url = status_url(json.loads(self.service_meta_path.read_text()))
                request = QNetworkRequest(QUrl(url))
                request.setTransferTimeout(1500)
                request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                     QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
                self.pending = True
                self.net.get(request)
            except (OSError, ValueError, KeyError):
                self.display({'phase': 'unavailable'})

        def received(self, reply):
            self.pending = False
            try:
                if reply.error().value != 0 or reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) != 200:
                    raise ValueError('Preview service unavailable')
                payload = bytes(reply.readAll())
                if len(payload) > 65536:
                    raise ValueError('Oversized preview status')
                state = json.loads(payload)
                if not isinstance(state, dict):
                    raise ValueError('Invalid status')
                self.display(state)
            except (ValueError, TypeError):
                self.display({'phase': 'unavailable'})
            finally:
                reply.deleteLater()

        def display(self, state):
            self.state_data = state
            phase = state.get('phase', 'unavailable')
            signature = (state.get('session'), phase, state.get('preview', ''), state.get('final', ''))
            is_initial = self.last_signature is None
            changed = signature != self.last_signature
            if changed:
                self.last_signature = signature
                self.changed_at = time.monotonic()
                if phase in ('paste_shortcut_sent', 'cancelled', 'failed') and not is_initial:
                    self.collapse_timer.stop()
                    self.collapse_timer.start(int(self.HOLD_SUCCESS_SECONDS * 1000))
                elif phase in ('recording', 'tail', 'finalizing'):
                    self.collapse_timer.stop()

            active = phase in ('recording', 'tail', 'finalizing')
            # Track initial connection to avoid falsely expanding on stale past sessions
            if is_initial and phase in ('paste_shortcut_sent', 'cancelled', 'failed'):
                recent_final = False
            else:
                recent_final = phase in ('paste_shortcut_sent', 'cancelled', 'failed') and (time.monotonic() - self.changed_at < self.HOLD_SUCCESS_SECONDS)
            should_expand = active or recent_final

            labels = {
                'recording': '正在听',
                'tail': '收音结束',
                'finalizing': '正在生成最终文字',
                'paste_shortcut_sent': '已输入',
                'failed': '输入失败',
                'cancelled': '本次已取消',
                'unavailable': '等待语音服务',
                'loading': '正在加载模型',
                'no_speech': '未检测到语音'
            }

            if should_expand:
                self.header_text = labels.get(phase, 'MI 语音')
            else:
                self.header_text = 'MI 语音 · ' + (labels.get(phase, '就绪') if phase in ('unavailable', 'loading') else '就绪')

            text = state.get('preview', '') if active else state.get('final', '')
            if not isinstance(text, str):
                text = ''
            if len(text) > 100:
                text = '…' + text[-100:]
            self.display_text = text or ('按住说话，文字会在这里出现…' if phase == 'recording' else '')

            self.set_expanded_target(should_expand)
            if changed or self.is_expanded != should_expand:
                self.update()
                QTimer.singleShot(0, lambda: self.save_evidence(state, should_expand))

        def save_evidence(self, state, expanded):
            info = {
                'pid': os.getpid(),
                'window_id': int(self.winId()),
                'platform': QGuiApplication.platformName(),
                'phase': state.get('phase'),
                'expanded': expanded,
                'geometry': [self.x(), self.y(), self.width(), self.height()],
                'active_window': self.isActiveWindow(),
                'accepts_focus': self.focusPolicy() != Qt.FocusPolicy.NoFocus,
                'show_without_activating': self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating),
                'no_focus_window_flag': bool(self.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus),
                'stay_on_top_flag': bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
                'skip_taskbar': True
            }
            (self.runtime_dir/'current.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
            if expanded and (state.get('preview') or state.get('final')):
                self.grab().save(str(self.runtime_dir/'last-preview.png'))
            elif not expanded:
                self.grab().save(str(self.runtime_dir/'last-compact.png'))

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

            w = float(self.width())
            h = float(self.height())
            phase = self.state_data.get('phase', 'unavailable')

            # 1. Jet-Black Capsule Body with Smooth Radius
            rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
            path = QPainterPath()
            path.addRoundedRect(rect, self.current_r, self.current_r)

            # Deep Apple black background
            painter.fillPath(path, QBrush(QColor(8, 8, 10, 250)))

            # Specular 1px glossy highlight border
            border_pen = QPen(QColor(255, 255, 255, 28), 1.0)
            painter.setPen(border_pen)
            painter.drawPath(path)

            if self.progress < 0.35:
                # ----------------- Compact Island Mode -----------------
                # Left indicator (Glowing dot)
                dot_center = QPointF(20.0, h / 2.0)
                if phase in ('ready', 'paste_shortcut_sent'):
                    dot_color = QColor(48, 209, 88)   # iOS success/ready green
                elif phase == 'recording':
                    dot_color = QColor(255, 159, 10)  # iOS orange mic indicator
                elif phase == 'loading':
                    dot_color = QColor(255, 214, 10)  # Yellow loading indicator
                elif phase in ('unavailable', 'failed', 'stopped'):
                    dot_color = QColor(142, 142, 147) # Gray offline/error
                else:
                    dot_color = QColor(142, 142, 147)
                painter.setBrush(QBrush(dot_color))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(dot_center, 4.0, 4.0)

                # Center text
                painter.setPen(QColor(242, 242, 247))
                title_font = QFont('sans-serif', 10, QFont.Weight.Medium)
                title_font.setStyleHint(QFont.StyleHint.SansSerif)
                painter.setFont(title_font)
                painter.drawText(QRectF(34.0, 0.0, w - 68.0, h),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 self.header_text)

                # Right minimal waveform (3 miniature bars)
                wave_x = w - 34.0
                painter.setBrush(QBrush(QColor(255, 159, 10) if phase == 'recording' else QColor(99, 99, 102)))
                for i in range(3):
                    bar_h = 4.0 + (math.sin(self.wave_phase + i * 1.5) + 1.0) * 2.8 if phase == 'recording' else 4.0
                    bar_rect = QRectF(wave_x + i * 5.5, (h - bar_h) / 2.0, 2.2, bar_h)
                    painter.drawRoundedRect(bar_rect, 1.1, 1.1)

            else:
                # ----------------- Expanded Dynamic Island Mode -----------------
                # Top Leading: Dynamic Apple Waveform Bars
                wave_x = 22.0
                wave_y = 15.0
                painter.setPen(Qt.PenStyle.NoPen)

                if phase == 'paste_shortcut_sent':
                    # Green success check badge
                    painter.setBrush(QBrush(QColor(48, 209, 88)))
                    painter.drawEllipse(QPointF(wave_x + 8.0, wave_y + 8.0), 6.5, 6.5)
                    painter.setPen(QPen(QColor(0, 0, 0), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    painter.drawLine(QPointF(wave_x + 5.5, wave_y + 8.0), QPointF(wave_x + 7.5, wave_y + 10.5))
                    painter.drawLine(QPointF(wave_x + 7.5, wave_y + 10.5), QPointF(wave_x + 11.5, wave_y + 6.0))
                    painter.setPen(Qt.PenStyle.NoPen)
                else:
                    # Jumping orange audio bars
                    painter.setBrush(QBrush(QColor(255, 159, 10)))
                    for i in range(4):
                        if phase == 'recording':
                            bar_h = 5.0 + (math.sin(self.wave_phase + i * 1.4) + 1.0) * 5.5
                        else:
                            bar_h = 5.0
                        bar_rect = QRectF(wave_x + i * 6.5, wave_y + (16.0 - bar_h) / 2.0, 3.2, bar_h)
                        painter.drawRoundedRect(bar_rect, 1.6, 1.6)

                # Top Title
                title_font = QFont('sans-serif', 10, QFont.Weight.Medium)
                title_font.setStyleHint(QFont.StyleHint.SansSerif)
                painter.setFont(title_font)
                painter.setPen(QColor(160, 160, 168))
                painter.drawText(QRectF(56.0, 13.0, 240.0, 20.0),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 self.header_text)

                # Top Trailing: Subdued Close button
                close_color = QColor(220, 220, 230) if self.close_hovered else QColor(140, 140, 148)
                painter.setPen(close_color)
                close_font = QFont('sans-serif', 13, QFont.Weight.Normal)
                painter.setFont(close_font)
                painter.drawText(QRectF(w - 34.0, 13.0, 20.0, 20.0),
                                 Qt.AlignmentFlag.AlignCenter, "×")

                # Body: Streaming text with smooth fade-in
                text_alpha = int(255 * min(1.0, max(0.0, (self.progress - 0.35) / 0.65)))
                painter.setPen(QColor(255, 255, 255, text_alpha))
                body_font = QFont('sans-serif', 13, QFont.Weight.Normal)
                body_font.setStyleHint(QFont.StyleHint.SansSerif)
                painter.setFont(body_font)
                text_rect = QRectF(22.0, 44.0, w - 44.0, h - 54.0)
                painter.drawText(text_rect,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                                 self.display_text)

            painter.end()

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position()
                # If clicking the subtle top-right '×' button
                if self.is_expanded and pos.x() >= self.width() - 38 and pos.y() <= 38:
                    self.close()
                    return
                self.is_dragging = True
                self.drag_start_pos = event.globalPosition().toPoint()
                self.drag_start_cx = self.anchor_cx
                self.drag_start_top = self.anchor_top
                event.accept()

        def mouseMoveEvent(self, event):
            if self.is_dragging and (event.buttons() & Qt.MouseButton.LeftButton):
                delta = event.globalPosition().toPoint() - self.drag_start_pos
                self.anchor_cx = self.drag_start_cx + delta.x()
                self.anchor_top = self.drag_start_top + delta.y()
                self.sync_geometry()
                event.accept()
                return

            # Hover detection for subtle close button
            if self.is_expanded and event.position().x() >= self.width() - 38 and event.position().y() <= 38:
                if not self.close_hovered:
                    self.close_hovered = True
                    self.setCursor(Qt.CursorShape.PointingHandCursor)
                    self.update()
            else:
                if self.close_hovered:
                    self.close_hovered = False
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    self.update()
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton and self.is_dragging:
                self.is_dragging = False
                self.save_position()
                event.accept()
            else:
                super().mouseReleaseEvent(event)

        def mouseDoubleClickEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.is_dragging = False
                self.reset_to_primary_screen(save=True)
                event.accept()

        def moveEvent(self, event):
            super().moveEvent(event)

        def closeEvent(self, event):
            event.accept()
            from PyQt6.QtWidgets import QApplication
            app_inst = QApplication.instance()
            if app_inst:
                app_inst.quit()

    return DynamicIslandWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if args.stop:
        stop_current()
        return
    lock = (RUNTIME/'instance.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Floating preview already running')
        return

    WindowCls = get_window_class()
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer

    app = QApplication(sys.argv[:1])
    app.setApplicationName('MI Dynamic Island Preview')

    window = WindowCls()
    window.show()
    window.save_evidence({'phase': 'starting'}, False)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())

    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(200)

    try:
        sys.exit(app.exec())
    finally:
        lock.close()


if __name__ == '__main__':
    main()
