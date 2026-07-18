#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PRIMARY_WORKTREE_ROOT="${PRIMARY_WORKTREE_ROOT:-/work/xg24i002/x10041/fsb_decoupled_diloco}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
CONFIG="${CONFIG:-$PROJECT_ROOT/configs/fs_diloco_tiny_local.yaml}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_tiny_local}"
SHARED_ROOT="${SHARED_ROOT:-$PRIMARY_WORKTREE_ROOT/runs/fs_diloco/$RUN_ID}"
NUM_LEARNERS="${NUM_LEARNERS:-2}"

if ! [[ "$NUM_LEARNERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "NUM_LEARNERS must be a positive integer: $NUM_LEARNERS" >&2
  exit 2
fi

cd "$PROJECT_ROOT"
mkdir -p "$SHARED_ROOT" "$PRIMARY_WORKTREE_ROOT/logs"
LOG_ROOT="$PRIMARY_WORKTREE_ROOT/logs/local_${RUN_ID}"
mkdir -p "$LOG_ROOT"

"$PYTHON_BIN" -m fs_diloco.syncer \
  --config "$CONFIG" \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --num-learners "$NUM_LEARNERS" \
  > "$LOG_ROOT/syncer.log" 2>&1 &
syncer_pid=$!

sleep 1

learner_pids=()
for ((learner_index = 0; learner_index < NUM_LEARNERS; learner_index++)); do
  printf -v learner_id 'learner_%03d' "$learner_index"
  "$PYTHON_BIN" -m fs_diloco.learner \
    --config "$CONFIG" \
    --run-id "$RUN_ID" \
    --shared-root "$SHARED_ROOT" \
    --num-learners "$NUM_LEARNERS" \
    --learner-id "$learner_id" \
    > "$LOG_ROOT/$learner_id.log" 2>&1 &
  learner_pids+=("$!")
done

for learner_pid in "${learner_pids[@]}"; do
  wait "$learner_pid"
done
wait "$syncer_pid"

"$PYTHON_BIN" -m fs_diloco.analysis "$SHARED_ROOT" --json > "$LOG_ROOT/summary.json"
cat "$LOG_ROOT/summary.json"
