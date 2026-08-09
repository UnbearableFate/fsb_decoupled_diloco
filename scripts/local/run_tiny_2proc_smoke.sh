#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PRIMARY_WORKTREE_ROOT="${PRIMARY_WORKTREE_ROOT:-$PROJECT_ROOT}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
CONFIG="${CONFIG:-$PROJECT_ROOT/configs/fs_diloco_tiny_local.yaml}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_tiny_local}"
SHARED_ROOT="${SHARED_ROOT:-$PRIMARY_WORKTREE_ROOT/runs/fs_diloco/$RUN_ID}"
NUM_LEARNERS="${NUM_LEARNERS:-2}"
LOG_ROOT="${LOG_ROOT:-$PRIMARY_WORKTREE_ROOT/logs/local_${RUN_ID}}"

if ! [[ "$NUM_LEARNERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "NUM_LEARNERS must be a positive integer: $NUM_LEARNERS" >&2
  exit 2
fi
cd "$PROJECT_ROOT"
mkdir -p "$LOG_ROOT"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/miyabi/capture_source_identity.py" \
  --project-root "$PROJECT_ROOT" \
  --output-json "$LOG_ROOT/source_identity.json" \
  --output-env "$LOG_ROOT/source_identity.env" \
  > "$LOG_ROOT/source_identity.log"
source "$LOG_ROOT/source_identity.env"

init_args=(
  --config "$CONFIG"
  --run-id "$RUN_ID"
  --shared-root "$SHARED_ROOT"
  --project-root "$PROJECT_ROOT"
)
if [[ "${FS_DILOCO_ALLOW_DIRTY_SNAPSHOT:-0}" == 1 ]]; then
  init_args+=(--allow-dirty-snapshot)
fi
"$PYTHON_BIN" -m fs_diloco.tools.init_run "${init_args[@]}" > "$LOG_ROOT/init_run.json"

readarray -t descriptor_fields < <(
  "$PYTHON_BIN" - "$LOG_ROOT/init_run.json" <<'PY'
import json
import sys

descriptor = json.load(open(sys.argv[1], encoding="utf-8"))["descriptor"]
print(descriptor["resolved_config_path"])
print(descriptor["descriptor_sha256"])
print(descriptor["git_commit"])
print(descriptor["source_fingerprint"])
print(int(bool(descriptor["git_dirty"])))
print(len(descriptor["static_learner_ids"] or []))
PY
)
RESOLVED_CONFIG="${descriptor_fields[0]}"
export FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256="${descriptor_fields[1]}"
export FS_DILOCO_EXPECTED_GIT_COMMIT="${descriptor_fields[2]}"
export FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT="${descriptor_fields[3]}"
export FS_DILOCO_GIT_COMMIT="${descriptor_fields[2]}"
export FS_DILOCO_SOURCE_FINGERPRINT="${descriptor_fields[3]}"
export FS_DILOCO_GIT_DIRTY="${descriptor_fields[4]}"
export FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1
if [[ "${descriptor_fields[5]}" -ne "$NUM_LEARNERS" ]]; then
  echo "Descriptor learner count ${descriptor_fields[5]} != NUM_LEARNERS=$NUM_LEARNERS" >&2
  exit 2
fi

"$PYTHON_BIN" -m fs_diloco.syncer \
  --config "$RESOLVED_CONFIG" \
  --shared-root "$SHARED_ROOT" \
  > "$LOG_ROOT/syncer.log" 2>&1 &
syncer_pid=$!

learner_pids=()
cleanup() {
  kill "$syncer_pid" "${learner_pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
for ((learner_index = 0; learner_index < NUM_LEARNERS; learner_index++)); do
  printf -v learner_id 'learner_%03d' "$learner_index"
  "$PYTHON_BIN" -m fs_diloco.learner \
    --config "$RESOLVED_CONFIG" \
    --shared-root "$SHARED_ROOT" \
    --learner-id "$learner_id" \
    --logical-launch-id "local-${RUN_ID}-${learner_id}" \
    > "$LOG_ROOT/$learner_id.log" 2>&1 &
  learner_pids+=("$!")
done

for learner_pid in "${learner_pids[@]}"; do
  wait "$learner_pid"
done
wait "$syncer_pid"
trap - EXIT INT TERM

"$PYTHON_BIN" -m fs_diloco.analysis "$SHARED_ROOT" --json > "$LOG_ROOT/summary.json"
cat "$LOG_ROOT/summary.json"
