# E6 implementation progress

## 2026-07-17 — plan audit

- Froze pause telemetry into load/apply and optimizer-reset segments plus a total. Pure waiting for a future latest is excluded because existing wait events already measure it.
- A cycle-share denominator cannot be inferred from log timestamps. The corrected plan adds explicit completed-cycle elapsed seconds and reports per-learner count/total/mean/share; old runs lacking the fields are unavailable rather than estimated.

## 2026-07-18 — APT implementation and tiny baselines verified

- Full and fragment adoption events now split load/apply and optimizer/scheduler
  reset time and report their sum. Post-publish/final-latest waiting occurs before
  the timed helper/action and is therefore excluded from pause time.
- Full and fragment completed-update events/CSV rows now record
  `local_cycle_elapsed_seconds`. Analysis reports per-learner adoption count,
  total, mean, completed-cycle denominator, and pause fraction; embedded headers
  from concurrent CSV initialization are ignored.
- Focused compute-node verification: 21 passed across telemetry, strategy,
  shared runtime primitive, and fragment analysis tests. Evidence:
  `artifacts/20260718-0140_adoption-telemetry-focused_pass.log`.
- Full tiny baseline (`e6_full_tiny_20260718_0128`): one measured adoption per
  learner, mean pause 0.895–0.953 ms, completed-cycle share 0.272–0.290%.
  Evidence: `artifacts/20260718-0128_full-tiny_pass.log`.
- Fragment tiny baseline (`e6_fragment_tiny_20260718_0135`): four measured
  adoption events per learner, mean pause 1.029–1.067 ms, completed-cycle share
  2.185–2.254%. Evidence:
  `artifacts/20260718-0135_fragment-tiny_pass.log`.
- The prior 9-node 5000-step run
  `codex_predict_full5000_20260717_023302` contains 49–50 adoption events per
  learner but predates both timing fields and the explicit denominator; analysis
  correctly marks all eight learners `unavailable` instead of estimating from
  walltime. Evidence: `artifacts/20260718-0143_prior-9node-analysis.json`.

## 2026-07-18 — APT-03 current 9-node baseline

- The successful same-day 9-node 50×10 run
  `e1_parallel_s1337_20260718` contains the new numerator and denominator fields.
  Every learner recorded 10 adoptions; mean pause was 0.3192–0.3359 s and total
  pause 3.1915–3.3587 s over 233.34–233.90 s of completed-cycle time.
- Per-learner adoption pause share was 1.368–1.436%. This is visible but not a
  dominant walltime component, so E6 creates no new optimization priority.
  Evidence: `artifacts/20260718-1625_nine-node-adoption.json`. APT-01 through
  APT-03 and the no-regression tiny evidence are complete.
