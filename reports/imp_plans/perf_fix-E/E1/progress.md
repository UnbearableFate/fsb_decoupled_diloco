# E1 implementation progress

## 2026-07-17 — plan audit

- Corrected the BF16 experiment: the existing `*_bf16all_*` profile changes compute dtype/device as well as publish dtype and is not a valid single-variable publish comparison. The final matrix must keep compute/device/io fixed, vary only `syncer.publish_dtype`, use identical source fingerprints, and cover at least three seeds.
- Telemetry now requires separate worker durations, concurrent checkpoint walltime, bytes/dtype, and Q6 round-trip error. The DB/latest commit remains main-thread-only after both I/O futures succeed; failpoints must cover both single-sided completion orders.

## 2026-07-18 — PIO parallel publication implementation verified

- Full publication now starts weight and outer-state atomic writes in two bounded
  workers. The main thread waits for both results before the sole SQLite
  transaction and `latest.json` replacement; workers never touch DB/latest.
- CSV, W&B, and `global_published` now expose per-worker seconds, concurrent
  checkpoint walltime, dtype, per-file bytes, and pre-alignment publication-cast
  L2/L∞/relative-L2 error. Error computation is chunked to avoid another
  model-sized working vector.
- Focused compute-node tests: 10 passed across parallel completion/failure,
  syncer runtime, and tensor codec cases. Evidence:
  `artifacts/20260718-0224_parallel-publication-focused_pass.log`.
- The complete six-failpoint matrix passed 60/60 recovery cases (10 iterations
  each). Transaction-predecessor crashes recovered from DB v0; DB/latest-side
  crashes recovered from committed v1; every case converged exactly once to v2.
  Evidence: `artifacts/20260718-0238_crash-matrix-full_pass.log`.
- Full tiny run `e1_parallel_tiny_20260718_0231` completed normally with all
  fields visible. Its measured checkpoint walltime was 5.23 ms versus 4.77 ms
  weight and 4.61 ms outer workers, satisfying the concurrent-walltime bound.
  Evidence: `artifacts/20260718-0231_parallel-tiny_pass.log`.
- The ≥3-seed 9-node FP32/BF16 publish-only comparison remains gated on the Q4
  validation evaluator and Q6 acceptance calculation; no dtype default changed.

## 2026-07-18 — PIO-02/L3 formal experiment controls

- Added a default-true `syncer.parallel_checkpoint_writes` control. False restores serial weight→outer file writes solely for a same-source timing ablation; both modes retain the identical DB/latest commit boundary.
- Added explicit CLI/resolved-config propagation and a narrow 50×10 full launcher override for seed and serial/parallel mode. Unit coverage proves serial ordering and DB-after-both behavior; default parallel failure/crash tests remain unchanged.
- Focused gate: 84 passed (`artifacts/20260718-1430_serial-experiment-green.log`). Full gate: 285 passed and ruff clean (`artifacts/20260718-1435_full-pytest-final.log`, `artifacts/20260718-1435_ruff-final.log`).
- Formal serial/parallel three-seed 50×10 submission remains; Q6's FP32/BF16 5000-step train/eval pairs are already queued separately.

## 2026-07-18 — PIO-02/L3 formal submissions

- Froze `perf_ablation_20260718_1445` at stable fingerprint `sha256:dd993230d29e203fc79d7886d86c567a2e8815a0fddd6941d201543ddd582ba3` after full tests/lint and the mandatory PBS syntax/group gate.
- Submitted serial jobs `2405760/62/64` and parallel jobs `2405761/63/65` for seeds 1337/2027/4049 using the same 9-node 50×10 full config. The only resolved experimental difference within each seed is `syncer.parallel_checkpoint_writes`.
- Q6 publish-only valid FP32/BF16 jobs are `2405771,2405635,2405637` versus `2405651/53/55`, with dependent validation; original FP32 seed-1337 `2405633` is excluded by its source fingerprint. Both formal result sets await terminal analysis.

## 2026-07-18 — PIO-02/03/04/05 formal decision

- All six serial/parallel 9-node 50×10 jobs completed from fingerprint
  `sha256:dd993230...`. Parallel publication reduced the three-seed mean
  `global_published` walltime from 0.8412 s to 0.4665 s (-44.5%) and checkpoint
  walltime by about 45.2%. Complete-training time remained noisy and did not
  show a consistent improvement, but the isolated critical-path result matches
  the two-worker timing and all crash/failure-order tests remain green.
- The valid publish-only FP32/BF16 5000-step comparison also completed from one
  fingerprint and protocol. BF16 halved weight and outer checkpoint bytes, but
  mean publish walltime increased from 0.4118 s to 0.6690 s (+62.5%) and mean
  complete-training time increased 2.01%. Q6 quality passed, so BF16 remains a
  capacity-oriented opt-in rather than the default; FP32 remains the default.
- Evidence: `artifacts/20260718-1605_pio-formal-matrix.csv`,
  `artifacts/20260718-1610_publish-dtype-formal-matrix.csv`, and Q6's corrected
  formal gate. PIO-01 through PIO-05 are complete; parallel checkpoint writes
  remain enabled by default.
