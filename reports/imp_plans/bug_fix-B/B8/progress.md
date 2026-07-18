# B8 implementation progress

## 2026-07-17 — L0 decision and RED

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; current worktree includes completed B1–B7 changes and preserved pre-existing user edits.
- The default is frozen to `max(120, 2 * heartbeat_interval_seconds)` and an explicit positive `liveness.learner_shutdown_timeout_seconds` overrides it. Cycle duration is present only on transient update rows/selected summaries and is not a reliable shutdown-wide estimate without a new state pipeline, so it is intentionally not added to the formula.
- SHT-01/02/04 RED covers the pure formula, nullable field validation, deterministic timeout with injected monotonic clock, and unconfirmed learner details including missing expected learners. The legacy `min(120, ...)` remains as the defining RED defect.

## 2026-07-17 — L1/L2 timeout and diagnostics GREEN

- Added the nullable liveness field, fail-closed positive validation, and pure default/override formula. Removed the 120-second cap; normal default is now at least 120 seconds and grows to two heartbeat intervals.
- Timeout events now carry a stable expected-ID-ordered `unconfirmed_learners` array with current status, classification reason, and last-seen heartbeat timestamp; expected learners never observed are explicit `unknown` entries.
- Compute-node command: `pytest -q tests/test_syncer_selection.py tests/test_config.py` — **82 passed** in 1.72 s (`artifacts/20260717-sht-green-attempt2.log`). This covers SHT-01/02/04. The corrected fixture expectation after one failed attempt is documented in `failures.md`.

## 2026-07-17 — L3 no-regression and completion

- SHT-03 normal one-learner replace pipeline reached v4, stopped by `stop_after_outer_steps`, confirmed all learners stopped, emitted no shutdown-timeout event, and passed the full invariant checker (`artifacts/normal-run/`, `artifacts/20260717-normal-checker.log`).
- A semantic trace comparison against the pre-B8 replace run matched all four outer steps (one update/64 tokens), stop publication, stopped confirmation, summary completion, and process exit (`artifacts/20260717-normal-semantic-compare.log`). The strict proposal-ID comparison's timing-sensitive local-step difference is preserved in `failures.md`.
- Full compute-node suite: **226 passed** in 13.59 s (`artifacts/20260717-full-pytest.log`). SHT-04 grep, `ruff check`, `py_compile`, and `git diff --check` passed.
- Documentation records the default formula, explicit large-model override, timeout diagnostics, and the unchanged safe no-finalize behavior. SHT-01 through SHT-04 are complete.
