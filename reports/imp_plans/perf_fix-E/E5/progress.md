# E5 implementation progress

## 2026-07-17 — plan audit

- Froze the CPU eligibility threshold before measurement: GPT-2 8-vector `read+aggregation+outer` p95 below 4 seconds (20% of the 20-second reference interval), followed by a three-seed 8-node colocated median walltime regression no greater than 10% versus the matching 9-node baseline.
- The 8-node launcher must run CPU syncer and GPU learner_000 as two supervised processes on rank 0; remaining ranks each host one learner. Publication I/O is reported separately from CPU merge compute.

## 2026-07-18 — SNC-01 dedicated-syncer resource ledger

- Added `syncer_resource_cost` to the standard run summary. It computes `read+aggregation+outer_step` separately from publication, reports p50/p95 and totals, and converts active time versus complete walltime into duty cycle, reserved syncer node-hours, and estimated idle GPU node-hours.
- Focused command: `pytest -q tests/test_syncer_resource_cost.py tests/test_fragment_analysis.py tests/test_run_metrics_csv.py`. Result: 6 passed in 0.09 s. Artifact: `artifacts/20260718-0645_snc01-resource-ledger-pass.log`.
- Backfilled run H (`codex_predict_gc_retry_wait0_fixed20_global50_full5000_20260717_073011`): 1,180.456 s complete walltime; 18.345 s merge compute and 44.946 s publish across 50 merges; 5.362% duty cycle; 0.3279 reserved syncer node-hours, of which 0.3103 GPU node-hours are estimated idle. Merge-compute p95 was 0.4912 s and publish p95 was 0.9906 s.
- Evidence: `artifacts/20260718-0647_snc01-existing-run-ledger-pass.json`.

## 2026-07-18 — SNC-02 reproducible device benchmark and tiny scale

- Added `scripts/miyabi/benchmark_syncer_device.py`. It prepares one authoritative model/index and eight BF16 proposal fixtures, then measures shared-FS read, aggregation, outer step, and parallel full publication separately on CPU or CUDA. Published benchmark outputs are removed immediately after their byte count is recorded; fixtures remain reproducible under the run root.
- Environment: Miyabi interactive job `2405305.opbs`, node `mg0048`, PyTorch 2.13.0+cu132. Five repetitions per device, eight vectors, float32 syncer compute/publish.
- Tiny CPU p95: read 3.126 ms, merge-compute 3.395 ms, publish 9.221 ms. Tiny CUDA p95: read 4.093 ms, merge-compute 8.777 ms, publish 13.986 ms. At tiny scale launch/transfer overhead dominates, as expected; this is a correctness smoke rather than the 124M decision datum.
- Artifacts: `artifacts/20260718-0702_snc02-tiny-cpu-pass.json` and `artifacts/20260718-0702_snc02-tiny-gpu-pass.json`.

## 2026-07-18 — SNC-02 GPT-2 124M device decision gate

- Reused one set of eight BF16 proposal fixtures and an identical float32 authoritative GPT-2 vector for five CPU and five CUDA repetitions on Miyabi node `mg0048`.
- CPU: merge-compute p50 0.0914 s, p95 0.2866 s; publish p50 0.1645 s, p95 0.1667 s. CUDA: merge-compute p50 0.1771 s, p95 0.1843 s; publish p50 0.8033 s, p95 0.9737 s.
- The frozen CPU eligibility threshold is p95 `<4 s`; 0.2866 s passes by 13.96×. Shared-FS cache state affects the read split (CPU's first sample was cold), but even the maximum measured CPU merge-compute was 0.3345 s and does not threaten the decision boundary. Publication remains separately reported and was faster on CPU because it avoided two concurrent device-to-host transfers.
- Artifacts: `artifacts/20260718-0710_snc02-gpt2-cpu-pass.json` and `artifacts/20260718-0713_snc02-gpt2-gpu-pass.json`. Fixture root: `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/e5_gpt2_device_benchmark_20260718_0710`.
- Decision: proceed to the 8-node CPU-syncer/GPU-learner colocation launcher and SNC-03 smoke/comparison gate; this does not yet authorize changing the default 9-node deployment.

## 2026-07-18 — SNC-03 colocated launcher implementation gate

- Added an explicit `--syncer-device` resolved-config override and `scripts/miyabi/run_8node_colocated_gpt2_wikitext2_5000steps.pbs`.
- Rank 0 starts a CPU-only syncer and GPU learner_000 as separately logged child processes, uses `wait -n -p` for fail-fast supervision, terminates the sibling on failure, and requires both exit codes to be zero. Ranks 1–7 map directly to learners 001–007. All ranks share the same seed/scan/ingestion overrides and source identity.

## 2026-07-18 — SNC-03 8-node smoke

- Job `2405379.opbs` ran the colocated launcher from immutable snapshot `e5_20260718_0740` (fingerprint `sha256:393cb9...`) and exited 0 in 46 seconds.
- The CPU syncer on rank 0 and colocated GPU learner_000 both closed normally; all eight learners reported stopped. The authoritative run reached v3 `stop_after_outer_steps`, selected 8/8 updates in every merge, and passed SQLite integrity.
- Complete training time was 38.711 s. This short acceptance workload intentionally has only three frequent merges and a 30.81% syncer duty cycle, so it is a correctness smoke rather than the resource decision sample. Evidence: `artifacts/20260718-1315_snc03-colocated-smoke.csv`.
- The smoke gate permits the frozen same-fingerprint, three-seed 8-node/9-node full-duration comparison; the 9-node launcher remains the default until its ≤10% median gate is evaluated.

## 2026-07-18 — SNC-03 formal paired submission

- After re-running `bash -n scripts/miyabi/*.pbs` and confirming every PBS group is the literal `xg24i002`, submitted same-snapshot pairs for seeds 1337/2027/4049.
- Dedicated 9-node jobs: `2405615`, `2405617`, `2405619`; colocated 8-node jobs: `2405616`, `2405618`, `2405620`. All use immutable snapshot `e5_20260718_0740`, the same base config, explicit seed, offline W&B, and distinct run roots.
- Jobs were running at the first audit; final median/≤10% decision remains pending terminal status and metric extraction.
- Static gate: `bash -n scripts/miyabi/*.pbs` passed for all 13 scripts; all 13 contain literal `group_list=xg24i002` and no placeholder. Python compile and `git diff --check` passed.
- Focused command: `pytest -q tests/test_config.py tests/test_syncer_resource_cost.py`. Result: 74 passed in 0.39 s. Artifact: `artifacts/20260718-0732_snc03-launch-config-pass.log`.
- Remaining: 8-node acceptance smoke, then a fresh same-fingerprint three-seed 8-node/9-node comparison. The E2 baseline snapshot predates the colocated launcher/device override and therefore cannot be reused as the formal SNC-03 comparator.

## 2026-07-18 — SNC-03 analysis export and smoke submission

- The run-metrics CSV now exports syncer merge-compute p95, duty-cycle percentage, and estimated idle GPU node-hours alongside the E2 matrix fields. Focused analysis tests: 6 passed in 0.13 s; artifact `artifacts/20260718-0800_analysis-matrix-pass.log`.
- Submitted 8-node acceptance smoke job `2405379.opbs`, run `e5_colocated_acceptance_20260718`, from immutable snapshot `/work/xg24i002/x10041/fs_diloco_experiment_snapshots/e5_20260718_0740` after the mandatory PBS static/group-ID gate.

## 2026-07-18 — pre-SNC-03 full repository gate

- Compute-node command: `pytest -q` → 262 passed in 20.91 s; `ruff check fs_diloco scripts/miyabi tests` → all checks passed.
- Environment: Miyabi interactive job `2405387.opbs`, node `mg0005`.
- Artifacts: `artifacts/20260718-0815_full-pytest-pass.log` and `artifacts/20260718-0815_ruff-pass.log`.

## 2026-07-18 — SNC-03 formal deployment decision

- All three dedicated 9-node and colocated 8-node jobs exited zero. Dedicated
  complete-training times were 1129.74/1141.98/1120.80 s (median 1129.74 s);
  colocated times were 1097.80/1150.39/1584.88 s (median 1150.39 s). The frozen
  median regression is +1.83%, within the ≤10% acceptance boundary.
- CPU merge-compute p95 remained below 4 s in every run. Seed 4049 nevertheless
  showed a +41.4% walltime outlier together with heterogeneous learner progress
  and lower selected counts. The 8-node launcher is therefore a valid
  experimental/capacity-saving variant, but the lower-variance dedicated
  9-node topology remains the operational default.
- Evidence: `artifacts/20260718-1500_snc03-formal-pairs.csv`. SNC-01 through
  SNC-03 are complete; the result is limited to the current GPT-2 124M scale.
