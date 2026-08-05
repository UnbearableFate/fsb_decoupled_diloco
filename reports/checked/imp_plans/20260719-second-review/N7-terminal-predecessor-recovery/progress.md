# N7 实施进度

## 2026-07-21 — TCP-01–09 focused PASS

- 实现摘要：把 evidence manifest 明确为提交点；manifest 前已有 checkpoint 时按
  samefile/SHA256 复用或原子覆盖；manifest 后完整校验版本、source/checkpoint/manifest
  路径、selection/quorum identity 与 source/checkpoint checksum，任一冲突 fail closed。
- 静态检查：登录节点 compileall 与 `git diff --check` 通过。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`；`.venv/bin/python`。
- 命令：`.venv/bin/python -m pytest -q tests/test_terminal_predecessor_capture.py`。
- 结果：`12 passed in 1.46s`。覆盖 hardlink、copy fallback、相同/错误 uncommitted
  checkpoint、checkpoint 后 manifest 前崩溃恢复、字节幂等、missing/corrupt/selection/source
  conflict fail-closed，以及 DB/latest 不变。
- artifact：
  `reports/imp_plans/20260719-second-review/N7-terminal-predecessor-recovery/artifacts/20260721-004500_n7-focused_pass.log`。
- 未覆盖：全量 pytest 和真实 terminal-partial tiny；留在综合验证。

## 2026-07-21 — terminal-partial focused integration 与综合门禁 PASS

- 增加 selector→evidence 的 focused integration：2 个 stopped learner、`quorum_min=2`、
  仅 1 个 fresh proposal 时 selector 返回 `closed_selected`；随后真实 capture 写正确
  checkpoint/manifest，并逐字节证明 SQLite/latest 未变化、无临时文件。这是计划 L4
  允许的 terminal-partial tiny 等价聚焦集成。
- expanded capture group 现为 13 条；包含 MFW/Checker/terminal 状态机的 final focused
  组合组共 `95 passed in 6.31s`，full `357 passed in 13.67s`。
- evidence manifest 的恢复/提交语义已同步 `docs/02-architecture.md`、
  `docs/04-data-flow.md`、`docs/06-configuration.md` 和 runtime-syncer module reference。
- artifacts：`artifacts/20260721_final_focused_pytest.log`、原 crash matrix artifact，
  以及共享 `20260721_final_full_pytest.log`。
