#!/usr/bin/env python3
"""Desktop mode selection dialog for MI Voice Input."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'artifacts/streaming-runtime'
CONFIG_DIR = Path.home() / '.config/mi-voice'
KEY_FILE = CONFIG_DIR / 'api.key'


def get_api_key() -> str:
    key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    if not key and KEY_FILE.is_file():
        try:
            mode = stat.S_IMODE(KEY_FILE.stat().st_mode)
            if mode == 0o600:
                key = KEY_FILE.read_text(encoding='utf-8').strip()
            else:
                key = ''
        except OSError:
            key = ''
    return key


def save_api_key(key: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    KEY_FILE.write_text(key.strip() + '\n', encoding='utf-8')
    KEY_FILE.chmod(0o600)


def check_running_service():
    meta_path = RUNTIME / 'current.json'
    if not meta_path.is_file():
        return {'running': False}
    try:
        data = json.loads(meta_path.read_text(encoding='utf-8'))
        pid = int(data['pid'])
        proc = Path(f'/proc/{pid}')
        if proc.stat().st_uid != os.getuid():
            return {'running': False}
        cmdline = (proc / 'cmdline').read_bytes().split(b'\x00')
        if str(ROOT / 'streaming_dictation.py').encode() not in cmdline:
            return {'running': False}
        return {'running': True, 'pid': pid, 'backend': data.get('backend', 'unknown')}
    except (OSError, ValueError, KeyError):
        return {'running': False}


def notify(message: str) -> None:
    subprocess.Popen(
        ['notify-send', '-a', 'MI 语音输入', '-i', 'audio-input-microphone', 'MI 语音输入', message],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch_backend(backend: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log_file = open(RUNTIME / 'desktop.log', 'a', encoding='utf-8')
    env = os.environ.copy()
    key = get_api_key()
    if key:
        env['DASHSCOPE_API_KEY'] = key
    script = ROOT / 'start_streaming_dictation.sh'
    subprocess.Popen(
        ['bash', str(script), '--backend', backend],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        cwd=str(ROOT),
        env=env,
    )
    label = '云端实时语音' if backend == 'qwen-api' else '本地离线模型'
    notify(f'正在启动{label}，悬浮窗将自动出现。')


def stop_service() -> None:
    script = ROOT / 'start_streaming_dictation.sh'
    subprocess.run(['bash', str(script), '--stop'], cwd=str(ROOT), check=False)
    notify('语音输入服务已停止。')


def wake_preview() -> None:
    preview_script = ROOT / 'floating_preview.py'
    subprocess.Popen(
        ['/usr/bin/python3', str(preview_script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(ROOT),
    )
    notify('悬浮预览窗已唤醒。')


class KeyInputDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('配置百炼 API Key')
        self.setFixedWidth(420)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel('未检测到百炼 API Key。请输入密钥以启用云端实时识别：\n（密钥将保存在 ~/.config/mi-voice/api.key，权限 600）')
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.input.setPlaceholderText('sk-...')
        layout.addWidget(self.input)

        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton('取消')
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton('保存并启动')
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.accept_key)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def accept_key(self):
        key = self.input.text().strip()
        if not key or '\n' in key or '\r' in key:
            QMessageBox.warning(self, '输入错误', '请输入有效的 API Key（单行，无换行）。')
            return
        save_api_key(key)
        self.accept()


class LauncherDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('MI 语音输入')
        self.setFixedWidth(460)
        self.status = check_running_service()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        if self.status['running']:
            backend_label = '云端实时语音' if self.status.get('backend') == 'qwen-api' else '本地离线模型'
            info_title = QLabel('语音服务正在运行')
            font = info_title.font()
            font.setPointSize(14)
            font.setBold(True)
            info_title.setFont(font)
            layout.addWidget(info_title)

            info_detail = QLabel(f'当前运行模式：{backend_label} (PID: {self.status["pid"]})')
            layout.addWidget(info_detail)

            btn_box = QHBoxLayout()
            btn_wake = QPushButton('唤醒悬浮窗')
            btn_wake.setStyleSheet('padding: 8px 16px;')
            btn_wake.clicked.connect(self.on_wake_clicked)

            btn_stop = QPushButton('停止服务')
            btn_stop.setStyleSheet('padding: 8px 16px;')
            btn_stop.clicked.connect(self.on_stop_clicked)

            btn_cancel = QPushButton('关闭')
            btn_cancel.setStyleSheet('padding: 8px 16px;')
            btn_cancel.clicked.connect(self.reject)

            btn_box.addWidget(btn_wake)
            btn_box.addWidget(btn_stop)
            btn_box.addStretch()
            btn_box.addWidget(btn_cancel)
            layout.addLayout(btn_box)
            return

        header = QLabel('请选择语音输入运行模式')
        title_font = header.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        header.setFont(title_font)
        layout.addWidget(header)

        # Mode 1: Cloud API
        has_key = bool(get_api_key())
        key_status_text = '已配置' if has_key else '需配置'
        self.btn_cloud = QPushButton()
        self.btn_cloud.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cloud.setStyleSheet('''
            QPushButton {
                text-align: left;
                padding: 12px 14px;
                border: 1px solid #3daee9;
                border-radius: 8px;
                background: rgba(61, 174, 233, 0.08);
            }
            QPushButton:hover {
                background: rgba(61, 174, 233, 0.18);
            }
        ''')
        cloud_layout = QVBoxLayout(self.btn_cloud)
        cloud_title = QLabel(f'⚡ 云端实时语音（百炼 Qwen-API）  [{key_status_text}]')
        ct_font = cloud_title.font()
        ct_font.setBold(True)
        ct_font.setPointSize(11)
        cloud_title.setFont(ct_font)
        cloud_desc = QLabel('极速实时预览 · 识别率高 · 免显存 · 松键秒出字')
        cloud_desc.setStyleSheet('color: #7f8c8d; font-size: 11px;')
        cloud_layout.addWidget(cloud_title)
        cloud_layout.addWidget(cloud_desc)
        self.btn_cloud.clicked.connect(self.on_cloud_clicked)
        layout.addWidget(self.btn_cloud)

        # Mode 2: Local Model
        self.btn_local = QPushButton()
        self.btn_local.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_local.setStyleSheet('''
            QPushButton {
                text-align: left;
                padding: 12px 14px;
                border: 1px solid #7f8c8d;
                border-radius: 8px;
                background: rgba(127, 140, 141, 0.08);
            }
            QPushButton:hover {
                background: rgba(127, 140, 141, 0.18);
            }
        ''')
        local_layout = QVBoxLayout(self.btn_local)
        local_title = QLabel('🛡️ 本地离线模型（Qwen 1.7B）')
        lt_font = local_title.font()
        lt_font.setBold(True)
        lt_font.setPointSize(11)
        local_title.setFont(lt_font)
        local_desc = QLabel('纯本地运算 · 不联网 · 需显卡显存 (~4.6 GB)')
        local_desc.setStyleSheet('color: #7f8c8d; font-size: 11px;')
        local_layout.addWidget(local_title)
        local_layout.addWidget(local_desc)
        self.btn_local.clicked.connect(self.on_local_clicked)
        layout.addWidget(self.btn_local)

        # Footer
        footer = QHBoxLayout()
        tip = QLabel('密钥位置：~/.config/mi-voice/api.key')
        tip.setStyleSheet('color: #95a5a6; font-size: 10px;')
        btn_cancel = QPushButton('取消')
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(tip)
        footer.addStretch()
        footer.addWidget(btn_cancel)
        layout.addLayout(footer)

    def on_cloud_clicked(self):
        key = get_api_key()
        if not key:
            dialog = KeyInputDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
        launch_backend('qwen-api')
        self.accept()

    def on_local_clicked(self):
        launch_backend('local')
        self.accept()

    def on_wake_clicked(self):
        wake_preview()
        self.accept()

    def on_stop_clicked(self):
        stop_service()
        self.accept()


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName('MI 语音输入')
    dialog = LauncherDialog()
    dialog.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
