# B10 implementation progress

## 2026-07-17 — L0 storage audit and MCA-01/02 RED

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; current worktree includes completed B1–B9 changes and preserved pre-existing user edits.
- Confirmed the full proposal metadata is an atomically replaced `updates/latest/learner_*.json` pointer, `insert_update_metadata` uses an explicit SQL column whitelist, and `update_history.jsonl` is generated from terminal SQLite rows. Therefore durable analysis requires two `updates` columns plus an idempotent connect-time migration; `fragment_updates` remains unchanged.
- Frozen ownership: a full-runner interval tracker is reset at the beginning of every cycle, counts only successful replace-strategy inner-poll adoptions, records the most recent interval-local completed step, and is snapshotted into `write_update`. Cycle-end/post-publish adoption and fragment/rebase/predict paths are excluded.
- MCA-01/02 were added first: they require count/latest-step semantics, explicit reset after an upload-skip-shaped boundary, pointer fields always present at `0`/`null`, and SQLite persistence. The current implementation has no tracker or schema fields, so test collection is expected to fail before implementation.

## 2026-07-17 — L1 interval metadata and durable ingest GREEN

- Added `MidCycleAdoptionTracker`, reset it at the beginning of every full learner cycle, and record only successful replace-strategy inner-poll adoption after the corresponding local step completes. Each adoption produces a `mid_cycle_global_adopted` event with interval step and cumulative interval count; cycle-end and post-publish actions cannot enter this tracker.
- `write_update` now requires an explicit interval snapshot, validates count/step consistency, and always writes `mid_cycle_adoption_count` plus `base_switched_at_step`; `update_written` repeats the snapshot for direct event correlation.
- Added compatible `updates` columns (`count NOT NULL DEFAULT 0`, nullable switch step), connect-time migration for pre-B10 databases, explicit ingest whitelist entries, and no fragment schema changes. Archive rows inherit both values because maintenance serializes DB rows.
- MCA-01/02 compute-node command: `pytest -q tests/test_midcycle_adoption_metadata.py tests/test_sqlite_store.py` — **11 passed** (`artifacts/20260717-mca-green-attempt2.log`), including an old-schema migration probe.

## 2026-07-17 — L2 MCA-03/04 integration and completion

- Added analysis aggregation over live plus archived full rows (`proposals_with_adoption`, total adoption count, switch-step values) and made all three mid-cycle event fields explicit in trace comparison profiles. The merge/staleness implementation remains untouched.
- MCA-04 ran the new one-learner `fs_diloco_tiny_midcycle_replace_local` fixture (replace + inner poll, 16-step intervals) to v4. Six proposals were published, four inner-poll adoptions occurred, every `update_written` snapshot matched the preceding interval events, three adopted proposals were durably ingested/archived, and the analysis count matched those archived rows (`artifacts/midcycle-run/`, `artifacts/20260717-midcycle-{run,assertions}.log`). The fourth adopted pointer was validly overwritten by a later final pointer after syncer stop before ingestion; learner event correlation still proves its published metadata snapshot.
- MCA-03 reran the no-poll scheduler/replace fixture to v4. Version, total tokens (256), stop reason, selected-count distribution, and applied count matched the pre-B10 B8 semantic baseline; every pointer/event/archive mid-cycle value was explicitly `0/null`, with no `mid_cycle_global_adopted` event (`artifacts/no-poll-run/`, `artifacts/20260717-no-poll-{run,assertions}.log`).
- Targeted merge/storage/analysis/adoption regression: **36 passed** (`artifacts/20260717-targeted-regression.log`). Full compute-node suite: **232 passed** in 12.73 s (`artifacts/20260717-full-pytest.log`). `ruff check`, `py_compile`, and `git diff --check` passed.
- Documentation now defines pointer and SQLite fields, connect-time migration, interval reset/event semantics, the single-base staleness approximation, and analysis output. MCA-01 through MCA-04 are complete.
