# Independent Codex current-state review supplement — Plan 02

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Continuity base: `68fdb0ab538b56bb2e99245fb55c1ff3f3c9d364`
- Current-state target: `550296db7eab0dbcc2abcb4d124c81acd162fd8c`
- Scope: the same all-66-file `fs_diloco/` current state as the primary report; this append-only supplement records findings exposed by a second independent pass over cleanup and capacity-observation boundaries.

The primary report remains immutable and its Medium analysis finding remains accepted. The following additional findings apply to the same frozen target.

## Additional findings

### High — cleaner deletes the authoritative update archive

`fs_diloco/tools/clean_run.py:249-256` classifies `metrics/update_history.jsonl` as disposable raw telemetry. In fact, `storage/maintenance.py` appends terminal update rows there and fsyncs them before pruning the active SQLite rows; the completed Checker and analysis reconstruct the complete update set from SQLite plus that archive. Deleting it makes the retained authority database insufficient to rerun the update-level completion checks.

The completed locked-environment G9 cleanup manifest confirms that this path already deleted `metrics/update_history.jsonl` (2,563,263 bytes). That loss is not recoverable from the cleaned run and must be recorded explicitly. The separately retained clean-worktree formal G9 run still contains its 2,572,977-byte update archive and remains independently auditable.

Fix: always retain fsync-before-prune histories, remove `update_history.jsonl` from cleanup candidates, add execute-time preservation coverage, and correct operations/tool documentation.

### Medium — direct completion evidence is not bound to the current terminal version

`_matching_pass_evidence()` validates final version for matched static/dynamic evidence but direct PASS evidence is accepted using only status, run root, run ID, descriptor and source identity. A stale direct artifact for the same run can therefore authorize destructive cleanup after the terminal summary changes.

Fix: recognize the supported direct schemas (`authority.terminal.final_version`, `authority.final_version`, or an explicit top-level terminal object), require exactly one consistent version, compare it with the current summary, and fail closed for missing, malformed or mismatched bindings. Add negative tests for missing and stale versions.

### Medium — merge observation contributors are not tied to the committed updates

`FencedSQLiteStore.commit_full_merge()` validates atomic observation kind/key/version but not `eligible_contributors` or `selected_instance_ids` against `selected_updates`. A malformed internal caller can commit valid model state while updating capacity hysteresis and per-instance contribution state for a different set.

Fix: require a nonempty unique selected-update instance list, require a unique observation list with the exact same identities, and require the eligible count to equal the selected-update count. Add missing, duplicate and count-mismatch RED cases at the fenced boundary.

## Final decision

**CHANGES_REQUIRED**

Together with the primary report, the target has one High and three Medium accepted findings. All require remediation and focused/full verification. Cleanup history loss must be recorded honestly; it cannot be repaired by changing the cleaner.
