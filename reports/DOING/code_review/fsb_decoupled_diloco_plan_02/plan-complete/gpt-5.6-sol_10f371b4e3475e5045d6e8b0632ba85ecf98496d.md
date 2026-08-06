# Independent Codex incremental review — Plan 02 final remediation

## Review identity

- Decision: **APPROVE**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Comparison base: `550296db7eab0dbcc2abcb4d124c81acd162fd8c`
- Review target: `10f371b4e3475e5045d6e8b0632ba85ecf98496d`
- Reviewed diff: `550296db7eab0dbcc2abcb4d124c81acd162fd8c..10f371b4e3475e5045d6e8b0632ba85ecf98496d`
- Ancestry: the base is an ancestor of the target.
- Gate role: required incremental review after the plan-complete current-state findings changed persistence/operator safety boundaries.

I saved this report before invoking or reading the external reviewer for this target.

## Scope and method

I reviewed the complete remediation increment across the fenced merge API, cleanup evidence/inventory logic, analysis database opener, tests, operations/tool documentation, immutable review reports, RED/failure records, remediation dispositions, and validation records. I also inspected the incidental formatting/import-only changes in `storage/sqlite_store.py`, `tools/analysis.py`, `tools/phase2_chaos_evidence.py`, and `tools/phase2_test_evidence.py` to distinguish them from behavior changes.

The review traced each accepted finding through its negative test, fixed control/data flow, and final validation:

- terminal update rows move from SQLite to fsynced `metrics/update_history.jsonl` before database pruning, and the cleaner now leaves that authority archive outside every candidate class;
- direct completion evidence must expose one consistent terminal version through a supported terminal/authority shape and that version must equal the current summary, while matched evidence retains its exact branch-summary binding;
- the fenced dynamic merge checks that selected-update instance IDs are nonempty and unique, observation IDs are nonempty and unique with the exact same set, and `eligible_contributors` equals the selected-update count before opening the mutation;
- analysis uses the shared SQLite `mode=ro` opener with `PRAGMA query_only=ON`, retaining the existing row factory and closing the connection in the established `finally` block.

## Findings

No Critical, High, Medium, or Low finding remains in this increment.

### Cleanup and evidence safety

`update_history.jsonl` is no longer selected as raw telemetry. The execution path still writes a new planned manifest, rebuilds the complete plan, compares evidence digest/candidate size/mtime, and checks each regular file immediately before unlink. Missing, invalid, conflicting, or stale direct terminal bindings fail closed. The actual G8 dry-run demonstrates that a supported `authority.final_version` artifact is accepted without including the authority archive in its five resolved candidates.

The documentation and remediation report accurately record the historical limitation: the old main-G9 cleanup already deleted its 2,563,263-byte update archive and that loss cannot be repaired. The pre-cleanup completed artifact remains frozen PASS evidence, and the uncleaned coherent detached formal run retains detailed raw recheck evidence; the main run itself is not described as fully recheckable.

### Atomic merge observation boundary

Contributor validation occurs before `_mutate()` and therefore before any checkpoint publication metadata transaction is opened. It uses the same `selected_updates` supplied to the legacy atomic merge, and the later in-transaction membership fence still revalidates each database row, placement, stream, incarnation and admission token. The additional check narrows malformed internal calls without changing the stored schema, merge math, runtime positive payload, or static/legacy path.

### Read-only analysis boundary

`open_readonly()` resolves an existing file, opens SQLite with URI `mode=ro`, applies the existing `sqlite3.Row` factory, and sets `query_only=ON`. `_db_summary()` continues to run integrity/read queries and closes the connection. The new test observes query-only mode and proves DDL is rejected while the normal summary remains functional.

### Incidental source changes

`sqlite_store.py` only restores import ordering around the existing `DynamicMembershipFenceError`; `phase2_chaos_evidence.py` replaces an E731 lambda with the equivalent local query function; `phase2_test_evidence.py` removes an unused `json` import. The remaining `analysis.py` line changes are Ruff formatting around the single opener substitution. None changes a protocol, schema, artifact format, Checker threshold, or PBS launcher.

## Validation evidence checked

- RED: PBS `2501969.opbs` failed before the analysis fix; PBS `2501974.opbs` failed with the expected update-archive, stale/missing evidence, and contributor-payload symptoms (`4 failed, 14 passed in 3.98s`). Neither requested cleanup deletion.
- Focused fixed group: PBS `2501991.opbs`, `20 passed in 4.00s` for cleanup, review remediation, and fragment analysis.
- Full repository suite: PBS `2501992.opbs`, `495 passed in 25.23s`.
- Dedicated analysis rerun: PBS `2501971.opbs`, `2 passed in 0.16s`.
- Corrected dry-run: completed G8 `plan02_phase2_g8_2501754`, five candidates/30,539 bytes, zero `metrics/update_history.jsonl` candidates; the 26,811-byte archive remained intact.
- Static gates: relevant Python compilation, Ruff lint/format, `git diff --check`, `bash -n scripts/miyabi/*.pbs`, and literal `group_list=xg24i002` validation passed.
- No new formal 9-node experiment was required: the fixes tighten cleanup/operator/API validation and do not change the already exercised runtime payload, merge math, persistence schema, workload configuration, or acceptance thresholds.

## Final decision

**APPROVE**

All findings from the plan-complete current-state review and the retained late Phase 2 supplement are fixed with RED coverage, focused/full compute-node validation, corrected dry-run evidence, synchronized documentation, and explicit disposition of the irreversible historical cleanup loss. The plan may proceed to external-review disposition and final status synchronization.
