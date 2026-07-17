# Q1：LR 调度混杂因子（委托 B2 执行）

## 1. 元信息

- 来源：review Q1（高）。量化事实：正式配置 base lr=5e-5、warmup=100、cosine horizon=5000 下，replace 模式每次 adoption 重置 scheduler → 实际 LR 轨迹是 0.01×→1.0×base 的锯齿（均值约 0.5×base），从不进入 cosine 衰减；preserve 模式（H、rebase-preserve）scheduler 连续累积，实际执行了 warmup 一次 + 完整 cosine。**因此 reset vs preserve 的一切 loss 差异同时含"moments 保留"与"完全不同的 LR 日程"两个因子**，现有 run 无法归因；"preserve 带来 384/395 点 local loss 改善"的机制解释必须加入该混杂因子。
- **执行载体**：机制修复的唯一权威规格是 [bug_fix-B/B2-scheduler-decoupling.md](../bug_fix-B/B2-scheduler-decoupling.md)（含 DiLoCo 系文献调研依据）。本文件不重复规格，只承载**质量侧**的验收追踪与纪律约束。

## 2. 质量侧完成谓词

B2 计划完成，且：

1. B2 的 SCH-02/03（LR 与 adoption 解耦、与 completion mode 无关）证据在 `reports/imp_plans/quality_fix-Q/Q1/progress.md` 中被引用归档；
2. run_analysis 中涉及 reset/preserve 质量差异的段落已追加混杂因子注记（追加不改写历史文本），指向 B2 修复 commit；
3. 新基线声明就位：明确记录"B2（建议连同 [Q3](Q3-data-shuffle.md)）合入 commit 之前的全部质量数据仅供机制参考，不得用于策略优劣结论"。

## 3. 对后续实验的纪律约束（本文件的存在意义）

- 在 B2 合入前，**不得**启动 reset/preserve 消融、prediction 质量对照（Q4 的对照部分）、staleness λ 消融（Q2）——先跑只会再产出一批不可归因数据；
- run_analysis 建议的"rebase-preserve + global_only"实验在 B2 之前**禁止执行**：正是 B2 缺陷 3 的触发组合（超 horizon 步 LR 恰好为 0，无告警）；
- 消融开关（moments reset/preserve 显式化）属于 B2 之后的实验计划，其配置形态遵循 [S5](../bug_fixing_plans/S5-config-strategy-grouping.md) 的策略分组。

## 4. 若 B2 计划被放弃

仅在 B2 被明确放弃时本文件升级为独立计划（承接其 §3 设计规格与 SCH 矩阵）。该分支发生前，此文件不含任何实施内容。
