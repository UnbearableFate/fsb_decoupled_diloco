#!/bin/bash
set -eEuo pipefail
trap 'echo "[ERROR] Failed at line $LINENO" >&2' ERR

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PRIMARY_WORKTREE_ROOT="${PRIMARY_WORKTREE_ROOT:-/work/xg24i002/x10041/fsb_decoupled_diloco}"
TRAIN_SCRIPT="${1:-$PROJECT_ROOT/scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs}"
EVAL_SCRIPT="${EVAL_SCRIPT:-$PROJECT_ROOT/scripts/miyabi/run_1node_validation_eval.pbs}"
QSUB_BIN="${QSUB_BIN:-qsub}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_fs_diloco_train_eval}"
SHARED_ROOT="${SHARED_ROOT:-$PRIMARY_WORKTREE_ROOT/runs/fs_diloco/$RUN_ID}"
CONFIG="${CONFIG:-}"
CACHE_ROOT="${CACHE_ROOT:-$PROJECT_ROOT/.cache/fs_diloco}"
WANDB_MODE="${WANDB_MODE:-offline}"

if [[ ! -f "$TRAIN_SCRIPT" || ! -f "$EVAL_SCRIPT" ]]; then
  echo "Train/eval PBS script is missing: $TRAIN_SCRIPT / $EVAL_SCRIPT" >&2
  exit 2
fi
if [[ "$RUN_ID" == *","* || "$SHARED_ROOT" == *","* || "$PROJECT_ROOT" == *","* ]]; then
  echo "PBS -v values cannot contain commas" >&2
  exit 2
fi

TRAIN_ENV="PROJECT_ROOT=$PROJECT_ROOT,PRIMARY_WORKTREE_ROOT=$PRIMARY_WORKTREE_ROOT,PYTHON_BIN=$PYTHON_BIN,RUN_ID=$RUN_ID,SHARED_ROOT=$SHARED_ROOT,CACHE_ROOT=$CACHE_ROOT,WANDB_MODE=$WANDB_MODE"
if [[ -n "$CONFIG" ]]; then
  TRAIN_ENV+=",CONFIG=$CONFIG"
fi
for name in \
  TRAINING_SEED \
  SYNC_SCAN_INTERVAL_SECONDS \
  INGEST_DURING_PUBLISH \
  SYNCER_PUBLISH_DTYPE \
  STALENESS_LAMBDA \
  MAX_STALENESS_VERSIONS \
  GLOBAL_ADOPTION_STRATEGY \
  CAPTURE_TERMINAL_PREDECESSOR_FOR_EVAL \
  COMPLETION_MODE; do
  if [[ -n "${!name:-}" ]]; then
    TRAIN_ENV+=",$name=${!name}"
  fi
done

train_job_id="$($QSUB_BIN -v "$TRAIN_ENV" "$TRAIN_SCRIPT")"
train_job_id="${train_job_id%%$'\n'*}"
if [[ -z "$train_job_id" ]]; then
  echo "Training qsub returned an empty job id" >&2
  exit 2
fi

EVAL_ENV="PROJECT_ROOT=$PROJECT_ROOT,PYTHON_BIN=$PYTHON_BIN,RUN_ROOT=$SHARED_ROOT,CACHE_ROOT=$CACHE_ROOT"
eval_job_id="$($QSUB_BIN -W "depend=afterok:$train_job_id" -v "$EVAL_ENV" "$EVAL_SCRIPT")"
eval_job_id="${eval_job_id%%$'\n'*}"
if [[ -z "$eval_job_id" ]]; then
  echo "Validation qsub returned an empty job id" >&2
  exit 2
fi

echo "RUN_ID=$RUN_ID"
echo "SHARED_ROOT=$SHARED_ROOT"
echo "TRAIN_JOB_ID=$train_job_id"
echo "VALIDATION_JOB_ID=$eval_job_id"
echo "VALIDATION_DEPENDENCY=afterok:$train_job_id"
