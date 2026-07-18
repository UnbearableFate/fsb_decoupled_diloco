# E6 failures

## 2026-07-18 — APT RED: adoption pause is not attributable

- Command: `pytest -q tests/test_adoption_telemetry.py`
- Result: expected RED, 3 failed.
- Failure signatures: full adoption outcomes do not carry load/apply timing,
  fragment events omit both timing components, and analysis has no per-learner
  pause aggregation or completed-cycle denominator.
- Evidence: `artifacts/20260718-0101_adoption-telemetry-red_fail.log`.

## 2026-07-18 — APT tiny analysis admitted a repeated CSV header

- Command: full tiny run `e6_full_tiny_20260718_0128`, followed by
  `python -m fs_diloco.analysis ... --json`.
- Training completed, but two learner processes concurrently initialized the
  shared CSV and left a repeated header row. Analysis treated its literal
  `learner_id` cell as a learner and emitted a spurious summary entry.
- Resolution: make the generic CSV reader discard embedded repeated headers,
  then rerun analysis/tests and both tiny profiles.
- Evidence: `artifacts/20260718-0128_full-tiny_pass.log` and the run-local
  `metrics/learner_metrics.csv`.
