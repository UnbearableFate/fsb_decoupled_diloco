# E4 failures

## 2026-07-18 — FDX RED: fragment discovery remains payload-glob based

- Command: `pytest -q tests/test_fragment_pointer_discovery.py`
- Result: expected RED, 3 failed.
- Failure signatures: no per-learner/per-fragment pointer path exists, fragment
  insertion accepts no pointer/frontier, and fixed-surface ingestion cannot be
  constructed or signature-cached.
- Evidence: `artifacts/20260718-0315_fragment-pointer-red_fail.log`.

## 2026-07-18 — FDX focused tests retained invalid multi-pending fixtures

- Command: fragment pointer/store/retention/selection focused group.
- Result: 25 passed, 2 failed.
- Failure signature: two legacy store tests inserted multiple pending updates for
  the same `(learner, fragment)` pair. Transactional latest-wins now correctly
  supersedes the first row, so those fixtures no longer represented the
  staleness and shutdown conditions they intended to test.
- Resolution: keep those concerns but use distinct learner/fragment pairs, then
  rerun the focused group.
- Evidence: `artifacts/20260718-0343_fragment-pointer-focused.log`.
