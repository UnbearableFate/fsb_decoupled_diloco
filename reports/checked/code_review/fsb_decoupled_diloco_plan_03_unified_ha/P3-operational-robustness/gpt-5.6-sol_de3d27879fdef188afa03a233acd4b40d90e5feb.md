# Codex independent review — P3-operational-robustness

- Reviewer: `gpt-5.6-sol`
- Base commit: `225db163ee5bbfbf16bba3d59e06c4fbd6d789f8`
- Target commit: `de3d27879fdef188afa03a233acd4b40d90e5feb`
- Scope: complete `git diff 225db163ee5bbfbf16bba3d59e06c4fbd6d789f8 de3d27879fdef188afa03a233acd4b40d90e5feb`, including P3 source, v4 DDL, compatibility storage/runtime changes, tests, Checker, PBS, requirement matrix, golden trace and retained evidence.
- Ancestry: base is an ancestor of target.
- Review independence: this report was completed and saved before invoking or reading any Claude review.

## Verdict

**CHANGES_REQUIRED**

No Critical issue was found in the fenced merge transaction itself, but one destructive cleanup boundary and four P3 correctness/acceptance invariants are not yet safe enough to complete the phase. The final compute gate (`285 focused / 794 full`) does not exercise the counterexamples below.

## Critical

### C1 — Cleanup can traverse a symlinked candidate directory and unlink files outside the run

- Evidence: `fs_diloco/tools/clean_run.py:99-102` accepts a supplied directory through `is_dir()` (which follows a directory symlink) and recursively enumerates it. `build_cleanup_plan` calls this helper on `logs/wandb`, `heartbeats`, `updates/latest` and `updates/payloads` at `fs_diloco/tools/clean_run.py:386-407`. `_candidate` at `fs_diloco/tools/clean_run.py:214-235` lstat-checks only the leaf; it never proves that every parent below `run_root` is a real directory. `execute_cleanup` later calls `candidate.path.unlink()` at `fs_diloco/tools/clean_run.py:510`.
- Impact: if, for example, `updates/payloads` or `logs/wandb` is a symlink to an external directory, the lexical child path still appears below the run and its leaf is a normal file. The cleaner can therefore unlink unrelated user data outside the exact completed run, violating both the destructive-action boundary and AUDIT-03.
- Required fix: resolve candidates lexically from the already validated run root, lstat every parent component, and require each to be a non-symlink directory before inventory and again before deletion. Refuse a symlinked candidate root rather than silently following or ignoring it.
- Missing RED: construct `run/updates/payloads -> outside/`, put a regular file in `outside`, and require both planning and execution to refuse while the outside file remains byte-identical. Cover a parent swap between plan and execute as well.

## High

### H1 — Audit pruning can archive the current global version and make the authority forget its frontier

- Evidence: `_audit_history_records` selects every `global_versions` row with `version <= cutoff_version` at `fs_diloco/storage/authority.py:4580-4587`, and `archive_audit_batch` deletes those exact rows at `fs_diloco/storage/authority.py:2951-2983`. Normal authority progress derives the frontier from `SELECT MAX(version) FROM global_versions` in `try_select_batch` (`fs_diloco/storage/authority.py:2315-2318`), `prepare_publication` (`2456-2460`) and `commit_merge` (`2583-2587`). The compaction test archives with `cutoff_version=1` when v1 is the latest version but never attempts another selection/commit (`tests/storage/test_authority_p3_operational.py:882-993`).
- Impact: archiving through the latest version can leave no current `global_versions` row. The next selection reports that v0 was never committed, or the next publication incorrectly targets v0. This violates recovery continuity and turns routine boundedness maintenance into a run-stopping operation.
- Required fix: keep the latest committed version and its dependency-closed publication/selection/artifact rows in the hot authority. Clamp the archivable version frontier to strictly less than `MAX(version)` (or introduce a separate durable current-frontier row) before computing the dependency closure.
- Missing RED: commit v1, request an archive cutoff at/above v1, then prove `latest_committed_version()==v1` and successfully select/prepare/commit v2. Also cover a v0-only authority.

### H2 — Initializer recovery does not bind the caller's full run identity

- Evidence: a reserved-staging retry compares only `run_id` and `source_fingerprint` at `fs_diloco/tools/init_run.py:89-96`, although `.identity` explicitly includes `config_sha256` at `fs_diloco/tools/init_run.py:188-198`. The completed-run fast path validates internal consistency, then compares only `run_id` to the requested config at `fs_diloco/tools/init_run.py:61-71`. Neither path proves that the current requested resolved config, git identity or expected descriptor identity equals the reserved/completed run.
- Impact: after a pre-marker crash, a caller can reuse the same run ID/source fingerprint with a different training configuration and silently publish the old staging root. Likewise, calling init against a completed run with a changed config can be reported as successful recovery. This violates INIT-01's same-identity retry/different-identity collision rule and can launch a workload different from the operator's current request.
- Required fix: compute the current resolved-config bytes/hash without mutating staging, bind it together with run ID, source fingerprint and relevant git/source identity, and compare it to `.identity`/descriptor before publishing or returning a completed run. Fail closed on any mismatch.
- Missing RED: crash after reservation, modify a meaningful config field while keeping run ID/source unchanged, and require retry rejection with no marker publication. Repeat against an already completed run.

### H3 — Re-entering terminal close can increase the supposedly frozen hard-crash budget

- Evidence: `begin_terminal_close` accepts controller states `open`, `closing` and `draining` (`fs_diloco/storage/authority.py:3307-3313`). Only fence creation is guarded by `is_new_close`, but every call unconditionally rewrites `reason`, `requested_at`, `hard_crash_cycle_token_budget` and even state back to `closing` at `fs_diloco/storage/authority.py:3375-3391`. A later acknowledgement trusts the rewritten budget at `3452-3456`.
- Impact: a different command ID (including a successor mistake) can raise a close that was frozen at one-cycle bound N to an arbitrary larger bound, or move a draining controller backwards. Token-gap accounting is then no longer the immutable per-close limit claimed by TOK-05/TERM-01.
- Required fix: permit mutation only for the `open -> closing` transition. A later begin command should either return an unchanged existing close only when all immutable close parameters match, or fail closed; it must never rewrite the budget, reason, timestamp, generation or draining state.
- Missing RED: begin with budget 8, call begin again with budget 80 and a new command ID, assert rejection/unchanged persisted budget, and prove a hard-crash gap of 9 remains rejected. Cover re-entry after the first acknowledgement moved state to draining.

### H4 — Descriptor loading makes immutable audit/telemetry growth part of every actor startup scan

- Evidence: `load_run_descriptor` unconditionally calls `validate_completed_run` at `fs_diloco/core/run_descriptor.py:136-139`. That validator recursively walks `final_root.rglob("*")` and lstats every mutable and immutable entry at `fs_diloco/storage/run_initializer.py:263-279` before reopening the DB. The manifest explicitly allows linearly growing `audit`, `metrics`, `logs`, payload and checkpoint prefixes.
- Impact: every actor descriptor load/restart becomes O(total historical files), directly contradicting AUDIT-04 and G6's requirement that immutable audit history not participate in startup/discovery scans. Large runs can suffer unbounded restart latency even though the new audit DB indexes are compacted.
- Required fix: split marker/immutable-identity validation from an explicit deep operator audit. Normal descriptor load should validate `.complete`, reservation/identity, declared immutable objects and the bounded top-level protocol structure without recursively enumerating mutable prefixes. Keep a separate opt-in full-tree integrity audit if needed.
- Missing RED: create increasing numbers of valid audit/telemetry files behind an instrumented filesystem boundary and show normal descriptor load touches a constant set of paths; the deep audit may scale separately.

## Medium

### M1 — No-job scheduler uncertainty ignores its own deadline in the compatibility outbox

- Evidence: for a submission with no job ID, `LearnerLaunchOutbox.reconcile` transitions `submitting/submission_unknown` to `terminal_uncertain` with a persisted deadline at `fs_diloco/runtime/launch_outbox.py:516-543`. On later passes, the no-job branch checks only `expires_at` (`497-511`); because `terminal_uncertain` is neither `submitting/submission_unknown` nor `planned/retryable`, it then falls through without comparing `uncertainty_deadline`. Default launch TTL is 900 seconds while the uncertainty timeout is 300 seconds.
- Impact: qsub failures/receipt loss without a known job can occupy anti-duplicate/capacity state well past the declared scheduler uncertainty deadline, violating SCHED-03/SCHED-04. The known-job path does honor the deadline, so coverage misses this asymmetric state.
- Required fix: evaluate an existing uncertainty deadline for both known-job and no-job `terminal_uncertain` rows before launch TTL handling, transitioning to the explicit policy state without releasing the tombstone incorrectly.
- Missing RED: qsub returns no job ID, advance through `submission_unknown -> terminal_uncertain`, restart/reconcile past uncertainty deadline but before launch TTL, and require the explicit terminal/manual-review state with the original deadline unchanged.

### M2 — P3's complete matrix/evidence overstates runtime behavior and does not retain structured per-requirement Checker output

- Evidence: DMB-05 claims the losing actor imports no torch and allocates no CUDA, but its only implementation owners are the authority/admission foundation and `fs_diloco/runtime/learner.py` still imports torch at module line 17; the runtime ordering is explicitly a P4 cutover gate. ENV-01's `write_actor_attestation` has no production caller (only tests). Nevertheless all 40 P3 rows are marked `complete`. In addition, `verify_phase_requirements` checks that an evidence path exists but not that it contains the advertised `checker requirements.<ID>` contract (`scripts/miyabi/check_plan03.py:350-399`); the final retained JSON contains only `p3_requirement_count: 40`, while the PBS invocation prints only `PASS` and does not save the structured `checks.requirements` payload.
- Impact: the phase matrix can pass with declarative labels even when an end-to-end assertion is deferred, and reviewers cannot reconstruct the individual requirement results from the cited final artifact. This is a completion-gate/evidence defect rather than a unit-test failure.
- Required fix: narrow/split P3 foundation requirements from P4 runtime-wiring requirements (leaving the latter pending), and retain the Checker's structured per-ID output in a tracked artifact whose contents are validated against the matrix contract. Do not claim actor emission or pre-torch admission until production wiring exists.
- Missing RED: Checker test that rejects an evidence artifact lacking `checks.requirements.<ID>.status=PASS`; architecture tests that prove attestation and admission-before-torch ordering once their owning phase is complete.

## Low

### L1 — Telemetry payload can overwrite its writer identity fields and the writer claim is not directory-durable

- Evidence: `ActorTelemetryWriter.event` places `**payload` after `actor_kind`, `actor_id`, `attempt_id`, timestamp and event type (`fs_diloco/observability/logging_utils.py:73-82`), so arbitrary payload keys replace the claimed identity. Claim creation fsyncs the file but not its parent directory (`fs_diloco/observability/logging_utils.py:52-68`).
- Impact: telemetry is explicitly non-authoritative, but accidental payload collisions can misattribute rows and a crash can lose the single-writer claim while leaving ambiguous attempt output.
- Suggested fix: reject reserved payload keys (or apply payload first), validate identity components/path consistency, and fsync the claim directory.
- Missing test: reserved-key rejection and claim visibility/durability hook coverage.

## Reviewed areas without additional blocking findings

- v4 bootstrap uses create-no-replace database/marker publication and cleans its own linked DB on marker collision.
- Selection credit is consumed only inside committed merge transactions, and deterministic reduction order is separate from contributor admission order.
- Direct-weight validation now rejects invalid integer/domain/finiteness cases and uses stable summation.
- Operator resolution is fenced by the persisted state hash and does not directly admit or invoke qdel.
- Audit batch/partition publication is immutable and digest-bound; source GC deletion itself lexically rejects leaf/parent symlinks.
- PBS syntax/group/walltime policy and focused/full compute evidence are present; `plans/AGENTS.md` is outside the frozen target and remains unstaged.
