#!/usr/bin/env python3
"""Unified Agent-friendly configuration and diagnostic tool for MI Voice."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
ASSETS_DIR = ROOT / 'assets'
CONFIG_DIR = HOME / '.config/mi-voice'
KEY_FILE = CONFIG_DIR / 'api.key'

REMAPPER_DIR = HOME / '.config/input-remapper'
REMAPPER_PRESETS = REMAPPER_DIR / 'presets'
USER_SYSTEMD_DIR = HOME / '.config/systemd/user'

LOCAL_BIN = HOME / '.local/bin'
DESKTOP_DIR = HOME / '桌面' if (HOME / '桌面').is_dir() else HOME / 'Desktop'
APP_DIR = HOME / '.local/share/applications'


def get_receiver_binary():
    tested = ROOT / 'artifacts/2026-10-12-native-receiver/atvvoice-tested'
    built = ROOT / 'artifacts/receiver-build/target/debug/atvvoice'
    if tested.is_file():
        return tested
    if built.is_file():
        return built
    return None


def inspect_bluetooth():
    info = {'matched': False, 'device_mac': None, 'name': None, 'paired': False, 'connected': False}
    try:
        raw_devices = subprocess.check_output(['bluetoothctl', 'devices'], text=True, stderr=subprocess.DEVNULL)
        for line in raw_devices.splitlines():
            line = line.strip()
            if not line.startswith('Device '):
                continue
            parts = line.split(' ', 2)
            if len(parts) < 3:
                continue
            mac, name = parts[1], parts[2]
            if 'RC' in name or '遥控器' in name or 'RC003' in name:
                info['matched'] = True
                info['device_mac'] = mac
                info['name'] = name
                break
        if info['device_mac']:
            raw_info = subprocess.check_output(['bluetoothctl', 'info', info['device_mac']], text=True, stderr=subprocess.DEVNULL)
            for iline in raw_info.splitlines():
                iline = iline.strip()
                if iline.startswith('Paired:'):
                    info['paired'] = 'yes' in iline
                elif iline.startswith('Connected:'):
                    info['connected'] = 'yes' in iline
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def inspect_keymap():
    remapper_bin = shutil.which('input-remapper-control')
    preset_mi_rc = REMAPPER_PRESETS / 'MI RC' / 'vibe_coding.json'
    preset_zh = REMAPPER_PRESETS / '小米蓝牙语音遥控器' / 'vibe_coding.json'
    config_json = REMAPPER_DIR / 'config.json'
    service_file = USER_SYSTEMD_DIR / 'input-remapper-autoload.service'

    service_enabled = False
    try:
        out = subprocess.check_output(['systemctl', '--user', 'is-enabled', 'input-remapper-autoload.service'],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        service_enabled = (out == 'enabled')
    except (OSError, subprocess.SubprocessError):
        service_enabled = False

    return {
        'tool_installed': bool(remapper_bin),
        'tool_path': remapper_bin,
        'presets_ready': preset_mi_rc.is_file() and preset_zh.is_file(),
        'config_ready': config_json.is_file(),
        'service_installed': service_file.is_file(),
        'service_enabled': service_enabled,
    }


def inspect_desktop():
    launcher = LOCAL_BIN / 'mi-voice'
    desktop_start = DESKTOP_DIR / 'mi-voice.desktop'
    desktop_stop = DESKTOP_DIR / 'mi-voice-stop.desktop'
    app_start = APP_DIR / 'mi-voice.desktop'
    app_stop = APP_DIR / 'mi-voice-stop.desktop'

    def check_exec(p):
        return p.is_file() and os.access(p, os.X_OK)

    return {
        'launcher_script': check_exec(launcher),
        'desktop_start': check_exec(desktop_start),
        'desktop_stop': check_exec(desktop_stop),
        'app_entry_start': app_start.is_file(),
        'app_entry_stop': app_stop.is_file(),
        'all_ready': check_exec(launcher) and check_exec(desktop_start) and check_exec(desktop_stop),
    }


def inspect_credentials():
    env_key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    file_exists = KEY_FILE.is_file()
    file_mode_valid = False
    file_has_content = False
    if file_exists:
        try:
            mode = stat.S_IMODE(KEY_FILE.stat().st_mode)
            file_mode_valid = (mode == 0o600)
            file_has_content = bool(KEY_FILE.read_text(encoding='utf-8').strip())
        except OSError:
            pass

    return {
        'configured': bool(env_key or file_has_content),
        'source': 'env' if env_key else ('file' if file_has_content else None),
        'file_exists': file_exists,
        'file_mode_secure': file_mode_valid,
        'file_path': str(KEY_FILE),
    }


def inspect_runtimes():
    cloud_py = ROOT / 'artifacts/cloud-asr/venv/bin/python'
    local_py = ROOT / 'artifacts/asr-comparison/runtime/venv/bin/python'
    local_model = ROOT / 'artifacts/asr-comparison/models/1.7B'
    receiver = get_receiver_binary()

    receiver_sha = None
    if receiver and receiver.is_file():
        h = hashlib.sha256()
        try:
            with open(receiver, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            receiver_sha = h.hexdigest()
        except OSError:
            pass

    return {
        'cloud_ready': cloud_py.is_file() and os.access(cloud_py, os.X_OK),
        'local_model_ready': local_py.is_file() and local_model.is_dir(),
        'receiver_path': str(receiver) if receiver else None,
        'receiver_sha256': receiver_sha,
        'dialog_script': (ROOT / 'launcher_dialog.py').is_file(),
    }


def inspect_all():
    bt = inspect_bluetooth()
    km = inspect_keymap()
    dt = inspect_desktop()
    cr = inspect_credentials()
    rt = inspect_runtimes()

    # Basic operational requirements:
    ready = (
        rt['receiver_path'] is not None and
        (rt['cloud_ready'] or rt['local_model_ready']) and
        dt['all_ready']
    )

    return {
        'overall_ready': ready,
        'bluetooth': bt,
        'keymap': km,
        'desktop': dt,
        'credentials': cr,
        'runtimes': rt,
    }


def setup_desktop(force=False):
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)

    launcher_content = f'''#!/usr/bin/env bash
set -euo pipefail

TASK_ROOT="{ROOT}"

case "${{1:-start}}" in
  start)
    if [[ $# -gt 1 ]]; then
      shift
      exec bash "$TASK_ROOT/start_streaming_dictation.sh" "$@"
    fi
    exec /usr/bin/python3 "$TASK_ROOT/launcher_dialog.py"
    ;;
  stop)
    systemctl --user stop mi-remote-dictation.service mi-remote-preview.service 2>/dev/null || true
    bash "$TASK_ROOT/start_streaming_dictation.sh" --stop
    notify-send -a 'MI 语音输入' -i process-stop 'MI 语音输入' '语音服务与悬浮窗已关闭。' 2>/dev/null || true
    ;;
  status)
    /usr/bin/python3 -c "
import json, os, sys
from pathlib import Path
meta_path = Path('$TASK_ROOT/artifacts/streaming-runtime/current.json')
if not meta_path.is_file():
    print('MI 语音输入未运行')
    sys.exit(0)
try:
    data = json.loads(meta_path.read_text(encoding='utf-8'))
    pid = int(data['pid'])
    proc = Path(f'/proc/{{pid}}')
    if proc.stat().st_uid != os.getuid():
        raise ValueError()
    cmdline = (proc / 'cmdline').read_bytes().split(b'\x00')
    if str(Path('$TASK_ROOT/streaming_dictation.py')).encode() not in cmdline:
        raise ValueError()
    print(f'MI 语音输入正在运行 (PID: {{pid}}, 后端: {{data.get(\"backend\", \"unknown\")}})')
except Exception:
    print('MI 语音输入未运行')
"
    ;;
  *)
    exec /usr/bin/python3 "$TASK_ROOT/launcher_dialog.py" "$@"
    ;;
esac
'''
    start_desktop_content = f'''[Desktop Entry]
Type=Application
Name=MI 语音输入
Comment=启动 MI 语音输入和悬浮预览（支持云端与本地模式）
Exec={LOCAL_BIN}/mi-voice start
Icon=audio-input-microphone
Terminal=false
StartupNotify=false
Categories=Utility;Accessibility;
Actions=Stop;

[Desktop Action Stop]
Name=关闭语音输入
Exec={LOCAL_BIN}/mi-voice stop
Icon=process-stop
'''
    stop_desktop_content = f'''[Desktop Entry]
Type=Application
Name=关闭 MI 语音服务
Comment=关闭语音输入、接收器、录音和悬浮预览
Exec={LOCAL_BIN}/mi-voice stop
Icon=process-stop
Terminal=false
StartupNotify=false
Categories=Utility;Accessibility;
'''
    files_created = []

    launcher_path = LOCAL_BIN / 'mi-voice'
    launcher_path.write_text(launcher_content, encoding='utf-8')
    launcher_path.chmod(0o755)
    files_created.append(str(launcher_path))

    for target in (DESKTOP_DIR / 'mi-voice.desktop', APP_DIR / 'mi-voice.desktop'):
        target.write_text(start_desktop_content, encoding='utf-8')
        target.chmod(0o755)
        files_created.append(str(target))

    for target in (DESKTOP_DIR / 'mi-voice-stop.desktop', APP_DIR / 'mi-voice-stop.desktop'):
        target.write_text(stop_desktop_content, encoding='utf-8')
        target.chmod(0o755)
        files_created.append(str(target))

    return {'success': True, 'files': files_created}


def setup_keymap(force=False):
    preset_source = ASSETS_DIR / 'input-remapper/vibe_coding.json'
    config_source = ASSETS_DIR / 'input-remapper/config.json'
    service_source = ASSETS_DIR / 'systemd/input-remapper-autoload.service'

    if not preset_source.is_file() or not config_source.is_file() or not service_source.is_file():
        raise RuntimeError('Assets templates missing in repository')

    for device_name in ('MI RC', '小米蓝牙语音遥控器'):
        target_dir = REMAPPER_PRESETS / device_name
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preset_source, target_dir / 'vibe_coding.json')

    # Merge autoload into existing config.json if present
    REMAPPER_DIR.mkdir(parents=True, exist_ok=True)
    config_dest = REMAPPER_DIR / 'config.json'
    if config_dest.is_file():
        try:
            existing_data = json.loads(config_dest.read_text(encoding='utf-8'))
        except Exception:
            existing_data = {}
        autoload = existing_data.setdefault('autoload', {})
        autoload['小米蓝牙语音遥控器'] = 'vibe_coding'
        autoload['MI RC'] = 'vibe_coding'
        config_dest.write_text(json.dumps(existing_data, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')
    else:
        shutil.copy2(config_source, config_dest)

    # Use dynamic absolute path of input-remapper-control in service
    remapper_bin = shutil.which('input-remapper-control') or '/usr/bin/input-remapper-control'
    service_text = service_source.read_text(encoding='utf-8')
    service_text = service_text.replace('/usr/bin/input-remapper-control', remapper_bin)

    USER_SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    service_dest = USER_SYSTEMD_DIR / 'input-remapper-autoload.service'
    service_dest.write_text(service_text, encoding='utf-8')

    # Reload systemd and attempt enabling
    try:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', '--user', 'enable', 'input-remapper-autoload.service'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass

    return {
        'success': True,
        'presets_configured': ['MI RC', '小米蓝牙语音遥控器'],
        'service_installed': str(service_dest),
    }


def setup_key(key: str):
    key = key.strip()
    if not key or '\n' in key or '\r' in key:
        raise ValueError('Invalid API Key format: must be non-empty single line')
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    KEY_FILE.write_text(key + '\n', encoding='utf-8')
    KEY_FILE.chmod(0o600)
    return {'success': True, 'path': str(KEY_FILE), 'mode': '0600'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    status_parser = sub.add_parser('status', help='Inspect configuration and environmental readiness')
    status_parser.add_argument('--json', action='store_true', help='Output machine-readable JSON format')

    setup_desktop_p = sub.add_parser('setup-desktop', help='Install desktop shortcuts and launcher script')
    setup_desktop_p.add_argument('--force', action='store_true')

    setup_keymap_p = sub.add_parser('setup-keymap', help='Install input-remapper presets and user service')
    setup_keymap_p.add_argument('--force', action='store_true')

    setup_key_p = sub.add_parser('setup-key', help='Configure DashScope API Key securely')
    setup_key_p.add_argument('key', nargs='?', help='API key (read from stdin or prompt if omitted)')

    setup_all_p = sub.add_parser('setup-all', help='Set up desktop entries and keymap presets, then report status')
    setup_all_p.add_argument('--json', action='store_true')

    args = parser.parse_args()

    if args.command == 'status':
        data = inspect_all()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print('=== MI 语音输入环境诊断 ===')
            print(f"整体可用就绪: {'已就绪' if data['overall_ready'] else '未就绪'}")
            bt = data['bluetooth']
            print(f"蓝牙遥控器: {'已发现 (' + bt['name'] + ' ' + str(bt['device_mac']) + ')' if bt['matched'] else '未发现配对设备'}")
            print(f"  - 配对: {'是' if bt['paired'] else '否'}, 连接: {'是' if bt['connected'] else '否'}")
            km = data['keymap']
            print(f"按键映射: {'工具就绪' if km['tool_installed'] else 'input-remapper 未安装'}")
            print(f"  - 预设文件: {'已就绪' if km['presets_ready'] else '缺失'}")
            print(f"  - 自动加载服务: {'已启用' if km['service_enabled'] else '未启用'}")
            dt = data['desktop']
            print(f"桌面快捷方式: {'全套就绪' if dt['all_ready'] else '部分缺失或未授权执行'}")
            cr = data['credentials']
            print(f"百炼 API Key: {'已配置 (' + cr['source'] + ')' if cr['configured'] else '未配置'}")
            rt = data['runtimes']
            print(f"接收器二进制: {rt['receiver_path'] or '未找到'}")
            print(f"云端运行环境: {'就绪' if rt['cloud_ready'] else '缺失'}")
            print(f"本地模型环境: {'就绪' if rt['local_model_ready'] else '缺失'}")
        sys.exit(0 if data['overall_ready'] else 1)

    elif args.command == 'setup-desktop':
        res = setup_desktop(args.force)
        print(f"桌面快捷方式与启动器已安装 ({len(res['files'])} 个文件)")
        sys.exit(0)

    elif args.command == 'setup-keymap':
        res = setup_keymap(args.force)
        print(f"按键映射预设已同步至设备名: {', '.join(res['presets_configured'])}")
        print(f"自启服务已更新: {res['service_installed']}")
        sys.exit(0)

    elif args.command == 'setup-key':
        key_val = args.key
        if not key_val:
            if not sys.stdin.isatty():
                key_val = sys.stdin.read().strip()
            elif 'DASHSCOPE_API_KEY' in os.environ:
                key_val = os.environ['DASHSCOPE_API_KEY'].strip()
            else:
                key_val = getpass.getpass('请输入百炼 API Key: ')
        setup_key(key_val)
        print('API Key 已安全保存至 ~/.config/mi-voice/api.key (权限 600)。')
        sys.exit(0)

    elif args.command == 'setup-all':
        setup_desktop(True)
        setup_keymap(True)
        data = inspect_all()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print('桌面快捷方式与按键映射已全部配置完成。')
            print(f"整体就绪状态: {'就绪' if data['overall_ready'] else '需进一步配置'}")
        sys.exit(0 if data['overall_ready'] else 1)


if __name__ == '__main__':
    main()
