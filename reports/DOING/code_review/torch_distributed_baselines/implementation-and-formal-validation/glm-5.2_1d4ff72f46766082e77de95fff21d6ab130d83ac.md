# Independent senior code review — torch distributed baselines (implementation-and-formal-validation)

## Identity and scope

- Reviewer model (actual identifier): `opencode-go/glm-5.2`.
- Review target commit: `1d4ff72f46766082e77de95fff21d6ab130d83ac` (`remediate torch baseline review findings`).
- Comparison base (plan branch point / previous phase-final): `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Plan-id: `torch_distributed_baselines`. Phase-id: `implementation-and-formal-validation`.
- Reviewer role: reviewer-only. No implementation, tests, configs, plans, existing reports,
  git state, scheduler jobs, or external services were modified. The only file created is this
  report.
- Independence note: this report was produced after the prior `gpt-5-codex_e30d49f...` report was
  already present on disk (it is part of the review-target tree), but the conclusions below were
  reached by directly inspecting the cumulative diff and surrounding source at the review-target
  commit, including the remediation commit `1d4ff72`. It does not merely summarize the prior review;
  the remediation diff and all in-scope files were re-inspected.

### Scope inspected (cumulative diff, 21 files, +3000/-2)

- `fs_diloco/baselines/__init__.py`, `protocol.py`, `artifacts.py`, `health.py`, `train.py`
- `fs_diloco/core/config.py` (TorchBaselineSection + resolve validation)
- `configs/torch_baseline_gpt2_wikitext2_8n_5000steps.yaml`,
  `configs/torch_baseline_gpt2_wikitext2_1rank_debug.yaml`,
  `configs/torch_baseline_tiny_2rank.yaml`
- `scripts/miyabi/run_8node_torch_ddp_gpt2_wikitext2_5000steps.pbs`,
  `scripts/miyabi/run_8node_torch_periodic_average_gpt2_wikitext2_5000steps.pbs`,
  `scripts/miyabi/check_torch_baseline_health.py`
- `tests/test_torch_baseline_protocol.py`, `tests/test_torch_baseline_health.py`,
  `tests/test_torch_baseline_artifacts_and_data.py`, `tests/test_config.py`
- `pyproject.toml` (console entry point)
- `plans/DOING/torch_distributed_baselines.md`,
  `reports/DOING/torch_distributed_baselines/progress.md` and `failures.md`,
  prior review `reports/DOING/code_review/torch_distributed_baselines/implementation-and-formal-validation/gpt-5-codex_e30d49f102ca91c44af5e5700457c98a6e26de6e.md`

Surrounding source inspected for dependency correctness:
`fs_diloco/modeling/hf_data.py`, `fs_diloco/runtime/learner.py` (lines 664–773),
`fs_diloco/storage/atomic_io.py`.

### Commands executed during the review (read-only)

- `git rev-parse HEAD`, `git log --oneline c1c6115...1d4ff72f...`, `git diff --stat`,
  `git show --stat 1d4ff72`, `git diff e30d49f..1d4ff72` (remediation diff), per-file `git diff`.
- `bash -n scripts/miyabi/run_8node_torch_ddp_gpt2_wikitext2_5000steps.pbs` and the periodic launcher
  — both OK.
- `python -m py_compile` on all five `fs_diloco/baselines/*.py` and
  `scripts/miyabi/check_torch_baseline_health.py` — OK.
- `ruff check` (0.15.21 from the shared venv) on `fs_diloco/baselines/` and the three baseline
  test modules — `All checks passed!`.
- `pytest tests/test_torch_baseline_artifacts_and_data.py tests/test_torch_baseline_health.py
  tests/test_torch_baseline_protocol.py -q` — `21 passed in 22.22s` (includes the two real
  `torch.multiprocessing.spawn` Gloo collectives).

## Prior-review finding dispositions re-verified at the review target

These are facts about the remediation, not inherited conclusions:

- Prior `High` (health checker could PASS a non-5000-step / non-100-interval run): remediated.
  `fs_diloco/baselines/health.py:23-24` introduces `FORMAL_MAX_STEPS=5000` and
  `FORMAL_AVERAGE_INTERVAL=100`; `health.py:143-153` appends a hard failure unless the manifest
  declares both values. Two new RED tests cover the false-positive cases
  (`tests/test_torch_baseline_health.py:227-251`). Independently re-verified: a manifest declaring
  `max_steps=200` with a completed summary now FAILs on `max_steps`, and a periodic manifest with
  `average_interval=200` FAILs on `average interval`.
- Prior `Medium` (PBS source selection relying on a foreign venv without pinning `PYTHONPATH`):
  remediated. Both launchers now set `PYTHONPATH="$PROJECT_ROOT"` and pass it through
  `MPI_ENV_ARGS` (`scripts/miyabi/run_8node_torch_*.pbs:22` and `:89`).
- Prior `Medium` (untested 5000-step final-checkpoint publication): remediated.
  `tests/test_torch_baseline_artifacts_and_data.py:148-187` exercises model+tokenizer publication
  through staging, atomic rename, and staging cleanup after an injected tokenizer failure.
- Prior `Low` (checker CLI numeric validation): remediated (`health.py:309-316`).

## Findings at the review target

### Critical
None.

### High
None. The High acceptance-correctness defect from the prior review is closed by the remediation
commit and is covered by regression tests that I re-ran successfully.

### Medium
None newly identified.

### Low

1. Final-checkpoint publication does not fsync the parent directory after the staging→final rename.
   Facts: `fs_diloco/baselines/train.py:189-209` stages model+tokenizer into
   `tempfile.mkdtemp(prefix=".final.", dir=paths.final_checkpoint.parent)`, then performs
   `os.replace(staging, paths.final_checkpoint)`. Neither `os.replace` nor any subsequent call
   fsyncs `paths.final_checkpoint.parent` (`checkpoints/`).
   Inference: a hard crash immediately after the rename and before the OS flushes the parent
   directory's dirent could leave the 2-hour terminal checkpoint unreferenced on disk even though
   its contained files were fsynced. This is consistent with the repository-wide convention in
   `fs_diloco/storage/atomic_io.py` (which also omits parent-directory fsync), so the window is
   narrow and the behavior matches the existing persistence invariant.
   Recommendation (non-blocking): after `os.replace`, `os.open(paths.final_checkpoint.parent)`
   and `os.fsync` the parent directory descriptor before the closing barrier, or extend
   `atomic_io` to offer a `fsync_dir` helper used here.

2. `evaluate_health` assumes manifest/summary JSON is a dict without a type guard.
   Facts: `health.py:106` `manifest = safe_read_json(paths.manifest)`, then `if manifest is None`
   returns PENDING; otherwise `manifest.get(...)`. `safe_read_json` (`atomic_io.py:77-81`) only
   catches `OSError` and `json.JSONDecodeError`, so a valid-JSON-but-non-dict payload would pass
   the `is None` check and then raise `AttributeError` on `manifest.get(...)`. The same pattern
   applies to `summary` at `health.py:138-139,196`.
   Inference: a corrupt-but-parseable manifest/summary would crash the checker rather than emit a
   machine-readable FAIL. Manifests and summaries are always written by the implementation as
   dicts (`artifacts.py:160-174`, `train.py:473-534`), so the probability is low.
   Recommendation (non-blocking): guard with `isinstance(manifest, dict)` and route non-dict
   payloads to a FAIL/PENDING entry, matching the fail-closed posture already used elsewhere.

3. Post-completion health acceptance only validates the steps 1–200 window.
   Facts: both PBS launchers invoke the checker with `--target-step 200`
   (`scripts/miyabi/run_8node_torch_*.pbs:156-160`) and run it once, after training. For a
   completed 5000-step run, `evaluate_health` confirms steps 1–200 loss decline (1–50 vs 151–200)
   and full completion via `summary.json`, but does not inspect loss behavior over steps ~4800–5000.
   Inference: a run that declined in the first 200 steps and later diverged could still PASS the
   post-completion check. This is explicitly the plan's acceptance contract
   (`plans/DOING/torch_distributed_baselines.md:23-28`), so it is by-design; recorded only as an
   observation/boundary, not a defect.

4. PBS launcher skips the post-training health check when training exits non-zero.
   Facts: both launchers run under `set -eEuo pipefail`; the `mpirun ... | tee` pipeline precedes
   the checker invocation (`scripts/miyabi/run_8node_torch_*.pbs:154-160`). A failed training run
   causes the script to exit before producing `final_health.json`.
   Inference: a crashed formal run publishes no machine-readable checker FAIL; the failure is still
   captured by per-rank heartbeats (`train.py:506-518`) and the rank-0 failure summary
   (`train.py:519-534`).
   Recommendation (non-blocking): consider `|| true` on the `mpirun` pipeline (or a trap-based
   finalize) so the checker always emits `final_health.json` for failed runs too.

5. Magic-number coupling of the 5000-step formal boundary.
   Facts: `train.py:85,450,492` hard-code `max_steps == 5000` for the final-checkpoint gate, and
   `health.py:23` hard-codes `FORMAL_MAX_STEPS=5000`. The `_resolve_baseline_config` 5000-step
   alignment guard (`train.py:85-86`) is asymmetric: it enforces `max_steps % average_interval == 0`
   only when `max_steps == 5000`.
   Inference: if the formal step count ever changes, two code locations (plus configs) must be
   updated in lockstep; misalignment for a non-5000 debug run is allowed by design.
   Recommendation (non-blocking): consider a single formal-run flag/constant or deriving the
   checkpoint gate from config rather than a literal.

6. `append_durable_csv` flushes without fsync.
   Facts: `artifacts.py:184-192` opens CSVs in append mode, writes, and `flush()`es without
   `os.fsync`. Per-rank CSVs and the rank-0 sync CSV are the primary training telemetry.
   Inference: the last few buffered rows may be lost on a hard crash; heartbeats and the manifest
   (which use `atomic_write_json` with fsync) remain authoritative for liveness/acceptance.
   Consistent with existing repo CSV writers.
   Recommendation (non-blocking): optionally fsync on a coarse cadence (e.g. every N steps) if
   crash-durability of trailing metrics matters for post-mortem analysis.

7. No dedicated test for `_fatal_log_evidence`.
   Facts: `health.py:35-52` scans `rank_*.jsonl` for `FATAL_LOG_PATTERN` and `baseline_failed`
   events. The health test module exercises pending/early-end/nonfinite-loss/missing-rank/missing-sync
   paths but none write a JSONL log file, so this scanner is currently uncovered.
   Inference: a regression in the regex or the event-type filter would not be caught. The regex
   and filter are simple and the fail-closed semantics are clear.
   Recommendation (non-blocking): add a unit test that writes a `rank_000.jsonl` with a traceback
   line and a `baseline_failed` event and asserts both surface as fatal evidence.

## Correctness and invariant assessment

Facts:
- DDP gradient accumulation is correct: `protocol.train_optimizer_step` wraps every non-final
  microbatch in `model.no_sync()` and lets the final forward/backward run unprotected, so exactly
  one reducer all-reduce fires per optimizer step (`protocol.py:65-96`). The
  `TrackingNoSync` unit test confirms two `no_sync` entries and forward states `[True, True, False]`
  for 3 accumulation steps, and the two-process Gloo DDP test shows the synced update equals the
  combined-batch reference gradient after clipping.
- Non-finite loss and non-finite gradient/parameter are rejected before the optimizer step and
  before/after the periodic average (`protocol.py:80-83,107-108,154-165`); the non-finite-loss test
  asserts the parameter is unchanged.
- Periodic averaging flattens trainable parameters to one BF16 tensor, SUM-all-reduces, divides by
  `world_size`, and `copy_`s the result back into the existing `Parameter` objects under
  `torch.no_grad` (`protocol.py:130-173`). Because `copy_` writes into existing Parameter tensors,
  AdamW `exp_avg`/`exp_avg_sq`, scheduler `last_epoch`, and optimizer step counters are retained;
  the two-process periodic test verifies weight convergence to the arithmetic mean while asserting
  the moment and scheduler state are byte-equal.
- The periodic schedule is exactly steps 100, 200, ..., 5000 (`should_average` + the dedicated
  schedule test), and the formal config aligns `inner_steps=100`, `max_local_steps=5000`, and
  `scheduler_total_steps=5000`.
- Collective safety: `all_gather_object` (runtimes), `broadcast_object_list` (setup result), all
  `barrier` calls, the DDP reducer, and the conditional periodic `all_reduce` are reached
  symmetrically on every rank because `should_average(step, interval)` is deterministic across
  ranks. A non-rank-0 worker loss is observable through PBS liveness and, for rank 0, through the
  failure summary/heartbeats.
- Data sharding uses `dataset.shard(num_shards=num_learners, index=learner_index, contiguous=True)`
  followed by the repo text→tokens→block protocol; the sharding test confirms rank 0 and rank 1
  receive disjoint, contiguous halves. Per-rank RNG streams derive from
  `seed + learner_index * 100_003`, matching the rest of the repository.
- Rank 0 is the sole writer of the manifest (via `O_EXCL`), the resolved config, source identity,
  sync metrics, summary, and final checkpoint; per-rank paths avoid concurrent writers. The
  launcher and `initialize_run_root` both refuse to overwrite an existing run, and source-identity
  mismatches are rejected.
- MPI→torchrun launch: `mpirun --map-by ppr:1:node -np 8` starts one supervisor per node, each
  invoking `torch.distributed.run --rdzv-backend=c10d --rdzv-endpoint=$MASTER_ADDR:$MASTER_PORT
  --nnodes=8 --nproc-per-node=1 --node-rank=$OMPI_COMM_WORLD_RANK --max-restarts=0`. `RDZV_ID` is
  `${PBS_JOBID}_${MODE}` (unique per job/mode). `MASTER_PORT` derives from the numeric job id, with
  a safe default for non-PBS execution. This is the standard static-rendezvous multi-node torchrun
  topology.
- `#PBS -W group_list=xg24i002` is a literal, valid group ID (not a placeholder), and `bash -n`
  passes on both launchers.

Inferences:
- The training and communication semantics (DDP all-reduce-per-step; local-SGD with BF16 parameter
  averaging and preserved inner optimizer/scheduler state) implement the intended baselines
  faithfully. The BF16 averaging is a documented approximation (consistent with the model's BF16
  dtype and analogous to BF16 H-communicator designs); it is not a correctness defect.
- The remediation correctly closes the prior High acceptance-correctness defect and adds the
  missing final-checkpoint and source-selection coverage. The remaining open items are runtime
  validation (the formal 8-node/200-step acceptance and the 5000-step handoff), which are recorded
  as outstanding in `progress.md` and are not code defects.

## Test coverage assessment

The in-scope test ladder is strong and directly targets the invariant under review:
- DDP `no_sync`-only-before-final-microbatch; non-finite loss rejection; BF16 arithmetic mean +
  optimizer/scheduler state retention; exact 100-step schedule.
- Two-process Gloo DDP equivalence to combined-batch gradient; two-process periodic convergence +
  state preservation.
- Manifest/overwrite exclusivity; source-identity mismatch rejection; deterministic non-overlapping
  rank shards; final-checkpoint staging publication + failure cleanup.
- Health checker pending/early-end/pass-DDP/pass-periodic-at-100-and-200/missing-rank/nonfinite/
  flat-loss/missing-periodic-sync/short-declared-run/wrong-interval.

Gaps are limited to Low-severity robustness/edge tests (see Low 2 and 7). No behavioral test gap
blocks formal submission.

## Reproducibility

`_seed_everything(seed)` is applied before model load (identical pretrained weights across ranks)
and again as `seed + rank * 100_003` before batch-iterator construction, matching the repository's
per-learner RNG convention. `model.compile: false`, `TOKENIZERS_PARALLELISM=false`, fixed
`RDZV_ID` per job, and clean-source enforcement all support reproducibility. The resolved config,
source identity, and runtimes are persisted in the manifest.

## Agreement with plan acceptance criteria

The plan requires: 8 ranks/hosts, NCCL+CUDA, finite and declining loss over steps 1–50 vs 151–200,
DDP 200 step syncs or periodic averages at 100/200, and a still-running job or a 0-exit completion
of all 5000 steps. The implementation and the remediated health checker enforce all of these; the
formal 8-node/200-step acceptance run itself is recorded as outstanding (a runtime experiment, not a
code defect).

## Decision

`APPROVE`