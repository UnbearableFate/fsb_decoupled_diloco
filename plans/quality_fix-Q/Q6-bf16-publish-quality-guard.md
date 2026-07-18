# Q6：BF16 publish 的质量门禁协议

## 1. 元信息

- 来源：review Q6（低，正面确认 + 一条预警）。当前量化链路核对无问题：upload BF16 → syncer FP32 聚合 → FP32 发布为单次量化；`align_state_to_publication_dtype`（syncer.py:89-104）保证 publish_dtype≠compute_dtype 时内存权威与磁盘一致。**预警**：若 [perf_fix-E/E1](../perf_fix-E/E1-publish-path-io.md) 启用 BF16 publish，theta 将每 outer step 经历一次 bf16 round-trip（约 3 位十进制有效数），50 步累计影响未知。
- 性质：**实验协议 + 可执行判定门禁**；round-trip telemetry 的运行时实现归 E1，
  Q6 负责消费该字段并给出 fail-closed 三态判定。本文件是 E1-L1 引用的质量侧权威定义。
- 前置：[Q4](Q4-prediction-validation-eval.md) L0 的 eval 协议。

## 2. 门禁定义（E1 实验必须满足）

1. **成对对照**：BF16 publish run 必须与同 source fingerprint、同 seed、仅 `syncer.publish_dtype` 不同的 FP32 run 配对，至少 3 seeds；`compute_dtype`/device/io dtype 不得随之改变；
2. **validation 判据**：先用 FP32 的 ≥3 seeds 冻结噪声底 `σ_fp32`，定义 `ε=max(0.01 nats, σ_fp32)`。BF16 paired loss degradation 的均值必须 ≤ε、每个 seed 必须 ≤2ε；任一失败即不通过，样本不足只能判“需更多 seed”；
3. **累积误差观测**：每 outer step 在 publication alignment 前记录 theta BF16 round-trip 的 L2、L∞、relative-L2；float32 publish 三者为 0。检查 relative-L2 对 version 的线性斜率及后半程/前半程均值比：斜率的 95% CI **不得完全位于 0 以上**（即下界 ≤0；区间跨 0 或完全为负均可）且比值 ≤1.25，才支持“没有累积增长”；否则不通过或需更多证据。原先“CI 必须覆盖 0”的表述会把误差显著下降误判为失败，既不检测累积误差，也与风险方向相反，已在正式结果判定前的执行审计中修正；
4. **默认值规则**：上述三项全部通过前，`publish_dtype` 默认保持 float32；任何配置模板/文档不得把 bfloat16 写成推荐值。通过后如改默认，属于配置语义变更，按仓库 fail-closed 惯例另行评审。

## 3. 完成谓词

1. ε、paired/worst-seed 与 half-ratio 阈值在首轮前冻结；首轮执行暴露 slope
   风险方向写反后，必须保留原失败、在接受任何正式结论前修正为“只拒绝 CI 全正”、
   加下降趋势回归并重跑完整门禁（QGB-01）；
2. round-trip 误差 telemetry 字段定义成文（实现归 E1 的 telemetry loop，本文件只定义口径）（QGB-02）；
3. E1-L1 实验按本门禁执行并出结论：通过/不通过/需更多 seed，三态之一明确写入 run_analysis（QGB-03）。

## 4. 范围与非目标

- **范围内**：门禁协议、可执行判定器、判据与误差观测口径。
- **非目标**：publication/telemetry 运行时实现（归 E1）；FP8/FP4 等更激进量化
  （Streaming DiLoCo 方向，未来研究矩阵）；learner 上传 BF16 的既有语义（已核对
  安全，不重开）。

## 5. 报告与文档同步

报告目录 `reports/imp_plans/quality_fix-Q/Q6/`（存放 ε 冻结记录与最终判定）。E1 计划 §1 已引用本文件；判定完成后 review 报告 Q6/E1 条目标注结论。本文件极小，若执行中发现门禁不足（如误差单调累积），升级路径是补充判据并在 failures.md 记录，而不是放宽。
