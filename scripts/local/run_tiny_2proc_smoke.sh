#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
CONFIG="${CONFIG:-$PROJECT_ROOT/configs/fs_diloco_tiny_local.yaml}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_tiny_local}"
SHARED_ROOT="${SHARED_ROOT:-$PROJECT_ROOT/runs/fs_diloco/$RUN_ID}"

cd "$PROJECT_ROOT"
mkdir -p "$SHARED_ROOT" "$PROJECT_ROOT/logs"
LOG_ROOT="$PROJECT_ROOT/logs/local_${RUN_ID}"
mkdir -p "$LOG_ROOT"

"$PYTHON_BIN" -m fs_diloco.syncer \
  --config "$CONFIG" \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  > "$LOG_ROOT/syncer.log" 2>&1 &
syncer_pid=$!

sleep 1

"$PYTHON_BIN" -m fs_diloco.learner \
  --config "$CONFIG" \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --learner-id learner_000 \
  > "$LOG_ROOT/learner_000.log" 2>&1 &
learner0_pid=$!

"$PYTHON_BIN" -m fs_diloco.learner \
  --config "$CONFIG" \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --learner-id learner_001 \
  > "$LOG_ROOT/learner_001.log" 2>&1 &
learner1_pid=$!

wait "$learner0_pid"
wait "$learner1_pid"
wait "$syncer_pid"

"$PYTHON_BIN" -m fs_diloco.analysis "$SHARED_ROOT" --json > "$LOG_ROOT/summary.json"
cat "$LOG_ROOT/summary.json"
