# N2/N4 失败记录

## 2026-07-21 — AST focused attempt 1 FAIL

- experiment：`AST-STOP-focused-01`，连续失败次数 1。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`，`.venv/bin/python`。
- 命令：`.venv/bin/python -m pytest -q tests/test_adoption_strategy.py
  tests/test_learner_rebase.py`。
- 预期：adoption/rebase 聚焦组全绿。
- 实际：`3 failed, 33 passed`；三条既有策略测试在新增 stop 检查读取
  `ctx.paths.stop_json` 时触发 `TypeError: unsupported operand type(s) for /: 'str' and
  'str'`。
- 事实：测试 `_context()` 使用 `RunPaths("unused")`，而 `RunPaths.shared_root` 的运行时
  契约是 `Path`；此前测试没有访问 path property，因此错误 fixture 未暴露。实现的 stop
  分支尚未进入，不能据此判断策略逻辑失败。
- artifact：
  `reports/imp_plans/20260719-second-review/N2-N4-adoption-stop-races/artifacts/20260721-001000_n2-n4-focused_pass.log`（文件名沿用预分配名称，内容为 fail）。
- 下一轮只修改：把测试 fixture 的默认 shared root 改为 `Path("unused")`；不修改策略
  逻辑。证伪检查：原三条失败与新 AST-STOP 测试一起重跑。
