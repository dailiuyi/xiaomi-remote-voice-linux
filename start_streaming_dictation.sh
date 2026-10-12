#!/usr/bin/env bash
set -euo pipefail
ASR_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
cd -- "$ASR_TASK_ROOT"
ASR_FLOAT=1
ASR_STOP=0
for ASR_OPTION in "$@"; do
  case "$ASR_OPTION" in
    --stop) ASR_FLOAT=0; ASR_STOP=1 ;;
    --replay|--replay=*|--help|-h) ASR_FLOAT=0 ;;
  esac
done
if [[ "$ASR_STOP" == 1 ]]; then
  /usr/bin/python3 "$ASR_TASK_ROOT/floating_preview.py" --stop || true
  /usr/bin/python3 "$ASR_TASK_ROOT/streaming_dictation.py" --stop || true
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
    if [[ $# -eq 0 ]]; then
      echo '现有 Qwen 语音服务已运行，已显示悬浮预览。'
      exit 0
    fi
    echo '语音服务已运行；请先 --stop 再更改运行参数。' >&2
    exit 1
  fi
fi
exec "$ASR_TASK_ROOT/artifacts/asr-comparison/runtime/venv/bin/python" "$ASR_TASK_ROOT/streaming_dictation.py" "$@"
