#!/bin/bash

# Submit one current plan04 scenario from a Miyabi login host.

set -eEuo pipefail
trap 'echo "[ERROR] plan04 submission failed at line $LINENO" >&2' ERR

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd -P)"
SCENARIO="${1:-}"

case "$SCENARIO" in
  baseline)
    EXPERIMENT_ID=0
    CONFIG="$SCRIPT_DIR/baseline.yaml"
    ;;
  normal)
    EXPERIMENT_ID=1
    CONFIG="$SCRIPT_DIR/experiment.yaml"
    ;;
  stagger_4_4)
    EXPERIMENT_ID=2
    CONFIG="$SCRIPT_DIR/timed_experiment.yaml"
    ;;
  stagger_3_3_2)
    EXPERIMENT_ID=3
    CONFIG="$SCRIPT_DIR/timed_experiment.yaml"
    ;;
  learner_failure_simultaneous)
    EXPERIMENT_ID=4
    CONFIG="$SCRIPT_DIR/fault_experiment.yaml"
    ;;
  learner_failure_staggered)
    EXPERIMENT_ID=5
    CONFIG="$SCRIPT_DIR/fault_experiment.yaml"
    ;;
  syncer_failure)
    EXPERIMENT_ID=6
    CONFIG="$SCRIPT_DIR/fault_experiment.yaml"
    ;;
  dual_syncer)
    EXPERIMENT_ID=7
    CONFIG="$SCRIPT_DIR/fault_experiment.yaml"
    ;;
  *)
    echo "Usage: bash do_experiments/full_protocol/experiment04/submit.sh SCENARIO" >&2
    echo "SCENARIO: baseline, normal, stagger_4_4, stagger_3_3_2," >&2
    echo "          learner_failure_simultaneous, learner_failure_staggered," >&2
    echo "          syncer_failure, dual_syncer" >&2
    exit 2
    ;;
esac

case "$(hostname)" in
  miyabi-g*) ;;
  *)
    echo "Run this script on a Miyabi login host (miyabi-g*)." >&2
    exit 2
    ;;
esac
if [[ -n "${PBS_JOBID:-}" ]]; then
  echo "Run this command outside a PBS allocation." >&2
  exit 2
fi

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
PBS_SCRIPT="$SCRIPT_DIR/run_experiment.pbs"
QUEUE=regular-g
WALLTIME=00:30:00
TIMEOUT_SECONDS=1200

test -x "$PYTHON_BIN"
test -f "$CONFIG"
test -f "$PBS_SCRIPT"
command -v git >/dev/null
command -v qsub >/dev/null
command -v rg >/dev/null

SOURCE_STATUS="$(
  git -C "$PROJECT_ROOT" status --short --untracked-files=all -- \
    fs_diloco configs do_experiments scripts/miyabi tests tools torch_ddp_baselines \
    pyproject.toml README.md docs plans/00-RESEARCH_PLAN.md website/app website/scripts
)"
if [[ -n "$SOURCE_STATUS" ]]; then
  echo "Formal source scopes are not clean:" >&2
  echo "$SOURCE_STATUS" >&2
  exit 2
fi

bash -n "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs
bash -n "$PBS_SCRIPT"
for pbs_script in "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs "$PBS_SCRIPT"; do
  group_directive="$(rg -N '^#PBS -W group_list=' "$pbs_script")"
  if [[ "$group_directive" != '#PBS -W group_list=xg24i002' ]]; then
    echo "Invalid PBS group directive in $pbs_script: $group_directive" >&2
    exit 2
  fi
done

STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_ID="plan04_e${EXPERIMENT_ID}_${SCENARIO}_${STAMP}"
RUN_ROOT="$PROJECT_ROOT/runs/full_protocol/$RUN_ID"
LOG_ROOT="$PROJECT_ROOT/logs/plan04/$RUN_ID"
SUPERVISOR_LOG="$PROJECT_ROOT/logs/plan04/${RUN_ID}_supervisor.log"
EVIDENCE_OUTPUT="$PROJECT_ROOT/reports/DOING/plan04/artifacts/${STAMP}_e${EXPERIMENT_ID}_${SCENARIO}.json"
SOURCE_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"

for value in \
  "$PROJECT_ROOT" "$PYTHON_BIN" "$CONFIG" "$SCENARIO" "$RUN_ID" \
  "$RUN_ROOT" "$LOG_ROOT" "$EVIDENCE_OUTPUT"; do
  if [[ "$value" == *","* || "$value" == *"="* || "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "PBS variable value contains an unsupported character: $value" >&2
    exit 2
  fi
done
if [[ -e "$RUN_ROOT" || -e "$LOG_ROOT" || -e "$EVIDENCE_OUTPUT" ]]; then
  echo "Generated run, log, or evidence path already exists." >&2
  exit 2
fi
mkdir -p "$PROJECT_ROOT/logs/plan04" "$PROJECT_ROOT/reports/DOING/plan04/artifacts"

PBS_VARIABLES="PROJECT_ROOT=$PROJECT_ROOT,PYTHON_BIN=$PYTHON_BIN,CONFIG=$CONFIG,SCENARIO=$SCENARIO,RUN_ID=$RUN_ID,RUN_ROOT=$RUN_ROOT,LOG_ROOT=$LOG_ROOT,EVIDENCE_OUTPUT=$EVIDENCE_OUTPUT,TIMEOUT_SECONDS=$TIMEOUT_SECONDS"
SUPERVISOR_JOB_ID="$(
  qsub \
    -q "$QUEUE" \
    -l "walltime=$WALLTIME" \
    -o "$SUPERVISOR_LOG" \
    -v "$PBS_VARIABLES" \
    "$PBS_SCRIPT"
)"
if [[ -z "$SUPERVISOR_JOB_ID" ]]; then
  echo "qsub returned no supervisor job ID." >&2
  exit 1
fi

printf '%s\n' \
  "supervisor_job_id=$SUPERVISOR_JOB_ID" \
  "source_commit=$SOURCE_COMMIT" \
  "experiment_id=$EXPERIMENT_ID" \
  "scenario=$SCENARIO" \
  "run_id=$RUN_ID" \
  "run_root=$RUN_ROOT" \
  "log_root=$LOG_ROOT" \
  "evidence_output=$EVIDENCE_OUTPUT" \
  "supervisor_log=$SUPERVISOR_LOG"
