# Q3 failures

## 2026-07-17 — DSH RED: shuffle API not implemented

- Command: `pytest -q tests/test_data_shuffle.py`
- Result: expected RED, 3 failed.
- Failure signature: `_batched_blocks()` rejected the new `shuffle`, `seed`, and
  `learner_index` keyword arguments, confirming that deterministic epoch
  permutation and the legacy opt-out path are not implemented yet.
- Evidence: `artifacts/20260717-2358_dsh-red_fail.log`.
