#!/usr/bin/env bash
set -euo pipefail
FLOAT_TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export DISPLAY="${DISPLAY:-:0}"
exec /usr/bin/python3 "$FLOAT_TASK_ROOT/floating_preview.py" "$@"
