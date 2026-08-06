# Plan 02 current-state and late Phase 2 review remediation

## Review set

- Phase 2 supplemental Codex report: `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-2/gpt-5.6-sol_180a243256798565bafd385467830a66b8d724c9.md`. It reviewed the cumulative Phase 2 remediation/cleanup surface and returned `CHANGES_REQUIRED` for one High and two Medium findings. Although concurrent review publication caused this report's range to overlap the later continuous reports, its findings are accepted and retained; they are not discarded because of the bookkeeping overlap.
- Plan-complete continuity base: `68fdb0ab538b56bb2e99245fb55c1ff3f3c9d364`.
- Full current-state target: `550296db7eab0dbcc2abcb4d124c81acd162fd8c`.
- Plan-complete Codex report: `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/plan-complete/gpt-5.6-sol_550296db7eab0dbcc2abcb4d124c81acd162fd8c.md`, decision `CHANGES_REQUIRED` for one Medium finding.
- External reviewer: `skipped-session-limit`. The required plan-complete invocation requested `claude-opus-5` with fresh session `915d9f41-d440-41a1-b91d-471b03d07d8f`; its JSON returned HTTP 429, zero model usage, and the explicit reset-at-06:00 Asia/Tokyo session-limit message. No report was fabricated and the skip does not block Codex remediation.

## Finding dispositions and ordered remediation

### High — cleanup removes the authoritative update archive

- Disposition: **fixed**, subject to the frozen-target validation below.
- Evidence: `archive_and_prune()` fsyncs terminal update rows to `metrics/update_history.jsonl` before pruning them from SQLite, but the cleaner classified that history as raw telemetry. The already completed main G9 cleanup therefore irreversibly deleted this 2,563,263-byte archive; the retained completed artifact proves the pre-cleanup result, while the coherent detached formal run remains available for detailed rechecking.
- RED: `test_clean_run_preserves_authority_and_one_learner_log` now requires the update archive to remain outside the deletion inventory. PBS `2501974.opbs` failed before the fix because the archive was still selected.
- Fix: remove `update_history.jsonl` from cleanup candidates; document preservation of all fsync-before-prune histories and narrow deletable telemetry to reconstructable learner metrics/update manifest.
- Verification: focused cleanup/remediation tests, full repository suite, and a dry-run against a completed retained run proving the archive is not a candidate. No further deletion is needed to validate this fix.

### Medium — direct PASS evidence is not terminal-version-bound

- Disposition: **fixed**, subject to validation.
- Evidence: direct evidence previously checked run/descriptor/source identity but did not compare its terminal version to the current stop/summary.
- RED: parameterized missing/stale terminal bindings in `test_clean_run_requires_evidence_for_current_terminal_version`; both were accepted before the fix in PBS `2501974.opbs`.
- Fix: recognize the direct Checker schemas' top-level `terminal.final_version`, `authority.terminal.final_version`, or `authority.final_version`; reject missing, invalid, conflicting, or nonmatching versions. Matched evidence retains its branch-summary check.
- Verification: focused cleanup tests and a current completed-artifact dry-run.

### Medium — atomic merge observation can describe unrelated contributors

- Disposition: **fixed**, subject to validation.
- Evidence: the fenced merge boundary required exact kind/key/version but did not tie observation contributor IDs/count to `selected_updates`.
- RED: missing contributor, duplicate contributor, and eligible-count mismatch cases in `test_merge_and_capacity_observation_share_one_rollback_boundary`; the first malformed call committed before the fix in PBS `2501974.opbs`.
- Fix: require nonempty unique selected-update incarnation IDs, unique observation IDs with the same set, and `eligible_contributors == len(selected_updates)` before opening the merge mutation.
- Verification: focused remediation tests, full suite, and existing formal positive-path evidence. The runtime already constructs the exact payload, so no format or merge-math change is expected.

### Medium — analysis authority database is opened read/write

- Disposition: **fixed**, subject to validation.
- Evidence: `fs_diloco.tools.analysis._db_summary()` used raw `sqlite3.connect(path)` rather than the shared enforced read-only opener.
- RED: `test_analysis_opens_authority_database_query_only` spies on the opener, requires `PRAGMA query_only=1`, verifies DDL is rejected, and preserves normal summary behavior. PBS `2501969.opbs` failed before the fix because the analysis module had no `open_readonly` symbol.
- Fix: use `storage.schema_bootstrap.open_readonly()` in `_db_summary()`.
- Verification: full fragment-analysis module and full repository suite.

No other Critical, High, Medium, or Low finding was produced by the available current-state report. `LeaderLeaseStore.assert_current()` is retained as a non-mutating diagnostic consistent with the active `LeaseSafetyTracker`, not as a competing writer boundary.

## Closure sequence

1. Freeze the four fixes, tests, documentation, reports, and append-only failure records in one review target.
2. Run Python compilation, focused lint/format checks, `git diff --check`, PBS syntax/literal-group checks, the affected compute-node test group, and the complete compute-node suite.
3. Dry-run the corrected cleaner against a retained completed run and confirm `metrics/update_history.jsonl` is absent from the inventory.
4. Record all four dispositions as `fixed`, including the irreversible main-G9 archive loss and the still-auditable detached formal evidence.
5. Perform a continuous incremental Codex review from `550296db7eab0dbcc2abcb4d124c81acd162fd8c` to the remediation target; invoke the required external reviewer once after saving the Codex report.
