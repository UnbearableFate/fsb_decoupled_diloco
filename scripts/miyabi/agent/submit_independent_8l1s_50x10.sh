#!/bin/bash

# Submit one short launcher control job. The launcher initializes a fresh run
# on a compute node and submits one syncer plus eight scalar bootstrap learners.

set -eEuo pipefail
trap 'echo "[ERROR] actor submission failed at line $LINENO" >&2' ERR

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

case "$(hostname)" in
  miyabi-g*) ;;
  *)
    echo "Run this script on a Miyabi login host (miyabi-g*)." >&2
    exit 2
    ;;
esac

if [[ -n "${PBS_JOBID:-}" ]]; then
  echo "Run this script outside a PBS allocation; it is a login-node control-plane command." >&2
  exit 2
fi

command -v git >/dev/null
command -v qsub >/dev/null
command -v rg >/dev/null

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
CONFIG="$PROJECT_ROOT/configs/full_protocol.yaml"
LAUNCHER_SCRIPT="$PROJECT_ROOT/scripts/miyabi/agent/run_independent_launcher.pbs"

LAUNCHER_QUEUE="debug-g"
ACTOR_QUEUE="debug-g"
LAUNCHER_WALLTIME="00:10:00"
SYNCER_WALLTIME="00:10:00"
LEARNER_WALLTIME="00:10:00"

test -x "$PYTHON_BIN"
test -f "$CONFIG"
test -f "$LAUNCHER_SCRIPT"

# Formal source identity requires these scopes to be clean. Untracked review
# outputs below reports/ do not affect the run identity.
SOURCE_STATUS="$(
  git -C "$PROJECT_ROOT" status --short --untracked-files=all -- \
    fs_diloco configs scripts/miyabi tests pyproject.toml README.md docs
)"
if [[ -n "$SOURCE_STATUS" ]]; then
  echo "Formal source scopes are not clean:" >&2
  echo "$SOURCE_STATUS" >&2
  exit 2
fi

# Enforce the repository's mandatory PBS pre-submit checks.
bash -n "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs
for pbs_script in "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs; do
  group_directive="$(rg -N '^#PBS -W group_list=' "$pbs_script")"
  if [[ "$group_directive" != '#PBS -W group_list=xg24i002' ]]; then
    echo "Invalid PBS group directive in $pbs_script: $group_directive" >&2
    exit 2
  fi
done

DAY="$(date +%Y%m%d)"
CLOCK="$(date +%H%M%S)"
STAMP="${DAY}_${CLOCK}"
RUN_ID="independent_8l1s_50x10_${STAMP}"
SHARED_ROOT="$PROJECT_ROOT/runs/full_protocol/$RUN_ID"
LOG_ROOT="$PROJECT_ROOT/logs/qsub_$RUN_ID"
LAUNCHER_LOG="$PROJECT_ROOT/logs/qsub_independent_launcher_${STAMP}.log"
SOURCE_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"

for value in "$PROJECT_ROOT" "$PYTHON_BIN" "$CONFIG" "$RUN_ID" "$SHARED_ROOT" "$LOG_ROOT"; do
  if [[ "$value" == *","* || "$value" == *"="* || "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "PBS variable value contains an unsupported character: $value" >&2
    exit 2
  fi
done

if [[ -e "$SHARED_ROOT" || -e "$LOG_ROOT" ]]; then
  echo "Generated run or log root already exists; rerun to obtain a new timestamp." >&2
  exit 2
fi

mkdir -p "$PROJECT_ROOT/logs"

PBS_VARIABLES="PROJECT_ROOT=$PROJECT_ROOT,PYTHON_BIN=$PYTHON_BIN,CONFIG=$CONFIG,ACTOR_QUEUE=$ACTOR_QUEUE,RUN_ID=$RUN_ID,SHARED_ROOT=$SHARED_ROOT,LOG_ROOT=$LOG_ROOT,SYNCER_WALLTIME=$SYNCER_WALLTIME,LEARNER_WALLTIME=$LEARNER_WALLTIME"

LAUNCHER_JOB_ID="$(
  qsub \
    -q "$LAUNCHER_QUEUE" \
    -l "walltime=$LAUNCHER_WALLTIME" \
    -o "$LAUNCHER_LOG" \
    -v "$PBS_VARIABLES" \
    "$LAUNCHER_SCRIPT"
)"

if [[ -z "$LAUNCHER_JOB_ID" ]]; then
  echo "qsub returned no launcher job ID." >&2
  exit 1
fi

printf '%s\n' \
  "launcher_job_id=$LAUNCHER_JOB_ID" \
  "source_commit=$SOURCE_COMMIT" \
  "run_id=$RUN_ID" \
  "actor_queue=$ACTOR_QUEUE" \
  "shared_root=$SHARED_ROOT" \
  "log_root=$LOG_ROOT" \
  "submission_receipt=$LOG_ROOT/submission_receipt.json" \
  "launcher_log=$LAUNCHER_LOG"
