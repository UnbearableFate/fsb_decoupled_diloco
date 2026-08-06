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

## 2026-08-06T11:50:00+09:00 — Phase 1 Syncer HA completion candidate PASS

### Facts

- Phase 1 implements full/static HA behind `coordination.syncer_ha.enabled=false` by default. A one-time `init-run` is the only schema/authority creator; schema v2 freezes run/source/config identity. `open_existing` and `open_readonly` have no implicit DDL, and fragment + HA fails closed.
- `LeaderLeaseStore` issues monotonic epochs. `FencedSQLiteStore` and its leader-bound adapter require an exact `LeaderToken` for all HA business mutation, revalidate it inside `BEGIN IMMEDIATE`, preserve named parameters and reject raw transaction/DDL escape. The frozen inventory contains all 31 legacy mutators; fragment mutators remain legacy-only.
- Checkpoints and canonical latest/stop/summary are epoch-scoped. DB version/control manifests carry writer, publication ID, relative path, size and configured digest. The fixed control files are convenience caches; `EpochControlReader` trusts only the current DB epoch and matching manifest SHA. DB-first successor recovery is idempotent across all publication windows.
- HA maintenance uses recursive `RunPaths` discovery, a fenced register/recheck/delete ledger, old-epoch orphan cleanup and bounded epoch history/control compaction. Candidate/epoch logs are owner-scoped. Optional learner-assisted recovery submission is disabled by default and, when enabled, reconciles qstat fingerprints with atomic claims, backoff, uncertainty and attempt/outstanding budgets before qsub; it never grants leadership.
- PBS role scripts validate descriptor/source identity before runtime import. The learner array is rerunable; the syncer candidate and learners are independent jobs sharing only the run filesystem/SQLite. All newly submitted acceptance jobs used minute-scale walltime overrides derived from observed workload.

### Associated tests and fault evidence

- PBS `2497905.opbs` on compute host `mg0012` ran the focused Phase 1 suite and full repository suite: `20 passed`, then `397 passed`, exit 0. Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-115000_phase1-test-suite_pass.json`.
- PBS `2497906.opbs` completed the six publication failpoints (`weight_temp`, `after_weight`, `after_outer`, `sqlite_transaction`, `after_db_commit`, `after_latest`) for 10 repetitions each. All 60 child processes died by injected `SIGKILL`, successor recovery reached v2/epoch 3, and every stale write was rejected. Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-113506_phase1-fault-matrix_pass.json`.
- Two-node PBS `2497922.opbs` on `mg0019/mg0023` proved both lock boundaries. Transaction-outside takeover acquired epoch 2; transaction-inside acquire observed `database is locked` and no tentative value until the old writer was killed, then acquired epoch 2 with integrity preserved. Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-113934_phase1-lock-boundary_pass.json`.

### Independent 9-node acceptance and completed Checker

- Launcher `2497948.opbs` initialized run `plan02_phase1_acceptance_2497948` and submitted crash candidate `2497950.opbs` (1-minute request), successor `2497951.opbs` (2 minutes) and rerunable 8-member learner array `2497952[].opbs` (2 minutes). The first leader was killed at `after_db_commit`; the successor acquired epoch 2 from a different PBS job and completed at v10.
- All eight learners stopped cleanly after contributing. Their final local steps were 160–184, so the verified workload exceeds the repository's 50-local-step × 10-global-step baseline on the local-step axis. Complete training time was 15.135 seconds, terminal total was 5,120 tokens, and the run is recovery/coordination evidence rather than a quality result.
- Read-only completed Checker `2497957.opbs` returned `PASS`: schema/protocol `2/3`, integrity `ok`, DELETE/FULL/query-only PRAGMAs, contiguous epochs 1–2, contiguous versions 0–10, terminal finalized by epoch 2, 11 weight/11 outer discoveries, 8 proposal pointers, one current syncer heartbeat, zero failure events across 2,712 log events, and bounded 47-page SQLite state.
- Artifacts: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-114613_phase1-independent-launch_pass.json` and `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-114700_phase1-completed-checker_pass.json`.

### Documentation and remaining gate

README, architecture, runtime/data flow, configuration, operations and module references now describe the verified HA authority chain, independent jobs, manual takeover, digest policy and 9-node result. PBS guidance requires a workload-based shortest practical walltime and explicit qsub override of materially longer defaults.

Phase 1 remains a completion candidate until the frozen review-target commit, mandatory Codex review, optional Claude Opus 5 review or `skipped-session-limit` record, finding disposition and phase-final commit are complete. Phase 2 has not started.

## 2026-08-06T12:00:10+09:00 — Phase 1 final associated tests PASS

After documentation synchronization and the new PBS walltime rule, login-node static validation passed (`bash -n`, literal group IDs, Ruff, `py_compile`, `git diff --check`). PBS `2498031.opbs` requested the estimated minimum practical walltime of 1 minute, started immediately on `mg0007`, and used 27 seconds. Focused Phase 1 result was `20 passed in 1.60s`; the full repository result was `399 passed in 22.93s`; exit status was 0. This supersedes the earlier 397-test count only as the latest full-tree enumeration and does not alter that earlier run's immutable evidence. Artifact: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-120100_phase1-final-test-suite_pass.json`.

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

## 2026-08-06T10:31:59+09:00 — Phase 0 completion gate PASS

### Facts

- The user changed the scoped completion rule in `plans/AGENTS.md`: a Codex-external reviewer that returns a verified session/usage quota exhaustion is recorded as `skipped-session-limit`; it produces no synthetic report, requires no retry, and does not block the current phase, plan, or subsequent work. Codex review remains mandatory. Ambiguous API errors, model/fallback mismatches, authentication failures, and permission-mode failures remain blocking.
- The immutable Codex report for review target `f404fbd4831adcd9ffb8e6229a0004b1affe9f4e` against base `c1c61153548ff7b2543d3ce1bc764c19432b138e` is `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/gpt-5.6-sol_f404fbd4831adcd9ffb8e6229a0004b1affe9f4e.md`. It reviewed the complete cumulative Phase 0 diff, found no `Critical`, `High`, `Medium`, or `Low` issue, and returned `APPROVE`.
- Claude attempts with sessions `4ca72c8d-7e52-4422-99f3-eb0dc04d1b0b` and `6f6f98ee-05b8-44f6-a2dc-de003438aa15` both returned explicit `You've hit your session limit` results. Machine-readable metadata verified actual/canonical model `claude-opus-5`, the requested session IDs, no fallback, and no permission denial; neither attempt created a report or changed the implementation tree. Under the new rule this reviewer is now `skipped-session-limit`, and the earlier append-only failure records remain as historical execution facts rather than current blockers.
- Every finding from the two earlier completed review rounds was already dispositioned in the 09:24 and 09:59 progress entries. The final Codex review produced no new finding requiring disposition.
- The authoritative Phase 0 artifact remains `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-095900_phase0-feasibility_pass.json`, SHA-256 `56056fdafed7b0f1bd7f472ca78771f876edb3204cb0989ff97cf1159d9cdc56`, with all FEAS-01 through FEAS-05 results `PASS`. The requirement matrix now marks those five rows `complete`.

### Inferences

- Phase 0 is complete under the current user-directed gate. The missing Claude report is an explicitly recorded quota skip, not an approval and not a source-quality claim.
- Phase 1 may begin from the Phase 0 boundary frozen in the research plan: scheduler recovery remains opt-in, a stopped in-transaction writer requires audited termination, rerunable jobs require physical-incarnation identity, and the measured 7.37-second writer wait is a tuning input.

### Follow-up

- Create the Phase 0 final commit, then begin Phase 1 work units in strict requirement order without rerunning the skipped external review.
## 2026-08-06 12:28 JST — Phase 1 self-review regression group

- 目标：验证 learner 纯文件系统 epoch reader、本地 monotonic lease safety、跨 observation recovery outstanding 预算、历史 qstat suspended reconciliation、当前 observation attempt 预算持久性、显式 PBS walltime 和 completed Checker canonical control gate的组合修改。
- 修改：HA learner把 `error` terminal generation视为可恢复诊断而非最终 stop；recovery claim归档保留当前 stale observation并在归档 receipt-missing claim前执行 reconciliation；独立 launcher在创建 run root前校验提交 walltime；completed Checker要求一个 active version及与 DB terminal/current row一致的 canonical latest/stop/summary。
- 静态验证：`.venv/bin/ruff check fs_diloco tests scripts/miyabi --output-format concise`、`git diff --check`、`bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh`和 PBS group placeholder扫描均通过。
- 运行环境与命令：Miyabi `debug-g` compute node `mg0012`，PBS `2498172.opbs`；提交前按此前 27 秒证据估算并显式使用 `qsub -l walltime=00:01:00 scripts/miyabi/run_plan02_phase1_tests.pbs`。
- 结果：focused `27 passed in 5.46s`；全量 `406 passed in 21.82s`；job `Exit_status=0`，实际 walltime `00:00:30`。结构化摘要：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-122800_phase1-test-suite_pass.json`。
- 边界：该轮在 dirty review candidate上运行，只证明关联回归；冻结 review-target commit后仍需重跑 fault/lock/1+8 acceptance与 completed Checker，才可进入 Phase 1完成门禁。

## 2026-08-06 12:34 JST — Phase 1 claim/GC/digest adversarial regression PASS

- 目标：补足 HA-04、HA-12、HA-14和 HA-15 的直接反例，验证 8 个并发 claimant不会利用 mkdir→claim.json窗口创建相邻 attempt，scheduler命令异常不会使 learner控制流异常退出，takeover发生在 GC claim与unlink之间时旧 token只能删除已冻结 orphan，以及 `off/checker/always` 三种 checkpoint digest发布语义。
- 修改：missing claim.json使用 attempt目录mtime作为有界不确定起点；当前 observation直到看到更新 observation前不归档；PBS query/submit/fingerprint lookup把命令缺失和 timeout转为可审计失败结果；新增并发 claim、scheduler timeout、stale-GC takeover和三 digest模式测试。
- 静态验证：Ruff、`git diff --check`、全部 PBS/shell `bash -n`和 literal group扫描通过。
- 运行环境与命令：Miyabi `debug-g` node `mg0012`，PBS `2498225.opbs`；依据上一轮实际 30 秒继续使用最短可行 `qsub -l walltime=00:01:00 scripts/miyabi/run_plan02_phase1_tests.pbs`。
- 结果：focused `33 passed in 5.15s`；全量 `412 passed in 21.23s`；job `Exit_status=0`，实际 walltime `00:00:28`。结构化摘要：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-123410_phase1-test-suite_pass.json`。
- 边界：仍是 dirty completion candidate证据；冻结 commit后的 final ladder尚未执行。

## 2026-08-06 12:45 JST — Phase 1 completed-terminal repair regression PASS

- 目标：验证正常 HA 终态在 early stop、summary 或 maintenance窗口中断后仍可由 successor取得新 epoch完成修复，并且只有 post-maintenance generation的 canonical stop/summary路径、owner、epoch与 SHA 全部匹配时，后续 candidate才拒绝取得运行权。
- 修改：正常收尾先发布 early stop generation，maintenance成功后再提交并成对发布更高 terminal generation；candidate对 completed DB terminal验证两条 control publication及文件 SHA，不完整时允许 takeover；repair路径重建 latest/heartbeat/summary、重跑幂等 maintenance并发布更高 generation；新增端到端协议回归。
- 静态验证：修改范围 Ruff、`git diff --check`、全部 PBS/shell `bash -n`和 literal group扫描通过。
- 运行环境与命令：Miyabi `debug-g` node `mg0004`，PBS `2498346.opbs`；依据此前 28秒证据估算并显式使用最短可行 `qsub -l walltime=00:01:00 scripts/miyabi/run_plan02_phase1_tests.pbs`。
- 结果：focused `34 passed in 5.56s`；全量 `413 passed in 22.70s`；job `Exit_status=0`，实际 walltime `00:00:31`。结构化摘要：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-124537_phase1-test-suite_pass.json`。
- 边界：这是 dirty completion candidate上的关联回归；冻结 review-target commit后仍需执行最终 fault/lock/1+8 acceptance与 completed Checker。

## 2026-08-06 12:46 JST — Phase 1 two-generation terminal smoke PASS_WITH_FOLLOWUPS

- 目标：在真实 GPU compute node运行完整 tiny HA训练，确认 early terminal generation使两个 learner停止，末次摄取与 maintenance后 DB terminal推进到 generation 2，并为该 generation登记 canonical stop和summary。
- 命令与walltime：Miyabi `debug-g` node `mg0007`，PBS `2498353.opbs`；依据上一 smoke实际12秒，显式使用 `qsub -l walltime=00:01:00 scripts/miyabi/run_plan02_phase1_smoke.pbs`，实际walltime `00:00:12`。
- 结果：job `Exit_status=0`；最终version 2、leader epoch 1且released、terminal generation 2；generation 1只有early stop，generation 2有stop/summary两条publication；staged Checker返回`PASS_WITH_FOLLOWUPS`。结构化摘要：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-124637_phase1-smoke_pass.json`。
- 边界：本轮run使用dirty source fingerprint，只是协议回归；正式Phase 1证据仍须固定到clean review-target commit。

## 2026-08-06 13:00 JST — Phase 1 clean 1+8 independent-job acceptance PASS

### Facts

- 冻结实现为commit `24e181bc8031e68ae32310c628d56422b3d7654b`；runtime source fingerprint为`sha256:5b5bac2807e9b1caa976e72bc8824721d1703d81e609b5368eacdc65a00e097f`，run `plan02_phase1_acceptance_2498481`的descriptor SHA为`7b7db838e74c26657c4e7d6802f66d2fc0811bafe6ec4a790cc6bc3347bd9c11`。
- 最短walltime依据历史实际用时估算：launcher请求15秒/实际1秒；crash syncer请求20秒/实际5秒并按预期`Exit_status=137`；successor请求45秒/实际19秒并`Exit_status=0`；8个learner各请求45秒/实际30秒并全部`Exit_status=0`；completed Checker请求20秒/实际5秒并`Exit_status=0`。
- 独立job为launcher `2498481.opbs`、crash syncer `2498482.opbs`、successor `2498483.opbs`、8节点learner array `2498484[].opbs`、Checker `2498521.opbs`。epoch 1提交v0后在`after_db_commit` failpoint被SIGKILL；epoch 2从DB恢复并连续提交v1–v10。
- completed Checker验证terminal generation 2、final version 10、5120 seen tokens、leader released、canonical latest/stop/summary与DB一致、无runtime failure event、无error/warning并返回`PASS`。结构化主证据：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-130015_phase1-completed-checker_pass.json`，SHA-256 `a49fd10e97f0d2b22efdd9111738d5e7eb3ddeb7332d9b9ea564d0172424dcba`。
- 同一clean源上的G2测试为PBS `2498366.opbs`（focused 34 passed / full 413 passed，29秒）；G3故障矩阵为`2498449.opbs`（6 failpoints × 10 iterations，123秒，artifact SHA-256 `061695f4ca542b3c7200f634c58c6a76f440927a7af71892fdcf90e19255e468`）；G4两节点lock为`2498464.opbs`（6秒，artifact SHA-256 `0136bcccf04dd4848cdbd6e338ce27276f12c2d332827ed9412ddc9ee1e61d16`）；clean smoke为`2498472.opbs`（12秒，terminal generation 2）。

### Inferences

- Phase 1的实现与实验阶梯已满足完成候选条件。该9节点workload每个learner约200以上local steps并完成10个global merge，超过仓库50-local-step × 10-global-step文档同步基线，因此同步更新了`docs/07-operations.md`；这是HA恢复与协议证据，不产生训练质量结论。

### Follow-up

- 对累计Phase 1 diff执行`plans/AGENTS.md`规定的Codex必做审查及Claude `claude -p`非交互审查；完成findings处置后才将HA-01…HA-20标记complete并进入Phase 2。

## 2026-08-06 13:25 JST — Phase 1 completion review opened; Claude skipped-session-limit

### Facts

- 冻结审查目标为`6042886f1a1ec55759cc01c1af230ab82c0f9ebe`，comparison base为Phase 0 final `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`。Codex先启动外部reviewer，然后在读取任何外部实质结论前完成独立审查并保存不可变报告`reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-1/gpt-5.6-sol_6042886f1a1ec55759cc01c1af230ab82c0f9ebe.md`；结论为`CHANGES_REQUIRED`。
- 外部调用为fresh `claude -p`，请求完整模型ID`claude-opus-5`、session `b602dc27-fd5d-4cdc-a90a-6db12a39fa28`、JSON输出、`bypassPermissions`和`--dangerously-skip-permissions`，无resume/continue/fallback。进程在零模型token/零turn时返回HTTP 429及明确文本`You've hit your session limit · resets 1:30pm (Asia/Tokyo)`，`permission_denials=[]`；因请求未进入模型，返回中没有可声称的实际model usage。没有Claude报告或实现改动产生。
- 按`plans/AGENTS.md`的唯一限额例外，此reviewer记为`skipped-session-limit`；这不是approval，但不阻断finding处置、当前phase、plan或后续任务，且不得要求重试或伪造报告。

### Codex finding disposition plan

- `Medium — partial independent qsub loses accepted syncer receipt`：接受，修改launcher逐次保留结构化receipt，第二次qsub失败返回`partial`及syncer job ID、CLI非零退出且不自动qdel，并新增先成功后失败的RED回归。
- `Medium — post-acquire startup failures escape cleanup`：接受，把acquire后的store/logger/renew线程部分初始化纳入单一cleanup ownership guard；renew线程启动超时先置stop；新增store-open与renewer-start失败回归。
- `Medium — canonical repair watchdog state/tests absent`：接受，在filesystem reader保留进程内epoch/owner/latest/terminal单调水位，消费`canonical_repair_wait_seconds`并记录两个要求的counter/event；补heartbeat无merge、旧600秒预算、canonical repair超窗和lower-epoch terminal拒绝回归。
- `Low — shared recursive discovery contract only partially adopted`：暂定`deferred-with-justification`至Phase 2 MEM-02/MEM-20。Phase 1 static路径与正式evidence均非空且正确；把liveness/analysis/metrics的公开发现接口改为dynamic instance递归语义属于Phase 2 identity/path contract，届时统一迁移，避免Phase 1先引入没有admission validator的半套dynamic扫描。负责人：Phase 2 implementation work unit。

### Verification boundary

- 修复触及learner control observation和syncer startup ownership，但不改变DB schema、checkpoint/terminal格式或fenced transaction协议。先执行静态检查和完整Phase 1/full pytest；若通过，再用最短受影响runtime smoke验证canonical publication，正式1+8只在该smoke显示运行时行为变化时重跑。

## 2026-08-06 13:42 JST — Phase 1 review remediation PASS

### Facts

- Codex的三个Medium finding均已修复：独立launcher逐个保留qsub receipt且partial submission非零退出；leadership acquire后的store/logger/renewer失败统一释放精确token和部分资源；filesystem epoch reader保留进程内单调水位、消费`canonical_repair_wait_seconds`并暴露`cache_rejected_lower_epoch_count`/`canonical_repair_wait_count`事件与计数。
- `Low — shared recursive discovery contract only partially adopted`维持`deferred-with-justification`至Phase 2 MEM-02/MEM-20。Phase 1的static路径与正式evidence非空且正确；dynamic instance递归发现必须与Phase 2的identity/path admission contract一起迁移，避免先暴露无validator的半套API。负责人：Phase 2 implementation work unit。
- 首轮remediation smoke `2498588.opbs`暴露final maintenance与HA heartbeat atomic writer的真实竞态，已在`failures.md`追加记录。authority temp现在至少等待lease/clock-skew GC grace，proposal temp的input-closed即时清理不变；RED回归验证fresh temp保留和grace边界删除。
- replacement完整测试为PBS `2498597.opbs`，请求walltime `00:00:45`、实际`00:00:31`、`Exit_status=0`：Phase 1 focused `39 passed in 5.68s`，全量`418 passed in 22.11s`。
- replacement tiny HA smoke为PBS `2498612.opbs`，根据历史12–13秒显式请求最短可行walltime `00:00:20`，实际`00:00:12`、`Exit_status=0`。run `plan02_phase1_review_remediation_2498597`达到final version 2、terminal generation 2、leader released、canonical stop/summary齐全、staged Checker `PASS_WITH_FOLLOWUPS`，日志无`error`、`uncaught_exception`或`lease_renewer_stop_failed`事件。
- 结构化证据为`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-134242_phase1-review-remediation_pass.json`；test log SHA-256 `1675b1aaba72f98d24a408ce32ec41b5aa0c0105f0856b08261a309e0235a101`，smoke log SHA-256 `49a71d505de3a0e70f290822532d4e64f33163084de80045367d48e765175e6e`。

### Inferences

- 受影响runtime smoke与完整回归均已通过，因此无需重跑已经冻结通过的正式1+8 acceptance。修复改变learner observation liveness和syncer startup/maintenance并发边界，属于关键invariant remediation，必须冻结新target并重复completion review gate。

### Follow-up

- 完成最终静态检查，提交新的Phase 1 review target，然后按`plans/AGENTS.md`对Phase 0 base到新target的累计diff重复Codex及fresh `claude -p`审查。只有该门禁完成后才把HA-01…HA-20标记`complete`。

## 2026-08-06 14:43 JST — Phase 1 repeat-review remediation关联验证

- 目标：处置review target `b21b29f83067dd36b4ff5ac295fe1f894da64e21` 的独立Codex/Claude findings，重新验证candidate/renew writer-lock、fenced business store、filesystem epoch reader、adoption stop authority、GC逐文件fence、递归discovery、source gate、SQL fence、性能遥测与post-acquire cleanup。
- 实现摘要：candidate在`SQLITE_BUSY/locked`时按总预算轮询；renewer在本地monotonic lease安全边界内重试并报告真实transaction/heartbeat wall+CPU；业务store使用独立60秒busy timeout且每次写仍在得锁后复验token；reader加入短扫描cache并只容忍低于最高合法epoch的torn目录；adoption只认authoritative terminal；旧epoch orphan先登记ledger、每个unlink前短fenced claim/引用复查，terminal时推进候选；analysis/metrics/Checker/probe统一使用`RunPaths`递归iterator；run descriptor新增git-dirty direct gate；Checker显式输出并加入§11.1核心指标与缺失阻断；SQL proxy拒绝CTE/注释伪装及写PRAGMA；文档同步上述边界。
- 测试：PBS `2498869.opbs`在`mg0012`使用基于31秒历史实测估算的显式`00:00:45` walltime，实际32秒、`Exit_status=0`；focused `51 passed in 5.87s`，full tree `432 passed in 22.84s`。证据`artifacts/20260806-143412_phase1-review-fixes-tests_pass.log`，SHA-256 `6ecdf5c4f69de1b2e22d309af94956b5aa95ce6ae2435f09e03e7a5fd136ac16`。
- fault/lock：PBS `2498876.opbs`按此前122秒实测请求`00:02:20`，实际125秒、exit 0；六个failpoint×10次共60 case全部PASS，artifact `20260806-1435_phase1-fault-matrix_pass.json`（SHA-256 `08f4ac36307a397f13a570f7b7c0c5456b03820978d0b699cdb45b58633669e1`）。2节点lock PBS `2498878.opbs`按此前6秒实测请求`00:00:15`，实际7秒、exit 0；outside takeover直接取得epoch2，inside先观察`database is locked`且tentative value为空、kill holder后取得epoch2；artifact `20260806-1435_phase1-lock-boundary_pass.json`（SHA-256 `3b917b7d17e3d1a7af0e5ebe25bc6c04256dd4527f9e5b239fb76bdcbc0448`）。
- smoke：同步Checker必填output后的replacement PBS `2498882.opbs`按12–13秒历史请求`00:00:20`，实际12秒、exit 0；v2/generation2/released，runtime无error，staged Checker `PASS_WITH_FOLLOWUPS`。artifact `20260806-1436_phase1-smoke-checker_pass.json`（SHA-256 `7724c00ab4e3d1a7af0e5ebe25bc4d9ef2193bf8b8d17e3c10719f66af22699b`）。本次smoke报告72个成功/0失败business transaction、1个成功/0失败renew、10次learner control scan与231 cache hit；staged模式唯一缺失核心字段是预期中的takeover sample。
- 处置状态：Codex的post-renewer startup cleanup finding为`fixed`并由terminal-state startup failure精确资源释放回归覆盖。Claude H1、M1–M9、L1–L3、L5–L9及§11.1 telemetry gap均为`fixed`并由上述关联组覆盖。L4（未被production调用的`LeaderLeaseStore.assert_current`及其私有时间map）为`deferred-with-justification`：当前公开HA写路径只使用共享`LeaseSafetyTracker`，立即删除该无调用诊断API会制造无收益接口变更；负责人为本plan Phase 2存储surface整理，完成前重新确认无调用后删除或明确保留。Claude H2保持Phase 1 blocking，必须在clean phase-final target上重跑1+8独立job takeover与completed Checker后才能标记`fixed`。
- 保留与清理：保留structured Checker/fault/lock artifacts、focused/full摘要、失败与replacement smoke日志；已核对并删除只属于已结束测试的`plan02_phase1_lock_2498878`及两次smoke run目录（约1.9 MiB，checkpoint/payload/W&B重复数据），不可恢复；没有删除报告、source/config或任何live/resumable run。
- 后续：创建clean review target，重跑最终1+8 acceptance与completed Checker；completed性能门禁必须有≥100真实renew、0 renew/business failure、business/renew p99与takeover latency阈值均通过。由于本轮修改并发/持久化/GC关键不变量，final acceptance后还需针对新target重复双模型完成审查。

## 2026-08-06 15:09 JST — Phase 1 final 1+8 acceptance and performance gate PASS

### Facts

- 最终clean实现为commit `831b1751c5572c39121113ac73099238f3fa9ed4`；run `plan02_phase1_final_831b175`的source fingerprint为`sha256:559daa3a650c9647781eb54bdfd19cbad32c836b10cd28a9418982ba506bb253`。在此前两次完整acceptance中，第一次暴露scheduler启动顺序导致的GPU slot死锁，第二次完成协议但business p99因过密采样超过门限；两项根因、修复和失败job均已完整追加到`failures.md`。
- 第三次launcher `2499027.opbs`请求最短实用walltime `00:00:10`、实际1秒并成功提交全部独立job；crash syncer `2499028.opbs`请求`00:00:15`、实际5秒并按预期在`after_db_commit`以137退出；successor `2499029.opbs`请求`00:00:30`、实际23秒且exit 0；8节点learner array `2499030[0-7].opbs`各请求`00:00:25`、实际17–19秒且全部exit 0；completed Checker `2499051.opbs`请求`00:00:10`、实际5秒且exit 0。每个walltime均依据紧邻失败/成功实测加最小调度余量显式覆盖，而非沿用PBS脚本的较长默认值。
- successor先获得GPU slot，epoch 1在提交v0后被杀死，epoch 2从DB恢复并连续提交v1–v10；最终terminal generation 2、5,120 tokens、leader released，canonical latest/stop/summary与DB一致。Checker返回`PASS`，errors/warnings/runtime failure均为0。
- §11.1性能可靠性门禁全部通过：business transaction 475 samples、0 failure、p95 `0.010985s`、p99 `0.020734s`、max `0.034290s`，低于p99 `0.05s`门限；renew 116 samples、0 failure、1 busy retry、p95 `0.022382s`、p99 `0.038958s`、max `0.085846s`，低于p99 `3.75s`门限；takeover `1.012068s`，低于`10.2s`门限。
- learner control reader覆盖8个进程、554次真实scan和9,450次cache hit，wall/CPU分别为`1.427571s`/`0.590398s`；heartbeat publication 114次；50次candidate observation产生0次writer transaction attempt；stale business commit和canonical adoption error均为0。boundedness为1个epoch目录、0 active claim、0 GC candidate row；7,188个runtime event中无failure。
- 最终结构化证据为`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1508_phase1-independent-launch_pass.json`（SHA-256 `17d126d44ed5cb075956459b8476d36bfe8138151b62acf1e16d0d3927cbfc2d`）、`20260806-1508_phase1-init-run_pass.json`（SHA-256 `d389db622af396e67ed504f86cb0dadf83424b76dbac770c69109d9bab4571bf`）及`20260806-1509_phase1-completed-checker_pass.json`（SHA-256 `cc1c956d5970b6ae181004addd5b50e79b34e7a1ecb7b3fdba085b74e730e20b`）。

### Finding disposition

- Claude review target `b21b29f83067dd36b4ff5ac295fe1f894da64e21`的`High — final acceptance predates reviewed source`现为`fixed`：本轮1+8、takeover和completed Checker全部直接运行于最终clean commit `831b1751c5572c39121113ac73099238f3fa9ed4`，且新性能门禁完整通过。
- 由于为满足该finding而修改了launcher调度依赖与acceptance采样配置，最终commit是新的关键不变量target，仍必须重复Phase 1累计diff的Codex与fresh `claude -p`完成审查。

### Follow-up

- 以Phase 0 final `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`为comparison base，对`831b1751c5572c39121113ac73099238f3fa9ed4`执行最后一轮独立完成审查；门禁关闭前HA-01…HA-20保持`completion-candidate`。

## 2026-08-06 15:26 JST — Phase 1 final-target review opened; Claude skipped-session-limit

### Facts

- 本轮目标为`831b1751c5572c39121113ac73099238f3fa9ed4`，comparison base为`1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`。Codex在读取任何本轮Claude实质结论前保存不可变报告`reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-1/gpt-5.6-sol_831b1751c5572c39121113ac73099238f3fa9ed4.md`，结论为`CHANGES_REQUIRED`。
- 外部reviewer使用fresh `claude -p`，显式模型`claude-opus-5`、session `4b13f9d7-973d-4298-9752-e756f6cd8cc8`、JSON输出、`bypassPermissions`及`--dangerously-skip-permissions`，无resume/continue/fallback。机器元数据验证实际及canonical model均为`claude-opus-5`、session ID匹配、`permission_denials=[]`；79 turns后返回明确`You've hit your session limit · resets 6:40pm (Asia/Tokyo)`。进程没有创建Claude报告或修改实现树。
- 按`plans/AGENTS.md`唯一限额例外，该外部reviewer记为`skipped-session-limit`；这不是approval，不创建或伪造报告，不要求重试，也不阻断当前finding处置和后续工作。Codex报告仍是必做门禁且已完成。

### Codex finding disposition plan

- `High — completed Checker fabricates strict-zero results and can miss shutdown renewal failure`：接受。把renewer最终停止与metrics snapshot排序收敛为可审计边界；Checker从DB epoch/owner映射和完整runtime failure vocabulary推导，而非写死canonical/stale零值；为非零/缺失/renewer-stop失败新增会使completed mode返回`BLOCKED`的RED测试。
- `Medium — cross-observation global outstanding budget is not atomic`：接受。为recovery claims增加全局atomic reservation lock，使全局outstanding检查、per-observation attempt预留及durable pending receipt属于同一仲裁；新增两个不同observation key的并发RED回归。
- `Medium — acceptance launcher loses partial qsub receipts`：接受。每次qsub后立即原子更新同一结构化artifact，partial失败保留全部已接受job ID、失败receipt与短walltime且非零退出，不自动qdel；新增可单测的提交编排回归。
- `Medium — two matched §11.1 gates absent`：接受。新增独立matched microbenchmark artifact与completed Checker必填输入，分别验证健康leader candidate observer相对无candidate的business transaction p99回归及normal checkpoint publish相对matched legacy baseline；candidate writer attempt使用明确instrumentation而不是`writer_lock_blocked`事件替代。
- `Low — parenthesized PRAGMA setter bypass`：接受。只允许无setter参数的显式只读PRAGMA及建模的只读参数形式，覆盖`journal_mode(WAL)`、`synchronous(OFF)`、`query_only(OFF)`和`busy_timeout(1)`。
- `Low — universal recursive discovery`：维持`deferred-with-justification`至Phase 2 MEM-02/MEM-20，负责人和边界不变。

### Follow-up

- 先新增RED回归并修复上述行为，再运行完整关联测试。matched性能验证和任何需要qsub的replacement都根据紧邻实测显式估算最短实用walltime；修复涉及recovery并发协议与完成门禁，必须冻结新review target并按关键不变量规则再次审查。

## 2026-08-06 15:56 JST — final-target review remediation regression PASS

### Facts

- Codex最后一轮finding已实现：renewer先停止再采集final metrics，stop/release及learner失败事件进入completed blocker；Checker从epoch/version/update/controller/terminal/control publication及每个预期learner的唯一process exit推导stale commit和canonical adoption结果，不再写死零值。
- recovery submission在所有observation key之上新增共享filesystem atomic reservation，使全局outstanding检查、attempt mkdir和durable pending claim同属一次仲裁；两个不同observation的并发回归验证`max_outstanding_candidates=1`时只产生一个attempt和一次qsub。
- acceptance launcher在第一次qsub之前建立pending artifact，并在每个crash/successor/learner qsub之后立即原子保存receipt；后续submission失败保留已经接受的job ID、失败receipt和显式短walltime，非零退出且不自动`qdel`。
- §11.1新增descriptor/source/config绑定的matched性能artifact：健康leader candidate observer与无observer各至少400个interleaved business样本，HA与Plan 01 legacy各至少100个alternating checkpoint样本；两项nearest-rank p99门槛冻结为`baseline * 1.25 + 0.002s`。completed Checker复算门槛并要求candidate writer attempt严格为0。
- fenced SQL proxy现在拒绝parenthesized setter形式的`journal_mode(WAL)`、`synchronous(OFF)`、`query_only(OFF)`和`busy_timeout(1)`；只保留显式建模的只读argument PRAGMA。
- 静态验证通过：Ruff、`py_compile`、`bash -n scripts/miyabi/*.pbs`、literal group ID扫描和`git diff --check`均为0。
- PBS `2499200.opbs`依据此前32秒实测请求`00:00:45`，实际33秒、`Exit_status=0`；focused Phase 1为`65 passed in 6.82s`，full tree为`446 passed in 23.95s`。artifact `20260806-155127_phase1-review-remediation-tests_pass.json`记录完整stdout SHA-256 `f16a68e15be6f764705b759ab4620567f32808bd12f33ec2985aa1ada5e70754`。
- PBS `2499210.opbs`依据此前125秒实测请求`00:02:20`，实际124秒、exit 0，六个failpoint×10次全部PASS；artifact `20260806-155341_phase1-fault-matrix_pass.json` SHA-256为`f299c668ea37235d40678a2319538e307a155b28aece37353d8046f3f88c9cc9`。
- 两节点lock PBS `2499211.opbs`依据此前7秒实测请求`00:00:15`，实际6秒、exit 0；artifact `20260806-155410_phase1-lock-boundary_pass.json` SHA-256为`38d9e4c138bd855839260cf1b1cb7ffb1435dcd627a25c5b656dcad0372b9f97`。
- tiny HA smoke PBS `2499212.opbs`依据此前12秒实测请求`00:00:20`，实际13秒、exit 0；v2、terminal generation 2、leader released，staged Checker `PASS_WITH_FOLLOWUPS`。artifact `20260806-155353_phase1-smoke-checker_pass.json` SHA-256为`208a999d0ecae294701abb1b2093e5379229ac19772182c3e124582fccdfcc16`。
- matched checkpoint probe改为从目标配置实际构建model/seed/tensor后，replacement PBS `2499241.opbs`仍按相邻33秒实测加充分余量请求`00:00:45`，实际34秒、exit 0；focused `65 passed in 6.75s`，full tree `446 passed in 24.10s`。artifact `20260806-160100_phase1-review-remediation-tests_pass.json`记录stdout SHA-256 `9be47591e5fe80a32b5a94f50b94882d5e3b8bde11ccf43dbdaa9cdd2b23d089`。
- walltime规则按用户澄清收敛为“尽可能短且有充分余量”：估算必须覆盖启动波动、运行波动和完整收尾，以更快进入计算节点并可靠跑完为共同目标，不以压到易超时为目标；首次无实测的matched wrapper默认保守使用1分钟，取得实测后再缩短。

### Finding disposition

- Codex High及四个accepted Medium/Low implementation findings均为`fixed`，并有行为回归及replacement fault/lock/smoke证据。recursive discovery Low继续按前述边界`deferred-with-justification`到Phase 2 MEM-02/MEM-20。
- 当前证据运行于dirty remediation source，只证明关联回归。必须提交clean review target后重跑1+8 takeover、matched性能job和completed Checker；matched性能实现首次正式运行也必须绑定该clean run descriptor。

### Follow-up

- 完成静态审计并创建新clean target；在该target上按紧邻实测并保留启动、运行和收尾余量，使用launcher `00:00:15`、crash `00:00:15`、successor `00:00:40`、learners `00:00:35`，随后根据matched预检实测选取有充分余量的短walltime并运行completed Checker。关键不变量改变，最终证据后仍需重复Codex完成审查；Claude本轮已经按规则记为`skipped-session-limit`，不重试且不阻断。

## 2026-08-06 16:05 JST — matched performance compute-node preflight PASS

- 目标：在冻结clean target前验证新增matched performance工具的真实model构建、100+100 checkpoint publication采样、400+400 business transaction采样和结构化输出路径；该预检使用当前dirty remediation source和旧clean run descriptor，因此只作为功能/时长预检，不能作为正式completed Checker输入。
- 提交前静态门禁：`bash -n scripts/miyabi/*.pbs`通过，所有`#PBS -W group_list=...`均为字面`xg24i002`且无placeholder。
- 运行环境与walltime：Miyabi `debug-g` compute node `mg0001`，PBS `2499251.opbs`；首次真实model matched运行保守请求`00:01:00`，实际`00:00:28`、`Exit_status=0`，为启动、运行波动和收尾保留了32秒余量。
- 结果：business baseline/observer各400样本，candidate observation 20次且writer transaction attempt为0，observer p99 `0.010470s`低于允许值`0.040279s`；legacy/HA checkpoint各100样本，HA p99 `0.008285s`低于允许值`0.017494s`；目标tensor为2048个`float32`元素。非正式结构化输出为`artifacts/20260806-1604_phase1-matched-performance-dev_review.json`，SHA-256 `b924a4ea8f39adb2fcef720b92d885767fa690e13f2df48be100ca5c1bbe5853`。
- 后续：正式clean-target matched job仍请求`00:01:00`。28秒实测尚不足以证明30或40秒在调度启动和共享文件系统波动下有可靠余量；一分钟仍是当前最短实用请求。

## 2026-08-06 16:10 JST — clean target `d9ab027` 1+8 acceptance runtime PASS

- clean commit `d9ab027fac7877e46d5cf4dda31d820dc8befbe2`的launcher PBS `2499280.opbs`请求15秒/实际2秒/exit 0；它提交crash `2499281.opbs`（请求15秒/实际5秒/预期exit 137）、successor `2499282.opbs`（请求40秒/实际22秒/exit 0）及learner array `2499283[0-7]`（各请求35秒/实际18–19秒/全部exit 0）。所有walltime均按相邻实测保留启动、运行和收尾余量。
- run `plan02_phase1_final_d9ab027`绑定source fingerprint `sha256:aeba76f01a31cfbb42e4a332ecd7e969c0a582ee3662bb716f8ac3e415319485`及descriptor SHA-256 `93a75184e2617f6b50f4a8151e50cf2411ec8c574eee352b8dcfa5cd78887b61`。launcher artifact为`artifacts/20260806-1610_phase1-independent-launch_pass.json`（SHA-256 `21d6f12fefc1e7fcffb32c1d19b2d8b593eab189c904cc1b92caebada2515ff8`），init artifact为`artifacts/20260806-1610_phase1-init-run_pass.json`（SHA-256 `a3cc3cb514e1e7ee31a3c88812ab788d4097120d3e068d3d418f21c52dde1c6a`）。
- runtime/调度阶梯通过，但随后的matched job `2499293.opbs`返回`BLOCKED`，因此该target没有运行completed Checker且不能作为Phase 1完成证据；失败及采样方法根因已在`failures.md`记录。

## 2026-08-06 16:19 JST — matched performance sampling remediation PASS

- 目标：处置PBS `2499293.opbs`暴露的matched business大块时间漂移，保持冻结的`baseline * 1.25 + 0.002s`门槛不变，并直接证明candidate只读路径没有writer transaction attempt。
- 修改：400+400 transaction改为32个25-sample细粒度AB/BA配对块；单个candidate线程跨块复用，baseline块等待线程idle并验证观察数不变，observer块至少完成一次`terminal_state + observe`；SQLite trace直接计数`BEGIN IMMEDIATE/EXCLUSIVE`。artifact和completed Checker同时核对batch顺序、每块样本数、observer/baseline observation count及instrumentation identity。
- 关联测试：Miyabi `debug-g` PBS `2499320.opbs`在`mg0008`运行focused和full tree；根据最近34秒实测并为启动/运行/收尾波动保留充分余量，请求`00:01:00`、实际`00:00:33`、exit 0。focused `67 passed in 6.95s`，full `448 passed in 23.96s`；结构化证据`artifacts/20260806-1618_phase1-matched-remediation-tests_pass.json`，stdout SHA-256 `3173f8a0f9978820806fadb2dd65b341cf97cd6cf5220e7c52b4c9edaf967eef`。
- compute preflight：PBS `2499323.opbs`在同一node以`00:01:00`请求运行35秒并exit 0。business baseline/observer各400样本、16个observer块各2次观察、16个baseline块均0次观察、SQLite trace writer attempt为0；baseline p99 `0.022691s`、observer p99 `0.023480s`，低于允许值`0.030364s`。checkpoint legacy/HA各100样本，HA p99 `0.014221s`低于允许值`0.019835s`。非正式dirty-source预检artifact为`artifacts/20260806-1619_phase1-matched-performance-remediation-dev_review.json`，SHA-256 `713dd40e93235340324f93e8720366516be96a13161a0b1115e5055c72fdfe78`。
- 边界：preflight故意绕过wrapper source gate，用当前dirty benchmark实现读取上一clean run descriptor，只证明采样逻辑和时长；不能作为completed Checker输入。提交新clean target后仍需绑定新descriptor执行正式1+8、matched gate和completed Checker。

## 2026-08-06 16:24 JST — final clean 1+8, matched performance, and completed Checker PASS

### Facts

- 最终clean executable source commit为`36762854bfcbbc23b71ab838913023d64cf37b5e`，source fingerprint `sha256:c2cd29b369c4825af1cf491ad414de68e11c77e78df0173915f6ef834827dbe2`。run `plan02_phase1_final_3676285`的descriptor SHA-256为`151a3f77ffa8c54bb9e2a038e36ed04382d1ccfa2ecfd75c401627cc3ba6bc17`，config SHA-256为`1b0afea92e9ffe98afb692360dc7a2edd9d57a706d7afde1a1826c556490c3f9`。
- 1+8独立job：launcher `2499329.opbs`请求15秒/实际2秒/exit 0；crash syncer `2499331.opbs`请求15秒/实际6秒/预期exit 137；successor `2499332.opbs`请求40秒/实际22秒/exit 0；learner array `2499333[0-7]`各请求35秒/实际18秒/全部exit 0。请求值基于相邻实测并分别保留9、18和17秒运行余量，且launcher另有13秒余量；没有因压缩walltime牺牲完成可靠性。
- runtime达到epoch 2、v10、terminal generation 2和5120 seen tokens；epoch 1只提交v0后被注入SIGKILL，epoch 2从DB恢复并提交v1–v10后released。8个learner的独立process exit均正常。
- formal matched job `2499345.opbs`请求1分钟、实际35秒、exit 0。business baseline/observer各400样本：32个25-sample AB/BA块中，16个observer块各2次观察、16个baseline块均0次观察，SQLite trace writer attempt为0；baseline p99 `0.022910s`、observer p99 `0.020053s`，低于允许值`0.030638s`。legacy/HA checkpoint各100样本，HA p99 `0.015462s`低于允许值`0.020234s`。artifact `artifacts/20260806-1623_phase1-matched-performance_pass.json`，SHA-256 `63299493aa9eaeb8d372c8296cbd1d73135b6cce358e838d761f0238a5a49d78`。
- completed Checker PBS `2499349.opbs`请求15秒、实际4秒、exit 0并返回`PASS`。它复核120次lease renew/0 failure、457次business transaction/0 failure/p99 `0.018573s < 0.05s`、takeover `1.030941s < 10.2s`、stale epoch business commit 0、canonical adoption error 0、runtime failure event 0、matched artifact identity/门槛和terminal/canonical control。主artifact为`artifacts/20260806-1624_phase1-completed-checker_pass.json`，SHA-256 `590129ac221c51679aafca517b92b86a92727e0d70908769021336609c58f74e`。
- submission/init artifacts分别为`artifacts/20260806-1622_phase1-independent-launch_pass.json`（SHA-256 `bf12888130785320e6d25eb727aeebc307b1491accf31256a755db76f9af67a2`）和`artifacts/20260806-1622_phase1-init-run_pass.json`（SHA-256 `207b90f1fa3816c2a0cc10a396d8097875defda468d51482eca4ce98c1c02aa0`）。

### Inferences and gate boundary

- 该9节点workload每个learner约执行200以上local steps并完成10个global merge，超过50-local-step × 10-global-step文档同步基线；`docs/07-operations.md`已同步最终验证结果。这是HA恢复、协议和控制面性能证据，不产生训练质量结论。
- runtime、matched性能和completed Checker现均绑定同一clean source/descriptor并通过，修复了本轮Codex High/Medium/Low accepted findings以及matched采样失败。Phase 1仍保持completion candidate，直到对Phase 0 base `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`至最终evidence target的累计diff完成最后一次必做Codex审查；Claude reviewer已在本轮按可核验会话限额记为`skipped-session-limit`且不重试、不阻断。

## 2026-08-06 16:30 JST — Phase 1 completion gate APPROVE

### Facts

- 最终review target为`fefef86b68aa346afee93680ad9c494657412074`，comparison base为Phase 0 final `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`。target相对正式run使用的clean executable commit `36762854bfcbbc23b71ab838913023d64cf37b5e`只新增不可变evidence和同步文档，`fs_diloco`、configs、PBS scripts及tests树无差异。
- 必做Codex不可变报告为`reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-1/gpt-5.6-sol_fefef86b68aa346afee93680ad9c494657412074.md`。审查覆盖Phase 1累计diff、全部前序finding remediation、错误处理、并发/持久化不变量、测试覆盖、最终1+8、matched性能、Checker及文档；`Critical`、`High`、`Medium`和Phase 1范围内`Low` findings均为0，最终决定`APPROVE`。
- Claude Opus 5在本轮较早的fresh `claude -p` session `4b13f9d7-973d-4298-9752-e756f6cd8cc8`已返回可核验session limit并按规则记为`skipped-session-limit`。没有Claude报告被创建或伪造；该skip不是approval，但不阻断Phase 1或后续任务，且不重试。
- 正式completed Checker artifact `20260806-1624_phase1-completed-checker_pass.json`仍为`PASS`且SHA-256为`590129ac221c51679aafca517b92b86a92727e0d70908769021336609c58f74e`。requirement matrix的HA-01至HA-20已全部更新为`complete`并绑定最终/替换证据；Phase 1发布前自检项全部勾选。

### Conclusion

- Phase 1完成门禁关闭。Phase 2现在可以开始，但本轮没有启动Phase 2；dynamic instance递归发现的明确follow-up仍由Phase 2 MEM-02/MEM-20负责。

## 2026-08-06 22:32 JST — Phase 2 P2-L0–L6 focused foundation PASS

- 实现dynamic schema v3、UUID incarnation、固定stream pool、bootstrap/registration/admission/replacement transaction、proposal与final-commit membership fence、幂等capacity observation、durable learner launch outbox、drain directive/ack和dynamic archive/GC。
- Phase 2 focused compute-node replacement run：PBS `2500745.opbs`，Miyabi `mg0011`，依据上一轮7.45秒实测请求`00:00:30`并在7.23秒完成；`9 passed`，包括1000次UUID生成和1000 churn、SQLite warm-up后page增长冻结上限、scheduler queued-over-TTL/unknown/absent、logical request单一admission及drain closure。
- 完整兼容回归：PBS `2500748.opbs`，Miyabi `mg0005`，依据现有full-tree约24秒证据请求`00:01:30`并在24.52秒完成；`457 passed`。static full、fragment和historical read-only路径无回归。
- 静态门禁：`py_compile`、`git diff --check`和`bash -n scripts/miyabi/*.pbs`通过；所有新PBS脚本使用literal group `xg24i002`。
- 当前边界：这些结果关闭focused实现回归，不替代G7 structured artifact、G8/G9独立job chaos、Phase 2 completed Checker或最终审查。

## 2026-08-06 22:58 JST — Phase 2 bounded-authority remediation PASS

- 持久化单调admission generation、lifetime scale-request budget及cooldown时间；bootstrap授权不再被动态maintenance归档后重建；非同stream replacement释放旧stream和旧launch状态；partial unique index同时约束current placement与stream。
- `input_closed`现在显式要求drained learner声明的final update已经进入proposal frontier；registration/drain reader校验epoch目录owner；terminal repair同步dynamic controller terminal状态。动态归档同时清理proposal frontier及已归档registration control publication，避免单epoch churn使active physical state线性增长。
- PBS `2500955.opbs`在Miyabi `debug-g`计算节点`mg0004`执行`.venv/bin/python -m pytest -q tests/test_plan02_phase2_dynamic.py`；依据上一轮7.20秒实测请求`00:01:00`，实际9秒、`Exit_status=0`，结果`11 passed in 7.27s`。
- 证据：`artifacts/20260806-225826_phase2-focused-tests_review.log`。剩余G9/matched dirty preflight、clean-source G7–G9/matched/completed Checker、文档与matrix关闭及双审查。

## 2026-08-06 23:02 JST — Phase 2 G9 dirty-source runtime preflight PASS

- 目标：在冻结clean target前，以超过50 local steps × 10 global steps基线的1+8 dynamic workload验证takeover、永久learner终止、两次唯一low observation、scheduler确认释放后的replacement、短暂停顿、duplicate physical job拒绝、最多9个并发计算节点和dynamic drain close。
- 提交前`bash -n scripts/miyabi/*.pbs`和literal `group_list=xg24i002`门禁通过。launcher PBS `2500962.opbs`初始化run `runs/fs_diloco/plan02_phase2_g9_2500962`；crash syncer `2500973.opbs`按预期exit 137，successor `2500974.opbs`在78秒完成到v60，victim `2500975.opbs`按预期exit 143，bootstrap learners `2500976`–`2500982`完成drain，duplicate `2500983.opbs`被拒绝，scheduler replacement `2500987.opbs`被admit并完成drain；checker `2500984.opbs`在9秒内返回`PASS`。
- 结构化证据`artifacts/20260806-225947_phase2-g9_pass.json`绑定descriptor `eb07be91cf82b9b8b2bcb6cd36fa71f6982f9163f2074d56cb6ac7b3f49308e4`和dirty source fingerprint `sha256:e7618ddc77bd0643bc2869ca4e94767a19ad4600b880f43029114a60e847341d`；稳定bootstrap 1+8、replacement恢复1+8、pause recovery、duplicate rejection、并发节点上限和terminal close均为true。
- 边界：该运行只证明runtime、chaos checker和walltime预检；dirty source identity不能进入Phase 2 completed Checker。正式G9必须在最终clean commit上重跑，并与G7/G8/compatibility/matched证据绑定同一source identity。

## 2026-08-06 23:09 JST — Phase 2 matched launcher remediation PASS

- matched launcher不再提交Miyabi不支持的non-rerunable learner array；static和dynamic各提交8个独立learner job，分别显式传`LEARNER_INDEX`与`BOOTSTRAP_SLOT`，并在每次qsub后原子持久化receipt。
- 新增mock scheduler编排回归，验证19个子job命令、无`-J`、每个slot唯一、static→dynamic顺序依赖及checker依赖。PBS `2501057.opbs`在Miyabi `debug-g`节点`mg0005`以相邻9秒实测请求`00:00:30`，实际9秒、exit 0；focused组`12 passed in 7.45s`。
- 证据：`artifacts/20260806-230848_phase2-focused-tests_matched-fix.log`。尚需重跑dirty matched runtime preflight；当前结果不替代clean-source matched性能门禁。

## 2026-08-06 23:17 JST — Phase 2 control-path performance remediation regression PASS

- dynamic heartbeat discovery现在把同一扫描中的有效heartbeat放入单个fenced transaction，保持逐instance membership/placement/stream/token重验；健康merge路径移除重复的完整maintenance扫描，post-merge maintenance仍保留，长期quorum/starvation等待按scheduler reconciliation周期维护。
- acceptance的registration/proposal visibility grace由2秒收敛到一个0.2秒scan interval；`input_closed`仍要求全部final pointer精确进入frontier后才启动该grace，未放宽membership、drain或5%性能阈值。
- 新增heartbeat批量transaction回归，验证两个current instance在一次DB transaction中更新且两个fence均成立。PBS `2501094.opbs`在Miyabi `debug-g`节点`mg0003`请求`00:00:30`、实际9秒、exit 0；focused组`13 passed in 7.11s`。证据：`artifacts/20260806-231634_phase2-focused-tests_performance-fix.log`。

## 2026-08-06 23:19 JST — Phase 2 matched runtime dirty-source preflight PASS

- launcher `2501095.opbs`提交两个顺序隔离的1+8 run：static jobs `2501096`–`2501104`及dynamic jobs `2501105`–`2501113`均正常terminal/exit 0，checker `2501114.opbs`实际1秒并返回`PASS`。请求walltime保持syncer`00:02:30`、learner`00:02:00`、checker`00:00:20`，覆盖相邻26秒完整训练及收尾波动。
- 相同model/data/seed/v60 workload下，static complete time为`26.349440s`，dynamic为`25.365443s`；按冻结口径`max(0, dynamic-static)/static`为`0.0% < 5%`。dynamic完整执行8次registration、63次唯一capacity observation、drain ack和terminal closure；批量heartbeat后business transaction由上一预检2348降至1660。
- 结构化证据：`artifacts/20260806-231714_phase2-matched-launch_pass.json`与`artifacts/20260806-231714_phase2-matched-performance_pass.json`。该结果重置matched实验连续失败计数，但仍是dirty-source时长/功能预检；正式门禁必须绑定最终clean source。
## 2026-08-06 23:37 JST — bootstrap scheduler identity and finite close paths

- Persisted identity-bound bootstrap qsub manifests and reconciled queued/running external bootstrap jobs through the launch outbox; logical admission now checks the physical PBS job identity.
- Added manual operator close requests, deadline closure, launch-budget closure, immutable registration-decision replay handling, and a completed-checker PBS wrapper.
- Expanded the Phase 2 focused matrix to 18 tests, including 8-slot bootstrap admission, ninth unsolicited rejection, scheduler retention beyond TTL, manifest reconciliation, replay collision safety, and manual/deadline/budget closure. PBS `2501171.opbs` passed all 18 tests in 6.96 seconds.
- Full repository compatibility PBS `2501174.opbs` passed 470 tests in 23.92 seconds.

## 2026-08-06 23:47 JST — PBS job identity normalization remediation

- Preserved raw qsub receipts while comparing PBS IDs by their server-independent numeric identity, preventing qstat's suffix-free spelling from looking like a different bootstrap job.
- Added raw/normalized equivalence coverage. Focused PBS `2501208.opbs` passed 18 tests in 7.42 seconds; full compatibility PBS `2501209.opbs` passed 470 tests in 23.63 seconds.

## 2026-08-06 23:54 JST — acceptance scheduler-horizon remediation

- G8 clean-source rerun `plan02_phase2_g8_2501215` passed takeover, victim/replacement, stream reuse, and dynamic drain evidence.
- Extended the G9/matched target to v120 after a measured scale-job queue cycle outlasted the v60 horizon; projected runtime remains within the 150-second syncer walltime with practical teardown margin.
- Focused PBS `2501249.opbs` passed 18 tests in 7.24 seconds; full compatibility PBS `2501250.opbs` passed 470 tests in 24.48 seconds.

## 2026-08-07 00:00 JST — acceptance scale queue routing

- Added optional validated `scaling.learner_queue`; production remains unset, while G8/G9 acceptance explicitly routes scale replacements to `debug-g` instead of inheriting the learner script's long production queue.
- Manually removed only doomed experiment jobs `2501271.opbs` and `2501281.opbs` after PBS estimated a five-hour replacement delay; no unrelated job was touched.
- Focused PBS `2501290.opbs` passed 19 tests in 7.71 seconds; full compatibility PBS `2501291.opbs` passed 471 tests in 24.22 seconds.

## 2026-08-07 00:32 JST — Phase 2 final clean-source technical gates PASS

- Frozen implementation commit `85febbaee653dcff04897eea35a15dd8f31172c2` and source fingerprint `sha256:b8684b5d90d22a341da3e30dfca375b6de4026103ee64769f2b71aae500fba69` include dynamic UUID identity, fixed stream pool/epoch mapping, schema v3 membership, fenced registration/admission/proposal/final commit, capacity hysteresis, PBS launch outbox/reconciliation, manual/deadline/budget/global close, generation-scoped drain ack, and bounded archive/GC.
- Focused PBS `2501472.opbs` passed 21 tests. Full compatibility PBS `2501475.opbs` passed 473 tests. Static PBS syntax and literal group checks remained prerequisites for every formal submission.
- G8 launcher `2501477.opbs` produced `artifacts/20260807-002525_phase2-g8_pass.json`: crash takeover, one admitted victim termination, one replacement, same-placement stream reuse at epoch 1, drain acknowledgement, and terminal completion all passed with no blocking errors.
- G9 launcher `2501510.opbs` submitted crash syncer `2501511`, successor `2501512`, bootstrap jobs `2501513`–`2501520`, duplicate `2501521`, checker `2501522`, and the single scale replacement recorded in the evidence. `artifacts/20260807-002637_phase2-g9_pass.json` is `PASS`: all eight bootstrap slots admitted exactly once, the duplicate was rejected, two distinct low observations created exactly one replacement request, current contributor count returned to eight, stream 2 was reused at epoch 1, the paused learner recovered, dynamic close completed, and maximum concurrent compute nodes stayed at nine.
- The G9 run completed global version 120 in approximately 61.65 seconds with 1,521,024 seen tokens. Every training cycle recorded 51 local steps, so the formal 9-node workload exceeds the 50-local-step × 10-global-step documentation synchronization baseline.
- G7 PBS `2501529.opbs` produced `artifacts/20260807-002836_phase2-g7_pass.json`. Compatibility PBS `2501530.opbs` produced `artifacts/20260807-002836_phase2-compatibility_pass.json`. Both returned `PASS` against the same frozen source.
- Matched launcher `2501534.opbs` completed static and dynamic v120 runs and checker `2501554.opbs`. `artifacts/20260807-002927_phase2-matched-performance_pass.json` is `PASS`: static complete time `46.836257489s`, dynamic `46.421222731s`, and frozen overhead `max(0,dynamic-static)/static = 0.0 < 0.05`.
- Completed Checker PBS `2501559.opbs` returned `PASS` and wrote `artifacts/20260807-003213_phase2-completed_pass.json`. It verified MEM-01 through MEM-20, SQLite schema/integrity version 3, final v120/terminal generation 2, expired epoch 1 plus released epoch 2, 127 control publications, the 64 active capacity-observation limit plus 60 archived observations, one scale request, eight bootstrap scheduler jobs, one admission per logical request, eight fixed streams, replacement stream epoch increment, zero blocking failure events, and no checker warnings/errors.
- README, architecture, runtime/data flow, configuration, operations, module references, research-plan scope, Phase 2 checklist, and requirement matrix were synchronized to the verified behavior and formal result. Phase 2 is therefore a technical completion candidate; the mandatory independent Codex/Claude phase review and full-plan current-state review remain before final closure.

## 2026-08-07 01:25 JST — Phase 2 completion review requires remediation

- Frozen cumulative review base/target: `fefef86b68aa346afee93680ad9c494657412074..7feb09992b7f40b255e0858020a50d811a602b9c`. The later `f39bae7b5d5f2b868e8a94dd98a7e2ce05e49614` commit changes only the documentation index and remains part of the next incremental target.
- Independent Codex reports: `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-2/gpt-5.6-sol_7feb09992b7f40b255e0858020a50d811a602b9c.md` and append-only supplement `gpt-5.6-sol_7feb09992b7f40b255e0858020a50d811a602b9c-retry1.md`; decision `CHANGES_REQUIRED`.
- Accepted findings: dynamic `no_progress_timeout` can publish a normal terminal while the controller remains open; merge and starvation capacity inputs have transaction-to-transaction crash gaps; token-target drain can advance to an unrelated outer-step ceiling; and physical PBS admission can occur before the scheduler receipt is durably bound. All are `fixed`-required because they affect Phase 2 terminal, takeover, scaling or admission invariants. RED coverage and fixes have not yet been applied at this record point.
- External reviewer invocation used fresh session `380b37f9-3cd8-4bde-95f8-04ebf81d6e31`, actual/canonical model `claude-opus-5`, and returned HTTP 429 with the explicit result `You've hit your session limit · resets 6am (Asia/Tokyo)`. Per `plans/AGENTS.md` this is `skipped-session-limit`: no Claude report was created or fabricated, the skip is not an approval, it is not retried, and it does not block Codex remediation.
- The retained G7/G8/G9/compatibility/matched/completed artifacts remain valid evidence for the frozen normal path, but they do not falsify these newly identified crash/timeout races. Phase 2 remains incomplete pending RED tests, fixes, compute-node verification, a new frozen target and incremental completion review.

## 2026-08-07 01:30 JST — Phase 2 review remediation passes focused and full compute tests

- Accepted finding dispositions are `fixed`: scheduler-unbound physical registrations now remain nonterminal and retry after exact job binding; dynamic merge and capacity hysteresis share one rollback boundary; starvation generation and its observation share one rollback boundary; token-triggered drain freezes at the committed version; no-progress starts and publishes the persisted drain; and non-error terminal publication fails closed unless dynamic input is fully closed.
- `scripts/miyabi/check_plan02_phase2.py` now rejects any missing or mismatched `merge:<version>` observation and any gap between persisted starvation generation and observation history. Dynamic `commit_full_merge` rejects callers that omit the atomic observation input.
- RED evidence is recorded in `failures.md`. Post-fix PBS `2501697.opbs` passed all 5 focused review-remediation tests in 3.50 seconds. PBS `2501698.opbs` passed all 23 Phase 2 focused tests in 7.41 seconds. Full regression PBS `2501704.opbs` passed all 480 tests in 25.78 seconds. Each used the statically validated literal-group script `scripts/miyabi/run_plan02_phase2_tests.pbs`; focused jobs requested `00:01:00`, and the full suite requested `00:02:00` based on prior runtime plus teardown margin.
- Static checks passed for the affected files: Python compilation, Ruff, `git diff --check`, `bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh`, and the placeholder group-ID scan. Remaining gates are a clean source commit, fresh Phase 2 runtime/matched/completed evidence for the changed atomic protocol, documentation synchronization, and incremental completion review.

## 2026-08-07 01:34 JST — Successor drain reason and final pre-freeze regression PASS

- The no-progress remediation now also restores the persisted controller reason when a successor resumes a `draining` or `closed` dynamic run. This prevents a takeover from changing `no_progress_timeout`, `manual`, `deadline`, or `launch_budget_exhausted` into the generic `completed` terminal reason.
- Focused PBS `2501733.opbs` requested `00:01:00` and passed all 29 tests in `tests/test_plan02_phase2_dynamic.py` plus `tests/test_plan02_phase2_review_remediation.py` in 7.09 seconds.
- Full compatibility PBS `2501735.opbs` requested `00:01:00`, passed all 481 tests in 30.41 seconds, and completed the Plan 01 two-process smoke plus telemetry assertions within the request. Static shell syntax, literal `group_list=xg24i002`, placeholder scan, and `git diff --check` remained clean before submission.
- These jobs exercised the dirty pre-freeze tree. The next step is to freeze one clean executable commit and rerun the Phase 2 formal runtime, matched-performance, and completed-Checker evidence against that exact source identity.

## 2026-08-07 01:31 JST — Stable scheduler-authorization remediation group PASS

- PBS `2501723.opbs` ran `tests/test_plan02_phase2_dynamic.py tests/test_plan02_phase2_review_remediation.py` on `mg0003` after the pending-request checksum and synthetic scheduler-binding tests were complete. With a `00:00:25` request it exited 0 after 9 seconds: `29 passed in 7.43s`.
- The associated group now covers unbound physical registration persistence and exact-ID retry, wrong-ID rejection after binding, conflicting immutable pending payload rejection, normal bound-PBS admission, both-null direct/local compatibility, merge/observation rollback, starvation allocation/observation rollback, token ceiling, no-progress drain, and completed-Checker observation completeness.
- Audit correction for the frozen external review: the primary invocation actually used session `4df65150-9d55-4a70-9d78-d99c082e17ca` with canonical model `claude-opus-5` and returned the explicit HTTP 429 session-limit result. The earlier appended `3498a066-5265-47cf-9e36-9d3a33a740b8` and `380b37f9-3cd8-4bde-95f8-04ebf81d6e31` identifiers do not identify that primary invocation and are superseded by this append-only correction. No valid Claude report exists and the disposition remains `skipped-session-limit`, not approval.
- The Codex scheduler-handoff supplement was created during concurrent workspace review activity; it is retained as an append-only finding record, not represented as a Claude result or external approval. The primary Codex review independently re-evaluated and accepted that physical-authorization race for remediation.
- Remaining risk: this focused pass does not replace the full repository regression or a clean-source runtime gate for the changed persistence/admission protocol.

## 2026-08-07 01:33 JST — Final pre-freeze remediation regression PASS

- After minimizing the scheduler test scaffolding and retaining explicit both-null direct/local compatibility, PBS `2501730.opbs` ran the 29-test dynamic/remediation group on `mg0003`; request `00:00:25`, actual 8 seconds, exit 0, `29 passed in 7.02s`.
- PBS `2501731.opbs` then ran `pytest -q tests` on `mg0003`; request `00:00:50`, actual 26 seconds, exit 0, `481 passed in 24.10s`. Logs are `fsdiloco_plan02_p2_tests.o2501730` and `fsdiloco_plan02_p2_tests.o2501731`.
- Pre-submission validation passed `bash -n scripts/miyabi/*.pbs`, confirmed every PBS group directive is the literal `xg24i002`, and found no group placeholder. Python compilation, Ruff lint/format checks, and `git diff --check` also passed for the affected source, Checker, and tests.
- This closes the associated unit/regression group. It does not yet prove clean-source multi-process takeover, scheduler launch, matched performance, or completed-Checker behavior after the atomic protocol changes; those remain the next formal work unit.

## 2026-08-07 01:35 JST — Remediated clean-source G8 PASS

- Frozen executable target `1a146cf403c33625d611eace0b421b606dd51300`, clean source fingerprint `sha256:cdf8f01bdb6f4bfd62dbe9a1103bca0a14f8b029ef3eaf12d8c77221aa94d0c0`. Launcher PBS `2501741.opbs` requested 15 seconds and submitted injected crash syncer `2501742`, successor `2501743`, admitted victim `2501744`, scheduler replacement `2501747`, and Checker `2501745` for run `plan02_phase2_g8_2501741`.
- `artifacts/20260807-013357_phase2-g8_pass.json` is `PASS`: the injected post-DB-commit crash produced epoch takeover, the admitted victim was terminated, one scheduler-bound replacement was admitted, the placement reused its stream at epoch 1, drain acknowledgement completed, and the controller reached terminal v12. The successor used 31 seconds of its 150-second request; Checker exit 0.
- This revalidates two-node independent jobs, crash recovery, scheduler authorization, replacement, stream fencing, atomic per-merge capacity history, and terminal closure on the remediated clean source. The 9-node scale/pause/duplicate gate remains separate.
