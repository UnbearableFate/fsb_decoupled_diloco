# E1：发布关键路径 I/O（BF16 publish 对照 + 并行写）

## 1. 元信息

- 来源：review E1（中）。每次 merge，syncer 串行同步写 `global_vN` + `outer_vN` 各约 498MB FP32（`publish_dtype` 默认 float32，config.py:83），完成后才提交 DB、切 latest（`publish_global`，syncer.py:167-238）。learner 上传已 BF16 减半，发布侧仍是每 merge ~1GB 串行写，是 run_analysis 认定的端到端主耗时之一。
- 性质：L1 为**受控实验**（BF16 publish 已实现，只欠对照数据）；L2 为**行为保持优化**（并行写）；报告中的选项 3（outer 延迟发布）为**显式非目标**。
- 影响文件：`fs_diloco/runtime/syncer.py`、metrics、PBS/配置。
- 关联：BF16 publish 的质量门禁由 [quality_fix-Q/Q6](../quality_fix-Q/Q6-bf16-publish-quality-guard.md) 定义——**没有 validation 对照，BF16 不得成为默认**。

## 2. 目标与完成谓词

1. telemetry：`publish_global` 拆分出 `publish_weight_seconds`、`publish_outer_seconds`、`publish_dtype` 字段（PIO-01）；
2. BF16 publish 对照完成：同 commit、单变量（仅 publish_dtype）、其余配置与基线一致的成对 9 节点 run，产出发布耗时对比 + Q6 要求的 validation 对比，结论写入 run_analysis（PIO-02）;
3. 并行写落地且正确性不降级：两文件并发写、**均完成后**才进入 DB 提交；crash matrix 在并发语义下全组合复验（PIO-03/04）；
4. 全量 pytest；tiny run 上 Checker PASS。

## 3. 范围与非目标

- **范围内**：telemetry、BF16 对照实验、weight/outer 并行写。
- **非目标**：outer state 延迟发布（两阶段 latest）——侵入 `theta==outer theta` 校验与 crash matrix 语义，报告已建议"profile 证明必要时再做"；本计划的 L0 telemetry 正是产生该 profile 证据的地方，决策留给数据。
- 兼容性：并行写不改变文件内容与提交点（DB 单事务），对外语义不变。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 telemetry | 字段名/边界/写入位置冻结（P5） | 发布耗时拆分字段 | tiny run 中字段可见；基线 9 节点 run 的发布耗时留档（可复用既有 run 日志推算，注明口径差异） |
| L1 BF16 对照 | 成对实验设计冻结：`fs_diloco_gpt2_wikitext2_8l_5000steps_predict_bf16all_cuda.yaml` 与其 FP32 等价配置 diff 审查（仅 dtype 差异，P6） | 无代码 | 成对 run + Q6 validation 对照；结论与 commit 写入 run_analysis |
| L2 并行写 | PIO-03 先 RED：注入两文件完成顺序的全部交错（含单侧失败），断言 DB 提交只在双完成后发生 | weight/outer 双线程写 | crash matrix（PIO-04）在"weight 完成 outer 未完成"等新中间态下复验：恢复只到事务前/后；tiny run 轨迹等价 |
| L3 收尾 | — | — | 全量 pytest；发布耗时改善数据（并行写预期省约一半发布墙钟）入报告 |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| PIO-01 | telemetry | 两个耗时字段 + dtype 字段出现在 metrics/事件中，数值合理（≈文件大小/带宽） |
| PIO-02 | BF16 成对 run | resolved config diff 仅 dtype；发布耗时、端到端、validation（Q6）三组数据齐备 |
| PIO-03 | 并发完成序 | 任意交错下 DB 提交前两文件均完整；单侧失败 → 无 DB 提交、可重试或干净失败 |
| PIO-04 | crash matrix 复验 | 六阶段 failpoint 在并行语义下全部满足"事务前/事务后"二态恢复 |
| PIO-05 | 无回归 | tiny run 事件轨迹与基线等价（telemetry 新字段在 profile 声明） |

## 6. 验证阶梯

1. 登录节点：静态检查。
2. 1 节点：pytest（含 crash matrix 单测）→ tiny run。
3. 9 节点：仅 L1 对照实验（G7 冻结配置；G8 阶段交接；不取消 in-flight）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/perf_fix-E/E1/`。PIO-04 是本计划风险集中点：并行写导致 crash matrix 任一用例行为含糊时立即停止，按 AGENTS.md 记录后回退到串行（保留 telemetry 与 BF16 对照成果）——发布提速不值得动摇提交点语义。

## 8. 文档同步

docs 发布协议章节补充并行写语义与"双完成才提交"不变量；run_analysis 记录 BF16 结论与适用条件（引用 Q6 门禁）。
