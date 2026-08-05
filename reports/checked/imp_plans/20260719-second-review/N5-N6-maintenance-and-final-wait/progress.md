# N5/N6 实施进度

## 2026-07-21 — MFW-01–06 focused PASS

- 实现摘要：删除 runtime maintenance 对 payload 历史 `.meta.json` 的扫描；提取
  `wait_for_final_fragment_progress()`，fragment finally 等待期间立即并按 heartbeat
  interval 写 `active, phase=final_fragment_wait`，adoption 阻塞跨过间隔后补写包含新
  merge event 的 heartbeat，且不延长 no-progress deadline。
- 静态检查：登录节点 compileall、`git diff --check` 通过；`rg` 确认 `fs_diloco/`
  不再存在 payload `.meta.json` glob。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`；`.venv/bin/python`。
- 命令：`.venv/bin/python -m pytest -q tests/test_retention.py
  tests/test_fragment_pointer_discovery.py tests/test_fragment_final_wait.py
  tests/test_shared_runtime_primitives.py`。
- 结果：修正一次测试时序期望后 `19 passed in 2.77s`。覆盖 tensor/tmp/GC 保持、固定
  discovery 面、heartbeat cadence、adoption 后版本可见、stop 立即返回和 timeout。
- artifact：
  `reports/imp_plans/20260719-second-review/N5-N6-maintenance-and-final-wait/artifacts/20260721-003500_n5-n6-focused_pass.log`。
- 未覆盖：MFW-07 真实 fragment learner finally 异常路径、fragment tiny 与全量 pytest；
  留在综合验证。

## 2026-07-21 — MFW-01–07、真实 final wait 与综合门禁 PASS

- 新增 `finalize_fragment_adoption_and_heartbeat()`，把 final adoption 的异常诊断边界与
  stopped heartbeat 分开；注入异常测试证明事件
  `final_fragment_adoption_failed` 后仍调用 stopped heartbeat，随后重新抛出原异常，
  不生成伪成功（MFW-07）。
- 真实 fragment final-wait run `second_review_fragment_finalwait_20260721`：配置 heartbeat
  0.5 秒、dead-after 1.5 秒、final wait 4 秒。实际 final-wait heartbeat 持续 3.53 秒，
  最大间隔 0.505 秒；syncer 在整个窗口的 20 个 liveness 采样均为 active=1、dead=0，
  最后固定 pointer 为 `stopped, phase=process_exit`。该 run 故意让目标不可达并以
  no-progress timeout 结束，只用于 MFW-04 的“无新版/stop、等待超过 dead-after”分支，
  不替代正常 fragment smoke。
- 正常 fragment tiny `second_review_fragment_20260721` 达到 global event 4，片版本 2/2，
  2 learners stopped、无 error/no-progress、payload 清空，fragment smoke assertion PASS。
- final focused 组合组 `95 passed in 6.31s`；full `357 passed in 13.67s`；静态搜索确认
  runtime 无 payload `.meta.json` scan。
- artifacts：`artifacts/20260721_mfw04_real_finalwait_evidence.json`、
  `artifacts/20260721_final_focused_pytest.log`，以及共享
  `20260721_fragment_tiny.log`、`20260721_final_full_pytest.log`。
