# FS DiLoCo Miyabi

Filesystem-backed Decoupled DiLoCo research prototype for Miyabi-G.

Milestone 1 uses independent single-GPU learners and one configurable CPU/GPU syncer process. Learners train GPT-style causal language models locally and publish immutable `safetensors` payloads behind one atomically replaced proposal pointer per learner. The syncer records authoritative state in a persistent SQLite database in the shared run directory, applies token/staleness-weighted merging with quorum and grace-window behavior on its configured device and dtype, steps an explicit flat-vector outer optimizer, logs syncer-side training telemetry to W&B, and publishes global weights in the configured publication dtype through `control/latest.json`.

The implementation intentionally does not use `torch.distributed`, NCCL, RPC, Ray, DeepSpeed, FSDP, or PCCL for milestone 1 communication.

## Layout

- `fs_diloco/core/`: configuration and shared identifiers.
- `fs_diloco/modeling/`: models, datasets, parameter indexing, and outer optimizers.
- `fs_diloco/protocol/`: fragment scheduling/codecs, merge selection, and liveness rules.
- `fs_diloco/storage/`: atomic filesystem I/O, safetensors, persistent SQLite, paths, archival, and reference-driven garbage collection.
- `fs_diloco/observability/`: JSONL logging, CSV metrics, and W&B telemetry.
- `fs_diloco/runtime/`: learner and syncer process implementations.
- `fs_diloco/tools/`: run inspection and LM Evaluation Harness utilities.
- `fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py`: stable `python -m` compatibility entry points.
- `configs/`: GPT-2/WikiText-2 and tiny synthetic smoke configs.
- `scripts/miyabi/`: PBS launch and inspection scripts.
- `scripts/local/`: synthetic CPU smoke helpers.
- `tests/`: focused unit and integration tests.
- `docs/`: wiki-style system documentation (architecture, runtime flow, data flow, configuration, operations, and per-module function reference).
- `reports/`: run analysis results, implementation records, and retained validation evidence.

## Documentation

Start at the [documentation index](docs/README.md). Highlights:

- [Overview](docs/01-overview.md): what the system is, design goals, full vs fragment modes, terminology.
- [Architecture](docs/02-architecture.md): process roles, runtime contract, merge protocol, liveness, fault tolerance.
- [Runtime flow](docs/03-runtime-flow.md): initialization, learner/syncer main loops, shutdown and resume.
- [Data flow](docs/04-data-flow.md): shared-directory layout, file formats, update state machine, SQLite schema.
- [Code structure](docs/05-code-structure.md) and [module reference](docs/modules/): per-function documentation for every package.
- [Configuration reference](docs/06-configuration.md): every YAML section and field.
- [Operations](docs/07-operations.md): launch commands, PBS scripts, checkpoint evaluation, troubleshooting.
- [Run analysis report](reports/run_analysis.md): analysis commands and recorded experiment results.

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
- `updates/latest/learner_XXX.json` is each full-mode learner's bounded proposal surface; it points to an immutable payload.
- Heartbeat JSON files are liveness hints.
- `control/latest.json` is the only global pointer learners poll.
- `control/syncer_metadata.sqlite3` is the authoritative commit record and is opened directly from the shared filesystem with rollback journaling and `synchronous=FULL`.
- Recovery is DB-first: `latest.json` is a rebuildable learner-facing cache, not a recovery authority.
- The active runtime retains only current checkpoints, fixed proposal pointers, and proposals referenced by active DB rows; terminal history is archived in append-only JSONL files.
- Learners overwrite the full model and reset the inner optimizer after adopting a newer global version.
- Outer optimizers are explicit flat-vector SGD, momentum/Nesterov, and AdamW-style implementations.

See [docs/07-operations.md](docs/07-operations.md) before launching on Miyabi.
