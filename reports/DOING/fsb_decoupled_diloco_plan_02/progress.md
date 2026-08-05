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
