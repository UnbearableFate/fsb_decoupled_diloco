# E2 implementation progress

## 2026-07-17 — plan audit

- Rejected the original claim that current publication can be hidden behind next-cycle grace: learners cannot train on version N+1 until files, DB commit, and latest publication complete. Hiding grace would change staleness/training semantics.
- Corrected L2 to the safe opportunity: while E1 file-I/O workers run, the main thread may ingest heartbeat/pointer metadata, but may not select, maintain, commit a version, or publish latest. OVL-05 measures discovery delay rather than claiming `max(grace,publish)`.

## 2026-07-18 — OVL-01/04 focused implementation

- Added monotonic, non-overlapping interval accounting for discovery, top-level idle polling, grace, read, merge, publish, and maintenance, with an explicit residual and quorum trigger. Full and fragment syncers carry the cycle counters across quorum waits and reset them only after a committed merge.
- Added the opt-in `sync.ingest_during_publish` path. E1 checkpoint workers remain file-only; the main thread polls their futures and may ingest heartbeat/pointer metadata while both files are pending. Version commit, selection, maintenance, and `latest.json` publication remain after both workers succeed.
- Command: `pytest -q tests/test_interval_telemetry.py tests/test_parallel_publication.py tests/test_run_metrics_csv.py tests/test_syncer_runtime.py`.
- Environment: Miyabi interactive compute node `mg0043`, `.venv`, NVIDIA 25.9 / NV-HPCX 25.9 modules.
- Result: 14 passed in 1.73 s. Artifact: `artifacts/20260718-0505_ovl01-04-focused-green.log`.
- Remaining: exercise telemetry in tiny full/fragment runs, measure shared-filesystem pointer polling cost, rerun the crash matrix/1000-cycle suite with overlap enabled, and perform the paired 9-node scan/overlap experiments.

## 2026-07-18 — OVL-01/04 overlap-enabled tiny run

- Added `configs/fs_diloco_tiny_publish_ingest_local.yaml`, differing from the full tiny smoke configuration by enabling `sync.ingest_during_publish` explicitly.
- Command: `RUN_ID=e2_publish_ingest_tiny_20260718_0525 CONFIG=$PWD/configs/fs_diloco_tiny_publish_ingest_local.yaml bash scripts/local/run_tiny_2proc_smoke.sh`.
- Environment: Miyabi interactive job `2405305.opbs`, compute node `mg0048`, one GPU visible, `.venv`, PyTorch 2.13.0+cu132.
- Result: clean `input_exhausted`, one committed merge from both learners, SQLite integrity `ok`, no syncer error/timeout flags. The measured 3.4164 s interval decomposed into 0.0850 s discovery, 3.0009 s idle, 0.2265 s terminal grace, 0.0015 s read, 0.0276 s merge, 0.0139 s publish, 0.0346 s maintenance, and 0.0263 s residual (0.77%).
- Checkpoint I/O completed faster than the 0.2 s poll in this synthetic run, so the expected ingestion counters were zero; the deterministic delayed-worker test remains the positive evidence that the callback executes while futures are pending.
- Run: `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/e2_publish_ingest_tiny_20260718_0525`. Artifact: `artifacts/20260718-0525_publish-ingest-tiny-pass.log`.

## 2026-07-18 — OVL-02 shared-filesystem pointer polling

- Added `scripts/miyabi/measure_pointer_polling.py`, a reproducible fixed-pointer benchmark that reports process CPU, stat walltime, call count, latency, and `/proc/self/io` deltas.
- Command: `python scripts/miyabi/measure_pointer_polling.py --root runs/fs_diloco/e2_pointer_poll_20260718_0534/control/update_latest --pointer-count 8 --interval-seconds 0.2 --duration-seconds 60`.
- Environment: Miyabi interactive job `2405305.opbs`, compute node `mg0048`; benchmark root is on the repository shared filesystem.
- Result: 300 polls / 2,400 stat calls in 60.0001 s used 0.02410 CPU-seconds (0.0402% of one CPU). Mean stat time was 9.90 µs; aggregate stat wall fraction was 0.0396%; `read_bytes` and `write_bytes` deltas were zero. This passes the `<1% syncer CPU` OVL-02 threshold with substantial margin.
- Artifact: `artifacts/20260718-0534_ovl02-pointer-poll-pass.json`. Fixture root: `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/e2_pointer_poll_20260718_0534`.

## 2026-07-18 — OVL-04 recovery and bounded-state regression

- Re-ran the complete publication recovery matrix after adding the future polling callback: 6 failpoints × 10 iterations. All 60 killed publishers recovered to exactly-once version 2 with readable DB-authoritative checkpoints and converged GC; no SQLite lock/busy failure occurred.
- Crash command: `python scripts/miyabi/publication_crash_probe.py --root runs/fs_diloco/e2_crash_matrix_20260718_0541 --iterations 10`. Artifact: `artifacts/20260718-0541_ovl04-crash-matrix-pass.log`.
- Bounded-state command: `pytest -q tests/test_bounded_1000_cycles.py tests/test_retention.py tests/test_sqlite_store.py tests/test_fragment_store.py`. Result: 19 passed in 4.74 s. Artifact: `artifacts/20260718-0548_ovl04-bounded-store-pass.log`.
- Environment: Miyabi interactive job `2405305.opbs`, compute node `mg0048`.
- Remaining OVL-03/05 evidence requires paired 9-node runs; the local and one-node correctness/overhead gates are green.

## 2026-07-18 — formal matrix override and launcher gate

- Added explicit runtime overrides for training seed, scan interval, and publish-time ingestion to `resolve_config` and both process CLIs. The 9-node 5,000-step launcher propagates the same values to every rank and records them before launch, allowing the three E2 variants and three seeds to share one audited base config without generated YAML drift.
- Added positive validation for `sync.scan_interval_seconds`; resolved snapshots retain every override.
- Static PBS gate: `bash -n scripts/miyabi/*.pbs` passed and every `group_list` is the literal `xg24i002` value.
- Test command: `pytest -q tests/test_config.py tests/test_source_identity.py tests/test_capture_source_identity.py`. Result after correcting an obsolete E3 test fixture: 75 passed in 1.85 s. Artifact: `artifacts/20260718-0607_experiment-overrides-pass.log`.
- The launcher is ready for the 3-variant × 3-seed OVL-03/05 submissions; source identity is captured once per job before ranks start.

## 2026-07-18 — pre-experiment repository gate

- Full compute-node suite: `pytest -q` → 259 passed in 18.16 s. Artifact: `artifacts/20260718-0620_full-pytest-pass.log`.
- Lint after removing two obsolete test-only imports: `ruff check fs_diloco scripts/miyabi/measure_pointer_polling.py tests` → all checks passed. Focused import-fix tests: 8 passed in 2.42 s.
- Artifacts: `artifacts/20260718-0626_ruff-pass.log` and `artifacts/20260718-0626_lint-fix-tests-pass.log`.
- Documentation now records the interval fields, publish-time ingestion boundary, new config flag, and formal launcher overrides.

## 2026-07-18 — OVL-03/05 matrix extraction

- Extended the stable run-metrics export with source fingerprint, seed, scan/ingestion settings, interval p50/p95, quorum-detection (`discovery+idle`) latency, quorum trigger distribution, publish-time ingestion totals, residual ratio, syncer merge p95/duty cycle, and idle node-hour estimate.
- Focused command: `pytest -q tests/test_run_metrics_csv.py tests/test_syncer_resource_cost.py`. Result: 6 passed in 0.13 s on Miyabi job `2405387.opbs`, node `mg0005`.
- Artifact: `reports/imp_plans/perf_fix-E/E5/artifacts/20260718-0800_analysis-matrix-pass.log` (shared E2/E5 analysis gate).
- Formal E2 matrix jobs: `2405350`–`2405358`; immutable source snapshot `/work/xg24i002/x10041/fs_diloco_experiment_snapshots/e2_20260718_0630`, fingerprint `sha256:197f85ed24ac9994bc9a97ab1011aa04b499a39fa687ce596f48f2613bc98ce0`.

## 2026-07-18 — OVL-03/05 formal 3-seed result

- All nine 9-node jobs `2405350`–`2405358` completed with `Exit_status=0`, v50 `stop_after_outer_steps`, 8/8 learners stopped, SQLite integrity OK, and the identical audited source fingerprint.
- Three-seed means for scan=2/no-ingest → scan=.2/no-ingest: complete time 1101.633 → 1075.273 s (-2.39%), global interval 21.6515 → 21.1251 s (-2.43%), and quorum-detection time 13.1646 → 12.2652 s (-6.83%). Update utilization stayed effectively flat (97.453% → 97.395%). Together with the 0.0402% CPU stat benchmark, this supports using the shorter scan for latency-sensitive experiments, while the asynchronous quorum-max rate (70.0% → 61.3%) is too noisy to claim a monotonic improvement.
- Enabling publish-time ingestion at scan=.2 executed 24 successful metadata ingestions across the three runs (8/run mean), but complete time was 1076.155 s versus 1075.273 s (+0.08%), interval 21.1431 versus 21.1251 s (+0.09%), and quorum detection 12.3868 versus 12.2652 s (+0.99%). It improved update utilization by 0.392 percentage points and quorum-max trigger rate by 11.33 points, but produced no wall-time benefit at this publish duration. The feature remains opt-in; it is not promoted as a default optimization.
- OVL-01 residual ratios were 0.084% for scan=2 and 0.283–0.284% for scan=.2, all well below 5%. Raw matrix: `artifacts/20260718-1300_e2-formal-matrix.csv`.
