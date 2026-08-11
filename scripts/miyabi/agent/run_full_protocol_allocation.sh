#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Full Protocol allocation failed at line $LINENO" >&2' ERR

: "${PROJECT_ROOT:?PROJECT_ROOT is required}"
: "${PYTHON_BIN:?PYTHON_BIN is required}"
: "${CONFIG:?CONFIG is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${SHARED_ROOT:?SHARED_ROOT is required}"
: "${EXPECTED_NODES:?EXPECTED_NODES is required}"
: "${NUM_LEARNERS:?NUM_LEARNERS is required}"
: "${SYNCER_TAKEOVER_BOUNDARY_VERSION:?SYNCER_TAKEOVER_BOUNDARY_VERSION is required}"
: "${PBS_NODEFILE:?PBS_NODEFILE is required}"

PRIMARY_WORKTREE_ROOT="${PRIMARY_WORKTREE_ROOT:-$PROJECT_ROOT}"
LOG_ROOT="${LOG_ROOT:-$PRIMARY_WORKTREE_ROOT/logs/qsub_${RUN_ID}}"
SYNCER_CUDA_VISIBLE_DEVICES="${SYNCER_CUDA_VISIBLE_DEVICES:-0}"
LEARNER_CUDA_VISIBLE_DEVICES="${LEARNER_CUDA_VISIBLE_DEVICES:-0}"
SYNCER_OMP_NUM_THREADS="${SYNCER_OMP_NUM_THREADS:-${OMP_NUM_THREADS:-8}}"
LEARNER_OMP_NUM_THREADS="${LEARNER_OMP_NUM_THREADS:-${OMP_NUM_THREADS:-8}}"

cd "$PROJECT_ROOT"
test -x "$PYTHON_BIN"
mapfile -t HOSTS < <(sort -u "$PBS_NODEFILE")
if [[ "${#HOSTS[@]}" -ne "$EXPECTED_NODES" ]]; then
  echo "Expected $EXPECTED_NODES unique nodes, got ${#HOSTS[@]}" >&2
  printf '%s\n' "${HOSTS[@]}" >&2
  exit 2
fi
expected_learners=$((EXPECTED_NODES - 1))
if [[ "$NUM_LEARNERS" -ne "$expected_learners" ]]; then
  echo "NUM_LEARNERS=$NUM_LEARNERS does not match allocation topology $expected_learners" >&2
  exit 2
fi

mkdir -p "$LOG_ROOT"
source_json="$LOG_ROOT/source_identity.json"
source_env="$LOG_ROOT/source_identity.env"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/miyabi/agent/capture_source_identity.py" \
  --project-root "$PROJECT_ROOT" \
  --output-json "$source_json" \
  --output-env "$source_env" \
  > "$LOG_ROOT/source_identity.log"
source "$source_env"

init_args=(
  --config "$CONFIG"
  --run-id "$RUN_ID"
  --shared-root "$SHARED_ROOT"
  --project-root "$PROJECT_ROOT"
)
if [[ "${FS_DILOCO_ALLOW_DIRTY_SNAPSHOT:-0}" == 1 ]]; then
  init_args+=(--allow-dirty-snapshot)
fi
init_json="$LOG_ROOT/init_run.json"
"$PYTHON_BIN" -m fs_diloco.tools.init_run "${init_args[@]}" > "$init_json"

readarray -t descriptor_fields < <(
  "$PYTHON_BIN" - "$init_json" <<'PY'
import json
import sys

descriptor = json.load(open(sys.argv[1], encoding="utf-8"))["descriptor"]
if descriptor["mode"] != "static":
    raise SystemExit("allocation runner requires a static descriptor")
print(descriptor["resolved_config_path"])
print(descriptor["descriptor_sha256"])
print(descriptor["git_commit"])
print(descriptor["source_fingerprint"])
print(int(bool(descriptor["git_dirty"])))
print(len(descriptor["static_learner_ids"]))
PY
)
RESOLVED_CONFIG="${descriptor_fields[0]}"
FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256="${descriptor_fields[1]}"
FS_DILOCO_EXPECTED_GIT_COMMIT="${descriptor_fields[2]}"
FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT="${descriptor_fields[3]}"
FS_DILOCO_GIT_DIRTY="${descriptor_fields[4]}"
if [[ "${descriptor_fields[5]}" -ne "$NUM_LEARNERS" ]]; then
  echo "Descriptor learner count ${descriptor_fields[5]} != allocation $NUM_LEARNERS" >&2
  exit 2
fi
export FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1

MPI_ENV_ARGS=(
  "PROJECT_ROOT=$PROJECT_ROOT"
  "PRIMARY_WORKTREE_ROOT=$PRIMARY_WORKTREE_ROOT"
  "PYTHON_BIN=$PYTHON_BIN"
  "RUN_ID=$RUN_ID"
  "SHARED_ROOT=$SHARED_ROOT"
  "RESOLVED_CONFIG=$RESOLVED_CONFIG"
  "LOG_ROOT=$LOG_ROOT"
  "SYNCER_CUDA_VISIBLE_DEVICES=$SYNCER_CUDA_VISIBLE_DEVICES"
  "LEARNER_CUDA_VISIBLE_DEVICES=$LEARNER_CUDA_VISIBLE_DEVICES"
  "SYNCER_OMP_NUM_THREADS=$SYNCER_OMP_NUM_THREADS"
  "LEARNER_OMP_NUM_THREADS=$LEARNER_OMP_NUM_THREADS"
  "FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256=$FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"
  "FS_DILOCO_EXPECTED_GIT_COMMIT=$FS_DILOCO_EXPECTED_GIT_COMMIT"
  "FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT=$FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"
  "FS_DILOCO_GIT_COMMIT=$FS_DILOCO_EXPECTED_GIT_COMMIT"
  "FS_DILOCO_SOURCE_FINGERPRINT=$FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"
  "FS_DILOCO_GIT_DIRTY=$FS_DILOCO_GIT_DIRTY"
  "FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1"
  "FS_DILOCO_FAULT_SCENARIO=${FS_DILOCO_FAULT_SCENARIO:-none}"
  "SYNCER_TAKEOVER_BOUNDARY_VERSION=$SYNCER_TAKEOVER_BOUNDARY_VERSION"
  "HF_HOME=${HF_HOME:-}"
  "HF_DATASETS_CACHE=${HF_DATASETS_CACHE:-}"
  "HF_HUB_CACHE=${HF_HUB_CACHE:-}"
  "HF_DATASETS_OFFLINE=${HF_DATASETS_OFFLINE:-0}"
  "HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-0}"
  "TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}"
)

printf 'RUN_ID=%s\nSHARED_ROOT=%s\nDESCRIPTOR_SHA256=%s\n' \
  "$RUN_ID" "$SHARED_ROOT" "$FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"
printf 'HOST %s\n' "${HOSTS[@]}"

mpirun \
  --mca mpi_abort_print_stack 1 \
  --report-bindings \
  --map-by ppr:1:node \
  --bind-to none \
  -np "$EXPECTED_NODES" \
  /usr/bin/env "${MPI_ENV_ARGS[@]}" \
  bash "$PROJECT_ROOT/scripts/miyabi/agent/run_full_protocol_rank.sh"

"$PYTHON_BIN" -m fs_diloco.tools.analysis "$SHARED_ROOT" | tee "$LOG_ROOT/summary.json"
