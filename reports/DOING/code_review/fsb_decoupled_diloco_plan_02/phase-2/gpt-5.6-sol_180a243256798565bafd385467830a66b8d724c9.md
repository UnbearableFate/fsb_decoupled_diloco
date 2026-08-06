# Independent Codex review — Plan 02 Phase 2 remediation increment

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Comparison base: `7feb09992b7f40b255e0858020a50d811a602b9c`
- Review target: `180a243256798565bafd385467830a66b8d724c9`
- Reviewed diff: `7feb09992b7f40b255e0858020a50d811a602b9c..180a243256798565bafd385467830a66b8d724c9`
- Ancestry: the base is an ancestor of the target.

## Scope and method

I saved this report before invoking or reading a new Claude review. I inspected the complete frozen increment across `fs_diloco/runtime/syncer.py`, `fs_diloco/storage/fenced_store.py`, the new `fs_diloco/tools/clean_run.py`, Phase 2 tests, the completed Checker, PBS launchers, retained G7/G8/G9/matched/completed artifacts, the requirement matrix, implementation records, README, and architecture/runtime/data/configuration/operations/module documentation. The review emphasized transaction boundaries, takeover behavior, terminal closure, scheduler-to-registration binding, destructive cleanup safety, Checker reproducibility, and whether tests exercise the negative API boundaries introduced by the remediation.

The accepted findings from the previous target are substantially remediated. Dynamic no-progress now enters a persisted drain and a normal dynamic terminal is rejected until input is closed; token-target closure freezes the committed head; merge observations share the merge transaction; starvation allocation and observation share one transaction; the completed Checker requires complete merge/starvation observation histories; and a scheduler-bearing registration remains pending until the durable launch row binds its physical job ID. The frozen formal evidence reaches dynamic v120 with 51 local steps per cycle and the completed Checker reports all MEM-01–MEM-20 predicates `PASS`. The findings below concern new cleanup behavior and one remaining storage-boundary validation gap.

## Findings

### High — the new cleaner deletes the authoritative update archive required to audit and rerun the completed Checker

`build_cleanup_plan()` unconditionally classifies `metrics/update_history.jsonl` as “superseded raw telemetry” and schedules it for deletion (`fs_diloco/tools/clean_run.py:249-256`). That file is not duplicate learner telemetry. `archive_and_prune()` appends terminal update rows to it with fsync before pruning those rows from SQLite, and both the Phase 2 completed Checker and analysis reconstruct the complete update set from active SQLite plus this archive (`fs_diloco/storage/maintenance.py:36`; `scripts/miyabi/check_plan02_phase2.py:327,352-356`; `docs/04-data-flow.md:240`). Once the cleaner removes it, the retained database no longer contains the pruned applied/dropped rows and the completed Checker cannot reproduce its per-version placement/stream and membership assertions.

This destructive path was exercised on the formal G9 run: `20260807-0150_phase2-g9-cleanup.json` records deletion of `metrics/update_history.jsonl` (2,563,263 bytes). The frozen completed artifact remains proof that the Checker passed before cleanup, and the independent coherent detached formal run remains available, but the main run's detailed update archive is not recoverable from the retained files. The cleaner must preserve all fsync-before-prune authority histories, including `update_history.jsonl`; only learner-generated `learner_metrics.csv` and `update_manifest.csv` are eligible raw telemetry here. Add a RED test that creates the update archive, executes cleanup, and proves the file remains intact. Correct the operations/tool documentation and record the already-incurred, nonrecoverable loss explicitly rather than implying that the main run can still be fully rechecked.

### Medium — direct PASS evidence is not bound to the current terminal version

For a direct evidence artifact, `_matching_pass_evidence()` validates `status`, errors, run root, run ID, descriptor digest, and source fingerprint, but never compares the evidence's terminal/final version with the current `summary.json` (`fs_diloco/tools/clean_run.py:118-181`). The matched branch does perform this comparison at lines 146-153. As a result, a stale direct PASS artifact for the same immutable descriptor and run directory can authorize deletion after the run's terminal summary has changed, even though it no longer proves the state being cleaned. This weakens the principal safety gate of a destructive tool.

Require every accepted direct evidence schema to expose a terminal final version and compare it with the current summary before inventory. The Phase 2 completed artifact exposes `authority.terminal.final_version`; the G8/G9 chaos artifact exposes `authority.final_version`. Unknown PASS schemas without a terminal binding should fail closed. Add negative tests for a mismatched final version and for an otherwise identity-matching PASS artifact with no recognized terminal version.

### Medium — atomic merge observations can carry contributors unrelated to the committed updates

`commit_full_merge()` now correctly requires a merge observation with exact kind, key, and global version, but it does not verify `eligible_contributors` or `selected_instance_ids` against `selected_updates` (`fs_diloco/storage/fenced_store.py:714-741`). `record_capacity_observation()` uses these fields to update each instance's `last_contributed_observation_seq`, compute low-window hysteresis, and decide scale-out. A malformed internal call can therefore atomically commit a mathematically valid merge while atomically persisting capacity state for a different contributor set; the “exact merge capacity observation” error and current tests cover only kind/key/version.

At the fenced storage boundary, require the observation's contributor IDs to be unique and exactly equal to the selected updates' non-null `learner_instance_id` set, and require its eligible count to equal the selected update count under the current Phase 2 definition. Add RED cases for a missing/different instance, a duplicate instance, and a mismatched eligible count. The runtime helper already constructs the correct payload, so this hardening should not alter the formal positive path or persistence format.

## Other reviewed boundaries

- The inline observation marker is scoped to the mutation lock and current thread, is cleared in a `finally`, and does not remain active for the caller's failpoint callback.
- Merge and starvation failpoints roll back both the state transition and observation allocation. Reserved observation kind/key namespaces cannot be populated through the standalone public mutator.
- The registration pending state intentionally preserves the existing direct/local path where both the request and launch lack physical PBS identities; scheduler-bearing requests cannot be admitted until the launch receipt is durable, and an exact normalized identity is required afterward.
- Dynamic target/no-progress termination preserves the first persisted close reason across takeover and refuses normal terminal publication before controller closure.
- The completed Checker validates contiguous committed versions, exactly one canonical merge observation for every v1+ commit, contiguous starvation generations, schema/epoch ownership, bounded membership state, terminal closure, formal workload size, and blocking runtime events.
- PBS scripts retain literal `group_list=xg24i002`; their changes are observability-only and introduce no new submission semantics.

## Validation evidence checked

- Focused cleanup/dynamic/remediation PBS regression: `2501924.opbs`, `40 passed in 7.50s`.
- Full repository PBS regression: `2501925.opbs`, `492 passed in 25.00s`.
- Static gates recorded at the target: Python compile, Ruff lint/format, `git diff --check`, `bash -n scripts/miyabi/*.pbs`, and literal PBS group validation all pass.
- Formal locked-source Phase 2 sequence at commit `61f571bbe4460b257abe8452c2ea63df79515b29`: G7, compatibility, G8, G9, matched performance, and completed Checker all `PASS`; authoritative completed artifact `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260807-014345_phase2-completed_pass.json`.

## Final decision

**CHANGES_REQUIRED**

The Phase 2 protocol remediation itself closes the prior crash and terminal gaps, but the new destructive cleaner currently removes an authority archive needed to reproduce the acceptance checks. The evidence gate and merge-observation API also need the negative validation described above. These changes require RED tests, focused/full verification, a new frozen target, and an incremental review before Phase 2 and the plan can be closed.
