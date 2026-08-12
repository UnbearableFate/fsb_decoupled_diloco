# plan04 进度

## 2026-08-12 — INIT 与当前设计更新

- Source：branch `new_plan04`；branch point 与 workflow pin 均为 `f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`。
- 重新冻结当前 workload：固定版本 GPT-2/WikiText-2，Full Protocol 为 200 local steps × 25 global steps、quorum 4；DDP 与 periodic-average baseline 均为 5,000 optimizer steps。
- Baseline 与 normal 的初始 PBS walltime 调整为 40 分钟；每项实验只运行 1 seed，normal 相对两种 baseline 的 loss/time 阈值调整为 30%。
- 删除旧 Full Protocol baseline/timed 配置和 2,000-step baseline 入口；统一 one-line submission、summary 与 comparison 路径。
- 回滚全部 learner runtime-attestation 启动屏障。Terminal admission 不要求八个提交 job 全部进入 runtime；learner fault 在 60 秒边界只从当前 admitted bootstrap learner 中选择目标。

## 2026-08-12 — IMPLEMENT 与 focused 验证

- Miyabi compute jobs `2540105.opbs`、`2540108.opbs`、`2540120.opbs` 使用 1-node `interact-g` 验证当前变更。
- 最终 focused 命令覆盖 plan04 harness、syncer composition、summary tool 和 standalone baseline config/artifact/protocol tests，结果为 `49 passed`。
- 完整 pytest 在未提交 source 上得到 `585 passed, 5 failed`；五项失败均由 harness 正确拒绝 dirty validation source，分类为 `source-invalid`，不作为产品失败。当前实现提交并冻结 clean candidate 后重跑完整测试。
- Login node 上 `git diff --check`、修改 Python 文件 compile、全部适用 PBS/Bash `bash -n` 检查通过；literal PBS group 均为 `xg24i002`。
- 当前 `qstat` 无 active/queued job。下一步为提交实现、执行 clean-candidate 全量测试和 PREFORMAL 审查。

## 2026-08-12 — Clean candidate 全量验证

- Candidate commit：`4e905f31a8b136dd2a6f210944552ff6bbaa5aff`。
- Miyabi compute job：`2540212.opbs`，1-node `interact-g`，默认模块 `nvidia/25.9` 与 `nv-hpcx/25.9`。
- 命令：`.venv/bin/python -m pytest -q`。
- 结果：`591 passed in 34.74s`；PBS 使用 walltime 约 1 分 50 秒。
- 下一步：完成 PREFORMAL current-state 审查并冻结正式 target；随后提交两种 5,000-step baseline。
