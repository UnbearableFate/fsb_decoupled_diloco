# B5 implementation progress

## 2026-07-17 — L0 plan audit and baseline

- Baseline commit is `c359b8322c33e0101328c2fc8522271691f1e52c`; B1–B3/B2 work and pre-existing user edits make the current worktree dirty.
- The plan was corrected before implementation to distinguish the SQLite transaction from the non-atomic JSONL fsync, and to use deterministic scanned-row bounds rather than noisy wall-time trends as MNT-04's hard gate.
- Current semantics were re-derived: update-history scanning exists only to remember terminal payload paths across an archive-commit/unlink crash. The replacement therefore stages those paths in `gc_pending` in the same SQLite transaction that removes archived active rows.
- Pre-change retention + 1000-cycle baseline passed on PBS `2404379.opbs`, node `mg0041`; its log is archived as `artifacts/20260717-b5-baseline.log`. RED tests cover transactional rollback, idempotent persistence/reopen, crash recovery, telemetry fields, and bounded scan rows.

## 2026-07-17 — L1–L4 gc_pending implementation, MNT-01–05

- Added the idempotent `gc_pending(file_path PRIMARY KEY, archived_at)` schema and connect-time creation for existing run DBs. `delete_archived_rows` stages resolved terminal payload paths in the same `BEGIN IMMEDIATE` transaction as active-row deletion; trigger-injected delete failure proves both operations roll back together (MNT-01).
- Runtime GC now reads only the bounded `gc_pending` set, deletes/recognizes missing terminal files, and clears successfully handled paths. Reopen/idempotency and archive-before-unlink crash recovery tests pass (MNT-02/03). `_archived_terminal_paths` is deleted; `update_history_jsonl` remains write-only append evidence in maintenance (MNT-05).
- Maintenance events and full/fragment syncer CSV metrics expose `gc_pending_rows`, `maintenance_scanned_rows`, and `maintenance_scan_seconds`; the invariant checker requires terminal `gc_pending` emptiness when the table exists.
- Retention + 1000-cycle group passed: 6 tests in 4.53s versus the pre-change 4-test baseline's 34.82s. Every 1000-cycle iteration scanned at most four pending rows even as archive history reached 4000 updates/1000 versions, and terminal pending count stayed zero. Artifact: `artifacts/20260717-mnt-green-attempt1.log`; PBS `2404379.opbs`, node `mg0041`.
- Static ruff/diff checks pass. Remaining B5 gates: current full suite plus tiny run/checker with telemetry inspection.

## 2026-07-17 — B5 pipeline and closeout

- One-node/one-learner full tiny run completed normally at authoritative v1 with `input_exhausted`; the first checker invocation incorrectly expected the two-learner config's v2 target and is documented in `failures.md`. Rerun against summary v1 returned PASS (`artifacts/checker_tiny_attempt2.txt`).
- The tiny syncer CSV/event recorded `maintenance_scanned_rows=1`, `gc_pending_rows=0`, and finite scan duration; terminal SQLite query returned zero pending rows. Raw evidence: `artifacts/tiny_run` and `artifacts/20260717-b5-tiny.log`.
- Current full compute suite passed: 209 tests in 11.45s (`artifacts/20260717-full-pytest.log`). Ruff, diff check, MNT-05 identifier removal, docs, and checker synchronization all pass.
- B5 completion predicate is satisfied for MNT-01–05. JSONL is now append-only research evidence, runtime scan cost is O(pending) rather than O(history), crash recovery is persistent, and terminal `gc_pending=0` is machine-checked. No 2/9-node gate is required.
