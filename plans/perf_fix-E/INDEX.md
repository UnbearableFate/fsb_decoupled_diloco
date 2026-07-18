# perf_fix-E 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 5 节（系统设计审查——训练运行效率，E1–E6）中的每个要点提供一份独立计划。与 bug 修复不同，本目录多数条目是**性能改进与受控实验**：计划的交付物常常是"telemetry + 对照数据 + 决策"，而不只是代码。风格仍遵循 `plans/ref/实施计划制定与 Agent 执行经验.md`，并特别强调 §3（昂贵实验前先有 telemetry，P5）与报告 P6（单变量、记 commit、多 seed）。

## 审计修正后的前置门禁

原索引把“同 commit、单变量”写成实验纪律，却没有给当前 dirty worktree 一个可执行的源码身份方案；`run_identity()` 也确实尚未记录 revision。任何正式对照前先完成共享 R0：run identity 与 resolved config 记录 `git_commit`、`git_dirty` 和包含 tracked/untracked 实验源码的 `source_fingerprint`。未获授权时不为实验擅自提交用户改动；同一对照组必须 fingerprint 完全相同，并归档 manifest/diff。干净 commit 是推荐形态，稳定 fingerprint 是 dirty worktree 的 fail-closed 等价证据。

质量实验还依赖 Q3 新数据基线与 Q4 validation 协议。E1 的 publish-dtype 对照不得使用现有 `*_bf16all_*` 配置充当“仅 publish_dtype”变量，因为该配置同时改变 `compute_dtype`/device；必须从同一 FP32 compute 配置生成只改 `syncer.publish_dtype` 的配对配置。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置/关联 |
| --- | --- | --- | --- | --- |
| 1 | [E3-materialize-default.md](E3-materialize-default.md) | fragment 物化缺省改为显式配置；物化成本单独计量并消融 | 配置语义修正 + 实验 | B4 已完成；正式消融等待 R0/Q3 |
| 2 | [E6-adoption-pause-telemetry.md](E6-adoption-pause-telemetry.md) | adoption 停顿计量 | telemetry | S1/B2 已完成；可先于正式实验落地 |
| 3 | [E1-publish-path-io.md](E1-publish-path-io.md) | 发布关键路径 I/O：精确 telemetry、并行写、BF16 publish 对照 | 实验 + 行为保持优化 | BF16 实验等待 Q4-L0/Q6；代码可先落地 |
| 4 | [E4-fragment-discovery-frontier.md](E4-fragment-discovery-frontier.md) | fragment 固定发现面 + frontier 短路 | 行为保持优化 | S3/B4/B5 已完成 |
| 5 | [E2-ingestion-publish-overlap.md](E2-ingestion-publish-overlap.md) | 短 scan + 发布 I/O 期间安全摄取 | 配置实验 + 有界协调优化 | 复用 E1 异步 I/O；不再声称 grace 与 publish 可完全重叠 |
| 6 | [E5-syncer-node-cost.md](E5-syncer-node-cost.md) | 第 9 节点成本入账 + CPU syncer/8 节点共置试验 | 计量 + 部署实验 | 复用新基线与 E1/E2 telemetry |

顺序依据：先消除 E3 的已知对照失真并补齐 E6/E1 telemetry，再统一 fragment 发现面；E2 复用 E1 的 I/O future，但严格限制为主线程 DB 摄取，不引入第二 writer；E5 最后消费完整成本分解。凡涉及吞吐结论的正式对照，必须满足 R0 源码身份、单变量和 ≥3 seeds。

## 统一约束

- 记录规则以 [plans/AGENTS.md](../AGENTS.md) 为准；报告路径映射：`plans/perf_fix-E/<Ex>-*.md → reports/imp_plans/perf_fix-E/<Ex>/`。
- 效率结论一律以 telemetry 字段为准，不以日志时间戳目测；每个计划的 L0 都是 telemetry loop（P5）。
- 吞吐对照实验属于交付物而非验收门禁的部分，遵循 G7/G8（冻结配置、阶段交接、不取消 in-flight 作业）。
- 正确性验证阶梯与 bug 计划相同：登录节点静态 → 1 节点 pytest+tiny → 需要时 2 节点；9 节点仅用于对照实验本身。

## 2026-07-18 实施状态

E3 → E6 → E1 → E4 → E2 → E5 已按上述依赖顺序完成；昂贵正式矩阵在共同前置
门禁闭合后并行排队，但没有越过 source/Q3/Q4 依赖。E1–E6 的无条件完成谓词均有
三 seed/动态或定向证据，决策与剩余风险已同步到 `reports/run_analysis.md` 和
`plans/00-RESEARCH_PLAN.md`。逐 ID 证据索引见
[`reports/imp_plans/20260718_perf-E_quality-Q_completion-audit.md`](../../reports/imp_plans/20260718_perf-E_quality-Q_completion-audit.md)。
