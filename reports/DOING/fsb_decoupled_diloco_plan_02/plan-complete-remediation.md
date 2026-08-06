# Plan 02 current-state and late Phase 2 review remediation

## Review set

- Phase 2 supplemental Codex report: `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-2/gpt-5.6-sol_180a243256798565bafd385467830a66b8d724c9.md`. It reviewed the cumulative Phase 2 remediation/cleanup surface and returned `CHANGES_REQUIRED` for one High and two Medium findings. Although concurrent review publication caused this report's range to overlap the later continuous reports, its findings are accepted and retained; they are not discarded because of the bookkeeping overlap.
- Plan-complete continuity base: `68fdb0ab538b56bb2e99245fb55c1ff3f3c9d364`.
- Full current-state target: `550296db7eab0dbcc2abcb4d124c81acd162fd8c`.
- Plan-complete Codex reports: primary `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/plan-complete/gpt-5.6-sol_550296db7eab0dbcc2abcb4d124c81acd162fd8c.md` and append-only supplement `gpt-5.6-sol_550296db7eab0dbcc2abcb4d124c81acd162fd8c-retry1.md`; together they return `CHANGES_REQUIRED` for one High and three Medium findings.
- External reviewer: `skipped-session-limit`. The required plan-complete invocation requested `claude-opus-5` with fresh session `915d9f41-d440-41a1-b91d-471b03d07d8f`; its JSON returned HTTP 429, zero model usage, and the explicit reset-at-06:00 Asia/Tokyo session-limit message. No report was fabricated and the skip does not block Codex remediation.

## Finding dispositions and ordered remediation

### High — cleanup removes the authoritative update archive

- Disposition: **fixed**.
- Evidence: `archive_and_prune()` fsyncs terminal update rows to `metrics/update_history.jsonl` before pruning them from SQLite, but the cleaner classified that history as raw telemetry. The already completed main G9 cleanup therefore irreversibly deleted this 2,563,263-byte archive; the retained completed artifact proves the pre-cleanup result, while the coherent detached formal run remains available for detailed rechecking.
- RED: `test_clean_run_preserves_authority_and_one_learner_log` now requires the update archive to remain outside the deletion inventory. PBS `2501974.opbs` failed before the fix because the archive was still selected.
- Fix: remove `update_history.jsonl` from cleanup candidates; document preservation of all fsync-before-prune histories and narrow deletable telemetry to reconstructable learner metrics/update manifest.
- Verification: PBS `2501991.opbs` passed the 20-test cleanup/remediation/analysis group in `4.00s`; PBS `2501992.opbs` passed all 495 repository tests in `25.23s`. A corrected dry-run against completed G8 `plan02_phase2_g8_2501754` produced five candidates but no `metrics/update_history.jsonl` candidate, and the 26,811-byte archive remained present. No further deletion was used to validate this fix.

### Medium — direct PASS evidence is not terminal-version-bound

- Disposition: **fixed**.
- Evidence: direct evidence previously checked run/descriptor/source identity but did not compare its terminal version to the current stop/summary.
- RED: parameterized missing/stale terminal bindings in `test_clean_run_requires_evidence_for_current_terminal_version`; both were accepted before the fix in PBS `2501974.opbs`.
- Fix: recognize the direct Checker schemas' top-level `terminal.final_version`, `authority.terminal.final_version`, or `authority.final_version`; reject missing, invalid, conflicting, or nonmatching versions. Matched evidence retains its branch-summary check.
- Verification: missing/stale evidence cases and the current completed-artifact dry-run passed in PBS `2501991.opbs`; the G8 dry-run also proved the direct `authority.final_version=12` schema remains accepted when it matches the current summary.

### Medium — atomic merge observation can describe unrelated contributors

- Disposition: **fixed**.
- Evidence: the fenced merge boundary required exact kind/key/version but did not tie observation contributor IDs/count to `selected_updates`.
- RED: missing contributor, duplicate contributor, and eligible-count mismatch cases in `test_merge_and_capacity_observation_share_one_rollback_boundary`; the first malformed call committed before the fix in PBS `2501974.opbs`.
- Fix: require nonempty unique selected-update incarnation IDs, unique observation IDs with the same set, and `eligible_contributors == len(selected_updates)` before opening the merge mutation.
- Verification: all malformed contributor cases and the existing positive atomic merge passed in PBS `2501991.opbs`; the complete suite passed in `2501992.opbs`. The runtime already constructs the exact payload, so no format or merge-math change occurred.

### Medium — analysis authority database is opened read/write

- Disposition: **fixed**.
- Evidence: `fs_diloco.tools.analysis._db_summary()` used raw `sqlite3.connect(path)` rather than the shared enforced read-only opener.
- RED: `test_analysis_opens_authority_database_query_only` spies on the opener, requires `PRAGMA query_only=1`, verifies DDL is rejected, and preserves normal summary behavior. PBS `2501969.opbs` failed before the fix because the analysis module had no `open_readonly` symbol.
- Fix: use `storage.schema_bootstrap.open_readonly()` in `_db_summary()`.
- Verification: the full fragment-analysis module passed in PBS `2501971.opbs` (`2 passed in 0.16s`), and the final complete suite including all cleanup/observation additions passed in `2501992.opbs` (`495 passed in 25.23s`).

No other Critical, High, Medium, or Low finding was produced by the available current-state report. `LeaderLeaseStore.assert_current()` is retained as a non-mutating diagnostic consistent with the active `LeaseSafetyTracker`, not as a competing writer boundary.

## Closure sequence

1. Freeze the four fixes, tests, documentation, reports, and append-only failure records in one review target.
2. Run Python compilation, focused lint/format checks, `git diff --check`, PBS syntax/literal-group checks, the affected compute-node test group, and the complete compute-node suite.
3. Dry-run the corrected cleaner against a retained completed run and confirm `metrics/update_history.jsonl` is absent from the inventory.
4. Record all four dispositions as `fixed`, including the irreversible main-G9 archive loss and the still-auditable detached formal evidence.
5. Perform a continuous incremental Codex review from `550296db7eab0dbcc2abcb4d124c81acd162fd8c` to the remediation target; invoke the required external reviewer once after saving the Codex report.

## Validation update

All four dispositions above are now **fixed and validated**. The pre-fix RED jobs were `2501969.opbs` and `2501974.opbs`; the affected group passed as `2501991.opbs` (`20 passed`), and complete-tree jobs `2501992.opbs` and `2501995.opbs` each passed all `495` tests. Static gates and a corrected G8 cleanup dry-run also passed; the dry-run retained `metrics/update_history.jsonl`. Only the continuous incremental review remains open.

The subsequent formatting-only target `d114b51cf6b44c32bac2e4d4d5e16824676618de` received an append-only review correction after its first report overstated the static result. Its Low Ruff finding is **fixed**: constants imports again precede declarations, the query helper is a typed local function, and the unused `json` import is absent. Ruff lint/format and Python compilation pass, and final full PBS `2502021.opbs` passed all 495 tests in 24.57 seconds. Because this is a lint-only correction with no behavior change, no behavioral RED was required.

Append-only review correction for formatting target `d114b51` produced one Low static finding; it is **fixed and validated** by full-source Ruff lint, format checking of the three files, Python compilation, and full PBS `2502021.opbs` (`495 passed`).

## Final review disposition

Continuous Codex review `gpt-5.6-sol_92ccb5e9ddfc73ad6a92676fb472acd3e3544f1d.md` covers `d114b51cf6b44c32bac2e4d4d5e16824676618de..92ccb5e9ddfc73ad6a92676fb472acd3e3544f1d` and returns **APPROVE** with no remaining finding. The required canonical `claude-opus-5` invocation used fresh session `befcf31c-16d5-4cb0-a45a-618d0f0b151f` and returned HTTP 429 with zero model usage and the explicit 06:00 Asia/Tokyo reset message; disposition is `skipped-session-limit` and no report was fabricated. All plan-complete and correction findings are fixed, validated, and continuously reviewed.

## Review closure

Final continuous Codex review `d114b51cf6b44c32bac2e4d4d5e16824676618de..92ccb5e9ddfc73ad6a92676fb472acd3e3544f1d` returned **APPROVE** with no findings. The required `claude-opus-5` invocation and a concurrently launched continuation invocation used fresh sessions `3a8ce2bb-ae81-4fb3-8dc7-6d07804e1787` and `9ffc2980-403b-4e14-9a4c-aca710843876`; both returned zero-usage HTTP 429 session-limit results with the explicit 06:00 Asia/Tokyo reset message before either continuation observed the other's result. The external reviewer is `skipped-session-limit`, no further retry is made, and neither invocation produced a report. No remediation or review gate remains open.

Concurrency audit correction: sessions `befcf31c-16d5-4cb0-a45a-618d0f0b151f`, `3a8ce2bb-ae81-4fb3-8dc7-6d07804e1787`, and `40ed395f-bb6f-4d61-ac26-3b9a5eec72d2` were overlapping invocations by workspace processes that had not yet observed one another's result. All returned the same zero-usage HTTP 429 session-limit condition, no Claude report was produced, and no further invocation is made. This does not change the `skipped-session-limit` disposition or the final Codex approval.
