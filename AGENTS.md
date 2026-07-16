# AGENTS.md

Use the `miyabi-development` Codex skill for all Miyabi-related work.

Do not run training, model loading, CUDA checks, torch imports, transformers imports, datasets preprocessing, `torchrun`, `mpirun`, or pytest runtime tests on Miyabi login nodes. Login nodes are control-plane only: inspect files, edit, run `bash -n`, review configs, submit jobs, inspect logs.

For runtime validation, use PBS interactive/debug or batch compute nodes. Start with 1-node checks, then 2-node checks, then 9-node batch.

This repository implements a filesystem-based Decoupled DiLoCo prototype:

Before submitting PBS scripts, run `bash -n scripts/miyabi/*.pbs` on a safe node. Fill real `#PBS -W group_list=<group_id>` values before submission.
