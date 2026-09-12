#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SOURCE_ROOT="${1:-$PROJECT_ROOT/data/sim_data/sim_data_0622_2}"
OUTPUT_ROOT="${2:-$PROJECT_ROOT/data/sim_data/sim_data_0622_2_converted}"
LEFT_TARGET="${3:-${LEFT_TARGET:-Grape Juice}}"
RIGHT_TARGET="${4:-${RIGHT_TARGET:-}}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
JOBS="${JOBS:-4}"
TASK_ARGS=()
if [ -n "${TASK_TEXT:-}" ]; then
  TASK_ARGS=(--task "$TASK_TEXT")
fi

cd "$PROJECT_ROOT"

"$PYTHON_BIN" scripts/convert_sim_robotwin_to_aloha_hdf5.py \
  --source-root "$SOURCE_ROOT" \
  --output-root "$OUTPUT_ROOT" \
  --left-target "$LEFT_TARGET" \
  --right-target "$RIGHT_TARGET" \
  "${TASK_ARGS[@]}" \
  --jobs "$JOBS" \
  --replace-output

echo "Done: $OUTPUT_ROOT"
