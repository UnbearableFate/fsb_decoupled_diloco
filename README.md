# FS DiLoCo Miyabi

Filesystem-backed Decoupled DiLoCo research prototype for Miyabi-G.

Milestone 1 uses independent learners (one CUDA device each when available, with a CPU fallback used by local synthetic runs) and one configurable CPU/GPU syncer process. Learners train GPT-style causal language models locally and publish immutable `safetensors` payloads behind atomically replaced proposal pointers (one per learner in full mode, one per learner/fragment pair in fragment mode). The syncer records authoritative state in a persistent SQLite database in the shared run directory, applies token/staleness-weighted merging with quorum and grace-window behavior on its configured device and dtype, steps an explicit flat-vector outer optimizer, optionally logs syncer-side training telemetry to W&B, and publishes global weights in the configured publication dtype. Full mode optionally enables independently scheduled syncer high availability: a one-time initializer freezes the run descriptor, SQLite issues monotonic leader epochs, every business mutation is fenced in its transaction, and learners adopt epoch-scoped canonical controls instead of trusting the fixed `control/latest.json` cache. That HA path supports either the original static learner set or dynamic membership with UUID process incarnations, a fixed virtual stream pool, fenced registration/admission, hysteretic scale-out, and generation-scoped terminal drain acknowledgements.

The implementation intentionally does not use `torch.distributed`, NCCL, RPC, Ray, DeepSpeed, FSDP, or PCCL for milestone 1 communication.

Plan 02 Phases 1 and 2 have passed their technical Miyabi gates. Phase 1 verified independent-job Syncer HA. Phase 2 verified full-mode dynamic membership through global version 120 with eight bootstrap learners, one permanent learner loss and scheduler-provided replacement, duplicate-job rejection, stream reuse with an incremented stream epoch, bounded active state, and dynamic drain closure. The completed Checker and matched static/dynamic performance gates returned `PASS`; the latter measured no positive dynamic control-path overhead under the frozen 5% formula. The formal Phase 2 workload used 51 local steps per cycle and 120 global versions, exceeding the 50-local-step × 10-global-step documentation baseline. These are recovery, membership, and control-plane results, not training-quality claims; exact jobs and retained artifacts are listed in [Operations](docs/07-operations.md#4-miyabi-pbs-%E6%89%B9%E4%BD%9C%E4%B8%9A).

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
# First run bash -n on every PBS script and verify literal group IDs.
# Replace HH:MM:SS with the shortest evidence-based estimate that still
# includes sufficient startup, runtime, and teardown margin.
qsub -l walltime=HH:MM:SS scripts/miyabi/run_1node_debug.pbs
```

Inspect a completed run:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
```

## Runtime Contract

- Large tensors are stored as `safetensors`.
- `updates/latest/learner_XXX.json` is each full-mode learner's bounded proposal surface; it points to an immutable payload.
- Fragment mode uses one fixed `updates/latest/learner_XXX_fNNN.json` pointer per learner/fragment pair.
- Heartbeat JSON files are liveness hints.
- With HA disabled, `control/latest.json` is the global pointer learners poll. With full-mode HA enabled, learners select the highest valid filesystem epoch and verify its canonical head/pointer checksum without opening SQLite; fixed `latest.json`/`stop.json`/`summary.json` files are repairable convenience caches only.
- `control/syncer_metadata.sqlite3` is the authoritative commit record and is opened directly from the shared filesystem with rollback journaling and `synchronous=FULL`.
- Recovery is DB-first: `latest.json` is a rebuildable learner-facing cache, not a recovery authority.
- HA checkpoints and controls use epoch-unique paths. A stale leader may finish writes only in its old epoch namespace; after a successor epoch commits, its token cannot mutate business state.
- Dynamic membership requires full mode plus Syncer HA. Each process creates a fresh `learner_li_<uuid4>` identity, receives an admitted placement/stream generation, and must present that membership fence again at final merge commit.
- Dynamic data sharding uses the immutable `membership.stream_pool_size`, not the current active-process count. A replacement may reuse a stream only with a strictly newer `stream_epoch`.
- Scale-out is an auditable launch outbox: distinct capacity observations drive hysteresis, PBS jobs remain reserved while queued/running, and one logical launch request can admit at most one process.
- Dynamic close freezes admission and the terminal merge bound, publishes a generation-scoped drain directive, and waits for healthy acknowledgements or fenced timeout revocation before declaring input closed.
- The bounded tensor surface retains current checkpoints, fixed proposal pointers, payloads referenced by active DB rows, and short-lived `gc_pending` payloads awaiting deletion; terminal metadata history is archived in append-only JSONL files.
- Full-mode adoption is strategy-dependent: `replace` overwrites the model and resets AdamW moments, while rebase/prediction reconciliation preserves unpublished local progress and optimizer state. Fragment adoption only replaces changed fragments and resets the optimizer only when configured.
- Outer optimizers are explicit flat-vector SGD, momentum/Nesterov, and AdamW-style implementations.

See [docs/07-operations.md](docs/07-operations.md) before launching on Miyabi.
