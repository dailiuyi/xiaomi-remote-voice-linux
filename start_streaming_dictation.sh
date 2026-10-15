#!/usr/bin/env bash
set -euo pipefail
ASR_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
cd -- "$ASR_TASK_ROOT"
if [[ -z "${DASHSCOPE_API_KEY:-}" && -f "$HOME/.config/mi-voice/api.key" ]]; then
  KEY_FILE_PATH="$HOME/.config/mi-voice/api.key"
  KEY_PERM="$(stat -c '%a' "$KEY_FILE_PATH" 2>/dev/null || stat -f '%Lp' "$KEY_FILE_PATH" 2>/dev/null || echo '')"
  if [[ "$KEY_PERM" == "600" ]]; then
    DASHSCOPE_API_KEY="$(tr -d '\r\n ' < "$KEY_FILE_PATH")"
    export DASHSCOPE_API_KEY
  else
    echo "警告: $KEY_FILE_PATH 权限过于开放 ($KEY_PERM)，必须为 600 (chmod 600 $KEY_FILE_PATH)，已拒绝自动加载。" >&2
  fi
fi
ASR_FLOAT=1
ASR_STOP=0
ASR_CHECK_ONLY=0
ASR_BACKEND_SELECTED="${ASR_BACKEND:-qwen-api}"
ASR_EXPECT_BACKEND=0
ASR_HAS_RECEIVER=0
for ASR_OPTION in "$@"; do
  if [[ "$ASR_EXPECT_BACKEND" == 1 ]]; then
    ASR_BACKEND_SELECTED="$ASR_OPTION"
    ASR_EXPECT_BACKEND=0
  fi
  case "$ASR_OPTION" in
    --stop) ASR_FLOAT=0; ASR_STOP=1 ;;
    --replay|--replay=*|--help|-h) ASR_FLOAT=0 ;;
    --check-config) ASR_FLOAT=0; ASR_CHECK_ONLY=1 ;;
    --backend) ASR_EXPECT_BACKEND=1 ;;
    --backend=*) ASR_BACKEND_SELECTED="${ASR_OPTION#*=}" ;;
    --receiver|--receiver=*) ASR_HAS_RECEIVER=1 ;;
  esac
done
if [[ "$ASR_HAS_RECEIVER" == 0 && ! -f "$ASR_TASK_ROOT/artifacts/2026-10-12-native-receiver/atvvoice-tested" && -f "$ASR_TASK_ROOT/artifacts/receiver-build/target/debug/atvvoice" ]]; then
  set -- "$@" --receiver "$ASR_TASK_ROOT/artifacts/receiver-build/target/debug/atvvoice"
fi
if [[ "$ASR_STOP" == 1 ]]; then
  /usr/bin/python3 "$ASR_TASK_ROOT/floating_preview.py" --stop || true
  /usr/bin/python3 "$ASR_TASK_ROOT/streaming_dictation.py" --stop || true
  exit 0
fi
# Help works even before either runtime has been restored.
for ASR_OPTION in "$@"; do
  if [[ "$ASR_OPTION" == '--help' || "$ASR_OPTION" == '-h' ]]; then
    exec /usr/bin/python3 "$ASR_TASK_ROOT/streaming_dictation.py" "$@"
  fi
done
case "$ASR_BACKEND_SELECTED" in
  qwen-api) ASR_PYTHON="$ASR_TASK_ROOT/artifacts/cloud-asr/venv/bin/python" ;;
  local) ASR_PYTHON="$ASR_TASK_ROOT/artifacts/asr-comparison/runtime/venv/bin/python" ;;
  *) echo '后端必须为 qwen-api 或 local。' >&2; exit 2 ;;
esac
if [[ ! -x "$ASR_PYTHON" ]]; then
  echo "缺少 $ASR_BACKEND_SELECTED 运行环境，请参照 BUILD_RUN.md 恢复。" >&2
  exit 1
fi
# Validate before showing a floating window or touching a receiver. This sends
# no cloud requests and prevents a missing key from leaving a stray preview.
"$ASR_PYTHON" "$ASR_TASK_ROOT/streaming_dictation.py" "$@" --check-config
if [[ "$ASR_CHECK_ONLY" == 1 ]]; then
  exit 0
fi
if [[ "$ASR_FLOAT" == 1 ]]; then
  mkdir -p "$ASR_TASK_ROOT/artifacts/floating-preview"
  nohup setsid /usr/bin/python3 "$ASR_TASK_ROOT/floating_preview.py" </dev/null > "$ASR_TASK_ROOT/artifacts/floating-preview/live.log" 2>&1 &
  # Reusing the live instance avoids loading a second GPU model just to show UI.
  if /usr/bin/python3 - <<'PY'
import json, os
from pathlib import Path
root=Path.cwd()
try:
    data=json.loads((root/'artifacts/streaming-runtime/current.json').read_text())
    proc=Path(f'/proc/{int(data["pid"])}')
    assert proc.stat().st_uid==os.getuid()
    assert str(root/'streaming_dictation.py').encode() in (proc/'cmdline').read_bytes().split(b'\x00')
except (OSError,ValueError,KeyError,AssertionError):
    raise SystemExit(1)
PY
  then
    if [[ $# -eq 0 ]] && /usr/bin/python3 - "$ASR_BACKEND_SELECTED" <<'PY'
import json, sys
from pathlib import Path
data=json.loads(Path('artifacts/streaming-runtime/current.json').read_text())
raise SystemExit(0 if data.get('backend', 'local') == sys.argv[1] else 1)
PY
    then
      echo '现有 Qwen 语音服务已运行，已显示悬浮预览。'
      exit 0
    fi
    echo '语音服务已运行；请先 --stop 再更改运行参数。' >&2
    exit 1
  fi
fi
exec "$ASR_PYTHON" "$ASR_TASK_ROOT/streaming_dictation.py" "$@"
