# N2/N4 实施进度

## 2026-07-21 — AST-STOP-01/02/04/05/06/07 focused PASS

- 实现摘要：predict reconcile 的 None 返回会复检 stop；stop 在场时走单一 abandon
  逻辑并清空 prediction state，无 stop 时仍保留 TimeoutError。predict/rebase
  after-publish 保留 newer-latest direct adoption 优先级，只在无新版且无 stop 时创建
  prediction reference/rebase anchor。
- 静态检查：登录节点 `python -m compileall -q fs_diloco tests` 与
  `git diff --check` 通过。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`；module
  `nvidia/25.9`、`nv-hpcx/25.9`；`.venv/bin/python`。
- 命令：`.venv/bin/python -m pytest -q tests/test_adoption_strategy.py
  tests/test_learner_rebase.py`。
- 结果：修正一次测试 fixture 后 `36 passed in 2.99s`。真 timeout 反例、stop-during-wait、
  stop 后不调用 prepare/snapshot、stop 与新版同时出现仍 direct adopt，以及普通策略轨迹
  均通过。
- artifact：
  `reports/imp_plans/20260719-second-review/N2-N4-adoption-stop-races/artifacts/20260721-001500_n2-n4-focused_pass.log`。
- 未覆盖：AST-STOP-03/08 的真实 runner/global-only predict tiny、全量 pytest 和最终
  Checker，留在综合验证阶梯。

## 2026-07-21 — AST-STOP-03/08 runner 与综合门禁 PASS

- 真实 `global_only + predict` tiny run：`second_review_predict_20260721`，1 learner，
  达到配置 v3 后正常 `stop_after_outer_steps`；终态 DB/latest/stop/summary 均为 v3，
  learner stopped、proposal payload 为零、无 error/no-progress/shutdown timeout。
- stop 后最后一次 publish 记录
  `global_prediction_start_skipped_on_stop(base_global_version=3)`，随后
  `process_exit(exit=0)`；plan-01 Checker 返回 `PASS`。这动态覆盖 AST-STOP-03/08，
  并证明 stop 路径没有创建即将丢弃的 prediction reference。
- final focused 组合组 `95 passed in 6.31s`；full `357 passed in 13.67s`。
- artifacts：共享 `20260721_predict_tiny.log`、`20260721_predict_stop_events.log`、
  `20260721_predict_tiny_checker.log`、`20260721_final_{focused,full}_pytest.log`。
