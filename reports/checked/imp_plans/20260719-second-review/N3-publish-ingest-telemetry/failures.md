# N3 失败记录

## 2026-07-21 — final Ruff gate FAIL

- experiment：`PUB-TEL-G5-lint-01`，连续失败次数 1。
- 命令：`.venv/bin/ruff check fs_diloco tests scripts/miyabi`。
- 实际：`tests/test_parallel_publication.py:96` 的测试 callback 用赋值 lambda，触发
  E731；运行时代码与 357 条 pytest 均未失败。
- 下一轮只把该无参 callback 改为同语义 `def`，不修改 telemetry 实现或断言。
