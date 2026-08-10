# Codex independent review — P5-delete-classic-refactor

- Reviewer: `gpt-5.6-sol`
- Base commit: `77e047cc5e291153736f9abbffb8986e6b912330`
- Target commit: `d2dbfed19eb5e9e0835167c13da40a80bc15273a`
- Review range: `git diff 77e047cc5e291153736f9abbffb8986e6b912330 d2dbfed19eb5e9e0835167c13da40a80bc15273a`
- Decision: **CHANGES_REQUIRED**

## Scope inspected

The review covered the P5 deletions and retained surfaces across production source, legacy readers, config loading, runtime entrypoints, authority/control adapters, analysis/evaluation tools, repository configs, PBS scripts, architecture and legacy tests, deletion accounting, checker changes, and synchronized documentation. It also traced the accepted dynamic-scaling and terminal-policy configuration fields to their production consumers because P5 removes the last classic launch/terminal implementations while documenting the unified runtime as the sole production path.

The 8 fragment-enabled configs, 5 fragment PBS scripts, separate full no-fragment control pair, classic/fragment writer modules, and old schema/bootstrap path were removed as intended. The retained top-level entrypoint shims resolve only to the v4 runtime, the v4 DDL does not recreate Fragment V0 tables, and runtime modules do not import the new legacy package.

## High findings

### H-01 — Legacy evaluation/export can mutate the historical run root

Evidence:

- `fs_diloco/tools/eval_lm_harness.py:186-219` resolves `export_dir` and `manifest_output` but never compares either path with `manifest["source_run_root"]`; `ensure_dir`, `save_pretrained`, and `atomic_write_json` therefore accept destinations inside the legacy run.
- `fs_diloco/tools/validation_eval.py:340-354` defaults to `run_root/metrics/validation_eval.json` and mutates `run_root/control/summary.json`. Supplying an explicit output inside the run is also accepted.
- Only `export_legacy_summary` is covered by an outside-root negative test. There is no equivalent evaluation/export test.

Impact:

P5's `LEGACY-01` contract says completed v1-v3/full/fragment runs are immutable query-only sources and every export/eval output must be explicitly written outside the old run root. The newly advertised legacy eval/export paths can create or replace files inside the source tree, invalidating the archived experiment and its inode/mtime/hash preservation guarantee.

Required fix and missing tests:

- Classify the source run as current v4 or legacy before any output mutation.
- For legacy sources, require explicit output destinations, resolve all destination ancestors, reject the run root and every descendant for model export, manifest, evaluation result, and any summary attachment, and never attach evaluation output to the legacy summary.
- Add pre-fix RED tests that exercise both tools and compare the complete legacy fixture file inventory before/after rejected and successful outside-root operations. Keep current-v4 in-run evaluation behavior covered separately.

### H-02 — The sole production runtime accepts dynamic scaling policy but has no scaling/scheduler composition

Evidence:

- `fs_diloco/core/config.py:200-221` exposes and validates the full `scaling` policy, including desired/low contributor windows, launch budgets, TTL, reconciliation interval, scheduler uncertainty, PBS script, queue, and walltime.
- After P5 deletes `runtime/launch_outbox.py`, no production runtime reads `config.scaling`; repository-wide references to these fields are confined to the dataclass/validator.
- `fs_diloco/runtime/syncer_v4.py:153-156,177-180` initializes the stream pool and processes already-published admission requests, but never records capacity observations, creates/reconciles dynamic `launch_requests`, or calls `PBSScheduler`.
- `fs_diloco/runtime/pbs_scheduler.py` and authority launch-state methods remain isolated primitives. Tests exercise the authority methods directly but do not cover a runtime loop that uses them.
- The P5 documentation now describes `scaling` as a dynamic scheduler policy/budget (`docs/06-configuration.md:30`) and the compatibility matrix claims fresh dynamic v4 supports replacement (`docs/08-compatibility-and-migration.md:8`).

Impact:

With the classic launch path removed, a configured dynamic run silently ignores its declared scale-out/replacement policy. Permanent learner loss cannot be replaced automatically and the mandatory P6 dynamic failure soak cannot maintain the requested contributor allocation. Accepting these options as valid creates a dangerous false-success configuration surface.

Required fix and missing tests:

- Compose a single leader-owned dynamic-capacity service around the existing authority launch state machine and `PBSScheduler` adapter, with persisted observations, deterministic request IDs, budgets, cooldown/TTL, uncertainty preservation, and current-fence admission reconciliation.
- Alternatively, until that service exists, reject `scaling.enabled=true` and remove claims of automatic policy support; this alternative does not satisfy the current plan's dynamic acceptance requirements.
- Add RED runtime tests for low-capacity window triggering, deduplicated request creation/submission, qsub-success/receipt-loss reconciliation, uncertainty/manual review, replacement admission, cooldown/budgets, successor replay, and close suppression.

### H-03 — Terminal policy fields are accepted but the v4 terminal loop ignores them and discards every final proposal

Evidence:

- `fs_diloco/core/config.py:224-232` exposes close policy, deadline, drain-ack timeout, registration/proposal visibility grace, terminal merge bound, and pre-close admission policy.
- Repository-wide production references for these names are confined to configuration validation.
- `fs_diloco/runtime/syncer_v4.py:870-880` closes only for the two global target forms, ignoring deadline and launch-budget close policies.
- `fs_diloco/runtime/syncer_v4.py:899-966` uses `leader.learner_recovery_wait_seconds` rather than `terminal.drain_ack_timeout_seconds`, ingests final receipts/proposals but never selects or merges them, and does not apply the configured visibility grace or admission policy.
- `LeaderSession.finalize_terminal` terminalizes acknowledged final updates as `terminal_final_update_not_selected`; consequently `terminal.max_terminal_merges` is always effectively zero even when configured as one.
- P5 documentation describes these fields as active terminal policy (`docs/06-configuration.md:31`).

Impact:

The unified runtime silently violates its accepted terminal configuration. It can wait six times longer than configured, cannot close on the declared dynamic policy/deadline, and drops a learner's acknowledged final proposal even when one bounded terminal merge was requested. This breaks the terminal-accounting and dynamic-soak acceptance contracts.

Required fix and missing tests:

- Implement an explicit leader-owned close-policy evaluator and bounded drain loop using the terminal section's timing/visibility/admission values.
- Permit at most `max_terminal_merges` fenced selections after the frozen contributors' final inputs become terminally knowable, then deterministically drop the remainder before finalization.
- Add RED tests for every close policy, monotonic deadline/ack timeout, pre-close registration handling, delayed visibility, terminal merge 0/1 bounds, final-update selection, and successor recovery during close/drain.

## Medium findings

### M-01 — The legacy SQLite URI is not robust for immutable path preservation

Evidence:

- `fs_diloco/legacy/reader.py:29-36` interpolates `Path.as_posix()` directly into a SQLite URI. URI-reserved characters such as `?` and `#` in a valid filename are parsed as URI syntax rather than path bytes.
- The connection uses `mode=ro` plus `PRAGMA query_only`, but does not use an immutable/read-only strategy for completed snapshots. A WAL-mode database can involve SQLite sidecar/open-time behavior not covered by the current DELETE-journal fixture.
- The preservation test covers only a simple path and database, not reserved path characters or WAL/SHM state.

Impact:

Some valid archived paths cannot be opened reliably, and the test does not prove the promised complete-tree immutability for realistic completed WAL-backed runs.

Required fix and missing tests:

- Build the SQLite file URI with correct percent encoding and choose/document a completed-snapshot opening policy that cannot create or modify source sidecars while still reading the committed database state.
- Add fixtures with URI-reserved path characters and WAL/SHM files; inventory every source entry before/after query, analysis, export, and evaluation.

## Low findings

None.

## Decision rationale

The deletion inventory and dependency convergence are substantially correct, but the retained legacy tooling violates the phase's immutable-source guarantee and the remaining sole runtime exposes two large no-op policy surfaces that are required for the plan's final dynamic and terminal gates. P5 cannot be finalized until the accepted findings are fixed and the affected focused/full compute-node tests pass.
