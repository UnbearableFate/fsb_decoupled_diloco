# E3 failures

## 2026-07-18 — MAT RED: interval and terminal materialization contract absent

- Command: `pytest -q tests/test_fragment_materialization.py`
- Result: expected RED, 4 failed and 1 passed.
- Failure signatures: fragment mode accepts `null`, zero, and negative
  materialization intervals; `publish_fragment_latest()` has no forced terminal
  materialization path or structured materialization telemetry.
- Evidence: `artifacts/20260718-0002_fragment-materialization-red_fail.log`.
