# Q2：staleness 加权——证据管道与 λ 消融

## 1. 元信息

- 来源：review Q2（中）。merge 是绝对快照的 token/staleness 加权平均（merge.py:16-33, 115-123；syncer.py:1761-1770）；stale proposal 缺少最近 1–2 版全局进展，平均它等价于把全局参数往回拉，而 `staleness_lambda=0.25` 只给 `1/(1+0.25s)` 的温和降权。G run 是现成旁证：staleness≥1 占 64% 对齐 local loss 全面回退（+0.05）。根本路线（base-relative displacement）已在 00 §4.5 规划；本计划做 review 建议的两件可先行低成本事：λ 从未消融（所有 run 固定 0.25）、Q2 影响无法回归分析（缺 per-merge 证据字段）。
- 性质：**telemetry + 受控消融实验**。merge 语义零改动。
- 影响文件：`fs_diloco/runtime/syncer.py`（metrics）、分析脚本、实验配置。
- 前置：消融 run 必须在 [B2](../bug_fix-B/B2-scheduler-decoupling.md)+[Q3](Q3-data-shuffle.md) 新基线之后（Q1 纪律）；validation 指标依赖 [Q4](Q4-prediction-validation-eval.md) L0 协议。

## 2. 目标与完成谓词

1. per-merge 证据字段落地：每次 merge 记录 `effective_staleness_mean=Σ(staleness×effective_weight)`、计数直方图、`fresh_effective_weight`，full/fragment 口径分别以 global/fragment version 为准（STL-01）；分析脚本输出 run-level 分布，并把 merge 行与其后第一个 learner update/loss 做明确标为 observational 的联动表（STL-02）；
2. 消融矩阵冻结为 λ={0.25 基线, 1.0, 4.0}（仅 λ 单变量）以及独立 fresh-only policy control（`max_staleness_versions=0`，不是伪装成 λ=∞）；同 source fingerprint、每条件 ≥3 seeds、Q4 validation 指标，结论入 run_analysis（STL-03）;
3. 数据反哺路线决策：消融结果与 00 §4.5（base-relative displacement）的优先级关系成文——若强降权已消除大部分回退，displacement 优先级可降；反之升（STL-04）。

## 3. 范围与非目标

- **范围内**：metrics 字段、分析能力、λ 消融。
- **非目标**：base-relative displacement 实现；运行时 merge 数学/默认值改变。fresh-only 只作为独立策略对照，不与 λ 曲线混为一条单变量序列。
- **审计修正**：终态 validation 每 run 只有一个点，current-only GC 也不保留每版本 checkpoint，不能声称做“per-merge staleness vs subsequent validation”回归。STL-02 的 per-merge 联动只使用随后 learner loss 并标为观察；validation 用于 STL-03 的 run-level 多 seed 比较。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 字段冻结 | 字段名/口径/写入位置（P5）；STL-01 单元先 RED | metrics 字段实现 | tiny run 字段可见；用既有 G run 日志（如可回放）或新 tiny run 验证口径 |
| L1 分析能力 | run summary + merge→next learner update observational 表冻结 | 分析脚本加 JSON/CSV 输出 | STL-02：对 tiny/既有兼容 run 产出示例；旧 run 缺字段不反推 |
| L2 消融 | 上述 3 个 λ 条件 + fresh-only、3 seeds、validation 协议冻结；等待新基线 | 无代码（配置矩阵） | 消融 run 组 + STL-03 结论；STL-04 决策成文 |

## 5. 测试矩阵与通过条件

| ID | 项目 | 通过条件 |
| --- | --- | --- |
| STL-01 | 字段 | per-merge staleness 构成字段完备，人工核算一致 |
| STL-02 | 分析 | 示例分析产出（staleness 构成与质量指标的联动图/表） |
| STL-03 | 消融 | P6 三件套满足；各 λ 档的 validation 数据齐备 |
| STL-04 | 决策 | 与 00 §4.5 的优先级关系有数据支撑地成文 |

## 6. 验证阶梯

登录节点静态 → 1 节点 pytest + tiny run（STL-01/02）→ 9 节点仅消融 run（G7/G8 纪律）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/quality_fix-Q/Q2/`。消融各档若与基线无显著差异，负结论照常入账（并直接抬升 00 §4.5 优先级）。按 AGENTS.md 三连败升级。

## 8. 文档同步

metrics 字段表补新字段；run_analysis 记录消融结论；00 §4.5 登记优先级决策。
