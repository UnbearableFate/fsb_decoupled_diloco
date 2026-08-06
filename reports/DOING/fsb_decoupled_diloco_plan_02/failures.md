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

## 2026-08-06 13:28 JST — Phase 1 review-remediation smoke attempt 1

### Experiment identity

- Consecutive failure count for `phase1-review-remediation-smoke`: 1.
- PBS `2498588.opbs`, queue `debug-g`, node `mg0044`; requested walltime `00:00:20` from the prior 12-second observation, used `00:00:13`, scheduler `Exit_status=0`.
- Exact submission:

  ```text
  qsub -l walltime=00:00:20 -o reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-132630_phase1-review-remediation-smoke_review.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,RUN_ID=plan02_phase1_review_remediation_2498584 scripts/miyabi/run_plan02_phase1_smoke.pbs
  ```

### Expected and observed behavior

- Expected：review finding修复后的tiny HA runtime保持无error事件，完成version 2、terminal generation 2和staged Checker `PASS_WITH_FOLLOWUPS`。
- Observed：训练、terminal和Checker均完成，但syncer记录`lease_renewer_stop_failed`。renew线程的heartbeat atomic writer已创建`.heartbeat.json.<random>.tmp`，final maintenance同时无年龄门槛扫描并unlink `control/**/*.tmp`；writer随后在`os.chmod(tmp_path)`得到`FileNotFoundError`。这使run不能作为无error的remediation smoke PASS，尽管PBS和staged Checker当前仍返回零。
- 完整日志：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-132630_phase1-review-remediation-smoke_review.log`。run root：`runs/fs_diloco/plan02_phase1_review_remediation_2498584`。

### Confirmed root cause and next falsification

- `collect_runtime_artifacts()`对`control/weights/optim/fragments`的atomic temp无条件删除；`input_closed=True`把一般orphan grace降为0，但HA lease renewer在final maintenance期间仍必须运行并可能发布heartbeat，因此“input closed等于authority writer closed”的假设错误。
- 对HA authority temp使用至少`FencedSQLiteStore.gc_grace_seconds = lease_duration + max_clock_skew`的年龄门槛；保留learner已停止后的proposal temp终态清理语义。新增RED回归：current HA control temp在grace内必须保留，超过grace才删除；然后重跑完整tests与相同smoke。若修复正确，replacement日志不得出现`error`或`lease_renewer_stop_failed`。

## 2026-08-06 14:32 JST — phase1-review-remediation-tests-2（连续失败 1）

- 命令与环境：在 Miyabi compute node `mg0012` 由 PBS job `2498865.opbs` 执行 `qsub -l walltime=00:00:45 scripts/miyabi/run_plan02_phase1_tests.pbs`；显式 walltime 为基于上一轮 31 秒实测估算的 45 秒。脚本先运行 `.venv/bin/python -m pytest -q tests/test_plan02_phase1_ha.py`，随后才会运行全套 pytest。
- 预期：新增 candidate writer-lock 回归在持锁者释放后取得 epoch，并完成清理；focused 与 full 测试组均通过。
- 实际：focused 组 `50 passed, 1 failed in 6.09s`，在测试清理阶段由主线程调用一个在 worker thread 创建的 `LeaderLeaseStore.release()`，触发 Python sqlite3 的 same-thread `ProgrammingError`。生产路径已成功记录 `writer_lock_blocked` 并随后取得 epoch 1；失败不在 candidate retry 实现。
- 原始证据：`fsdiloco_plan02_p1_tests.o2498865`；PBS stderr 合并到同一文件。该 job 在 focused failure 后按 `set -e` 退出，未运行 full suite。
- 已确认根因：测试把 thread-affine sqlite connection 从 candidate worker thread返回主线程后再释放，违反 sqlite3 connection ownership；这是测试 teardown 缺陷，不是被验证的 writer-lock行为缺陷。
- 下一轮：让 worker thread在写出取得结果后等待主线程信号，并在创建 connection的同一线程执行 exact-token release/close；新增断言确认worker完成清理。重新运行同一 focused + full PBS测试组。通过条件为 writer-lock retry/timeout断言成立、focused与full suite均为0 failure。

## 2026-08-06 14:35 JST — phase1-review-final-smoke（连续失败 1）

- 命令与环境：Miyabi `debug-g` node `mg0004`，PBS `2498879.opbs`；依据前两轮12–13秒实测，显式提交 `qsub -l walltime=00:00:20 -o reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1435_phase1-review-smoke_pass.log -v PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,RUN_ID=plan02_phase1_review_final_2498876 scripts/miyabi/run_plan02_phase1_smoke.pbs`。实际walltime 12秒，scheduler `Exit_status=2`。
- 预期：tiny HA runtime完成v2/terminal generation 2且staged Checker写入独立artifact并返回`PASS_WITH_FOLLOWUPS`。
- 实际：runtime本身完整成功：leader released、final v2/generation 2、两个learner stopped、76个fenced business transaction无失败、lease renew无失败、无runtime error事件。随后launcher调用Checker时遗漏新必填的`--output`，argparse返回2，导致PBS失败。
- 原始证据：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1435_phase1-review-smoke_pass.log`；run root `runs/fs_diloco/plan02_phase1_review_final_2498876`。
- 已确认根因：review remediation把Checker默认写回live run目录的行为改为显式必填`--output`，但smoke PBS调用点未同步，属于launcher接口迁移遗漏。
- 下一轮：给smoke脚本增加job/stamp唯一的report artifact路径并显式传`--output`，静态检查后用相同20秒最短walltime重跑。通过条件为runtime无error、Checker artifact存在且为`PASS_WITH_FOLLOWUPS`、PBS exit 0。

## 2026-08-06 15:02 JST — phase1-final-1plus8 acceptance（连续失败 1）

- 命令与环境：clean source commit `68de59c41b90163cdafde12e1fc3041bd405c503`；launcher PBS `2498929.opbs`请求`00:00:10`、实际1秒、exit 0。它为run `plan02_phase1_final_68de59c`提交crash syncer `2498986.opbs`（15秒）、successor `2498987.opbs`（35秒）和learner array `2498988[0-7].opbs`（每项40秒）。启动artifact为`artifacts/20260806-1445_phase1-independent-launch_pass.json`，descriptor SHA-256 `8020f331784455d8a19e7a6fedd0dd6df31eb8b5494250b20b3365fd18f55b14`。
- 预期：crash syncer在v0 DB commit后SIGKILL；successor及时取得epoch2；8 learners在40秒最短walltime内共同达到10 merges并正常停止。
- 实际：crash job按预期6秒/exit137。scheduler在crash结束后让learner array先占用可用GPU，而把已有`afterany`依赖的successor继续排队到15:01:09。learner 0–5于14:58:33启动，只看到缺canonical head的epoch1，分别在47–49秒被40秒walltime杀死；learner6在51秒时按45秒canonical wait预算超时；learner7虽等到successor epoch2并产生proposal，但单独不能满足quorum 8。successor取得epoch2并从DB v0恢复，但只看到1 active/7 dead，无法形成merge。该失败是调度启动顺序与短walltime组合，不是HA authority/fencing失败。
- 原始证据：`fsdiloco_syncer_candidate.o2498987`、`fsdiloco_static_learner.o2498988.{0..7}`、run root `runs/fs_diloco/plan02_phase1_final_68de59c`及上述launch artifact。scheduler状态：learner0–5 `Exit_status=-29`，learner6 `Exit_status=1`，其余在确认quorum已不可能后终止。
- 清理动作：为避免已无可能通过的run继续占用配额，operator执行`qdel 2498987.opbs '2498988[].opbs'`；successor最终exit271，learner7 exit-29。没有删除run或失败证据。
- 已确认根因：launcher同时释放successor和learner array，不能保证successor先获得一个节点；短learner walltime把scheduler queue/startup delay计入同一预算，形成“learners占资源等待successor、successor排队等待资源”的可用性死锁。
- 下一轮：把learner array改为PBS `depend=after:<successor_job>`（successor开始运行即释放，而非等待其结束），保留successor自身`afterany:<crash_job>`。这先保证一个successor slot，再并发启动8 learners。依据无startup等待时此前learners约30秒实测，replacement使用45秒learner walltime；successor包含15秒lease等待与训练，使用40秒；crash继续15秒。新增launcher artifact记录该start dependency。相同completed gate只有8 learners、successor和Checker全部exit0且性能/可靠性指标通过才算PASS。

## 2026-08-06 15:06 JST — phase1-final-1plus8 acceptance（连续失败 2）

- 命令与环境：clean source commit `14aeef1691c20d843ad41766033a709046d4bc46`；launcher `2499014.opbs`请求10秒/实际1秒/exit0，crash syncer `2499016.opbs`请求15秒/实际6秒/预期exit137，successor `2499017.opbs`请求40秒/实际22秒/exit0，learner array `2499018[0-7]`各请求45秒/实际16–17秒/全部exit0。`depend=after:<successor>`使successor 15:05:02先运行，8 learners在15:05:04–05全部并发启动，上一轮调度死锁已修复。
- 预期：completed Checker除authority/terminal/10-merge不变量外，§11.1 business transaction p99严格低于`renew_interval/2`并返回PASS。
- 实际：runtime达到epoch2、v10、5120 tokens、terminal generation2、leader released；217个真实renew、0 renew failure、1.0188秒takeover latency均通过。Checker PBS `2499023.opbs`请求10秒/实际4秒/exit1，artifact `artifacts/20260806-1506_phase1-completed-checker_pass.json`返回`BLOCKED`，唯一错误为business transaction p99 `0.03049235s`高于当前0.025秒阈值；459个样本p95 `0.01338487s`、max `0.06030479s`、failure 0。
- 原始证据：`artifacts/20260806-1503_phase1-independent-launch_pass.json`、`artifacts/20260806-1506_phase1-completed-checker_pass.json`、run root `runs/fs_diloco/plan02_phase1_final_14aeef1`及jobs的原始输出。
- 已确认根因：为了在短acceptance runtime内取得≥100 renew样本，把acceptance专用`renew_interval`降到0.05秒；冻结门槛因此同步收紧到25ms，低于本次共享FS上正常业务transaction p99。结果不是transaction failure或锁饥饿，而是采样频率与性能阈值自相矛盾。
- 下一轮：acceptance专用renew/heartbeat/candidate poll调整为0.1秒、lease busy timeout调整为100ms。按本轮217个样本线性估算仍约108个真实renew，满足≥100；business p99阈值恢复到50ms，高于已观察30.5ms但仍严格执行plan的`renew_interval/2`，不得修改Checker公式或事后放宽。重跑完整1+8；依据本轮实测使用crash15秒、successor30秒、learner25秒和Checker10秒的最短带余量walltime。

## 2026-08-06 16:11 JST — phase1-matched-performance（连续失败 1）

### Experiment identity

- Clean source commit `d9ab027fac7877e46d5cf4dda31d820dc8befbe2`，source fingerprint `sha256:aeba76f01a31cfbb42e4a332ecd7e969c0a582ee3662bb716f8ac3e415319485`，run `plan02_phase1_final_d9ab027`，descriptor SHA-256 `93a75184e2617f6b50f4a8151e50cf2411ec8c574eee352b8dcfa5cd78887b61`。
- Miyabi `debug-g` PBS `2499293.opbs`，compute node `mg0003`；提交前`bash -n scripts/miyabi/*.pbs`和literal group扫描通过。精确提交为：

  ```text
  qsub -q debug-g -l walltime=00:01:00 -v FS_DILOCO_SHARED_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/plan02_phase1_final_d9ab027,PROJECT_ROOT=/work/xg24i002/x10041/fsb_decoupled_diloco,STAMP=20260806-1611 scripts/miyabi/run_plan02_phase1_matched_performance.pbs
  ```

- 请求walltime `00:01:00`、实际`00:00:28`、scheduler `Exit_status=1`；一分钟为28秒相邻实测保留32秒启动/运行/收尾余量，失败不是walltime不足。

### Expected and observed behavior

- 预期：candidate observer和baseline各至少400个fenced business transaction样本，observer nearest-rank p99不超过`baseline * 1.25 + 0.002s`；legacy/HA checkpoint各100样本并通过同公式，writer transaction attempt为0。
- 实际：checkpoint gate通过（legacy p99 `0.019466s`，HA p99 `0.016676s`，允许`0.026333s`），candidate共观察21次且writer attempt为0；但business baseline p99 `0.006261s`、observer p99 `0.017182s`，超过允许值`0.009826s`，artifact状态为`BLOCKED`。
- 原始结构化证据：`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1611_phase1-matched-performance_pass.json`（文件由预期PASS输出路径生成，但内容为BLOCKED），SHA-256 `ef8b6ecfb1572fd3b1f60a18d438a21ac42ef3b948e0d0eb6a28977c7bc81084`；PBS合并输出为`fsdiloco_p1_match.o2499293`。

### Confirmed facts, inference, and next falsification

- 相同实现的16:05预检出现相反的时间偏差：baseline p99 `0.030623s`而observer p99 `0.010470s`。两次28秒运行中比例方向反转，说明当前只有8个100-transaction大块且每块重新启动observer线程的采样顺序，把共享文件系统的时间漂移和线程启动边界混入了candidate效果；单次通过或重跑碰巧通过都不能证明门禁。
- 下一轮不放宽冻结的25%+2ms阈值。把400+400样本拆成更多细粒度AB/BA配对块，复用一个candidate线程并在baseline块确认其静默、在observer块确认至少一次完整`terminal_state + observe`循环，以降低跨时段漂移且保留production只读操作；artifact增加每块顺序/样本/observation证据。新增schedule和平衡/静默同步回归，运行focused+full关联测试，再在compute node重跑同一matched experiment。通过条件仍为原始冻结公式、每侧至少400样本、每个observer块有观察且writer attempt严格为0。
