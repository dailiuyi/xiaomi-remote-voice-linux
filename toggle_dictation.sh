#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_META="$SCRIPT_DIR/artifacts/streaming-runtime/current.json"

is_service_running() {
    if [[ ! -f "$RUNTIME_META" ]]; then
        return 1
    fi
    /usr/bin/python3 - <<PY
import json, os, sys
from pathlib import Path
meta_file = Path('$RUNTIME_META')
try:
    data = json.loads(meta_file.read_text())
    pid = int(data['pid'])
    proc = Path(f'/proc/{pid}')
    if proc.stat().st_uid != os.getuid():
        sys.exit(1)
    cmdline = (proc/'cmdline').read_bytes().split(b'\x00')
    if b'streaming_dictation.py' in cmdline or str(Path('$SCRIPT_DIR')/'streaming_dictation.py').encode() in cmdline:
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
}

if is_service_running; then
    # Stop dictation service and floating island preview
    bash "$SCRIPT_DIR/start_streaming_dictation.sh" --stop
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -i microphone -a "MI 语音" "MI 语音服务已停止" "语音输入与灵动岛悬浮窗已关闭。"
    fi
    echo "MI 语音服务已停止"
else
    # Start dictation service and floating island preview
    mkdir -p "$SCRIPT_DIR/artifacts/streaming-runtime"
    nohup setsid bash "$SCRIPT_DIR/start_streaming_dictation.sh" </dev/null > "$SCRIPT_DIR/artifacts/streaming-runtime/live.log" 2>&1 &
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -i microphone -a "MI 语音" "MI 语音服务已启动" "语音识别与灵动岛已就绪，按住遥控器语音键说话。"
    fi
    echo "MI 语音服务已启动"
fi
