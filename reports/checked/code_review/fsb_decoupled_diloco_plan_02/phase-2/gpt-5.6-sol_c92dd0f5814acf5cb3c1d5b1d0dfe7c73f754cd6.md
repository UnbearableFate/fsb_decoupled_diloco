# Independent Codex review — Plan 02 Phase 2 remediation target

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Comparison base: `7feb09992b7f40b255e0858020a50d811a602b9c`
- Review target: `c92dd0f5814acf5cb3c1d5b1d0dfe7c73f754cd6`
- Reviewed diff: `7feb09992b7f40b255e0858020a50d811a602b9c..c92dd0f5814acf5cb3c1d5b1d0dfe7c73f754cd6`
- Ancestry: the base is an ancestor of the target.

## Scope and method

This incremental review covers the Phase 2 remediation diff: dynamic no-progress drain and successor recovery; atomic merge/starvation capacity observations; reason-specific terminal ceilings; scheduler-receipt-bound registration; completed-checker invariants; conservative exact-run cleanup; associated tests, PBS launchers/checkers, retained evidence, requirement records, and synchronized documentation. I saved this report before invoking or reading the external reviewer.

I traced each modified persistence transition from its runtime caller into the fenced SQLite transaction and checked recovery behavior, idempotency keys, controller/input closure, reserved request namespaces, scheduler admission binding, cleanup evidence and deletion boundaries. I also reviewed the new negative tests and the retained formal G7/G8/G9, matched-performance, completed-checker, and cleanup artifacts.

## Findings

### Low — no-progress terminal-ceiling documentation contradicts the implemented protocol

`FencedSQLiteStore.begin_dynamic_drain()` freezes `stop_after_global_tokens` at `current_version`, but every other non-global-target reason—including `no_progress_timeout`—allows up to `current_version + max_terminal_merges`, bounded by the configured outer target (`fs_diloco/storage/fenced_store.py:1756-1767`). Three operational descriptions instead say that no-progress closes at the current version (`docs/03-runtime-flow.md:196`, `docs/06-configuration.md:179`, and `docs/modules/runtime-syncer.md:95`). The configuration table also describes `max_terminal_merges` as applying only to manual/budget/deadline close (`docs/06-configuration.md:169`), omitting no-progress.

This does not affect the validated runtime, but it can cause operators and later maintainers to expect a stricter stop boundary than the persisted controller permits. State that only the token target freezes at the current version, and that no-progress uses the same bounded terminal-merge allowance as manual/budget/deadline closure. This is documentation-only and does not require a behavioral RED test.

## Correctness, concurrency, persistence, cleanup, and tests

The four findings from the previous target are otherwise resolved:

- dynamic no-progress now enters a persisted drain, and normal dynamic terminal publication is gated on a closed controller and closed input;
- merge capacity observation is part of the merge transaction, starvation generation and observation advance atomically, reserved observation namespaces cannot be forged through the public API, and the completed checker proves exact merge coverage plus contiguous starvation generations;
- token-triggered drain freezes at the current committed version rather than inheriting the outer-step target;
- scheduler-bearing registrations remain pending, with their request files preserved, until the exact logical launch request has a durable matching PBS receipt.

The cleanup tool is conservative: it validates terminal stop/summary agreement, stopped membership, passing evidence tied to the same run/source/descriptor, SQLite sidecars, exact run-root containment, file identity and execution-time revalidation; dry-run is the default and the deletion manifest is written before removal. The retained cleanup manifest proves the successful canonical G9 cleanup was restricted to 28 redundant files while authority state, checkpoints needed for audit/recovery, control/configuration, representative logs, and reports were retained.

Relevant verification at the frozen executable state includes focused PBS job `2501910` (`40 passed`), full PBS job `2501912` (`492 passed`), Python compilation, Ruff, `git diff --check`, `bash -n scripts/miyabi/*.pbs`, and literal PBS group IDs. Formal canonical evidence includes G7 `2501753`, compatibility `2501752`, G8 launcher/checker `2501754/2501758`, G9 launcher/checker chain `2501765/2501778` reaching version 120 with 1,516,128 tokens and replacement/duplicate rejection, matched performance `2501807/2501826`, and completed checker `2501846`; all recorded terminal results are `PASS`.

## Final decision

**CHANGES_REQUIRED**

The remediation code and formal evidence satisfy the previously blocking Phase 2 protocol findings. The remaining Low finding is a small but concrete documentation contradiction and should be corrected before Phase 2 is closed, followed by a continuous incremental review of the documentation-only target.
