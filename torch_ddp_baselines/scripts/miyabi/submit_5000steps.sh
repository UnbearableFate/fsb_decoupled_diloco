#!/bin/bash

set -eEuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
PBS_SCRIPT="$PROJECT_ROOT/torch_ddp_baselines/scripts/miyabi/run_gpt2_wikitext2_5000steps.pbs"
STAMP="$(date +%Y%m%d_%H%M%S)"

case "$(hostname)" in
  miyabi-g*) ;;
  *)
    echo "Run this script on a Miyabi login host (miyabi-g*)." >&2
    exit 2
    ;;
esac
if [[ -n "${PBS_JOBID:-}" ]]; then
  echo "Run this submission command outside a PBS allocation." >&2
  exit 2
fi

bash -n "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs
bash -n "$PBS_SCRIPT"
group_directive="$(sed -n '/^#PBS -W group_list=/p' "$PBS_SCRIPT")"
if [[ "$group_directive" != '#PBS -W group_list=xg24i002' ]]; then
  echo "Invalid PBS group directive: $group_directive" >&2
  exit 2
fi
source_status="$(
  git -C "$PROJECT_ROOT" status --porcelain --untracked-files=all -- \
    fs_diloco configs do_experiments scripts/miyabi tests tools torch_ddp_baselines \
    pyproject.toml README.md docs plans/00-RESEARCH_PLAN.md website/app website/scripts
)"
if [[ -n "$source_status" ]]; then
  echo "Standalone baseline sources must be committed before submission:" >&2
  printf '%s\n' "$source_status" >&2
  exit 2
fi

ddp_run_id="${STAMP}_torch_ddp_gpt2_wikitext2_8n_5000"
periodic_run_id="${STAMP}_torch_periodic_average_gpt2_wikitext2_8n_5000"
ddp_job="$(qsub -N torch_ddp_5000 -v "MODE=ddp,RUN_ID=$ddp_run_id" "$PBS_SCRIPT")"
printf 'DDP_JOB=%s DDP_RUN_ID=%s\n' "$ddp_job" "$ddp_run_id"
periodic_job="$(qsub -N torch_pavg_5000 -v "MODE=periodic_average,RUN_ID=$periodic_run_id" "$PBS_SCRIPT")"

printf 'PERIODIC_JOB=%s PERIODIC_RUN_ID=%s\n' "$periodic_job" "$periodic_run_id"
