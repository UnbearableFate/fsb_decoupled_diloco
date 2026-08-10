# Codex independent incremental review — P4 mandatory fenced runtime

- Base commit: `19d40b5173eb1a16227013a451fced0e3eb13ace`
- Target commit: `e565ad8f9a71af128c6df7c1dfb4e42a9e520388`
- Review relation: the base is an ancestor of the target; this report reviews the complete increment `19d40b5173eb1a16227013a451fced0e3eb13ace..e565ad8f9a71af128c6df7c1dfb4e42a9e520388`.
- Reviewer: Codex, actual model `gpt-5.6-sol`
- Independence: this report was completed and saved before starting or reading the Claude review for this target.
- Scope: changed production protocol/runtime/storage code, Checker, tests, plan/matrix contract, retained RED/GREEN/runtime/cleanup evidence and prior finding dispositions.
- Verdict: **CHANGES_REQUIRED**

## Critical

None.

## High

### H1 — A leadership change inside rejected-disposition replay can still delete the only repair trigger and leave the learner waiting forever

Evidence: `_admit_requests()` verifies the token before dispatch (`fs_diloco/runtime/syncer_v4.py:378`), but the existing-disposition branch then republishes the rejection and deletes the global hot request without another authority fence (`syncer_v4.py:439-454`). `repair_rejected_admission_control()` writes only under the caller's epoch (`fs_diloco/protocol/admission_v4.py:917-958`). If that caller is fenced after the pre-dispatch check, it can publish under its now-old epoch and still unlink the hot request. A successor has no hot observation from which to invoke the repair. `read_admission_response()` checks only the current epoch's rejection (`admission_v4.py:404-431`) and does not consume the durable global disposition, so the exact rejected learner returns pending until timeout.

This is deterministic without relying on clock timing: after epoch 1 leaves a rejected disposition plus hot request, enter the epoch-2 repair function, explicitly fail epoch 2 and acquire/publish epoch 3 before returning to the epoch-2 caller. The current code archives the hot file, while epoch 3 has no request-specific rejection.

Impact: the new M4 remediation is not closed under another takeover. A valid terminal rejection can become unreachable even though its durable disposition remains present; admission recovery is not monotonic across repeated failover.

Required fix: make the durable rejected disposition directly consumable by `read_admission_response()` (strictly validate run/descriptor/request SHA, epoch/owner, exact old rejection path and rejection payload) so visibility does not depend on a hot-file-triggered successor repair. Current-epoch republication may remain an optimization. A token check immediately before archive is useful but cannot by itself close the check-to-unlink lease race on a shared filesystem.

Missing RED test: inject a second takeover between disposition repair entry and hot archival, publish the third epoch as current, then assert the hot request may be gone but the waiting learner still receives the exact retained rejection rather than pending/timeout.

## Medium

### M1 — Runtime evidence with no cleanliness marker is still accepted as clean

Evidence: `scripts/miyabi/check_plan03.py:553-568` removes `None` markers and defines `runtime_source_is_clean = not dirty_markers or ...`. Therefore a PASS artifact can omit all three supported `git_dirty` locations (or set them to null), name the target commit, and satisfy every `requirements_covered` row. The new test covers only explicit `true` (`tests/test_plan03_checker.py:486-510`); older fixtures still assert that a marker-less artifact passes.

Impact: a runtime executed from arbitrary dirty tracked or untracked source can be represented by a hand-authored marker-less artifact and pass the phase-final source gate. Commit equivalence does not attest the actual worktree used by the job.

Required fix: require at least one explicit boolean cleanliness marker for runtime evidence and require every present marker to be exactly `false`. If historical artifacts must remain readable, use an explicit, finite legacy attestation/migration rather than treating every marker-less future artifact as clean.

Missing RED test: a source-matching PASS artifact with `requirements_covered` but no marker (and separately a null/non-boolean marker) must produce `structured-checker-evidence`; a marker of exact boolean `false` remains the positive case.

### M2 — The exact-command replay API validates the command name and result, but not the recorded command request

Evidence: `LeaderSession.replay_committed_static_binding()` selects only `command_kind` and `result_json` (`fs_diloco/storage/authority.py:908-926`). The command table also stores `request_sha256`, and the normal `_command()` path compares it against the canonical typed request (`authority.py:4705-4721`). The new shortcut never reconstructs or compares that request. Because `command_id` is caller-provided and SQLite has no constraint tying the `admit-<request SHA>` suffix to the command request, another same-kind command record under that ID with an equal binding result is treated as proof for the fresh admission request.

Impact: the recovery boundary still depends on a naming convention rather than exact durable request proof. Current production call sites follow the convention, but a supported authority caller, future refactor or corrupted record can recreate the same generation-fence bypass this change was meant to remove.

Required fix: make the replay API accept/reconstruct all canonical `bind_or_replace_static_attempt` request fields, select `request_sha256`, and compare the exact expected command-request digest before returning the typed result. Treat a mismatch as `CommandConflictError`; keep schema/type failures loud.

Missing RED test: create a same-kind command record under the target admission command ID using different canonical command arguments but an equal current binding result, then publish the fresh same-attempt admission request and assert it is rejected rather than replayed.

## Low

None beyond the explicitly owned P5/P6 deferrals already recorded in the finding-disposition artifact.

## Positive observations

- Returning the canonical request digest from the publication API closes the learner hot-file reread race for both modes without breaking the path-returning compatibility API.
- Invalid raw-byte replay now preserves the first immutable history/disposition and validates exact bytes, digest and classification before removing a duplicate hot entry.
- Stale-token, schema and explicit admission-invariant failures now propagate instead of being silently converted to filesystem deferrals.
- Clean target `37b3fee` has a complete six-job runtime matrix, tracked requirement evidence and cleanup manifests; the reported results and source identity are internally consistent.

## Required disposition before P4 completion

H1 changes the cross-epoch terminal visibility guarantee and must be RED-locked and fixed. M1 is a completion-gate integrity hole and must be fixed. M2 should be fixed in the same exact-replay boundary while the public protocol is already under review. Because these fixes affect admission recovery and evidence fencing, freeze a new target and repeat the incremental review gate.
