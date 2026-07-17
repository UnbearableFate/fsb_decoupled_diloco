# perf_fix-E 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 5 节（系统设计审查——训练运行效率，E1–E6）中的每个要点提供一份独立计划。与 bug 修复不同，本目录多数条目是**性能改进与受控实验**：计划的交付物常常是"telemetry + 对照数据 + 决策"，而不只是代码。风格仍遵循 `plans/ref/实施计划制定与 Agent 执行经验.md`，并特别强调 §3（昂贵实验前先有 telemetry，P5）与报告 P6（单变量、记 commit、多 seed）。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置/关联 |
| --- | --- | --- | --- | --- |
| 1 | [E3-materialize-default.md](E3-materialize-default.md) | fragment 物化缺省改为显式配置；物化成本单独计量并消融 | 配置语义修正 + 实验 | 无 |
| 2 | [E1-publish-path-io.md](E1-publish-path-io.md) | 发布关键路径 I/O：BF16 publish 对照 + weight/outer 并行写 | 实验 + 行为保持优化 | BF16 质量门禁见 [quality_fix-Q/Q6](../quality_fix-Q/Q6-bf16-publish-quality-guard.md) |
| 3 | [E2-ingestion-publish-overlap.md](E2-ingestion-publish-overlap.md) | 事件化 ingestion（短 scan）与发布流水线化 | 架构实验 | E 系列中体量最大；先做 L0 telemetry |
| 4 | [E6-adoption-pause-telemetry.md](E6-adoption-pause-telemetry.md) | adoption 停顿计量 | telemetry | 改进本身委托 E1/策略计划 |
| 5 | [E5-syncer-node-cost.md](E5-syncer-node-cost.md) | 第 9 节点成本入账 + CPU syncer 试验 | 计量 + 部署实验 | 无 |
| 6 | [E4-fragment-discovery-frontier.md](E4-fragment-discovery-frontier.md) | fragment 固定发现面 + frontier 短路 | 行为保持优化 | 建议在 [S3](../bug_fixing_plans/S3-full-fragment-loop-dedup.md) 之后 |

顺序依据：E3 是已确认的对照失真源且改动最小；E1 的 BF16 配置已就绪只欠对照；E2 潜在收益最大但侵入协调核心，telemetry 先行；E4/E5/E6 为低severity 收尾。凡涉及吞吐结论的 9 节点对照 run，必须满足 P6 纪律（同 commit、单变量、run_identity 含 git commit——若 run identity 尚未含 commit，先完成该 R0 项）。

## 统一约束

- 记录规则以 [plans/AGENTS.md](../AGENTS.md) 为准；报告路径映射：`plans/perf_fix-E/<Ex>-*.md → reports/imp_plans/perf_fix-E/<Ex>/`。
- 效率结论一律以 telemetry 字段为准，不以日志时间戳目测；每个计划的 L0 都是 telemetry loop（P5）。
- 吞吐对照实验属于交付物而非验收门禁的部分，遵循 G7/G8（冻结配置、阶段交接、不取消 in-flight 作业）。
- 正确性验证阶梯与 bug 计划相同：登录节点静态 → 1 节点 pytest+tiny → 需要时 2 节点；9 节点仅用于对照实验本身。
