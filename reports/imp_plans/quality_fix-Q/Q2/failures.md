# Q2 failures

## 2026-07-18 — STL-01/02 evidence contract RED (attempt 1)

- Command: `pytest -q tests/test_staleness_evidence.py` on Miyabi job `2405507.opbs`, node `mg0016`.
- Expected RED: the per-merge weighted staleness evidence and observational linker are absent.
- Actual: collection failed importing `merge_staleness_evidence` from the syncer.
- Frozen contract: exact weighted mean, fresh effective-weight mass, count histogram for full/fragment bases, plus an explicitly observational merge→first subsequent learner-loss table and aggregate histogram.
- Evidence: `artifacts/20260718-1125_stl01-02-red-fail.log`.
- Next implementation: add one mode-parameterized helper, wire it into both merge paths/CSV/W&B, and add legacy-safe analysis that never invents missing old-run fields.

## 2026-07-18 — formal override focused test setup (attempt 1)

- Command: focused Q2/Q5/config pytest group on Miyabi job `2405507.opbs`.
- Result: 84 passed, one config test failed before runtime because it requested the
  prediction adoption strategy from the tiny config, whose stepwise polling is
  intentionally disabled. The requested override was therefore an invalid config.
- Evidence capture also initially targeted a nonexistent aggregate artifact directory;
  subsequent runs use the existing per-plan artifact directories.
- Resolution: exercise the prediction override from the 5000-step base configuration,
  which satisfies its required polling/adoption preconditions.
