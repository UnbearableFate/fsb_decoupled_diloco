# Q6 failures

## 2026-07-18 — plan scope omitted the executable gate

- The initial scope called Q6 a protocol while declaring all code out of scope,
  yet QGB-03 requires a reproducible three-state calculation over paired runs.
  A prose-only decision would not be fail closed or auditable.
- Corrected the plan so publication telemetry remains E1-owned while Q6 owns
  the pure gate and run-root validator. This did not expand into publication
  runtime behavior.

## 2026-07-18 — QGB trend criterion rejected beneficial negative slopes

- The first formal gate artifact (`artifacts/20260718-1515_qgb03-formal-gate.json`) reported `FAIL` even though all three paired validation losses passed and every round-trip relative-L2 trend was significantly decreasing.
- Root cause: the frozen implementation required a two-sided slope interval to contain zero. That rejects both confidently increasing error (the intended risk) and confidently decreasing error (the observed safe direction).
- Plan correction: the trend interval must not be wholly positive (`slope_ci95_low <= 0`), while retaining the independent second-half/first-half ratio limit of 1.25. This accepts a flat/uncertain or decreasing trajectory and still fails statistically supported cumulative growth.
- The default remains float32 until the corrected executable gate and full evidence audit pass.

## 2026-07-18 — QGB acceptance calculator RED (attempt 1)

- Command: `pytest -q tests/test_publish_quality_gate.py` on Miyabi job `2405507.opbs`, node `mg0016`.
- Expected RED: no executable three-state Q6 gate exists yet.
- Actual: collection failed with `ModuleNotFoundError: fs_diloco.tools.publish_quality_gate`.
- Frozen contract: `epsilon=max(0.01, sample_sd(fp32 losses))`, paired mean/worst-seed loss limits, minimum three matched seeds, and per-seed round-trip slope CI/half-ratio boundedness.
- Evidence: `artifacts/20260718-1035_qgb-red-fail.log`.
- Next implementation: add the pure gate/trend calculator first, then add a run-root CLI that validates source/protocol/config pairing before consuming results.
