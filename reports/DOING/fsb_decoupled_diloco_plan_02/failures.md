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
