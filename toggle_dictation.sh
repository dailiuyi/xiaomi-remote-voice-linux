#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR

check_running_components() {
    /usr/bin/python3 - <<'PY'
import json, os, sys
from pathlib import Path

root = Path(os.environ['SCRIPT_DIR']).resolve()
my_pid = os.getpid()
uid = os.getuid()
running = {}

def identify_proc(proc_path):
    try:
        if proc_path.stat().st_uid != uid:
            return None
        cmd_raw = (proc_path / 'cmdline').read_bytes().split(b'\x00')
        cmd_parts = [c.decode(errors='ignore') for c in cmd_raw if c]
        if not cmd_parts:
            return None
        exe_name = Path(cmd_parts[0]).name
        try:
            cwd = Path(os.readlink(proc_path / 'cwd')).resolve()
        except Exception:
            cwd = None

        targets = {
            'streaming_dictation.py': 'streaming_dictation',
            'floating_preview.py': 'floating_preview',
            'dictation.py': 'dictation',
        }
        if 'python' in exe_name:
            for arg in cmd_parts[1:]:
                arg_path = Path(arg)
                for t_name, t_comp in targets.items():
                    if arg_path.name == t_name:
                        full_path = arg_path if arg_path.is_absolute() else (cwd / arg_path if cwd else root / arg_path)
                        try:
                            if full_path.resolve() == (root / t_name).resolve():
                                return t_comp
                        except Exception:
                            pass
        if 'atvvoice' in exe_name:
            try:
                exe_path = Path(os.readlink(proc_path / 'exe')).resolve()
                if str(exe_path).startswith(str(root)):
                    return 'atvvoice'
            except Exception:
                pass
            if cwd and (cwd == root or str(cwd).startswith(str(root))):
                return 'atvvoice'
    except (OSError, ProcessLookupError):
        pass
    return None

# 1. 优先通过元数据校验当前主组件
meta = root / 'artifacts/streaming-runtime/current.json'
if meta.exists():
    try:
        pid = int(json.loads(meta.read_text())['pid'])
        proc = Path(f'/proc/{pid}')
        if proc.exists() and identify_proc(proc) == 'streaming_dictation':
            running['streaming_dictation'] = pid
    except Exception:
        pass

float_meta = root / 'artifacts/floating-preview/current.json'
if float_meta.exists():
    try:
        pid = int(json.loads(float_meta.read_text())['pid'])
        proc = Path(f'/proc/{pid}')
        if proc.exists() and identify_proc(proc) == 'floating_preview':
            running['floating_preview'] = pid
    except Exception:
        pass

# 2. 扫描 /proc 作为兜底
for proc_entry in Path('/proc').iterdir():
    if not proc_entry.name.isdigit():
        continue
    pid = int(proc_entry.name)
    if pid == my_pid:
        continue
    comp = identify_proc(proc_entry)
    if comp and comp not in running:
        running[comp] = pid

if running:
    print(" ".join(f"{k}:{v}" for k, v in running.items()))
    sys.exit(0)
sys.exit(1)
PY
}

stop_services() {
    local running_info
    local was_running=0

    if running_info=$(check_running_components); then
        was_running=1
    fi

    # 1. Gracefully call standard stop
    bash "$SCRIPT_DIR/start_streaming_dictation.sh" --stop >/dev/null 2>&1 || true

    # 2. Re-check and terminate any remaining processes cleanly with exit confirmation
    if /usr/bin/python3 - <<'PY'
import json, os, signal, sys, time
from pathlib import Path

root = Path(os.environ['SCRIPT_DIR']).resolve()
my_pid = os.getpid()
uid = os.getuid()

def identify_proc(proc_path):
    try:
        if proc_path.stat().st_uid != uid:
            return None
        cmd_raw = (proc_path / 'cmdline').read_bytes().split(b'\x00')
        cmd_parts = [c.decode(errors='ignore') for c in cmd_raw if c]
        if not cmd_parts:
            return None
        exe_name = Path(cmd_parts[0]).name
        try:
            cwd = Path(os.readlink(proc_path / 'cwd')).resolve()
        except Exception:
            cwd = None

        targets = {
            'streaming_dictation.py': 'streaming_dictation',
            'floating_preview.py': 'floating_preview',
            'dictation.py': 'dictation',
        }
        if 'python' in exe_name:
            for arg in cmd_parts[1:]:
                arg_path = Path(arg)
                for t_name, t_comp in targets.items():
                    if arg_path.name == t_name:
                        full_path = arg_path if arg_path.is_absolute() else (cwd / arg_path if cwd else root / arg_path)
                        try:
                            if full_path.resolve() == (root / t_name).resolve():
                                return t_comp
                        except Exception:
                            pass
        if 'atvvoice' in exe_name:
            try:
                exe_path = Path(os.readlink(proc_path / 'exe')).resolve()
                if str(exe_path).startswith(str(root)):
                    return 'atvvoice'
            except Exception:
                pass
            if cwd and (cwd == root or str(cwd).startswith(str(root))):
                return 'atvvoice'
    except (OSError, ProcessLookupError):
        pass
    return None

def scan_pids():
    found = []
    for proc_entry in Path('/proc').iterdir():
        if not proc_entry.name.isdigit():
            continue
        pid = int(proc_entry.name)
        if pid == my_pid:
            continue
        if identify_proc(proc_entry) is not None:
            found.append(pid)
    return list(set(found))

pids = scan_pids()
for pid in pids:
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass

for _ in range(30):
    pids = scan_pids()
    if not pids:
        break
    time.sleep(0.1)

if pids:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for _ in range(20):
        pids = scan_pids()
        if not pids:
            break
        time.sleep(0.1)

if pids:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.3)
    pids = scan_pids()

if pids:
    sys.stderr.write(f"Remaining processes still alive: {pids}\n")
    sys.exit(1)

(root / 'artifacts/streaming-runtime/current.json').unlink(missing_ok=True)
(root / 'artifacts/floating-preview/current.json').unlink(missing_ok=True)
sys.exit(0)
PY
    then
        if [[ "$was_running" -eq 1 ]]; then
            if command -v notify-send >/dev/null 2>&1; then
                notify-send -i process-stop -a "MI 语音" "MI 语音服务已停止" "语音输入后台服务与悬浮窗已全部关闭。"
            fi
            echo "MI 语音服务已停止"
        else
            if command -v notify-send >/dev/null 2>&1; then
                notify-send -i dialog-information -a "MI 语音" "MI 语音服务未运行" "当前没有正在运行的语音服务，无需关闭。"
            fi
            echo "MI 语音服务未运行"
        fi
    else
        if command -v notify-send >/dev/null 2>&1; then
            notify-send -u critical -a "MI 语音" "MI 语音服务停止失败" "部分后台组件未能停止，元数据保留。"
        fi
        echo "MI 语音服务停止失败：部分后台进程仍在运行" >&2
        return 1
    fi
}

start_services() {
    local running_info
    if running_info=$(check_running_components); then
        # 仅当核心语音听写服务已在运行时才短路返回
        if echo "$running_info" | grep -Eq 'streaming_dictation|dictation'; then
            if command -v notify-send >/dev/null 2>&1; then
                notify-send -i dialog-information -a "MI 语音" "MI 语音服务已在运行" "语音识别与灵动岛悬浮窗当前处于就绪状态。"
            fi
            echo "MI 语音服务已在运行"
            return 0
        fi
    fi

    mkdir -p "$SCRIPT_DIR/artifacts/streaming-runtime"
    rm -f "$SCRIPT_DIR/artifacts/streaming-runtime/current.json"
    nohup setsid bash "$SCRIPT_DIR/start_streaming_dictation.sh" </dev/null > "$SCRIPT_DIR/artifacts/streaming-runtime/live.log" 2>&1 &
    local starter_pid=$!

    # 轮询等待后台实际加载就绪（写入 current.json），最长等待 15 秒
    local ready=0
    for _ in $(seq 1 75); do
        sleep 0.2
        if [[ -f "$SCRIPT_DIR/artifacts/streaming-runtime/current.json" ]]; then
            ready=1
            break
        fi
        if ! kill -0 "$starter_pid" 2>/dev/null; then
            if ! check_running_components | grep -Eq 'streaming_dictation|dictation'; then
                break
            fi
        fi
    done

    if [[ "$ready" -eq 1 ]]; then
        if command -v notify-send >/dev/null 2>&1; then
            notify-send -i microphone -a "MI 语音" "MI 语音服务已启动" "语音识别与灵动岛已就绪，按住遥控器语音键说话。"
        fi
        echo "MI 语音服务已启动"
        return 0
    else
        if command -v notify-send >/dev/null 2>&1; then
            notify-send -u critical -a "MI 语音" "MI 语音服务启动失败" "服务未在预期时间内就绪，请检查 live.log。"
        fi
        echo "MI 语音服务启动失败，请检查日志: $SCRIPT_DIR/artifacts/streaming-runtime/live.log" >&2
        return 1
    fi
}

ACTION="${1:---toggle}"

case "$ACTION" in
    --stop|-s|stop)
        stop_services
        ;;
    --start|start)
        start_services
        ;;
    --restart|restart)
        stop_services
        sleep 0.5
        start_services
        ;;
    --status|status)
        if running_info=$(check_running_components); then
            echo "MI 语音服务运行中: $running_info"
            exit 0
        else
            echo "MI 语音服务未运行"
            exit 1
        fi
        ;;
    --toggle|toggle|*)
        if check_running_components >/dev/null 2>&1; then
            stop_services
        else
            start_services
        fi
        ;;
esac
