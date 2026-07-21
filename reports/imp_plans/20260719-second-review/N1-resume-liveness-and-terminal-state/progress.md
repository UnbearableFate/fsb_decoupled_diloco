# N1/N8 实施进度

## 2026-07-21 00:05 JST — RSM focused group PASS

- 工作单元：RSM-01–08 聚焦实现与回归；尚未声明整份计划完成。
- 基线：`2eec4337927954058a357cd0dc041a189064fa64`，工作树原有用户改动保持未覆盖。
- 实现摘要：增加 resume heartbeat 内容 fence 与原子 `prepare_full_resume`；把 terminal
  selector 改为 `open / closed_empty / closed_selected` 三态；full/fragment terminal grace
  后重算 input closure，并删除 input-closed 分支的第二次常规 discovery。
- 静态检查（Miyabi 登录节点 `miyabi-g3`）：
  `python -m compileall -q fs_diloco tests`、`git diff --check` 均通过。
- 运行环境：Miyabi 1 节点交互 allocation `2421495.opbs`，compute host `mg0010`；
  module 为 `nvidia/25.9`、`nv-hpcx/25.9`；Python `.venv/bin/python`。
- 测试命令：
  `.venv/bin/python -m pytest -q tests/test_liveness.py tests/test_resume.py
  tests/test_syncer_selection.py tests/test_terminal_state_machine.py`。
- 结果：`37 passed in 2.21s`。覆盖旧 stopped heartbeat fence、新 active pointer 接管、
  resume transaction rollback、completed-run fail-closed、error marker 归档、terminal 三态、
  grace 内 reopen，以及 full/fragment closed-empty discovery 调用数为 1。
- artifact：
  `reports/imp_plans/20260719-second-review/N1-resume-liveness-and-terminal-state/artifacts/20260721-000000_n1-focused_pass.log`。
- 未覆盖：全量 pytest、真实 crash/watchdog/resume tiny、2 节点原地恢复和 Checker；这些
  保留在最终验证阶梯，当前不得据此宣称 N1 整体完成。

## 2026-07-21 — RSM-09/10、Checker 与综合门禁 PASS

- Checker 增加 `--require-resume-progress` 与 structured artifact：要求最后一条
  `run_resumed(vN)` 后先观察本代 `learner_liveness_updated(active>0)`，再出现
  `outer_step_applied/global_published(v>N)`，其间禁止 `input_exhausted/stop_published/error`；
  同时核对 persisted resume ID 和完整 learner fence set，stdout 三值契约不变。
- 1-node run `second_review_resume2_20260721` 从 v1 推进到 v2；其立即 post-commit kill
  导致 phase-A 非权威 metric 行缺失，完整 Checker 如实 `BLOCKED`，已记录 failures，
  未放宽门禁。
- 最终 RSM-09 使用 reusable launcher
  `scripts/miyabi/run_2node_resume_regression.pbs`，PBS job `2421684.opbs`，hosts
  `mg0215 + mg0601`，walltime `00:00:50`，Exit_status=0。phase A 在 mg0215 建立
  SQLite v1、DB learner=stopped、固定 heartbeat=stopped 且无 stop，再在 30 秒 terminal
  grace 内 SIGKILL 精确 syncer PID；phase B syncer 在 mg0215 原地 resume，新 learner 在
  mg0601 发布本代 heartbeat。
- structured 顺序：`run_resumed(v1, fence_count=1)` → 本代 `active=1` →
  `outer_step_applied(v2)`；其间无 stop/error。终态 DB/latest/stop/summary 均为 v2，
  新 heartbeat 为 mg0601/stopped；扩展 Checker `PASS`。
- RSM-10 completed-run fail-closed 与 error marker archive 由 `tests/test_resume.py` 覆盖。
- final compute focused group：allocation `2421687.opbs`，`95 passed in 6.31s`；final full：
  `357 passed in 13.67s`。标准 full tiny `second_review_full_20260721` 的 plan-01 Checker
  另行 `PASS`。
- 关键 artifacts：
  `artifacts/20260721_rsm09_2node_resume_evidence.json`、
  `artifacts/20260721_rsm09_2node_checker_details.json`、
  `artifacts/20260721_rsm09_2node_pbs_accounting.log`、共享
  `reports/imp_plans/20260719-second-review/20260721_final_{focused,full}_pytest.log`。
