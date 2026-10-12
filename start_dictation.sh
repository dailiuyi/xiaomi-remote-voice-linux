#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TASK_VENV="$TASK_ROOT/artifacts/dictation-runtime/venv"
TASK_LIBS="$TASK_VENV/lib/python3.12/site-packages/nvidia"
for TASK_LIBRARY in "$TASK_LIBS"/*/lib; do
  if [[ -d "$TASK_LIBRARY" ]]; then
    export LD_LIBRARY_PATH="$TASK_LIBRARY${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
done
export HF_HUB_OFFLINE=1
cd -- "$TASK_ROOT"
if [[ $# -eq 0 ]]; then
  set -- live
fi
exec "$TASK_VENV/bin/python" "$TASK_ROOT/dictation.py" "$@"
