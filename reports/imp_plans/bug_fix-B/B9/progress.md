# B9 implementation progress

## 2026-07-17 — L0 clock-domain audit and RED

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; current worktree includes completed B1–B8 changes and preserved pre-existing user edits.
- The existing SQLite `ingested_at` was confirmed but rejected for deadline arithmetic because it is wall clock and can cross syncer restarts/nodes. The frozen design is a per-syncer-process insertion-ordered registry storing first-seen monotonic and wall timestamps; repeated ingestion never refreshes it, resume rows have no entry and conservatively disable ETA shortening.
- Capacity is `max(64, 4 * num_learners * max(1, num_fragments))`; applied/known-dropped IDs are discarded and FIFO eviction only suppresses an estimate. `committed_at` remains research metadata but never participates in adaptive deadline arithmetic.
- ETA-01/02/04 RED updates existing ETA tests to inject ±large learner wall-clock values, require invariant output from syncer monotonic first-seen, and require stable/bounded/discardable lifecycle. The current implementation lacks the registry and still subtracts `committed_at` from syncer wall time.

## 2026-07-17 — L1 monotonic registry and ETA GREEN

- Added a bounded process-local first-seen registry with immutable monotonic/wall timestamps, full learner/fragment identity, FIFO capacity eviction, explicit discard, and config-derived capacity. Metadata insertion observes only newly inserted DB rows; duplicate ingest preserves the first timestamp, full supersession removes the previous learner entry, and resume-existing rows remain unregistered/conservative.
- Adaptive ETA now uses `first_seen.monotonic + measured_cycle_seconds - now_monotonic`. `committed_at` is absent from the calculation path. Registry state is threaded through both syncer loops, grace reingestion, and shutdown ingestion; selected/applied and known missing IDs are discarded.
- Compute-node command: `pytest -q tests/test_syncer_selection.py tests/test_shared_runtime_primitives.py` — **22 passed** (`artifacts/20260717-eta-green-attempt1.log`). ETA-01/02/04 cover ±wall-clock immunity, duplicate/supersession lifecycle, capacity/discard, and unchanged fixed-mode proposal-source behavior.

## 2026-07-17 — L2 adaptive integration and completion

- ETA-03 adaptive two-learner pipeline completed v4 with `stop_after_outer_steps`, no error/no-progress event, four `grace_window_shortened` events, and invariant checker PASS (`artifacts/adaptive-run/`, `artifacts/20260717-adaptive-{run,assertions,checker}.log`). The observed ETA was already elapsed (0 s) in this tiny fast workload, which validly shortens the 10 s initial window without cross-clock arithmetic.
- Full compute-node suite: **227 passed** in 13.72 s (`artifacts/20260717-full-pytest.log`). ETA-04 function-body audit found neither `committed_at` nor `time.time`; `ruff check`, `py_compile`, and `git diff --check` passed.
- Docs now mark learner `created_at`/`committed_at` and syncer `ingested_at` as wall-clock evidence only, describe the process-local monotonic registry/capacity/resume behavior, and define the conservative one-scan observation offset. ETA-01 through ETA-04 are complete.
