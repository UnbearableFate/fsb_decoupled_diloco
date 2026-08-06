# Independent Codex review — Plan 02 Phase 0

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `f04697be5e94a25d611db4a00a49b212882a7fc6`
- Comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`
- Relevant diff: `c1c61153548ff7b2543d3ce1bc764c19432b138e..f04697be5e94a25d611db4a00a49b212882a7fc6`
- Source identity: branch `codex/fsb_decoupled_diloco_plan_02`; the experiment recorded base commit `c1c61153548ff7b2543d3ce1bc764c19432b138e`, `git_dirty=true`, and source fingerprint `sha256:f44bd4ea24f02be5a37760c6c94a9f1dea8639f012c7d87488202e3a588977c7`. Recomputing the fingerprint after the review-target commit produced the same value. The later workflow-only commit `d9fea98ae527cdf64f56edabce0f8525909d1e13` is outside this review.

## Scope inspected

I reviewed the full 27-file diff, including all new and modified Phase 0 Python probes, both PBS scripts, the feasibility Checker and aggregator, focused tests, requirement matrix, research-plan update, progress/failure records, retained failure evidence, final two-node Checker artifact, and PBS logs. I also checked the recorded source fingerprint against the committed source scopes and inspected the final evidence fields for FEAS-01 through FEAS-05.

No `Critical` or `High` finding was identified. Four `Medium` findings and two `Low` findings follow.

## Facts and findings

### Medium — FEAS-02 does not exercise canonical adoption for terminal artifacts

The fault injector publishes epoch-scoped `latest`, `stop`, and `summary` artifacts, and demonstrates that the old writer can overwrite all three fixed caches. However, `_select_highest_epoch()` scans only `syncer_epochs/e*_*/latest/head.json`, so the only canonical object ever selected is `latest` (`scripts/miyabi/plan02_fault_probe.py:240-257,291-299,344-381`). The returned `business_state_failed` and `cache_pollution_reported` values are unconditional literals rather than results derived from a reader or Checker decision (`scripts/miyabi/plan02_fault_probe.py:350-373`). The Checker consequently verifies those self-reported booleans and one latest selection, while only checking that all fixed caches were repaired (`scripts/miyabi/check_plan02_feasibility.py:74-88`). The focused test has the same coverage boundary (`tests/test_plan02_feasibility.py:55-61`).

This does not prove the FEAS-02 matrix claim that an old writer may overwrite `latest`, `stop`, and `summary` while the reader/Checker still adopts current-epoch canonical state (`plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv:3`). A regression that resolves terminal state from stale fixed `stop.json` or `summary.json` would still pass this probe.

### Medium — FEAS-04 treats child artifact publication as successful job completion

`_wait_for_child()` stops polling as soon as the expected child JSON files exist and defines `completed` solely by artifact count (`scripts/miyabi/plan02_pbs_capability.py:136-177`). Scalar and array capability decisions then require the artifact fields but do not require a scheduler terminal state or `Exit_status=0` (`scripts/miyabi/plan02_pbs_capability.py:232-276`). The hard-coded classifier matrix is compared with itself and is reported as `state_classifier_validated` (`scripts/miyabi/plan02_pbs_capability.py:180-219`). In the final retained evidence, both historical queries are `classification="unknown"` with no parsed fields even though `completed=true` (`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011600_phase0-feasibility_pass.json:462-474,697-709`).

The artifact proves that the child workloads started and published their fingerprints, but it does not provide structured evidence that the scheduler reported successful terminal completion or that the real terminal record is normalized correctly. This leaves the `finished` portion of the FEAS-04 acceptance matrix unverified (`plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv:5`) and could accept a child that fails after publishing its marker.

### Medium — FEAS-05 compares protocol/schema/run identity only between two shared files

The pre-import gate checks `protocol_version`, `schema_version`, and `run_id` only for equality between `run_descriptor.json` and `bootstrap_complete.json`; it has no independent expected/current values supplied by the candidate runtime (`scripts/miyabi/plan02_source_gate.py:63-110`). The probe mutates commit, dirty/source fingerprint, resolved configuration contents, or raw descriptor bytes, but it never constructs a self-consistent descriptor/marker pair whose protocol, schema, or run identity disagrees with the candidate expectation (`scripts/miyabi/plan02_fault_probe.py:418-455,459-553`). The Checker and tests enumerate the same four mismatch cases (`scripts/miyabi/check_plan02_feasibility.py:134-146`; `tests/test_plan02_feasibility.py:64-83`).

Thus a candidate handed a mutually consistent but incompatible protocol/schema descriptor has no local comparison that can fail closed before runtime import. This is weaker than the plan's pre-import expected/current protocol/schema contract and the FEAS-05 matrix statement covering source, config, protocol, and run-descriptor mismatch (`plans/DOING/fsb_decoupled_diloco_plan_02.md:250-259`; `plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv:6`).

### Medium — the clock probe does not establish a bounded inter-node clock offset

Each node independently timestamps when it observes one shared marker, and the aggregator treats the difference between those local observation midpoints as clock skew (`scripts/miyabi/sqlite_shared_fs_probe.py:310-347`; `scripts/miyabi/plan02_phase0_aggregate.py:41-55`). That quantity is clock offset plus the difference in scheduling, polling, and shared-filesystem observation delay. Those terms can enlarge or cancel the offset. The final artifact acknowledges that launch and observation delay are included, but it supplies no round-trip or delay bound from which the two-second clock-offset bound follows (`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011600_phase0-feasibility_pass.json:38-75`). The Checker trusts the derived `within_bound` boolean (`scripts/miyabi/check_plan02_feasibility.py:90-110`).

The observed 1.005 ms span is useful evidence that the two ranks observed the marker nearly together, but it is not by itself a defensible upper bound on wall-clock offset. FEAS-03 uses that bound to freeze later lease timing assumptions, so cancellation can yield a false `PASS`.

### Low — qstat evidence persists unfiltered environment fields

`_query_job()` retains every parsed `qstat -f` field (`scripts/miyabi/plan02_pbs_capability.py:84-97`), and repeated full `Variable_List` values are committed in the final evidence (for example `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011600_phase0-feasibility_pass.json:378`). The reviewed artifact contains no credential, but persisting an unrestricted scheduler environment creates an avoidable secret-leak risk for future invocations. Only the fields needed for classification, exit status, job name, array state, and the allowlisted request fingerprint variables should be retained.

### Low — retained PBS logs do not follow the required result suffix

The scoped reporting convention requires `YYYYMMDD-HHMMSS_<experiment-id>_<pass|fail|review>.<ext>`. Retained logs such as `20260806-011600_phase0-pbs.log`, `20260806-011200_phase0-pbs.log`, and the two failed-run logs omit a result suffix. This does not affect runtime correctness, but it weakens artifact classification and conflicts with `plans/AGENTS.md`.

## Inferences and risk assessment

- FEAS-01 is supported by direct evidence: a stopped writer held `BEGIN IMMEDIATE`, the contender received `database is locked`, the uncommitted row stayed invisible, `SIGKILL` released the lock, and integrity remained `ok`.
- The shared-SQLite portion of FEAS-03 is supported: two hosts participated, cross-node reopen saw the committed counter, eight distinct writers committed 400/400 transactions with both acquire and renew events, 1,198 handled busy events, zero starvation, rollback journal mode, FULL synchronous, and integrity `ok`.
- FEAS-02, the clock portion of FEAS-03, FEAS-04 terminal-state handling, and FEAS-05 protocol/schema pinning remain under-proven for their stated acceptance criteria. The final Checker can therefore report `PASS` when those specific invariants have not been exercised.
- Error paths generally fail closed, atomic JSON publication is used consistently, destructive cleanup has a strict resolved-path guard, and the two recorded failures have appropriate regression coverage and retained evidence.
- The experiment's recorded dirty source is traceable because its full source fingerprint equals the subsequently committed source scopes; this review does not treat the base commit value alone as the source identity.

## Recommendations and missing tests

1. Generalize canonical discovery/adoption by artifact kind, select epoch-2 `latest`, `stop`, and `summary` independently after fixed-cache pollution, and derive the business/Checker result from those selections. Add a regression that leaves each fixed cache stale in turn and fails if any reader adopts epoch 1.
2. Poll child jobs through a real scheduler terminal/absent transition, retain a safe allowlist of qstat fields, require successful terminal status when the scheduler exposes it, and represent unavailable history explicitly rather than validating a synthetic matrix as real capability. Add fixtures for actual Miyabi running, historical-terminal, and absent output plus a child that writes its marker and then exits nonzero.
3. Give the source gate independent expected run/protocol/schema values without importing `fs_diloco`, and add protocol, schema, and run-ID mismatch cases in which descriptor and marker remain mutually consistent but the candidate expectation differs.
4. Replace the one-way marker observation with a repeated two-way timestamp exchange or another measured method that yields a defensible offset interval from bounded round-trip delay. Make the Checker consume the worst bound, and add deterministic delayed/cancelled-offset tests.
5. Rename retained log artifacts to include their terminal result and record the rename in the append-only progress/failure records.

## Acceptance assessment

| Requirement | Assessment | Evidence |
| --- | --- | --- |
| FEAS-01 | Satisfied | Direct writer-lock, rollback, successor, and integrity evidence. |
| FEAS-02 | Changes required | Only latest canonical adoption is exercised; terminal decisions are self-reported. |
| FEAS-03 | Changes required | Shared SQLite evidence is strong; clock-offset bound is not established. |
| FEAS-04 | Changes required | Submission and fingerprint propagation work; terminal success/history is not established. |
| FEAS-05 | Changes required | Source/config/raw-descriptor cases work; independent protocol/schema/run expectation is absent. |

## Final decision

**CHANGES_REQUIRED.** The findings are evidence/Checker defects in the Phase 0 feasibility gate rather than production-protocol changes, but they allow the gate to overstate several acceptance criteria. Remediate them and rerun the affected focused and two-node capability evidence before declaring Phase 0 complete.
