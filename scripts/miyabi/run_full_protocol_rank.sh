#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Full Protocol rank failed at line $LINENO" >&2' ERR

: "${OMPI_COMM_WORLD_RANK:?OMPI_COMM_WORLD_RANK is required}"
: "${PYTHON_BIN:?PYTHON_BIN is required}"
: "${RESOLVED_CONFIG:?RESOLVED_CONFIG is required}"
: "${SHARED_ROOT:?SHARED_ROOT is required}"
: "${LOG_ROOT:?LOG_ROOT is required}"
: "${PBS_JOBID:?PBS_JOBID is required}"

rank="$OMPI_COMM_WORLD_RANK"
scenario="${FS_DILOCO_FAULT_SCENARIO:-none}"
case "$scenario" in
  none|learner_replacement|syncer_takeover) ;;
  *) echo "unsupported fault scenario: $scenario" >&2; exit 2 ;;
esac

learner_command() {
  local learner_index="$1"
  local logical_id="$2"
  local attempt_id="${3:-}"
  local learner_id
  printf -v learner_id 'learner_%03d' "$learner_index"
  local -a command=(
    "$PYTHON_BIN" -m fs_diloco.learner
    --config "$RESOLVED_CONFIG"
    --shared-root "$SHARED_ROOT"
    --learner-id "$learner_id"
    --logical-launch-id "$logical_id"
  )
  if [[ -n "$attempt_id" ]]; then
    command+=(--attempt-id "$attempt_id")
  fi
  CUDA_VISIBLE_DEVICES="$LEARNER_CUDA_VISIBLE_DEVICES" \
  OMP_NUM_THREADS="$LEARNER_OMP_NUM_THREADS" \
    "${command[@]}"
}

launch_learner() {
  local learner_index="$1"
  local learner_id logical_id
  printf -v learner_id 'learner_%03d' "$learner_index"
  logical_id="allocation-${PBS_JOBID%%.*}-${learner_id}"
  learner_command "$learner_index" "$logical_id" >"$LOG_ROOT/${learner_id}.log" 2>&1
}

launch_replaced_learner() {
  local learner_index="$1"
  local learner_id old_logical new_logical new_attempt old_attempt old_generation
  printf -v learner_id 'learner_%03d' "$learner_index"
  old_logical="allocation-${PBS_JOBID%%.*}-${learner_id}"
  new_logical="replacement-${PBS_JOBID%%.*}-${learner_id}"
  new_attempt="attempt-${PBS_JOBID%%.*}-${learner_id}-replacement"

  learner_command "$learner_index" "$old_logical" >"$LOG_ROOT/${learner_id}.log" 2>&1 &
  local learner_pid=$!
  readarray -t old_fence < <(
    "$PYTHON_BIN" - "$SHARED_ROOT/control/syncer_metadata.sqlite3" "$learner_id" <<'PY'
import sqlite3
import sys
import time

database, learner_id = sys.argv[1:]
deadline = time.monotonic() + 120.0
while time.monotonic() < deadline:
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
        binding = connection.execute(
            "SELECT logical_launch_id, attempt_id, binding_generation "
            "FROM static_contributor_bindings WHERE learner_id=? AND status='active'",
            (learner_id,),
        ).fetchone()
        credit = connection.execute(
            "SELECT committed_credit FROM selection_state WHERE stable_contributor_key=?",
            (learner_id,),
        ).fetchone()
        connection.close()
        if binding is not None and credit is not None and int(credit[0]) >= 1:
            print(binding[0])
            print(binding[1])
            print(binding[2])
            raise SystemExit(0)
    except sqlite3.Error:
        pass
    time.sleep(0.05)
raise SystemExit("learner did not reach the registered replacement boundary")
PY
  )
  old_logical="${old_fence[0]}"
  old_attempt="${old_fence[1]}"
  old_generation="${old_fence[2]}"
  kill -TERM "$learner_pid"
  set +e
  wait "$learner_pid"
  local killed_status=$?
  set -e
  if [[ "$killed_status" -eq 0 ]]; then
    echo "learner completed before replacement injection" >&2
    exit 1
  fi

  "$PYTHON_BIN" -m fs_diloco.tools.request_static_replacement \
    --shared-root "$SHARED_ROOT" \
    --run-id "$RUN_ID" \
    --descriptor-sha256 "$FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256" \
    --learner-id "$learner_id" \
    --old-logical-launch-id "$old_logical" \
    --old-attempt-id "$old_attempt" \
    --old-binding-generation "$old_generation" \
    --new-logical-launch-id "$new_logical" \
    --new-attempt-id "$new_attempt" \
    --reason "registered learner process replacement" \
    >"$LOG_ROOT/${learner_id}_replacement_authorization.json"

  learner_command "$learner_index" "$new_logical" "$new_attempt" \
    >>"$LOG_ROOT/${learner_id}.log" 2>&1
  "$PYTHON_BIN" - "$LOG_ROOT/learner_replacement.json" "$learner_id" \
    "$old_attempt" "$old_generation" "$new_attempt" "$killed_status" <<'PY'
import json
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
payload = {
    "artifact_version": 1,
    "learner_id": sys.argv[2],
    "old_attempt_id": sys.argv[3],
    "old_binding_generation": int(sys.argv[4]),
    "new_attempt_id": sys.argv[5],
    "injected_exit_status": int(sys.argv[6]),
}
temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, target)
PY
}

run_syncer() {
  local log_path="$1"
  CUDA_VISIBLE_DEVICES="$SYNCER_CUDA_VISIBLE_DEVICES" \
  OMP_NUM_THREADS="$SYNCER_OMP_NUM_THREADS" \
    "$PYTHON_BIN" -m fs_diloco.syncer \
      --config "$RESOLVED_CONFIG" \
      --shared-root "$SHARED_ROOT" \
      >"$log_path" 2>&1
}

if [[ "$rank" -eq 0 ]]; then
  if [[ "$scenario" == "syncer_takeover" ]]; then
    set +e
    FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION=2 \
      run_syncer "$LOG_ROOT/syncer_primary.log"
    primary_status=$?
    set -e
    if [[ "$primary_status" -eq 0 ]]; then
      echo "primary syncer did not fail at the registered boundary" >&2
      exit 1
    fi
    run_syncer "$LOG_ROOT/syncer_successor.log"
    "$PYTHON_BIN" - "$LOG_ROOT/syncer_takeover.json" "$primary_status" <<'PY'
import json
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
payload = {"artifact_version": 1, "primary_exit_status": int(sys.argv[2])}
temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, target)
PY
  else
    run_syncer "$LOG_ROOT/syncer.log"
  fi
  exit 0
fi

learner_index=$((rank - 1))
if [[ "$scenario" == "learner_replacement" && "$learner_index" -eq 0 ]]; then
  launch_replaced_learner "$learner_index"
else
  launch_learner "$learner_index"
fi
