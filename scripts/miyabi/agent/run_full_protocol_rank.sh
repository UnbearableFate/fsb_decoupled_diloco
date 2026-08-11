#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Full Protocol rank failed at line $LINENO" >&2' ERR

: "${OMPI_COMM_WORLD_RANK:?OMPI_COMM_WORLD_RANK is required}"
: "${PYTHON_BIN:?PYTHON_BIN is required}"
: "${RESOLVED_CONFIG:?RESOLVED_CONFIG is required}"
: "${SHARED_ROOT:?SHARED_ROOT is required}"
: "${LOG_ROOT:?LOG_ROOT is required}"
: "${PBS_JOBID:?PBS_JOBID is required}"
: "${SYNCER_TAKEOVER_BOUNDARY_VERSION:?SYNCER_TAKEOVER_BOUNDARY_VERSION is required}"

rank="$OMPI_COMM_WORLD_RANK"
scenario="${FS_DILOCO_FAULT_SCENARIO:-none}"
case "$scenario" in
  none|syncer_takeover) ;;
  *) echo "unsupported fault scenario: $scenario" >&2; exit 2 ;;
esac

run_learner() {
  local bootstrap_slot="$1"
  CUDA_VISIBLE_DEVICES="$LEARNER_CUDA_VISIBLE_DEVICES" \
  OMP_NUM_THREADS="$LEARNER_OMP_NUM_THREADS" \
    "$PYTHON_BIN" -m fs_diloco.learner \
      --config "$RESOLVED_CONFIG" \
      --shared-root "$SHARED_ROOT" \
      --bootstrap-slot "$bootstrap_slot"
}

SYNCER_COMMAND=(
  "$PYTHON_BIN" -m fs_diloco.syncer
  --config "$RESOLVED_CONFIG"
  --shared-root "$SHARED_ROOT"
)

run_syncer() {
  local log_path="$1"
  CUDA_VISIBLE_DEVICES="$SYNCER_CUDA_VISIBLE_DEVICES" \
  OMP_NUM_THREADS="$SYNCER_OMP_NUM_THREADS" \
    "${SYNCER_COMMAND[@]}" >"$log_path" 2>&1
}

if [[ "$rank" -eq 0 ]]; then
  if [[ "$scenario" == "syncer_takeover" ]]; then
    fault_marker="$LOG_ROOT/syncer_primary_fault_boundary.json"
    FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION="$SYNCER_TAKEOVER_BOUNDARY_VERSION" \
    FS_DILOCO_FAULT_PAUSE_MARKER_PATH="$fault_marker" \
    CUDA_VISIBLE_DEVICES="$SYNCER_CUDA_VISIBLE_DEVICES" \
    OMP_NUM_THREADS="$SYNCER_OMP_NUM_THREADS" \
      "${SYNCER_COMMAND[@]}" >"$LOG_ROOT/syncer_primary.log" 2>&1 &
    primary_pid=$!
    deadline=$((SECONDS + 120))
    while [[ ! -f "$fault_marker" ]]; do
      if ! kill -0 "$primary_pid" 2>/dev/null; then
        wait "$primary_pid" 2>/dev/null || true
        echo "primary syncer exited before the registered fault boundary" >&2
        exit 1
      fi
      if ((SECONDS >= deadline)); then
        kill -KILL "$primary_pid" 2>/dev/null || true
        wait "$primary_pid" 2>/dev/null || true
        echo "primary syncer did not reach the registered fault boundary" >&2
        exit 1
      fi
      sleep 0.1
    done
    kill -KILL "$primary_pid"
    set +e
    wait "$primary_pid"
    primary_status=$?
    set -e
    if [[ "$primary_status" -eq 0 ]]; then
      echo "primary syncer survived the registered fault injection" >&2
      exit 1
    fi
    run_syncer "$LOG_ROOT/syncer_successor.log"
    "$PYTHON_BIN" - "$fault_marker" "$LOG_ROOT/syncer_takeover.json" \
      "$primary_status" "$primary_pid" <<'PY'
import json
import os
import pathlib
import sys

fault_marker = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
payload = {
    "artifact_version": 1,
    "primary_exit_status": int(sys.argv[3]),
    "primary_pid": int(sys.argv[4]),
    "fault_boundary": json.loads(fault_marker.read_text(encoding="utf-8")),
}
temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, target)
directory = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
  else
    run_syncer "$LOG_ROOT/syncer.log"
  fi
  exit 0
fi

bootstrap_slot=$((rank - 1))
printf -v learner_log 'bootstrap_%03d.log' "$bootstrap_slot"
run_learner "$bootstrap_slot" >"$LOG_ROOT/$learner_log" 2>&1
