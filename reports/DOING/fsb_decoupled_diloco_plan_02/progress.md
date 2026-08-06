# fsb_decoupled_diloco_plan_02 implementation progress

This record is append-only. Facts, inferences, and follow-up work are separated in each entry.

## 2026-08-06T00:46:29+09:00 — G0 execution baseline

### Facts

- Plan branch: `codex/fsb_decoupled_diloco_plan_02`.
- Plan branch point and initial comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Initial worktree was clean before the branch was created.
- Host classification: Miyabi-G login/control plane (`miyabi-g1`), with no `PBS_JOBID` or `PBS_NODEFILE`.
- Repository and scoped instructions, Plan 02, its design and requirement matrix, the research plan, the required architecture/runtime/data/configuration/operations documents, and the Miyabi development/operations instructions were read.
- Live scheduler inspection reported project `xg24i002`, enabled Miyabi-G queues, no running jobs, and no allocated nodes.
- Phase order is frozen as Phase 0 → Phase 1 → Phase 2. Phase 1 work will not start until the Phase 0 Checker returns `PASS`.
- Phase 0 is limited to probes, supervisors, evidence collection, and the feasibility Checker; it does not alter the production protocol.

### Inferences

- Runtime tests and project imports must run in a confirmed PBS compute allocation; the current login shell is restricted to source edits, scheduler inspection, and static checks.
- The existing shared-SQLite probe covers stress and kill/reopen but does not yet cover contention telemetry or the writer-lock pause boundary.
- The existing source capture helper records source identity but does not yet provide the pre-import source/config/descriptor gate required by FEAS-05.

### Follow-up

- Implement Phase 0 probes and their deterministic local unit coverage.
- Run login-node static validation, then execute the Phase 0 runtime matrix on the smallest sufficient PBS allocation.
- Preserve structured evidence under this report directory and run the Phase 0 Checker.

Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-004629_g0-baseline_review.json`.

## 2026-08-06T01:00:00+09:00 — P0-L0 focused probe tests

### Facts

- Environment: confirmed Miyabi-G PBS compute node `mg0004`, interactive job `2496450.opbs`, `select=1`, queue `interact-g`.
- Loaded modules in the compute shell: `nvidia/25.9` and `nv-hpcx/25.9`.
- Command:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_capture_source_identity.py tests/test_source_identity.py
  ```

- Result: `11 passed in 6.79s`.
- The associated group covered the writer-lock supervisor, fixed-cache stale-writer counterexample and repair, source/config/descriptor pre-import mismatch gate, shared-SQLite contention telemetry, PBS state/job-ID normalization, and the Phase 0 Checker stdout/fail-closed contract.
- The interactive allocation completed normally after the test group.

### Inferences

- The probe implementations are ready for the real two-node shared-filesystem and scheduler-capability run.
- Local multiprocess contention and fault injection are not substitutes for the required cross-node evidence; FEAS-03 and FEAS-04 remain incomplete until the batch probe finishes.

### Follow-up

- Submit the statically validated two-node Phase 0 feasibility script and retain its structured Checker artifact.

Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010000_p0-l0-focused-tests_pass.json`.

## 2026-08-06T01:05:00+09:00 — attempt-1 cleanup-target remediation tests

### Facts

- The Phase 0 PBS script no longer reads the generic inherited `WORK_ROOT` variable.
- Its work path is derived unconditionally as a job-specific strict child of the Plan 02 artifact directory; path resolution and the expected `work_<stamp>_<job-id>` shape are checked before use and immediately before cleanup.
- Cleanup uses `find <validated-job-directory> -depth -delete`; a broad or mismatched path fails closed.
- The attempt-1 run-generated files were inventoried, moved to a recoverable task-local trash directory, and then deleted after the failure evidence was persisted. The deleted content was 528 KiB of probe-only files; source, reports, and unrelated account data were not removed.
- Static validation passed:

  ```text
  bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh
  git diff --check
  .venv/bin/ruff check scripts/miyabi/sqlite_shared_fs_probe.py scripts/miyabi/plan02_fault_probe.py scripts/miyabi/plan02_source_gate.py scripts/miyabi/plan02_pbs_capability.py scripts/miyabi/plan02_phase0_aggregate.py scripts/miyabi/check_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_plan02_feasibility.py
  ```

- Focused remediation command on confirmed compute node `mg0008`, interactive job `2496513.opbs`:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py
  ```

- Result: `8 passed in 2.55s`.

### Inferences

- The exact failure mode from attempt 1 now has a regression assertion and a runtime-independent fail-closed cleanup guard.
- The two-node feasibility experiment must still be rerun to validate the full job path.

### Follow-up

- Submit phase0-feasibility attempt 2 using the same resource shape and frozen source tree.

## 2026-08-06T01:12:00+09:00 — attempt-2 SQLite startup-lock remediation tests

### Facts

- Connection setup now installs a five-second startup busy timeout before applying `PRAGMA synchronous=FULL`, then sets the short configured timeout used by measured contention transactions.
- Added a regression in which a separate connection holds `BEGIN IMMEDIATE` while the contender opens; the holder releases within the startup boundary and the contender must then commit with integrity intact.
- Attempt-2 probe-only output (424 KiB) was deleted after the complete MPI traceback and structured failure summary were persisted.
- Focused remediation command on confirmed compute node `mg0008`, interactive job `2496518.opbs`:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py
  ```

- Result: `9 passed in 2.89s`.

### Inferences

- Expected writer-lock contention during simultaneous connection setup is now governed by a bounded startup wait instead of failing outside the contention retry loop.
- The next two-node run is attempt 3 for the unchanged phase0-feasibility experiment; another failure would trigger the three-failure escalation gate.

### Follow-up

- Run phase0-feasibility attempt 3 without modifying its source while live.

## 2026-08-06T01:10:00+09:00 — Phase 0 feasibility attempt 3 PASS

### Facts

- PBS parent job: `2496519.opbs`, queue `debug-g`, `select=2:mpiprocs=4`, hosts `mg0008` and `mg0009`, terminal walltime 28 seconds.
- Nested scalar capability job: `2496521.opbs`, terminal walltime one second.
- Workload-specific completion marker and Checker stdout were both present: `PLAN02_PHASE0_COMPLETE=...phase0-feasibility_pass.json` and `PASS`.
- The Checker returned `PASS` for FEAS-01 through FEAS-05.
- FEAS-01: a stopped `BEGIN IMMEDIATE` holder blocked a contender; `SIGKILL` released the lock; the tentative row rolled back; integrity remained `ok`.
- FEAS-02: the old process overwrote all three fixed caches after epoch 2 publication; the reader still selected epoch 2 canonical state; repair restored all cache epochs to 2.
- FEAS-03: two hosts produced a conservative clock/observation span of `0.000499179` seconds; cross-node reopen saw the same committed state; eight writers committed all 400 transactions with 3,169 handled busy events, zero starvation, maximum measured wait `4.005841390986461` seconds, `journal_mode=DELETE`, `synchronous=FULL`, and integrity `ok`.
- FEAS-04: compute-node scalar `qsub/qstat` and request fingerprint propagation worked. The array command was rejected with `cannot submit non-rerunable Array Job`; the probe therefore selected the independent-manifest fallback.
- FEAS-05: matching identity passed; commit, dirty/fingerprint, config, and descriptor mismatches all stopped before importing `fs_diloco` and produced zero business writes.
- The job-scoped raw work directory was removed after the final structured artifact was fsynced.

### Inferences

- SQLite, fixed-cache, clock/shared-filesystem, manual independent restart, compute-node scalar submission, and source-pinning boundaries are feasible on the measured Miyabi environment.
- The array result is safe for fallback selection but not yet a definitive statement that the scheduler lacks array capability: the submitted array did not explicitly request the rerunable mode required by the scheduler error.

### Follow-up

- Add `qsub -r y` to the array-only capability attempt and rerun the Phase 0 evidence bundle so the initial orchestration decision reflects actual scheduler capability.
- Do not begin Phase 1 until the refined Phase 0 evidence is frozen and the Phase 0 completion review gate passes.

Artifacts:

- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011200_phase0-feasibility_pass.json`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011200_phase0-pbs.log`.

## 2026-08-06T01:12:00+09:00 — PBS array capability refinement PASS

### Facts

- Static validation and the Phase 0 focused group passed after making the array submission explicitly rerunable: `10 passed in 2.93s` on compute node `mg0004`, interactive job `2496523.opbs`.
- Compute-node scalar child `2496527.opbs` completed and propagated the logical request fingerprint through `Variable_List`, `Job_Name`, and its child artifact.
- Rerunable array job `2496528[].opbs` submitted successfully with `-r y -J 0-1`; array indices 0 and 1 ran on `mg0009` and `mg0010`, respectively, and produced distinct audited artifacts.
- The normalized parent array ID is `2496528[]`; qstat exposed `array_indices_submitted=0-1`, per-array running state, rerunable mode, request variables, and the array job name.
- Refined capability decision: compute-node automatic scalar submission is supported, PBS job arrays are supported when explicitly rerunable, and initial learner orchestration should use a PBS job array.
- The temporary three-child-artifact directory and scheduler stdout files were deleted after the standalone capability evidence was persisted.

### Inferences

- The prior independent-manifest selection reflected a probe omission, not a scheduler capability limit.
- The complete Phase 0 bundle should be regenerated so its single authoritative artifact contains this corrected capability decision.

### Follow-up

- Rerun the complete two-node Phase 0 bundle and use its Checker artifact as the Phase 0 completion candidate.

Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011500_pbs-capability-refined_pass.json`.

## 2026-08-06T01:14:00+09:00 — Phase 0 completion-candidate evidence PASS

### Facts

- Final Phase 0 parent job: `2496530.opbs`, queue `debug-g`, `select=2:mpiprocs=4`, hosts `mg0004` and `mg0008`, terminal walltime 23 seconds.
- Scheduler children: scalar `2496531.opbs`; rerunable array `2496533[].opbs` with indices on `mg0011` and `mg0012`.
- The final Checker returned `PASS`; every FEAS-01 through FEAS-05 requirement entry returned `PASS`.
- Cross-node clock/observation span was `0.001004814` seconds, below the frozen two-second maximum.
- Cross-node contention committed all 400 transactions with 1,198 handled busy events, zero starvation, maximum wait `2.615531600022223` seconds, safe PRAGMAs, and integrity `ok`.
- Compute-node scalar submission, job-name/request-variable propagation, job-state normalization, and rerunable job arrays all passed. The frozen initial learner orchestration is `pbs_job_array`; an independent manifest remains the defined fallback if future scheduler capability changes.
- The requirement matrix now marks FEAS-01 through FEAS-05 `complete` and points each row to the final evidence artifact.
- `plans/00-RESEARCH_PLAN.md` now records the Phase 0 boundary: independent jobs/manual restart are required, automatic recovery/scaling stay opt-in, and an indefinitely stopped writer transaction requires operator/scheduler termination.
- Final job-specific raw output and child stdout files were removed only after the structured evidence was persisted.

### Inferences

- Phase 0 is a completion candidate. It is not complete until the repository-wide dual-model phase review, finding disposition, any remediation tests, and the phase-final commit finish.
- No Phase 1 implementation may begin before that review gate passes.

### Follow-up

- Freeze a Phase 0 review-target commit and run the two independent reviews against the same commit.

Artifacts:

- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011600_phase0-feasibility_pass.json`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-011600_phase0-pbs.log`.

## 2026-08-06T09:24:46+09:00 — Phase 0 dual-review remediation evidence PASS

### Facts

- The independent reviews of review target `f04697be5e94a25d611db4a00a49b212882a7fc6` against branch point `c1c61153548ff7b2543d3ce1bc764c19432b138e` both returned `CHANGES_REQUIRED`. Their immutable reports are:
  - `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/gpt-5.6-sol_f04697be5e94a25d611db4a00a49b212882a7fc6.md`;
  - `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/claude-opus-5_f04697be5e94a25d611db4a00a49b212882a7fc6.md`.
- Focused remediation tests ran on compute node `mg0038` in PBS job `2497257.opbs`:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_capture_source_identity.py tests/test_source_identity.py
  ```

- Result: `20 passed in 8.12s`.
- Final static validation passed from the Miyabi login/control plane:

  ```text
  bash -n scripts/miyabi/*.pbs
  rg -n '#PBS -W group_list=<group_id>' scripts/miyabi/*.pbs
  .venv/bin/ruff check scripts/miyabi/sqlite_shared_fs_probe.py scripts/miyabi/plan02_fault_probe.py scripts/miyabi/plan02_source_gate.py scripts/miyabi/plan02_pbs_capability.py scripts/miyabi/plan02_phase0_aggregate.py scripts/miyabi/check_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_plan02_feasibility.py
  .venv/bin/python -m py_compile scripts/miyabi/sqlite_shared_fs_probe.py scripts/miyabi/plan02_fault_probe.py scripts/miyabi/plan02_source_gate.py scripts/miyabi/plan02_pbs_capability.py scripts/miyabi/plan02_phase0_aggregate.py scripts/miyabi/check_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_plan02_feasibility.py
  git diff --check
  ```

- The placeholder search produced no matches; every Phase 0 PBS script retains the literal group ID `xg24i002`.
- The authoritative remediated two-node run was submitted with:

  ```text
  qsub -o /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-092200_phase0-remediation_review.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-092200 scripts/miyabi/run_plan02_phase0_feasibility.pbs
  ```

- Parent PBS job `2497282.opbs` used `debug-g`, `select=2:mpiprocs=4`, hosts `mg0004` and `mg0005`, finished in 41 seconds with `Exit_status=0`, and printed both `PHASE0_CHECKER=PASS` and `PLAN02_PHASE0_COMPLETE=.../20260806-092200_phase0-feasibility_pass.json`.
- The resolved settings were two hosts, 8 contention writers, 50 transactions per writer, `busy_timeout_ms=10`, `retry_timeout_seconds=60`, 5 ms lock hold, 20 two-way clock rounds, and a 2-second maximum absolute clock-offset bound.
- FEAS-01 through FEAS-05 all returned `PASS`. The two-way interval intersection was `[-0.003513105, 0.003404724]` seconds, giving an absolute upper bound of `0.003513105` seconds. Cross-node contention committed `400/400` transactions with 1,521 handled busy events, zero starvation, 38 acquire and 362 renew actions, a 2.945-second maximum wait, `journal_mode=DELETE`, `synchronous=FULL`, and integrity `ok`.
- Scheduler evidence contains real `queued`, `prologue`, `running`, and `finished` observations. Scalar child `2497284.opbs` and array physical incarnations `2497285[0].opbs` and `2497285[1].opbs` all have terminal `Exit_status=0` and `run_count=1`; both array children record `Rerunable=True`. Only allowlisted qstat fields and the `PLAN02_REQUEST_FINGERPRINT` variable are retained.
- Source pinning independently rejects commit, dirty fingerprint, resolved config, descriptor, protocol, schema, and run-ID mismatches before runtime import and before business writes. The Checker now retains the failing source input in structured `_blocked.json` evidence.
- Artifact SHA-256 is `c7a0fc831b87f0b6017aa5e11f55028e8a05ef91d435e4d1e2533ff14c8edb7a`. The artifact records dirty source fingerprint `sha256:d4f466082cbc69c95fd8053d53dfa05c413668c8a115091595237fa6b77f93ac` over commit `d9fea98ae527cdf64f56edabce0f8525909d1e13`.
- The successful job removed its job-scoped work directory. After the replacement PASS artifact was persisted, the exact finished attempt-1 raw directory `work_20260806-090551_2497224` was inventoried at 788 KiB and deleted as redundant probe-only telemetry. The retained `_blocked.json` and complete remediation log preserve the failure, root cause, and Checker behavior; the deleted raw directory is not recoverable from the workspace.
- Two superseded successful reruns (`20260806-091226_*` and `20260806-091643_*`, 195,397 bytes total) were also deleted after the final artifact subsumed their evidence. They are not recoverable from the workspace; the authoritative `20260806-092200_*` JSON/log pair is retained.
- Correction to the append-only record: the sentence near the top of `failures.md` saying no Phase 0 failure had been recorded was an obsolete placeholder. The subsequent timestamped entries are authoritative. Earlier progress timestamps were recorded out of execution order; this entry uses the actual terminal time and does not rewrite prior append-only records.

### Finding dispositions

Codex report:

- `Medium — FEAS-02 canonical adoption`: **fixed**. The probe discovers, selects, and validates the highest epoch independently for `latest`, `stop`, and `summary`; exact discovery, pollution, and repair maps are Checker-enforced and regression-tested.
- `Medium — child marker versus scheduler completion`: **fixed**. A marker is insufficient; scalar/array completion requires terminal history plus `Exit_status=0`, with a regression showing that a marker followed by exit 7 remains incomplete.
- `Medium — independent protocol/schema/run expectation`: **fixed**. The pre-import gate accepts independent expected values and has mutually consistent descriptor/marker mismatch cases for protocol, schema, and run ID.
- `Medium — clock method`: **fixed**. The one-way midpoint comparison was replaced by 20 filesystem two-way exchanges with nonnegative-delay offset intervals, an intersection, and an explicit absolute upper bound checked against configuration.
- `Low — qstat environment retention`: **fixed**. qstat persistence uses an allowlist and extracts only the Plan 02 request fingerprint; a sanitization regression is present and the final artifact contains no `Variable_List` or inherited environment.
- `Low — artifact result suffix`: **deferred-with-justification**. Historical append-only `*_phase0-pbs.log` filenames remain immutable because renaming would break recorded evidence links; every new remediation log uses the required `_review.log` result suffix. Owner: future Plan 02 experiment authors.

Claude report:

- `H1 — real queued/finished scheduler evidence`: **fixed**. A future-start scalar produces deterministic `W/Q` evidence, polling continues through terminal history, `no_record` and query failure are distinct, and the final run proves queued/prologue/running/finished plus exit-zero completion.
- `M1 — empty repair map fail-open`: **fixed**. The Checker requires the exact three cache kinds, epoch 2 values, and nonempty per-kind discovery counts.
- `M2 — constant booleans`: **fixed**. FEAS-01/02/05 booleans and business-write counts are derived from retained observations and independently checked.
- `M3 — contention and starvation not falsifiable`: **fixed**. The Checker requires two hosts, exact DB/event/request/commit count equality, every writer represented, `busy_errors > 0`, both acquire and renew, and zero explicit starvation; contention workers emit starvation evidence and fail nonzero on timeout.
- `M4 — no blocked artifact after stage failure`: **fixed**. The PBS script has a job-level `ERR` path that atomically creates structured failure input, invokes the Checker, emits `BLOCKED`, and retains raw evidence. Remediation attempt 1 exercised this path.
- `M5 — rerunable incarnation semantics`: **fixed**. The research boundary and artifact record rerun semantics and per-physical-child `run_count`, `Rerunable`, and exit status; Phase 2 may not treat PBS job ID as an exactly-once physical identity.
- `M6 — aggregate/source-gate direct coverage`: **fixed**. Direct tests cover the clock aggregate bound, independent source-gate values, terminal child failure, blocked-artifact retention, and PBS sanitization/command behavior.
- `L1 — clock measurement wording`: **fixed** by the two-way interval method and explicit bound above.
- `L2 — restart capability naming`: **fixed**. The probe now distinguishes `scheduler_query_supported` and `manual_independent_job_supported`; an unverified restart claim is not emitted, and restart execution remains explicitly deferred to Phase 1.
- `L3 — artifact hygiene`: **fixed** by qstat allowlisting and final-artifact secret/environment scanning.
- `L4 — contradictory failure header`: **rejected-with-evidence** as an immutable historical line under the append-only rule; the correction in this entry establishes which timestamped records are authoritative without rewriting history.
- `L5 — nonmonotonic historical timestamps`: **rejected-with-evidence** for the same append-only reason; subsequent records use actual execution timestamps.
- `L6 — suspended state classification`: **fixed**. PBS `S` is classified separately as `suspended`.
- `L7 — dead `killed` assignment`: **fixed**.
- `L8 — brittle source-string tests`: **deferred-with-justification**. The qsub array assertion is now behavior-based. The remaining literal assertion protects the exact destructive cleanup primitive in a PBS shell script and complements resolved-prefix runtime guards; impact is limited to a possible false-positive test after an equivalent cleanup refactor. Owner: Phase 1 cleanup/tooling work.
- `L9 — unrestricted STAMP`: **fixed** with a strict alphanumeric/hyphen allowlist before path construction.
- `L10 — stopped holder outliving supervisor`: **deferred-with-justification**. PBS reaps the job process tree and the supervisor kills/reaps the holder on ordinary exceptions; an abrupt supervisor kill could retain the stopped holder until scheduler cleanup, but cannot outlive the PBS job. Owner: Phase 1 P1-L1 fault-supervisor hardening.
- `L11 — transaction-outside boundary`: **deferred-with-justification**. The plan explicitly assigns the transaction-outside lease-expiry takeover branch to Phase 1 P1-L1; Phase 0 establishes only the writer-lock side and does not claim the production lease protocol exists. Owner: Phase 1 P1-L1.
- `L12 — manifest fallback evidence link`: **fixed**. FEAS-04 now references both the authoritative array-capability artifact and the earlier real scheduler-rejection artifact that selected `independent_manifest`.
- `L13 — writable cross-node verifier`: **fixed**. Cross-node visibility uses a SQLite URI `mode=ro` path that performs no DDL or writes; a regression verifies that readonly open cannot create a missing DB.

### Inferences

- The original High and all Medium review findings are remediated with executable regression coverage and new two-node evidence. Remaining deferrals are Low-severity historical-record or Phase 1 hardening items and do not weaken the Phase 0 feasibility conclusions.
- Because remediation changed the source gate, clock evidence method, scheduler terminal contract, and Checker safety boundary, this is a new Phase 0 completion candidate rather than a phase-final result. A fresh review-target commit and repeated independent dual-model gate are required before Phase 1 begins.

### Follow-up

- Freeze the remediated tree in a new review-target commit, verify the committed source fingerprint matches this final artifact, and repeat the Phase 0 reviews against the plan branch point.

Artifacts:

- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-092200_phase0-feasibility_pass.json`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-092200_phase0-remediation_review.log`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-090551_phase0-feasibility_blocked.json`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-090551_phase0-remediation_review.log`.

## 2026-08-06T09:59:38+09:00 — Phase 0 second-review remediation PASS

### Facts

- The repeated independent reviews of review target `c43a519997a581357561981cd448b07a24df5fdb` against plan branch point `c1c61153548ff7b2543d3ce1bc764c19432b138e` both returned `CHANGES_REQUIRED`:
  - `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/gpt-5.6-sol_c43a519997a581357561981cd448b07a24df5fdb.md`;
  - `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/claude-opus-5_c43a519997a581357561981cd448b07a24df5fdb.md`.
- Claude ran through a fresh non-interactive `claude --print` process with requested/actual model `claude-opus-5`, session `71f3d342-9272-47aa-9bd5-bd46790601d8`, `bypassPermissions`, no fallback argument, `permission_denials=[]`, and machine-readable result `REVIEW_REPORT_WRITTEN`. Codex saved its report before reading Claude's report.
- The user-directed governance commit `d9fea98ae527cdf64f56edabce0f8525909d1e13` moved the completion gate from root `AGENTS.md` to scoped `plans/AGENTS.md` and replaced the Herdr workflow with verified non-interactive `claude -p`. It is intentionally part of the cumulative review diff and does not change Phase 0 executable source.
- Review remediation added a true two-node FEAS-01 path: rank 0 on `mg0004` spawned and stopped the writer transaction, rank 1 on `mg0008` observed `database is locked`, saw zero uncommitted rows, requested `SIGKILL`, and then committed the successor row on `mg0008`. The retained evidence records two distinct hosts, holder state `T`, holder exit `-9`, rollback, successor commit, safe PRAGMAs, and integrity `ok`; the original single-node probe remains nested as the 1-node control.
- FEAS-05 now connects the pre-import gate to a falsifiable guarded runtime. Matching identity imports `fs_diloco` only after gate success and writes exactly one control row; each of seven mismatches reports gate-import false, runtime-import false, runtime not started, and zero guarded writes. The shared probe DB changes from all-zero counts to exactly one matching `syncer_leader` row, with zero mismatch-owner rows.
- `capture_source_identity.py` now includes a present ignored explicit file scope. The final manifest contains `uv.lock` (747,721 bytes, SHA-256 `240e2fd8dc4294b2e3aef8c5c2061e209549451a66632d8419c90bc374d64a8b`) even though repository policy keeps that environment-specific file out of Git.
- Direct regressions now cover contention JSON hostname/PID publication, DB-side aggregation across two hosts, missing hostname failure, Checker one-host rejection, cross-node writer-lock combination, independent source/marker mismatch branches, orchestration consistency, missing business snapshots, and wall-clock discontinuity.
- The first focused test attempt failed and is recorded in `failures.md`. After its two test-only root causes were fixed, compute PBS job `2497331.opbs` on `mg0004` ran:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_capture_source_identity.py tests/test_source_identity.py
  ```

- Result: `23 passed in 12.45s`, parent `Exit_status=0`, marker `PLAN02_PHASE0_TESTS_COMPLETE=2497331.opbs`.
- Final static validation passed:

  ```text
  bash -n scripts/miyabi/*.pbs
  rg -n '#PBS -W group_list=<group_id>' scripts/miyabi/*.pbs
  .venv/bin/ruff check scripts/miyabi/capture_source_identity.py scripts/miyabi/check_plan02_feasibility.py scripts/miyabi/plan02_fault_probe.py scripts/miyabi/plan02_pbs_capability.py scripts/miyabi/plan02_phase0_aggregate.py scripts/miyabi/plan02_source_gate.py tests/test_capture_source_identity.py tests/test_plan02_feasibility.py tests/test_sqlite_probe.py
  .venv/bin/python -m py_compile scripts/miyabi/capture_source_identity.py scripts/miyabi/check_plan02_feasibility.py scripts/miyabi/plan02_fault_probe.py scripts/miyabi/plan02_pbs_capability.py scripts/miyabi/plan02_phase0_aggregate.py scripts/miyabi/plan02_source_gate.py tests/test_capture_source_identity.py tests/test_plan02_feasibility.py tests/test_sqlite_probe.py
  git diff --check
  ```

- The placeholder search produced no matches; all Plan 02 PBS scripts use literal group `xg24i002`.
- The authoritative remediated full run was submitted with:

  ```text
  qsub -o /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-095900_phase0-remediation_review.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-095900 scripts/miyabi/run_plan02_phase0_feasibility.pbs
  ```

- Parent `2497333.opbs` used `debug-g`, `select=2:mpiprocs=4`, hosts `mg0004`/`mg0008`, finished in 58 seconds with `Exit_status=0`, and printed `PHASE0_CHECKER=PASS` plus `PLAN02_PHASE0_COMPLETE=.../20260806-095900_phase0-feasibility_pass.json`.
- FEAS-01 through FEAS-05 all returned `PASS`. The 20-round clock interval was `[-0.002982041, 0.003113702]` seconds, absolute offset bound `0.003113702` seconds, and maximum wall/monotonic discontinuity `0.000000544` seconds versus the frozen 0.1-second limit.
- Eight contention writers across both hosts committed `400/400` transactions with 4,139 handled busy events, zero starvation, 48 acquire and 352 renew actions, `journal_mode=DELETE`, `synchronous=FULL`, and integrity `ok`. Maximum writer wait was `7.372740368` seconds; this exceeds the Phase 1 starting suggestion of a 5-second busy timeout and must be treated as a P1-L1 tuning input, not copied as a validated default.
- Scheduler evidence again contains real queued/prologue/running/finished states. Scalar `2497335.opbs` and array physical children `2497336[0].opbs`/`2497336[1].opbs` have exit zero and `run_count=1`; array children record `Rerunable=True`. The selected orchestration is `pbs_job_array`; the Checker now enforces consistency between capability and selection.
- Final artifact SHA-256 is `56056fdafed7b0f1bd7f472ca78771f876edb3204cb0989ff97cf1159d9cdc56`. It records commit `c43a519997a581357561981cd448b07a24df5fdb`, `git_dirty=true`, and source fingerprint `sha256:ccdc35c09745ebbdff4be5ae9b50646a04b785a27c84ecc685aa4fd0e345682a`; recomputation on the current source tree matches exactly. The successful raw work directory was deleted by its guarded cleanup.
- The requirement matrix marks FEAS-01 through FEAS-05 as `completion-candidate`, not phase-complete. FEAS-04 explicitly labels the older manifest-fallback artifact as historical cross-version evidence.

### Finding dispositions

Codex report for `c43a519…`:

- `Medium — FEAS-05 zero-write evidence detached from actor`: **fixed** with the guarded positive-control runtime and per-case write counts described above.
- `Low — orchestration/capability consistency`: **fixed** in the Checker with both array and fallback negative tests.
- `Low — source-gate defensive branch coverage`: **fixed** with direct table-driven `check_gate()` tests for dirty-only, marker protocol, marker config checksum, and missing resolved config.
- `Low — wall-clock discontinuity not bounded`: **fixed** with a 0.1-second frozen bound, aggregate field, Checker assertion, and negative test.

Claude report for `c43a519…`:

- `M1 — FEAS-01 only single-node`: **fixed** by the two-rank holder/contender/successor experiment while retaining the single-node control.
- `M2 — hostname-ordering regression missing`: **fixed** with writer JSON hostname/PID assertions, direct contention aggregation, and Checker one-host negative cases.
- `M3 — FEAS-05 non-observations`: **fixed** with matching positive signals and mismatch zero signals for runtime import and guarded DB writes.
- `L1 — manual independent capability self-asserted`: **fixed** by renaming it `independent_job_query_supported` and deriving it from the real parent qstat result; actual restart remains correctly assigned to Phase 1.
- `L2 — missing business snapshots and orchestration consistency fail-open`: **fixed** with typed exact-count snapshots and cross-field selection assertions.
- `L3 — duplicate cache booleans`: **fixed** at the decision boundary. The retained descriptive field remains for artifact compatibility, but the Checker no longer treats it as independent evidence; it directly validates polluted epochs, canonical selection, business outcome, and repair.
- `L4 — shared-SQLite module documentation drift`: **fixed** in `docs/modules/scripts.md` and `docs/07-operations.md`.
- `L5 — governance change unrecorded`: **fixed** by the fact record in this entry.
- `L6 — obsolete failure header`: **fixed** by the append-only correction in `failures.md`; prior text remains immutable.
- `L7 — matrix prematurely complete`: **fixed** with explicit `completion-candidate` status until the phase-final gate passes.
- `L8 — cross-version fallback citation`: **fixed** by labeling the 01:12 artifact as historical cross-version evidence.
- `L9 — aggregate empty maximum`: **fixed**; all-starved inputs retain `maximum_writer_wait_seconds=null` and flow to structured Checker failure instead of raising during aggregation.
- `L10 — queue latency coupled to BLOCKED`: **deferred-with-justification**. A successfully submitted but unobserved child is scheduler-uncertain, so claiming array unsupported and silently selecting fallback would be unsafe; `BLOCKED` is retryable and preserves the raw evidence. Owner: Phase 1 scheduler reconciliation/backoff implementation.
- `L11 — ignored `uv.lock` absent from fingerprint`: **fixed** by explicit-file-scope capture and regression coverage.

### Inferences

- The Medium findings from both second-round reviews now have behavioral tests and new two-node evidence. Remaining deferred findings are Low severity and assigned to the Phase 1 work unit that implements their real protocol context.
- Cross-node FEAS-01 and source-gated runtime writes change key acceptance evidence, so the next commit is another review target under the key-invariant repetition rule. Phase 1 remains blocked until that final independent gate passes.

### Follow-up

- Freeze this evidence-matched tree, repeat the independent dual review against the plan branch point, then create a phase-final commit only if both reports approve or all new findings are dispositioned without another key-invariant change.

Artifacts:

- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-095900_phase0-feasibility_pass.json`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-095900_phase0-remediation_review.log`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-100100_phase0-remediation-tests_review.log`.
- `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-100000_phase0-remediation-tests_review.log` (failed focused attempt; retained in full).

## 2026-08-06T10:04:19+09:00 — Phase 0 second-review artifact retention cleanup

### Facts

- Scheduler history confirmed that representative focused-test job `2497331.opbs` and representative full-run job `2497333.opbs` are both terminal with `Exit_status=0`; duplicate jobs `2497332.opbs` and `2497334.opbs` are also terminal.
- The authoritative PASS evidence remains `20260806-095900_phase0-feasibility_pass.json` plus its complete remediation log, the smallest successful focused-test log remains `20260806-100100_phase0-remediation-tests_review.log`, and the complete failed focused-test log remains `20260806-100000_phase0-remediation-tests_review.log`.
- After exact-path inventory, three redundant successful-run files totaling 99,093 bytes were deleted: `20260806-095849_phase0-feasibility_pass.json`, `20260806-095849_phase0-review2-remediation_review.log`, and `20260806-100200_phase0-remediation-tests_review.log`. They are not recoverable from the workspace; their duplicate scheduler jobs remain auditable through PBS history.
- A credential-pattern scan of the authoritative JSON/log and both immutable second-review reports found no credential value or environment dump. The sole text match was the Claude report's explicit statement that `Variable_List` was excluded from the final artifact.

### Inferences

- The retained files are the smallest representative evidence set for the completed remediation while preserving the only failed-attempt log required for root-cause audit.
