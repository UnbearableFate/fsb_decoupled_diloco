# N3 实施进度

## 2026-07-21 — PUB-TEL-01/02/03/04/06 focused PASS

- 实现摘要：以 `checkpoint_wait_ingestion_callback()` 在 caller 边界决定传 callable 或
  `None`，避免条件表达式落入 lambda body；flag=false 时 publish-ingest 四字段严格为
  0，flag=true 时仍由主线程真实调用 callback。
- 静态检查：登录节点 compileall 与 `git diff --check` 通过。
- 环境：Miyabi allocation `2421495.opbs`，host `mg0010`；`.venv/bin/python`。
- 命令：`.venv/bin/python -m pytest -q tests/test_parallel_publication.py
  tests/test_run_metrics_csv.py`。
- 结果：`12 passed in 1.51s`。覆盖关闭/开启 callback、并行/串行 publication、单边
  checkpoint failure 不提交 DB/latest，以及 metrics totals。
- artifact：
  `reports/imp_plans/20260719-second-review/N3-publish-ingest-telemetry/artifacts/20260721-002000_n3-focused_pass.log`。
- 未覆盖：PUB-TEL-05 真实 full tiny CSV、全量 pytest；留在综合验证。

## 2026-07-21 — PUB-TEL-05 与综合门禁 PASS

- 真实 full tiny run `second_review_full_20260721` 实际完成 v1 merge，
  `syncer_metrics.csv` 非空；每行
  `publish_ingest_passes=0,publish_ingested_updates=0,
  publish_ingested_heartbeats=0,publish_ingest_seconds=0.0`，证明 false caller 传入 None，
  不是零 workload。DB/latest/stop/summary 一致，Checker `PASS`。
- `reports/run_analysis.md` 已补历史口径勘误：修复前 passes 可含空转，历史非零 inserted
  update/heartbeat 仍可解释为实际插入；稳定字段定义同步系统与 module docs。
- final focused 组合组 `95 passed in 6.31s`；full `357 passed in 13.67s`。
- artifacts：共享 `20260721_full_tiny.log`、`20260721_full_tiny_checker.log`、
  `20260721_final_{focused,full}_pytest.log`。
