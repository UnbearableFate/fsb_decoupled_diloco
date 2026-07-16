# Design

This repository implements milestone 1 of the filesystem-based Decoupled DiLoCo plan. The design follows the Decoupled DiLoCo idea of independent learners communicating asynchronously with a central synchronizer using quorum, grace-window, and token-weighted merging. The project uses the Miyabi-G facts that each GPU node has one NVIDIA Hopper H100 with 96 GB GPU memory and that the system provides a Lustre/DDN EXAScaler shared filesystem.

References:

- Miyabi system page: https://www.cc.u-tokyo.ac.jp/en/supercomputer/miyabi/system.php
- Decoupled DiLoCo paper: https://arxiv.org/abs/2604.21428
- Google DeepMind blog: https://deepmind.google/blog/decoupled-diloco/
- PCCL Async DiLoCo docs: https://pccl.primeintellect.ai/DiLoCo%20-%20Distributed%20Low-Communication/AsyncDiloco
- PCCL example: https://github.com/PrimeIntellect-ai/pccl/blob/main/python/examples/nanogpt_diloco/async_diloco.py

## Milestone 1 Scope

The first implementation uses full-model parameter vectors as one logical fragment. Learners train locally, serialize their current full trainable parameter vector to `local_params` in `safetensors`, and then write metadata JSON. The syncer only treats the metadata JSON as the update commit marker; orphan tensor files are ignored.

No milestone 1 code depends on `torch.distributed`, NCCL collectives, RPC, Ray, DeepSpeed, FSDP, or PCCL. PBS scripts use MPI only as a process launcher across allocated nodes.

## Syncer State

SQLite is authoritative after ingestion. It tracks learners, updates, global versions, events, and DB dumps. The DB path defaults to syncer-local storage under `${TMPDIR:-/tmp}/fs_diloco/$RUN_ID`; consistent backups are copied to the shared filesystem with SQLite's backup API.

## Merge Semantics

For selected updates, the syncer loads each local parameter vector `p_i`, computes staleness `s_i = current_global_version - base_global_version_i`, and assigns:

```text
raw_weight_i = tokens_this_update_i / (1 + staleness_lambda * s_i)
```

After normalization, `p_bar = sum_i alpha_i * p_i` and the outer pseudo-gradient is:

```text
grad = theta_t - p_bar
```

The sign follows the Async DiLoCo pseudo-gradient pattern where an outer optimizer subtracts `outer_param - local_param`.

## Learner Adoption

Learners load the latest published global weight file, overwrite their full trainable model parameters, and rebuild the inner optimizer/scheduler. This reset is logged as `inner_optimizer_reset`.

## Fragment Extension

Milestone 1 treats the full model as `fragment_id = 0`. A future fragment implementation can add:

```text
fragments/fragment_index.json
updates/pending/learner_000/update_<uuid>_fragment_000.params.safetensors
mailbox/learner_000/global_v000123_fragment_000.safetensors
```

The first practical extension should use layer-based fragments, followed by balanced tensor fragments if metrics show a need.
