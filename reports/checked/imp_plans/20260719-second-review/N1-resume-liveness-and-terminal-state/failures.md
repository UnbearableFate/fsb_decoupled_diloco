# N1/N8 失败记录

## 2026-07-21 — 1-node resume pipeline attempt 1 FAIL

- experiment：`RSM-09-1node-01`，连续失败次数 1。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`。
- 目标：phase A 在至少提交 v1 后 SIGKILL syncer，让 learners 正常写旧代 stopped
  heartbeat；phase B 原地 resume 并提交 vN+1。
- 实际：`FS_DILOCO_PUBLICATION_FAILPOINT=after_db_commit` 同样作用于初始化 v0，syncer
  在 DB v0 commit 后、首次 latest 前即被杀；learners 只记录 `process_start` 后等待缺失的
  latest，未进入训练/最终 stopped。人工中断 wait 后只终止精确记录的两个 learner PID，
  未删除 run artifact。
- 已确认原因：failpoint 名称不区分 initialization publication 与 merge publication；该
  注入方法无法建立 RSM-09 所需前置状态，不能用这个 run 判断 resume liveness 修复。
- 证据：run root
  `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/second_review_resume_20260721`；
  logs `logs/local_second_review_resume_20260721/`。
- 下一轮只修改实验控制：正常启动 phase A，外部只读轮询 SQLite committed version，
  观察到 `v>=1` 后向已解析的 syncer PID 发 SIGKILL；不改运行时代码和 timeout。随后等待
  learners 自然 stopped，再启动 resume phase。

## 2026-07-21 — 1-node resume Checker follow-up BLOCKED

- experiment：`RSM-09-1node-02-checker`；phase-B 进程本身已成功从 v1 提交到 v2，
  但独立 Checker 返回 `BLOCKED`。
- 环境：Miyabi allocation `2421687.opbs`，host `mg0010`。
- 原因已定位：phase A 的外部轮询在看到 SQLite v1 commit 后立即 SIGKILL syncer，落在
  权威 DB commit 与非权威 `syncer_metrics.csv` append 之间；最终 CSV 只有 phase-B v2
  一行，违反 plan-01 完成态的 `metric row count == version` 约束。
- 该结果不表示 resume-progress 失败：日志顺序仍为 `run_resumed(v1)` → 本代
  `active=2` → `outer_step_applied(v2)` → `input_exhausted(v2)`；但不能把三值 Checker
  的 `BLOCKED` 改写成 PASS，也不放宽原 Checker 的完整 telemetry 门禁。
- artifacts：`20260721_resume2_checker_stdout.log`、
  `20260721_resume2_checker_details.json`。
- 后续使用可复用 2-node launcher：phase A 等待 v1 指标落盘且 DB/heartbeat 均 stopped，
  在 30 秒 terminal grace 内受控杀 syncer，再跨节点 resume；该更强实验已由 job
  `2421684.opbs` 的独立 Checker 返回 PASS。
