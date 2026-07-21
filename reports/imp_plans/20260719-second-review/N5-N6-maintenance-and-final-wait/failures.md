# N5/N6 失败记录

## 2026-07-21 — MFW focused attempt 1 FAIL

- experiment：`MFW-focused-01`，连续失败次数 1。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`。
- 命令：`.venv/bin/python -m pytest -q tests/test_retention.py
  tests/test_fragment_pointer_discovery.py tests/test_fragment_final_wait.py
  tests/test_shared_runtime_primitives.py`。
- 预期：19 条聚焦测试通过。
- 实际：`1 failed, 18 passed`。MFW-05 测试期望 heartbeat 序列
  `[(0.0,0),(4.5,1)]`，实际为 `[(0.0,0),(2.0,0),(4.5,1)]`。
- 已确认原因：helper 在 t=2.0 到达 heartbeat deadline 时先写 active heartbeat，再读取
  此刻刚可见的 latest；handler 耗时后再补一份携带 event=1 的 heartbeat。额外 t=2.0
  heartbeat 正是“不晚于配置间隔”的目标行为，不是过频（与前一份间隔恰为 2 秒）。
- artifact：
  `reports/imp_plans/20260719-second-review/N5-N6-maintenance-and-final-wait/artifacts/20260721-003000_n5-n6-focused_pass.log`（预分配名称，内容为 fail）。
- 下一轮只修改测试期望以包含 t=2.0 heartbeat，并增加相邻间隔/最终 event 断言；不改
helper 调度逻辑。

## 2026-07-21 — final focused command assembly FAIL

- experiment：`G5-focused-command-01`；这不是 MFW 实现失败，不计入同一 experiment 的
  连续失败次数。
- 环境：Miyabi allocation `2421687.opbs`，host `mg0010`。
- 命令在聚焦文件列表中误写了不存在的 `tests/test_maintenance.py`，pytest 在 collection
  前以 `file or directory not found` 退出，实际未运行任何测试。
- artifact：`reports/imp_plans/20260719-second-review/20260721_final_focused_pytest.log`。
- 下一次先用 `rg --files tests` 解析真实文件名，移除该路径；不修改运行时代码或测试。
