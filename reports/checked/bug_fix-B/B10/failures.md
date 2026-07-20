# B10 failures

## 2026-07-17 — MCA-01/02 expected RED

- Command: `pytest -q tests/test_midcycle_adoption_metadata.py tests/test_sqlite_store.py`
- Result: collection failed because `MidCycleAdoptionTracker` does not exist yet (`artifacts/20260717-mca-red.log`).
- Classification: expected RED, attempt 1. The test fixes the interval reset/count contract before implementation.
