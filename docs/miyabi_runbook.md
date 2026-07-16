# Miyabi Runbook

Use the `miyabi-development` workflow for this repository.

## Node Policy

Miyabi login nodes are control-plane only. Do not run training, model loading, CUDA checks, torch imports, transformers imports, datasets preprocessing, pytest runtime tests, `torchrun`, or `mpirun` on login nodes.

Allowed login-node checks:

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m py_compile fs_diloco/*.py
```

Runtime validation must run inside PBS compute/debug nodes.

## 1-Node Runtime Smoke

Submit:

```bash
qsub scripts/miyabi/run_1node_debug.pbs
```

The script starts a GPU-backed syncer and one GPU learner on the same compute node using `configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml`. Acceptance evidence is:

- `control/latest.json` reports version `1` or higher;
- `weights/global_v000001.safetensors` exists;
- `db_dumps/metadata_*_v000001.db` exists;
- learner log shows finite `inner_step_summary` loss values;
- syncer log shows `outer_step_applied` and `global_published`.

## 2-Node Runtime Smoke

Submit:

```bash
qsub scripts/miyabi/run_2node_debug.pbs
```

Rank 0 runs the syncer with `CUDA_VISIBLE_DEVICES=${SYNCER_CUDA_VISIBLE_DEVICES:-0}`; rank 1 runs `learner_000` with `CUDA_VISIBLE_DEVICES=${LEARNER_CUDA_VISIBLE_DEVICES:-0}`. MPI is used only as a process launcher and environment is passed with `/usr/bin/env`, not `mpirun -x`.

## 9-Node Acceptance

Submit:

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

The PBS scripts use `#PBS -W group_list=xg24i002`, matching the project reported by `qstat --limit` on this checkout.

For a short acceptance run that still uses real GPT-2/WikiText-2 and eight learners:

```bash
qsub -v CONFIG=$PWD/configs/fs_diloco_gpt2_wikitext2_8l_acceptance.yaml scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

Rank 0 is the syncer. Ranks 1-8 are `learner_000` through `learner_007`. The default config uses quorum 4 and max quorum 8. For acceptance, set `sync.stop_after_outer_steps` to at least `3` and verify at least three committed global versions, at least four selected learners per outer step, and no duplicate applied update IDs.

## 1-Node LM Evaluation

Submit a smoke evaluation job for the fixed 5000-step checkpoint:

```bash
qsub -v CHECKPOINT=$PWD/runs/fs_diloco/20260709_142811_fs_diloco_gpt2_wikitext2_8l_5000steps/weights/global_v000047.safetensors,TASK_SUITE=smoke,EVAL_LIMIT=20 scripts/miyabi/run_1node_lm_eval.pbs
```

The job runs only on a compute node. It exports the FS DiLoCo global weights to a HuggingFace checkpoint directory, runs `lm_eval` with the HuggingFace backend, and writes summary metrics to `runs/lm_eval/<EVAL_ID>/metrics.csv`.

Use `TASK_SUITE=full` for `wikitext,lambada_openai,hellaswag,piqa,arc_easy,arc_challenge,winogrande,openbookqa`. You can also override `TASKS`, `CHECKPOINT`, `RUN_ROOT`, `BATCH_SIZE`, and `MODEL_DTYPE`.

## Inspection

```bash
scripts/miyabi/inspect_run.sh runs/fs_diloco/<RUN_ID>
```

This reads `latest.json`, `stop.json`, metrics CSV files, and the newest DB dump.
