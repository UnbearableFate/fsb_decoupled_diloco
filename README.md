# FS DiLoCo Miyabi

Filesystem-backed Decoupled DiLoCo research prototype for Miyabi-G.

Milestone 1 uses independent single-GPU learners and one GPU-backed syncer process. Learners train GPT-style causal language models locally, publish full trainable parameter vectors as a single logical fragment (`fragment_id = 0`) through `safetensors` files, and commit JSON metadata files on the shared filesystem. The syncer ingests those metadata files into a syncer-local SQLite database, applies token/staleness-weighted merging with quorum and grace-window behavior on its local GPU, steps an explicit flat-vector outer optimizer, logs syncer-side training telemetry to W&B, and publishes new global weights through `control/latest.json`.

The implementation intentionally does not use `torch.distributed`, NCCL, RPC, Ray, DeepSpeed, FSDP, or PCCL for milestone 1 communication.

## Layout

- `fs_diloco/core/`: configuration and shared identifiers.
- `fs_diloco/modeling/`: models, datasets, parameter indexing, and outer optimizers.
- `fs_diloco/protocol/`: fragment scheduling/codecs, merge selection, and liveness rules.
- `fs_diloco/storage/`: atomic filesystem I/O, safetensors, SQLite, paths, and retention.
- `fs_diloco/observability/`: JSONL logging, CSV metrics, and W&B telemetry.
- `fs_diloco/runtime/`: learner and syncer process implementations.
- `fs_diloco/tools/`: run inspection and LM Evaluation Harness utilities.
- `fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py`: stable `python -m` compatibility entry points.
- `configs/`: GPT-2/WikiText-2 and tiny synthetic smoke configs.
- `scripts/miyabi/`: PBS launch and inspection scripts.
- `scripts/local/`: synthetic CPU smoke helpers.
- `tests/`: focused unit and integration tests.
- `docs/`: split bilingual user guide, design notes, Miyabi runbook, and experiment plan.

## Documentation

- [Bilingual documentation index](docs/user-guide/00-README.zh-en.md): Chinese and English docs split by overview, training, dataflow, storage/schema, modules, configuration, and operations.
- [Compatibility guide entry](docs/USER_GUIDE.zh-en.md): short redirect for the original single-file guide path.
- [Miyabi runbook](docs/miyabi_runbook.md): node policy and PBS launch commands.
- [Design notes](docs/design.md): protocol-level design summary.
- [Experiments](docs/experiments.md): suggested correctness, optimizer, and resilience matrices.

## Quick Commands

Static checks on a Miyabi login node:

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m compileall -q fs_diloco
```

Runtime checks must run inside PBS compute/debug nodes, not on login nodes.

Local synthetic smoke on a safe runtime node:

```bash
scripts/local/run_tiny_2proc_smoke.sh
```

Miyabi 1-node debug batch:

```bash
qsub scripts/miyabi/run_1node_debug.pbs
```

Inspect a completed run:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
```

## Runtime Contract

- Large tensors are stored as `safetensors`.
- Learner update metadata JSON is the commit marker.
- Heartbeat JSON files are liveness hints.
- `control/latest.json` is the only global pointer learners poll.
- SQLite stays local to the syncer and is backed up to `db_dumps/`.
- Learners overwrite the full model and reset the inner optimizer after adopting a newer global version.
- Outer optimizers are explicit flat-vector SGD, momentum/Nesterov, and AdamW-style implementations.

See `docs/miyabi_runbook.md` before launching on Miyabi.
