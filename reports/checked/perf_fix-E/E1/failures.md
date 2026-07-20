# E1 failures

## 2026-07-18 — PIO RED: checkpoint writes are serialized

- Command: `pytest -q tests/test_parallel_publication.py`
- Result: expected RED, 3 failed.
- Failure signature: the weight saver blocks at a two-worker barrier because
  `publish_global()` does not start the outer saver until weight completion.
  Consequently the per-file/dtype/bytes/round-trip telemetry and single-side
  failure contract are also absent.
- Evidence: `artifacts/20260718-0205_parallel-publication-red_fail.log`.
