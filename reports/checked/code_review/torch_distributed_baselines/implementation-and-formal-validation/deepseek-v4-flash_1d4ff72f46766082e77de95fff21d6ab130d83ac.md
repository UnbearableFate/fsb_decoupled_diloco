# Independent senior code review — torch distributed baselines (implementation-and-formal-validation)

## Identity and scope

- Reviewer model (actual identifier): `opencode-go/deepseek-v4-flash`.
- Review target commit: `1d4ff72f46766082e77de95fff21d6ab130d83ac` (`remediate torch baseline review findings`).
- Comparison base (plan branch point / previous phase-final): `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Plan-id: `torch_distributed_baselines`. Phase-id: `implementation-and-formal-validation`.
- Review scope: the complete cumulative diff from `c1c6115` through `1d4ff72` — 21 files, +3000/−2 — covering
  `fs_diloco/baselines/` (`__init__.py`, `train.py`, `protocol.py`, `artifacts.py`, `health.py`),
  `fs_diloco/core/config.py` (`TorchBaselineSection` + `resolve_config` validation),
  `configs/torch_baseline_gpt2_wikitext2_8n_5000steps.yaml`, `configs/torch_baseline_gpt2_wikitext2_1rank_debug.yaml`,
  `configs/torch_baseline_tiny_2rank.yaml`, `scripts/miyabi/check_torch_baseline_health.py`,
  `scripts/miyabi/run_8node_torch_ddp_gpt2_wikitext2_5000steps.pbs`,
  `scripts/miyabi/run_8node_torch_periodic_average_gpt2_wikitext2_5000steps.pbs`,
  `tests/test_torch_baseline_protocol.py`, `tests/test_torch_baseline_artifacts_and_data.py`,
  `tests/test_torch_baseline_health.py`, `tests/test_config.py`, `pyproject.toml`,
  `plans/DOING/torch_distributed_baselines.md`,
  `reports/DOING/torch_distributed_baselines/progress.md`, and `reports/DOING/torch_distributed_baselines/failures.md`.
- Reviewer role: reviewer-only. No implementation, tests, configs, plans, existing reports, git state,
  scheduler jobs, or external services were modified. The only file created is this report.
- Independence note: the two earlier reports for this phase
  (`gpt-5-codex_e30d49f...` against the pre-remediation target and
  `glm-5.2_1d4ff72...` against this same target) are present in the review-target tree. This report does
  not rely on them: the cumulative diff, the remediation commit `1d4ff72`, the launchers, and all
  surrounding dependency modules were re-inspected independently, and the finding dispositions were
  re-verified against the code rather than inherited.

### Commands executed and files inspected (read-only)

- `git rev-parse HEAD`, `git log --oneline c1c6115..1d4ff72` (two commits: `e30d49f` implement, `1d4ff72` remediate),
  `git diff --stat c1c6115 1d4ff72`, `git diff e30d49f 1d4ff72` (remediation diff), per-file `git diff`.
- `bash -n scripts/miyabi/run_8node_torch_ddp_gpt2_wikitext2_5000steps.pbs` and
  `scripts/miyabi/run_8node_torch_periodic_average_gpt2_wikitext2_5000steps.pbs` — both OK.
- `rg -n '^#PBS -W group_list=' scripts/miyabi/*.pbs` — all 17 scripts, including both new launchers, use the
  literal `xg24i002`; no placeholder `#PBS -W group_list=` remains.
- `python -m py_compile` on all `fs_diloco/baselines/*.py` and `scripts/miyabi/check_torch_baseline_health.py` — OK.
- `ruff check` on `fs_diloco/baselines`, `fs_diloco/core/config.py`, the three baseline test modules,
  `tests/test_config.py`, and the checker wrapper — `All checks passed!`.
- `git diff c1c6115 1d4ff72 --check` — clean.
- `pytest -q tests/test_torch_baseline_protocol.py tests/test_torch_baseline_artifacts_and_data.py
  tests/test_torch_baseline_health.py tests/test_config.py` — `136 passed in 13.53s` (includes the two real
  two-process Gloo `torch.multiprocessing.spawn` collectives).
- `pytest -q` (complete repository suite) — `383 passed in 23.33s`, matching the recorded
  `reports/DOING/torch_distributed_baselines/progress.md` remediation-regression result.
- Config resolution via the repository venv for all three baseline YAMLs — all resolve with the expected
  `num_learners` / `max_local_steps` / `inner_steps` / backend / `require_distinct_hosts`.
- Surrounding dependency sources inspected: `fs_diloco/modeling/hf_data.py` (`build_batch_iterator`,
  `wikitext_batches`, `_batched_blocks`, `synthetic_batches`), `fs_diloco/modeling/hf_model.py`
  (`load_causal_lm_and_tokenizer`, `model_dtype`), `fs_diloco/runtime/learner.py` (`train_one_step`,
  `build_inner_optimizer_and_scheduler`, `inner_lr_multiplier`, `maybe_autocast`),
  `fs_diloco/storage/atomic_io.py`, `fs_diloco/observability/logging_utils.py`,
  `scripts/miyabi/capture_source_identity.py`, and a pre-existing 9-node PBS launcher for convention comparison.

## Prior-review finding dispositions re-verified at the review target

Facts about the remediation diff, verified directly against the current code:

- Prior `High` (health checker could PASS a run that is not the 5000-step / 100-interval formal experiment):
  **fixed**. `fs_diloco/baselines/health.py:23-24` defines `FORMAL_MAX_STEPS=5000` and
  `FORMAL_AVERAGE_INTERVAL=100`; `health.py:141-153` now appends a hard failure unless the manifest declares
  exactly `max_steps=5000` and `average_interval=100`. Regression tests
  (`tests/test_torch_baseline_health.py:227-251`) cover a declared-200-step completion and an interval-200
  manifest. I re-ran both tests (`test_health_checker_rejects_short_declared_formal_run`,
  `test_health_checker_requires_periodic_step_100_boundary`) and confirmed they FAIL the checker.
- Prior `Medium` (PBS source selection from a foreign venv without pinning `PYTHONPATH`): **fixed**. Both
  launchers set `PYTHONPATH="$PROJECT_ROOT"` (`scripts/miyabi/run_8node_torch_*.pbs:22`) and pass it through
  `MPI_ENV_ARGS` (`:89`).
- Prior `Medium` (untested 5000-step final-checkpoint publication): **fixed**.
  `tests/test_torch_baseline_artifacts_and_data.py:148-187` covers staging, atomic rename, and staging
  cleanup after an injected tokenizer failure.
- Prior `Low` (checker numeric CLI validation): **fixed** (`fs_diloco/baselines/health.py:309-316`).

## Findings at the review target

### Critical

None.

### High

None. The prior High acceptance-correctness defect is closed by the remediation commit and is covered by
regression tests that I re-ran successfully.

### Medium

1. **Single-rank failure is not propagated to peers quickly; peers block at the next collective until the
   300 s process-group timeout.**
   Facts: `train.py:330-447` performs a distributed collective (DDP reducer via `protocol.py:37-96`, or the
   periodic BF16 `all_reduce` at `protocol.py:130-173`) every optimizer step. The exception path at
   `train.py:498-540` runs only on the failing rank; peers keep waiting in the loop until `dist` raises a
   timeout (300 s, `train.py:225`). The launchers set `--max-restarts=0`
   (`scripts/miyabi/run_8node_torch_*.pbs:147`) so torchrun gives no automatic failover, and the
   `mpirun ... | tee` pipeline (`:154`) has no watchdog.
   Inference: a single node/rank crash can stall the remaining seven ranks for up to five minutes before
   they fail; the run then dies via mpirun's non-zero exit and the launcher's `set -e` trap. It is still
   detectable — per-rank heartbeats (`train.py:412-421`), the failure summary (`train.py:519-534`), and PBS
   liveness (`health.py:160-174`) — so this is a robustness/speed-of-failure issue, not silent-corruption risk.
   Recommendation (non-blocking): consider an explicit per-step heartbeat-monitor or a shorter process-group
   timeout / `torchrun --max-restarts` policy tuned for the formal run, or a wrapper watchdog that aborts all
   nodes when one rank dies, if faster failure turnaround is desired.

2. **Per-step fsync'd heartbeats and CSV appends over 8 ranks × 5000 steps may strain the 2-hour walltime.**
   Facts: `train.py:412-421` calls `write_heartbeat` every step; `artifacts.py:195-221` routes it through
   `atomic_write_json` → `atomic_write_bytes` (`atomic_io.py:23-40`) which does a write + `os.fsync` + rename
   on the shared filesystem. The per-rank CSV (`artifacts.py:184-192`) and the rank-0 sync CSV are appended
   every step as well. The formal launchers cap `#PBS -l walltime=02:00:00`
   (`scripts/miyabi/run_8node_torch_*.pbs:6`). At ~40,000 fsync'd writes plus ~2×5000 CSV appends, a network
   filesystem with per-file-fsync latency in the tens of milliseconds adds minutes of pure I/O on the critical
   path of a ~1.5–2 h GPT-2 run.
   Inference: no evidence that the budget is violated today (the 1-node and 2-node runs completed comfortably),
   but the margin is a runtime risk that only the formal 8-node run will measure.
   Recommendation (non-blocking): measure step time early in the formal run; if needed, write heartbeats on a
   coarse cadence (e.g. every 10 steps) or batch the CSV flush/fsync, keeping the current dense behavior for a
   debug config.

3. **The health-checker wait loop treats transient non-PENDING observations as terminal FAIL.**
   Facts: `health.py:319-331` breaks out of the wait loop on any `status != "PENDING"`. A scheduler-side
   hiccup that makes `query_pbs_job` return `state == "unknown"` (any non-`{R,B,Q}` state at `health.py:170-173`,
   produced by `health.py:55-78`), or a job observed mid-transition between `R` and `F` while the terminal
   summary is not yet visible (the `completed_full_run` guard at `health.py:138-159` only short-circuits the
   qstat query once `summary.json` is present with `final_step == max_steps`), would end the monitoring run
   with FAIL rather than re-poll.
   Inference: in the two real invocation paths the window is narrow — the launcher's post-completion check
   runs after `mpirun` returns with `summary.json` already written, and the external 200-step wait completes
   while the job is comfortably in `R` — so this is an operational robustness issue, not a correctness defect
   in the acceptance contract itself.
   Recommendation (non-blocking): re-poll on transient states (e.g. treat `unknown` and a not-yet-terminal
   job as PENDING for a bounded retry) and only FAIL on a confirmed terminal state or timeout.

### Low

1. **No parent-directory fsync after the final-checkpoint rename.**
   Facts: `train.py:189-209` stages into `tempfile.mkdtemp(prefix=".final.", dir=paths.final_checkpoint.parent)`,
   writes model+tokenizer, then `os.replace(staging, paths.final_checkpoint)` without fsyncing
   `checkpoints/` afterward.
   Inference: a crash immediately after the rename could lose the directory entry. This matches the existing
   `atomic_io.py` convention (which also omits parent-dir fsync), so the persistence invariant is not newly
   weakened.
   Recommendation (non-blocking): `os.open` + `os.fsync` the parent directory descriptor after the rename.

2. **`append_durable_csv` flushes without fsync.**
   Facts: `artifacts.py:184-192` appends, `flush()`es, but does not `os.fsync`. Matches the repository's
   existing CSV writers.
   Inference: the last buffered trailing rows may be lost on a hard crash; heartbeats/manifest/summary remain
   authoritative.
   Recommendation (non-blocking): coarse-grained fsync (every N steps) if trailing-metric durability matters.

3. **The launcher skips the post-training health check when training exits non-zero.**
   Facts: both launchers run under `set -eEuo pipefail`; a failing `mpirun ... | tee` pipeline
   (`scripts/miyabi/run_8node_torch_*.pbs:154`) aborts the script before
   `scripts/miyabi/check_torch_baseline_health.py` runs (`:156-160`), so no `final_health.json` is produced for
   failed runs.
   Inference: failures are still captured by per-rank logs/heartbeats and the rank-0 failure summary, and by
   the PBS job record; this is a convenience gap, not an acceptance gap.
   Recommendation (non-blocking): `|| true` on the pipeline (or a trap) so the checker always emits a
   machine-readable FAIL for failed runs.

4. **`evaluate_health` assumes manifest/summary JSON are dicts.**
   Facts: `health.py:106` (`manifest = safe_read_json(...)`) and `health.py:138` (`summary = ...`) guard only
   against `None`; `safe_read_json` (`atomic_io.py:77-81`) catches `OSError`/`JSONDecodeError`, so a
   parseable-but-non-dict payload would raise `AttributeError` on `.get(...)` instead of a machine-readable FAIL.
   Inference: writers always emit dicts (`artifacts.py:160-174`, `train.py:473-534`), so the probability is low.
   Recommendation (non-blocking): `isinstance` guard routing non-dict payloads to FAIL/PENDING.

5. **`_fatal_log_evidence` has no dedicated test.**
   Facts: `health.py:35-52` scans `logs/rank_*.jsonl` for `FATAL_LOG_PATTERN` and `baseline_failed` events;
   none of the health tests write a JSONL file, so the scanner is uncovered.
   Recommendation (non-blocking): a unit test writing a traceback line and a `baseline_failed` event.

6. **`run()` and `_resolve_baseline_config` are only exercised through manual smoke runs.**
   Facts: the torchrun CLI lifecycle is covered by the recorded smoke runs (Gloo 2-process and NCCL 1-node/2-node,
   `progress.md`) but not by an automated test in the suite.
   Recommendation (non-blocking): a lightweight subprocess test for `_resolve_baseline_config` and the
   manifest/summary publication path.

7. **Magic-number coupling of the 5000-step formal boundary.**
   Facts: `train.py:85`, `:450`, `:492` hard-code `max_steps == 5000` for the checkpoint gate and alignment
   guard, and `health.py:23` hard-codes `FORMAL_MAX_STEPS = 5000`.
   Recommendation (non-blocking): centralize the formal boundary so a future step-count change updates one
   place (plus configs).

## Correctness and invariant assessment

Facts (verified against the code at the review target):

- **DDP gradient accumulation semantics.** `protocol.train_optimizer_step` (`protocol.py:37-96`) wraps every
  non-final microbatch in `model.no_sync()` and runs the final forward/backward unprotected, so exactly one
  reducer all-reduce fires per optimizer step; loss is scaled by `1/accumulation_steps` per microbatch and the
  reported loss is the mean of the unscaled micro-batch losses (`protocol.py:73-78`, `:92-93`), matching
  `learner.train_one_step`. The `TrackingNoSync` unit test asserts two `no_sync` entries and forward states
  `[True, True, False]` for three accumulation steps, and the two-process Gloo test shows the synced update
  equals the combined-batch reference gradient after identical clipping. DDP backward is synchronous, so the
  subsequent `clip_grad_norm_` and `optimizer.step()` see fully reduced gradients.
- **Periodic BF16 averaging preserves optimizer/scheduler state.** `average_trainable_parameters`
  (`protocol.py:130-173`) flattens trainable parameters to one BF16 tensor, SUM-all-reduces, divides by
  `world_size`, and `copy_`s back into the existing `Parameter` objects under `no_grad`, so AdamW
  `exp_avg`/`exp_avg_sq`, scheduler `last_epoch`, and step counters are retained; the two-process periodic test
  verifies convergence to the arithmetic mean and byte-identical moment/scheduler state.
- **Collective safety.** `all_gather_object` (`train.py:247-248`), `broadcast_object_list` (`train.py:263`),
  the barriers (`train.py:267,449,453,471`), the DDP reducer, and the conditional periodic `all_reduce` are
  reached symmetrically on every rank because `should_average(step, interval)` is deterministic; the schedule
  is exactly steps 100, 200, ..., 5000 (`should_average` + the dedicated schedule test), aligned with the formal
  config `inner_steps=100`, `max_local_steps=5000`, `scheduler_total_steps=5000`.
- **Non-finite rejection.** Non-finite loss, gradient norm, and pre/post-average parameters raise
  `FloatingPointError` before the optimizer step / before and after the all-reduce (`protocol.py:80-83`,
  `:107-108`, `:154-165`); the non-finite-loss test asserts the parameter is unchanged.
- **Data sharding.** `wikitext_batches` shards by `dataset.shard(num_shards=num_learners, index=learner_index,
  contiguous=True)` (`hf_data.py:160`), then applies the repo text→tokens+EOS→block protocol; the sharding test
  confirms disjoint, deterministic rank-0/rank-1 halves. Per-rank RNG streams derive from
  `seed + learner_index * 100_003`, consistent with the repository.
- **Artifact and checkpoint publication.** Rank 0 is the sole writer of the manifest (O_EXCL,
  `artifacts.py:103-118`), resolved config, source identity, sync CSV, summary, and final checkpoint; per-rank
  paths avoid concurrent writers. The launcher and `initialize_run_root` both refuse to overwrite existing
  training evidence (`artifacts.py:120-182`, PBS `:98-102`), and source-identity mismatches against the captured
  launcher environment are rejected (`artifacts.py:134-145`, `tests/...:48-71`). Final checkpoint publication
  is staged and atomically renamed with staging cleanup on failure (`train.py:189-209`).
- **Health acceptance.** `evaluate_health` enforces mode, world-size, NCCL/CUDA, distinct-host count, manifest
  `max_steps=5000` / `average_interval=100`, job liveness or completed-full-run status, fatal log evidence,
  complete per-rank step coverage with finite losses, a strict loss-decline comparison of steps 151–200 versus
  1–50, and the exact DDP step-sync / periodic 100-and-200 sync set (`health.py:82-292`). Numeric CLI inputs are
  validated (`health.py:309-316`). Tests cover pending, early-end-fail, DDP pass at 200, periodic pass at 100/200,
  missing rank, non-finite loss, flat loss, missing periodic sync, short declared run, and wrong interval.
- **MPI → torchrun launch.** Each PBS node runs one unbound MPI supervisor (`mpirun --map-by ppr:1:node
  --bind-to none -np 8`, `:122-128`) that invokes
  `torch.distributed.run --rdzv-backend=c10d --rdzv-endpoint=$MASTER_ADDR:$MASTER_PORT --rdzv-id=$RDZV_ID
  --nnodes=8 --nproc-per-node=1 --node-rank=$OMPI_COMM_WORLD_RANK --max-restarts=0` (`:137-150`); the
  `bash -lc '...' bash "${TRAIN_ARGS[@]}"` invocation passes `"$@"` through correctly (`$0="bash"`,
  `$1..$n` = train args). `RDZV_ID` is `${PBS_JOBID}_${MODE}` (unique per job/mode) and `MASTER_PORT` derives
  from the numeric job id with a safe default. `#PBS -W group_list=xg24i002` is a literal group id, `bash -n`
  passes, and preflight refuses existing run roots.
- **Config validation.** `resolve_config` (`config.py:531-548`) enforces backend ∈ {gloo,nccl}, `num_learners>=1`,
  positive `max_local_steps` and `inner_steps` when the baseline is enabled, and the repository config test
  selects the `runs/torch_baselines` namespace only for baseline-enabled configs (`tests/test_config.py:36-47`).

Inferences:

- The two baselines implement the intended semantics faithfully: standard DDP with exactly one all-reduce per
  optimizer step, and local SGD with BF16 parameter averaging every 100 steps while retaining per-rank AdamW
  and scheduler state. BF16 averaging is a documented approximation consistent with the model's BF16 dtype and
  is exercised end-to-end (flattened numel 124,439,808 recorded on the 1-node NCCL run).
- The remediation closes the prior High acceptance-correctness defect and adds the missing checkpoint and
  source-selection coverage. The remaining open items are runtime: the formal 8-node/200-step acceptance and
  the 5000-step handoff, both recorded as outstanding in `progress.md`, plus the Medium walltime/failure-timing
  margins above.

## Test coverage assessment

The in-scope ladder is strong and targets the invariants under review: DDP `no_sync` boundaries; non-finite
rejection; BF16 arithmetic mean with optimizer/scheduler retention; exact 100-step schedule; two-process Gloo
DDP equivalence to the combined-batch gradient; two-process periodic convergence; manifest/overwrite
exclusivity; source-identity mismatch; deterministic non-overlapping rank shards; final-checkpoint staging and
cleanup; and a broad health-checker matrix. The complete repository suite (383 tests) passes, including real
two-process Gloo collectives. Gaps are confined to the Low-severity robustness/edge items above (JSONL fatal
evidence, `_resolve_baseline_config`/`run` automation, transient-state handling); none blocks formal submission.

## Reproducibility

`_seed_everything(seed)` is applied before model load (identical weights across ranks) and again as
`seed + rank * 100_003` before batch-iterator construction (`train.py:301-310`), matching the repository's
per-learner RNG convention; `model.compile: false`, `TOKENIZERS_PARALLELISM=false`, per-job `RDZV_ID`, fixed
`MASTER_PORT` derivation, and clean-source enforcement support reproducibility. The resolved config, source
identity, and runtime topology are persisted in the manifest; per-rank CSVs, logs, heartbeats, and the summary
are retained under the run root.

## Agreement with plan acceptance criteria

The plan requires: 8 ranks/hosts, NCCL+CUDA, finite and declining loss over steps 1–50 versus 151–200, DDP
step-syncs for all 200 steps or periodic all-rank averages at steps 100/200, and either a still-running job or a
0-exit completion of all 5000 steps. The implementation and the remediated health checker enforce all of these,
and the checker tests cover both the PASS and the fail-closed negative cases. The formal 8-node/200-step
acceptance run and the 5000-step handoff remain outstanding runtime experiments, not code defects.

## Decision

`APPROVE`
