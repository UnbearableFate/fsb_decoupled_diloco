# fsb_decoupled_diloco_plan_02 failure record

This record is append-only. No Phase 0 test or experiment failure has been recorded yet.

## 2026-08-06T01:02:00+09:00 — phase0-feasibility attempt 1

### Experiment identity

- Consecutive failure count: 1.
- PBS job: `2496503.opbs`, queue `debug-g`, `select=2:mpiprocs=4`, hosts `mg0004` and `mg0005`.
- Submission command:

  ```text
  qsub -o /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010119_phase0-pbs.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-010119 scripts/miyabi/run_plan02_phase0_feasibility.pbs
  ```

- Source state: branch `codex/fsb_decoupled_diloco_plan_02`, uncommitted Phase 0 probe implementation frozen for the duration of the job.

### Expected behavior

The script should create a job-specific work directory below `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/`, run the two-node probes, write one final Checker artifact, and delete only that job-specific work directory after `PASS`.

### Observed facts

- The executable workload started (`substate=42`), loaded `nvidia/25.9` and `nv-hpcx/25.9`, and reached the probe body.
- The job log printed `WORK_ROOT=/work/xg24i002/x10041`, which is the account work root rather than a job-specific directory.
- The parent environment already defined `WORK_ROOT`; the PBS script used `${WORK_ROOT:-...}` and therefore accepted that unrelated broad value.
- The success cleanup path was `rm -rf -- "$WORK_ROOT"`. Allowing the job to reach it could have deleted unrelated account data.
- Codex immediately submitted `qdel 2496503.opbs`. The job finished after 18 seconds before cleanup. No unfinished jobs remained.
- A nested scalar capability child, `2496506.opbs`, completed in one second and left an auditable child artifact. No array child was submitted before the parent was stopped.
- Run-generated files were created directly under `/work/xg24i002/x10041`; no repository or account data was observed deleted.

### Confirmed root cause

`WORK_ROOT` is too generic for a task-local override and collided with an environment value. This violates the repository safety rule against repurposing common system or environment variables and made the cleanup target unsafe.

### Next modification and falsification test

- Replace `WORK_ROOT` with a plan-specific variable such as `PLAN02_PHASE0_WORK_DIR` and do not accept an inherited override for the destructive cleanup target.
- Derive the directory only from the validated report root, timestamp, and PBS job ID.
- Before cleanup, resolve both paths and assert that the target is a strict child of `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/` with the expected `work_` prefix.
- Add a regression test that sets `WORK_ROOT` to a broad sentinel and verifies the script neither reads it nor contains a cleanup using it.
- Rerun focused tests before the second two-node attempt.

### Evidence

- Complete parent log: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010119_phase0-pbs.log`.
- Structured failure summary: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010200_phase0-feasibility-attempt1_fail.json`.

## 2026-08-06T01:07:00+09:00 — phase0-feasibility attempt 2

### Experiment identity

- Consecutive failure count: 2.
- PBS job: `2496515.opbs`, queue `debug-g`, `select=2:mpiprocs=4`, hosts `mg0005` and `mg0008`.
- Submission command:

  ```text
  qsub -o /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010700_phase0-pbs.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-010700 scripts/miyabi/run_plan02_phase0_feasibility.pbs
  ```

### Expected behavior

Eight writers across two nodes should open the preinitialized shared SQLite database, execute 50 acquire/renew transactions each with bounded busy retries, and produce one result per writer without starvation or integrity failure.

### Observed facts

- The cleanup-target remediation worked: the job used the exact job-scoped directory `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/work_20260806-010700_2496515`.
- Writer-lock, old-cache-writer, source-pinning, and cross-node commit/reopen probes completed and wrote evidence before the contention stage.
- Four contention ranks failed while opening their connections. The exception was `sqlite3.OperationalError: database is locked` at `PRAGMA synchronous=FULL` in `_configure_connection()`.
- The MPI job aborted at the contention command; the parent job finished after eight seconds. The Phase 0 Checker was not reached.
- A rollback journal remained because MPI terminated writers during the failed start; this is failed-run evidence, not an integrity conclusion.

### Confirmed root cause

`_configure_connection()` set `PRAGMA synchronous=FULL` before installing the requested `busy_timeout`. When eight processes opened simultaneously, some reached that PRAGMA after another rank had begun a write transaction and failed immediately instead of applying the probe's bounded wait policy.

### Next modification and falsification test

- Install a startup busy timeout before connection PRAGMAs, set `synchronous=FULL`, then reduce `busy_timeout` to the contention value used for measured transactions.
- Add a regression test that holds `BEGIN IMMEDIATE` while a contention process opens; release the holder within the startup timeout and require the contender to complete.
- Rerun the focused SQLite/Phase 0 tests before attempt 3.

### Evidence

- Complete parent/MPI traceback: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010700_phase0-pbs.log`.
- Structured failure summary: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-010700_phase0-feasibility-attempt2_fail.json`.

## 2026-08-06T09:06:23+09:00 — phase0-remediation attempt 1

### Experiment identity

- Consecutive failure count for the post-review `phase0-remediation` experiment: 1.
- PBS parent job: `2497224.opbs`, queue `debug-g`, `select=2:mpiprocs=4`, hosts `mg0010` and `mg0011`, terminal walltime 22 seconds, `Exit_status=1`.
- Submission command:

  ```text
  qsub -o reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-090551_phase0-remediation_review.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-090551 scripts/miyabi/run_plan02_phase0_feasibility.pbs
  ```

- Resolved Phase 0 settings remained two hosts, 8 contention writers, 50 transactions per writer, `busy_timeout_ms=10`, `retry_timeout_seconds=60`, 5 ms lock hold, 20 two-way clock rounds, and a 2-second clock-offset bound.

### Expected behavior

The remediated bundle should aggregate host coverage from every contention-writer artifact, verify held/queued through finished scheduler states, emit a strict Checker `PASS`, atomically persist the final evidence, and delete only its validated job work directory.

### Observed facts

- Writer-lock, all-artifact stale-cache adoption, independent source/protocol/schema/run-ID mismatch gates, cross-node readonly reopen, eight-writer contention, the two-way clock exchange, and the PBS probe all ran before aggregation.
- Aggregation failed at `plan02_phase0_aggregate.py:84` with `KeyError: 'hostname'` while deriving distinct contention hosts.
- Each raw `contend_<rank>.json` omitted `hostname`, although the process stdout included it. `sqlite_shared_fs_probe.main()` added `hostname` only after it had already written `--output-json`.
- The new job-level failure path worked as intended: it printed `PHASE0_CHECKER=BLOCKED`, atomically retained `20260806-090551_phase0-feasibility_blocked.json`, and preserved the 788 KiB job-scoped raw work directory for diagnosis.
- Separately, the PBS subprobe recorded `qsub: Invalid Option -- 'h'`; this Miyabi compute-node qsub wrapper advertises `-h` in usage but rejects it. Because submission was rejected, the probe safely selected the manual/manifest capability path rather than claiming automatic support.

### Confirmed root causes

1. The new aggregation invariant consumed a field that was present only in stdout, not in the writer's structured JSON, due to publication ordering in `sqlite_shared_fs_probe.main()`.
2. A held scalar job is not a usable deterministic queued-state probe through this site's compute-node qsub wrapper.

### Next modification and falsification test

- Add hostname/PID defaults before writing contention JSON, then require `host_count >= 2` in both aggregate evidence and Checker tests.
- Replace `qsub -h` with a five-second future `qsub -a <date_time>` start, observe the resulting `W` state as queued, and let the job start naturally; retain terminal `F/Exit_status=0` polling.
- Rerun `tests/test_plan02_feasibility.py` and `tests/test_sqlite_probe.py` on the existing compute validation path, then submit remediation attempt 2 with the same resource shape.
- After a passing rerun, retain this log and blocked summary, then remove the diagnosed job-scoped raw directory as redundant intermediate data.

### Evidence

- Parent traceback and workload log: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-090551_phase0-remediation_review.log`.
- Structured fail-closed summary: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-090551_phase0-feasibility_blocked.json`.
- Diagnosed raw work (temporary until the next terminal result): `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/work_20260806-090551_2497224/`.

## 2026-08-06T09:48:00+09:00 — append-only record correction

The opening sentence stating that no Phase 0 failure had been recorded is an obsolete initialization placeholder. It cannot be rewritten under the append-only rule. The timestamped failure entries above are authoritative; the diagnosed raw directory from the 09:06 remediation failure was deleted only after its blocked artifact/log were retained and a replacement PASS completed.

## 2026-08-06T09:57:00+09:00 — phase0-review2-remediation focused tests attempt 1

### Experiment identity

- Consecutive failure count for `phase0-review2-remediation focused tests`: 1.
- PBS job `2497329.opbs`, queue `debug-g`, compute host `mg0004`, walltime 11 seconds, `Exit_status=1`.
- Command from `scripts/miyabi/run_plan02_phase0_tests.pbs`:

  ```text
  .venv/bin/python -m pytest -q tests/test_plan02_feasibility.py tests/test_sqlite_probe.py tests/test_capture_source_identity.py tests/test_source_identity.py
  ```

### Expected behavior

All focused tests should pass after adding cross-node writer-lock evidence, a guarded source-runtime write, direct aggregate/source-gate coverage, and ignored `uv.lock` fingerprinting.

### Observed facts

- Result: `3 failed, 20 passed in 8.09s`.
- Two Checker tests raised `UnboundLocalError: incarnations` for the manifest-fallback fixture. The physical-incarnation assertion was accidentally indented into the non-array `else` branch, where `incarnations` is undefined.
- The source-identity test expected a clean repository but created an untracked `.gitignore`; the ignored `uv.lock` was not the dirty cause.

### Confirmed root causes and next falsification

1. Move the physical-incarnation assertion back inside the `array_supported` branch; retain a separate fallback-orchestration assertion in the `else` branch.
2. Commit the test repository's `.gitignore` in its baseline fixture so an ignored lock file can be present while `git_dirty=false`.
3. Rerun the identical focused group on a compute node before any full Phase 0 experiment.

### Evidence

- Complete test log: `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-100000_phase0-remediation-tests_review.log`.

## 2026-08-06T10:08:00+09:00 — Phase 0 final dual-review Claude attempt 1 blocked

### Experiment identity

- Review target: `f404fbd4831adcd9ffb8e6229a0004b1affe9f4e`.
- Comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Claude session: `4ca72c8d-7e52-4422-99f3-eb0dc04d1b0b`.
- Invocation: fresh non-interactive `claude --print` process with explicit model `claude-opus-5`, `--permission-mode bypassPermissions`, `--dangerously-skip-permissions`, JSON output, no resume/continue, and no fallback model.

### Expected behavior

Claude Opus 5 should independently review the frozen Phase 0 diff and write only its assigned immutable report after Codex has launched the process and independently saved the Codex report.

### Observed facts

- The process returned after 209,771 ms with `is_error=true` and message `You've hit your session limit · resets 1:30pm (Asia/Tokyo)`.
- Machine-readable metadata verified actual model `claude-opus-5`, canonical model `claude-opus-5`, provider `firstParty`, the requested session ID, and `permission_denials=[]`.
- Claude did not create its assigned report. The independent Codex report was completed and saved before this result was inspected at `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-0/gpt-5.6-sol_f404fbd4831adcd9ffb8e6229a0004b1affe9f4e.md`.
- No implementation, test, configuration, plan, or unrelated report change was produced by Claude. Phase 0 remains a completion candidate and Phase 1 has not started.

### Confirmed cause and next action

- This is an external Claude Code account session-limit block, not a review finding or source failure. Repository policy forbids substituting another model or treating an unverified/incomplete Claude run as a completed gate.
- After the stated reset, start a new independent Opus 5 session against the same frozen commits and use the retry report name `claude-opus-5-retry1_f404fbd4831adcd9ffb8e6229a0004b1affe9f4e.md`. Re-verify model/session/permission metadata, then read both reports and disposition any findings before creating the Phase 0 final commit.

## 2026-08-06T10:13:25+09:00 — Phase 0 final dual-review Claude attempt 2 blocked

### Experiment identity

- Consecutive failure count for the same final Claude review experiment: 2.
- Review target: `f404fbd4831adcd9ffb8e6229a0004b1affe9f4e`.
- Comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Claude session: `6f6f98ee-05b8-44f6-a2dc-de003438aa15`.
- Invocation: fresh non-interactive `claude --print` with explicit `claude-opus-5`, `bypassPermissions`, `--dangerously-skip-permissions`, JSON output, no resume/continue, and no fallback.

### Expected behavior

After `claude --help` reconfirmed every required option, Opus 5 should review the same frozen diff, create only its assigned report, and return `REVIEW_REPORT_WRITTEN` with verified model/session metadata.

### Observed facts

- The process ran for 125,426 ms and returned exit code 1, `is_error=true`, `terminal_reason=api_error`, HTTP status 429, and `You've hit your session limit · resets 1:30pm (Asia/Tokyo)`.
- Metadata again verified canonical/actual model `claude-opus-5`, provider `firstParty`, the requested session ID, and `permission_denials=[]`; there was no fallback.
- No Claude report was created and no working-tree change was produced. The already immutable Codex report for this target remains byte-identical to its committed form.
- This attempt was launched before the already committed attempt-1 failure record became visible in the active workspace view, so its prompt named the still-absent base Claude report path rather than the documented `retry1` path. Because no report was written, no immutable filename collision or report mutation occurred.

### Confirmed cause and next action

- The same external Claude account session limit remains the sole blocker. This is the second consecutive failure of the final Claude review experiment; no source modification can falsify it before the stated reset.
- Do not launch a third attempt before 13:30 Asia/Tokyo. After reset, use a fresh session and the documented `claude-opus-5-retry1_f404fbd4831adcd9ffb8e6229a0004b1affe9f4e.md` path, then continue the gate only if the process returns successfully and the report/metadata checks pass.

## 2026-08-06T11:10:00+09:00 — Phase 1 initial validation failures

### Experiment identity and observed facts

- `phase1-associated-tests` attempt 1, PBS `2497697.opbs` on `mg0003`, exit 1 after 2 seconds: focused result was 14 passed/1 failed because HA+fragment validation reported the generic recovery-submission dependency before the required fragment incompatibility. The validation order was corrected and the same branch was added to the focused config matrix.
- `phase1-smoke` attempt 1, PBS `2497708.opbs` on `mg0027`, exit 1 after 9 seconds: `_FencedConnection` converted named-parameter mappings to a tuple of keys, so SQLite inserted column-name strings instead of metadata values. Parameter normalization now preserves mappings; the RED regression asserts exact stored values.
- `phase1-smoke` attempt 2, PBS `2497742.opbs` on `mg0009`, exit 271: HA GC keyword arguments were passed to `LeaderLeaseStore` rather than the fenced business store. Store wiring was corrected and GC registration/recheck coverage added. This was the second consecutive smoke failure, not the third; no failure-escalation review threshold was reached.

### Falsification and outcome

The replacements progressed through focused/full results 18/395, then 20/397, and the tiny HA smoke reached staged `PASS_WITH_FOLLOWUPS`. The final associated-suite evidence is retained at `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-115000_phase1-test-suite_pass.json`.

## 2026-08-06T11:35:12+09:00 — Phase 1 fault-matrix attempt 1

PBS `2497902.opbs` on `mg0035` exited 1 after 7 seconds. The `weight_temp` failpoint ran before the publication directory existed, and same-epoch repair rebuilt canonical latest with `time.time()`, causing an immutable-byte collision. The checkpoint worker now creates its unique publication directory before that failpoint; canonical `published_at` comes from the stable DB version row. A same-epoch repeated-repair RED test was added. Replacement job `2497906.opbs` passed all 60 crash cases.

## 2026-08-06T11:44:00+09:00 — Phase 1 independent-launch attempts 1 and 2

- Attempt 1, launcher `2497930.opbs` on `mg0036`, exit 1 after 1 second: shell redirection targeted a file below the not-yet-created run root, so the shell failed before `init-run` could create that root. Initializer stdout was moved to the already-existing report artifact directory.
- Attempt 2, launcher `2497931.opbs`, exit 0, submitted child jobs `2497932.opbs`, `2497933.opbs` and `2497934[].opbs` with the generic scripts' 24-hour resource default. qstat estimated a materially later start. The exact queued children were canceled with authorized `qdel`; no live run data was removed. The acceptance launcher now overrides debug queue walltimes to 1 minute for the injected-crash candidate and 2 minutes for successor/learners.

Attempt 3, `2497948.opbs`, submitted the short-walltime children and completed successfully. This scheduling correction also established the repository rule that every future qsub must estimate the shortest practical walltime from the workload and prior observations, overriding a materially longer script default.
