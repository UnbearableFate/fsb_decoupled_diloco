# Independent Codex review — Plan 02 Phase 0 remediation

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `c43a519997a581357561981cd448b07a24df5fdb`
- Comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`
- Relevant cumulative diff: `c1c61153548ff7b2543d3ce1bc764c19432b138e..c43a519997a581357561981cd448b07a24df5fdb`
- Source identity: branch `codex/fsb_decoupled_diloco_plan_02`; the final experiment recorded commit `d9fea98ae527cdf64f56edabce0f8525909d1e13`, `git_dirty=true`, and source fingerprint `sha256:d4f466082cbc69c95fd8053d53dfa05c413668c8a115091595237fa6b77f93ac`. Recomputing the committed source scopes at the review target produced the same fingerprint with `git_dirty=false`, establishing byte identity while preserving the pre-commit Git metadata distinction.

## Scope and methods

I reviewed the complete 35-file cumulative diff, including the relocation and `claude -p` rewrite of the plan completion gate, Plan 02 and research-plan requirements, all Phase 0 probe/aggregate/Checker code, both PBS scripts, the focused tests, append-only progress/failure records, and the retained blocked/final structured artifacts. I inspected scheduler state observations and physical-incarnation history, source fingerprints, clock intervals, contention rows and totals, cache discovery/adoption evidence, source-gate cases, artifact hygiene, and cleanup/error paths. I also reran static syntax/lint/diff checks and used a Checker mutation to test whether the orchestration decision is independently enforced.

No `Critical` or `High` finding was identified. One `Medium` and three `Low` findings follow.

## Findings

### Medium — FEAS-05's zero-business-write evidence is not connected to the gated actor

`source_pinning_probe()` creates one empty `business.sqlite3`, snapshots it, and then invokes `plan02_source_gate.py` for each case without passing that database or any guarded runtime action to the subprocess (`scripts/miyabi/plan02_fault_probe.py:482-583`). On a successful gate it writes only a filesystem sentinel (`:574-575`). No case can reach a business mutator, because neither the gate nor its caller has a post-gate database write. The final `mismatch_actor_business_writes` value is then computed from the unchanged, unrelated database (`:584-609`). Consequently the final artifact's before/after database hashes and all-zero counts prove that an untouched database stayed untouched, not that a mismatched candidate was prevented from a leader/membership/business write.

The exit-code and sentinel evidence does validly prove that all seven mismatches block before a simulated runtime starts, and `fs_diloco_imported=false` proves the gate itself is pre-import. The unproven part is the separate FEAS-05 acceptance statement that the actor makes zero leader/membership/business writes (`plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv:6`). The Checker accepts the detached before/after equality and derived zero as sufficient (`scripts/miyabi/check_plan02_feasibility.py:263-285`), while the end-to-end test repeats that assertion without exercising a guarded write path (`tests/test_plan02_feasibility.py:72-95`).

Concrete fix: execute each case through a small candidate harness whose only path to a case-specific SQLite insert is after a zero gate exit. Require exactly one matching-case audit/business insert and zero inserts for every mismatch, while continuing to require no `fs_diloco` import before the gate. A regression should demonstrate that a harness which writes despite a blocked gate is detected. This can remain a probe-only schema; it need not import production code in Phase 0.

### Low — the Checker does not enforce consistency between scheduler capability and selected orchestration

The Checker accepts either orchestration string independently of `job_array_supported` and `automatic_submission_supported` (`scripts/miyabi/check_plan02_feasibility.py:209-258`). A mutated otherwise-passing payload with `job_array_supported=true`, valid rerunable physical-incarnation evidence, `automatic_submission_supported=false`, and `initial_learner_orchestration=independent_manifest` still returns FEAS-04 `PASS`. The probe itself currently emits a consistent result (`scripts/miyabi/plan02_pbs_capability.py:420-432`), and the retained artifact correctly selects `pbs_job_array`, so this does not invalidate the measured result. It does leave the standalone Checker weaker than the frozen policy that array availability selects array and lack of array selects manifest.

Concrete fix: require `job_array_supported => automatic_submission_supported && initial_learner_orchestration == "pbs_job_array"`, and otherwise require `initial_learner_orchestration == "independent_manifest"`. Add both inconsistent payloads to the Checker mutation test.

### Low — source-gate defensive branches still lack direct tests

The end-to-end source probe covers independent expected protocol/schema/run-ID mismatch, commit mismatch, combined dirty/fingerprint mismatch, mutated config bytes, and descriptor hash mismatch (`tests/test_plan02_feasibility.py:72-95`). It does not isolate `git_dirty` alone, marker-vs-descriptor protocol/schema/run-ID mismatch, marker config checksum mismatch, an invalid/missing resolved config path, or `check_gate()`'s exception result. These branches exist and fail closed in `scripts/miyabi/plan02_source_gate.py:77-122,156-176`, but a regression could collapse or mislabel them without the current test detecting it.

Concrete fix: add table-driven direct `check_gate()` tests with the source-capture helper stubbed to a fixed identity. This is a coverage gap rather than evidence that a current branch behaves incorrectly.

### Low — the Checker records but does not bound wall-clock discontinuity during clock exchange

The two-way clock exchange correctly derives a nonnegative-delay interval and records `wall_monotonic_elapsed_delta_seconds` per round plus its maximum (`scripts/miyabi/sqlite_shared_fs_probe.py:433-497`). The Checker validates the interval intersection and absolute offset bound but ignores the wall-versus-monotonic delta (`scripts/miyabi/check_plan02_feasibility.py:145-166`). A local wall-clock step during a round invalidates the constant-offset assumption even if a numerically narrow intersection happens to remain. The retained run is healthy: its maximum delta is sub-microsecond, so this is not a defect in the current conclusion.

Concrete fix: freeze a conservative discontinuity threshold and require the recorded maximum below it, with a synthetic failing Checker test.

## Facts

- The final parent job `2497282.opbs` finished on two distinct hosts with `Exit_status=0`; the final Checker artifact is `PASS` for FEAS-01 through FEAS-05.
- The source fingerprint in the final artifact exactly matches the review-target source scopes. No production `fs_diloco` module was changed by Phase 0.
- FEAS-01 directly observes a stopped `BEGIN IMMEDIATE` holder, a blocked contender, invisible uncommitted state, `SIGKILL`, successor commit, rollback, and integrity `ok`.
- FEAS-02 discovers two canonical objects for each of `latest`, `stop`, and `summary`, selects epoch 2 after epoch-1 fixed-cache pollution, and repairs all three caches.
- FEAS-03 uses 20 two-way exchanges across two nodes; the retained intersection is `[-0.003513105, 0.003404724]` seconds. Eight writers across both hosts commit all 400 rows with 1,521 handled busy events, zero starvation, both acquire and renew actions, safe PRAGMAs, and integrity `ok`. Cross-node visibility is read-only.
- FEAS-04 records real `queued`, `prologue`, `running`, and `finished` scheduler states. Scalar and both array physical jobs have terminal exit zero; array children are rerunable and each records `run_count=1`. Persisted qstat fields are allowlisted.
- FEAS-05 independently compares candidate expectations for protocol, schema, and run ID and blocks all mismatches before its runtime sentinel and before importing `fs_diloco`; only the business-write proof has the Medium gap above.
- The job-level `ERR` path produced a retained `_blocked.json` during the remediation failure, demonstrating fail-closed artifact publication before a later successful run cleaned its own work directory.
- Static checks completed successfully: PBS shell syntax, literal group IDs, Ruff, Python compilation, and diff whitespace. The recorded focused group is `20 passed in 8.12s` on a compute node.
- Root `AGENTS.md` now contains only repository-wide general rules. The plan completion gate is scoped under `plans/AGENTS.md` and explicitly requires a new non-interactive `claude --print`/`claude -p` Opus 5 process with JSON metadata verification, bypass permission mode, reviewer-only restrictions, and no Herdr dependency.

## Correctness, regression, and invariant assessment

The Phase 0 changes do not alter the production training protocol, so direct runtime regression risk is limited to the pre-existing SQLite probe's expanded commands. Existing `stress`, `verify`, and `kill-reopen` paths retain their behavior; focused tests exercise these paths and the new readonly/contend/clock modes.

Concurrency and persistence evidence is strong. SQLite writes use `BEGIN IMMEDIATE`, every contention row has a `(writer_id, sequence)` primary key, DB-side totals are compared to per-writer results, and non-busy operational failures propagate. JSON evidence is published with temporary files, file fsync, and replace. The old-cache counterexample stages bytes before the lease changes, resumes after epoch 2 publication, and proves why fixed caches cannot be authoritative. Scheduler child markers no longer substitute for terminal success.

Error handling is fail closed at the Checker, source gate, contention worker, and PBS job levels. The retained remediation failure demonstrates the structured blocked path. The Medium finding is not an error-handling bypass in the gate itself; it is a test/evidence construction flaw that leaves one stated acceptance criterion unproven.

Test coverage materially improved and covers the historical failure modes. Remaining direct branch gaps are listed above. The use of a literal destructive-cleanup assertion is brittle but intentionally protects a PBS safety primitive and is paired with runtime resolved-prefix validation.

## Acceptance criteria assessment

| Requirement | Assessment |
| --- | --- |
| FEAS-01 writer-lock boundary | Satisfied by direct observable and DB-integrity evidence. |
| FEAS-02 old fixed-cache writer | Satisfied for latest, stop, and summary canonical adoption plus repair. |
| FEAS-03 clock/shared SQLite | Satisfied by the retained two-node evidence; wall-step Checker threshold is a Low hardening item. |
| FEAS-04 PBS capability | Satisfied by real lifecycle and terminal records; Checker consistency is a Low independent-validation gap. |
| FEAS-05 source pinning | Partially satisfied: pre-import mismatch rejection is proven, but zero guarded business writes are not behaviorally exercised. |
| Two-value Checker and blocked artifact | Satisfied, including a real stage-failure artifact. |
| Documentation and requirement matrix | The measured boundaries and array fallback are documented, but FEAS-05 should not remain a completion candidate until the Medium evidence gap is fixed or explicitly deferred with adequate impact/ownership. |
| Review workflow relocation | Satisfied: the gate is in `plans/AGENTS.md` and uses verified non-interactive `claude -p`, not Herdr. |

## Inferences

- The current source gate implementation is likely suitable as a pre-import identity predicate because every comparison is fail closed. The retained experiment nevertheless cannot support the stronger causal statement about prevented database mutations until the probe connects the gate result to a guarded mutator.
- The final scheduler and clock evidence resolves the major ambiguities in the first completion candidate; neither currently blocks Phase 0.
- The source-fingerprint match makes the pre-commit dirty experiment auditable against the review target despite the differing recorded commit ID.

## Recommendations

1. Fix the FEAS-05 harness and add a regression that would fail if a blocked candidate can execute its guarded insert; regenerate the final two-node evidence because this changes a Phase 0 acceptance proof.
2. Tighten orchestration consistency in the Checker while touching FEAS-04 tests, or record the Low item as a Phase 1 Checker follow-up with explicit ownership.
3. Add direct source-gate branch tests. This can be done without changing the gate interface.
4. Treat the wall/monotonic discontinuity threshold as a documented follow-up unless the next evidence regeneration already changes the clock Checker.

## Final decision

**CHANGES_REQUIRED**

The remediation convincingly resolves the writer-lock, canonical-cache, cross-node SQLite/clock, scheduler-terminal, artifact-hygiene, and blocked-artifact concerns. One Medium acceptance-evidence defect remains: the business database used to claim zero mismatched-actor writes is never reachable from the gated actor, so its unchanged state is tautological. Phase 0 should remain a completion candidate until that causal path is exercised and the associated test/evidence is regenerated.
