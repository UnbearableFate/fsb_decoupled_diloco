#!/bin/bash

set -eEuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
PBS_SCRIPT="$PROJECT_ROOT/torch_ddp_baselines/scripts/miyabi/run_gpt2_wikitext2_500steps.pbs"
STAMP="$(date +%Y%m%d_%H%M%S)"

ddp_run_id="${STAMP}_torch_ddp_gpt2_wikitext2_8n_500"
periodic_run_id="${STAMP}_torch_periodic_average_gpt2_wikitext2_8n_500"
ddp_job="$(qsub -N torch_ddp_500 -v "MODE=ddp,RUN_ID=$ddp_run_id" "$PBS_SCRIPT")"
periodic_job="$(qsub -N torch_pavg_500 -v "MODE=periodic_average,RUN_ID=$periodic_run_id" "$PBS_SCRIPT")"

printf 'DDP_JOB=%s DDP_RUN_ID=%s\n' "$ddp_job" "$ddp_run_id"
printf 'PERIODIC_JOB=%s PERIODIC_RUN_ID=%s\n' "$periodic_job" "$periodic_run_id"
