# Q6：BF16 publish 的质量门禁协议

## 1. 元信息

- 来源：review Q6（低，正面确认 + 一条预警）。当前量化链路核对无问题：upload BF16 → syncer FP32 聚合 → FP32 发布为单次量化；`align_state_to_publication_dtype`（syncer.py:89-104）保证 publish_dtype≠compute_dtype 时内存权威与磁盘一致。**预警**：若 [perf_fix-E/E1](../perf_fix-E/E1-publish-path-io.md) 启用 BF16 publish，theta 将每 outer step 经历一次 bf16 round-trip（约 3 位十进制有效数），50 步累计影响未知。
- 性质：**实验协议/门禁，无代码**。本文件是 E1-L1（BF16 对照）引用的质量侧门禁的权威定义。
- 前置：[Q4](Q4-prediction-validation-eval.md) L0 的 eval 协议。

## 2. 门禁定义（E1 实验必须满足）

1. **成对对照**：BF16 publish run 必须与同 commit、同 seed、仅 `syncer.publish_dtype` 不同的 FP32 run 成对提交（P6 单变量）；
2. **validation 判据**：两组终态 checkpoint 按 Q4 协议评估；BF16 组 validation loss 劣化超过阈值 ε（SPECIFY 冻结，建议取 FP32 组多 seed 标准差的 1 倍作为 ε 的量级参照——若尚无多 seed 数据，先跑 FP32 双 seed 估计噪声底）→ 判不通过；
3. **累积误差观测**：per-outer-step 记录 `theta` 的 bf16 round-trip 误差范数（syncer 侧一行计算：量化前后差的 L2/L∞），确认误差是有界抖动而非随版本单调累积（趋势图入报告）；
4. **默认值规则**：上述三项全部通过前，`publish_dtype` 默认保持 float32；任何配置模板/文档不得把 bfloat16 写成推荐值。通过后如改默认，属于配置语义变更，按仓库 fail-closed 惯例另行评审。

## 3. 完成谓词

1. ε 与噪声底估计方法冻结并成文（QGB-01）；
2. round-trip 误差 telemetry 字段定义成文（实现归 E1 的 telemetry loop，本文件只定义口径）（QGB-02）；
3. E1-L1 实验按本门禁执行并出结论：通过/不通过/需更多 seed，三态之一明确写入 run_analysis（QGB-03）。

## 4. 范围与非目标

- **范围内**：门禁协议、判据、误差观测口径。
- **非目标**：任何代码实现（归 E1）；FP8/FP4 等更激进量化（Streaming DiLoCo 方向，未来研究矩阵）；learner 上传 BF16 的既有语义（已核对安全，不重开）。

## 5. 报告与文档同步

报告目录 `reports/imp_plans/quality_fix-Q/Q6/`（存放 ε 冻结记录与最终判定）。E1 计划 §1 已引用本文件；判定完成后 review 报告 Q6/E1 条目标注结论。本文件极小，若执行中发现门禁不足（如误差单调累积），升级路径是补充判据并在 failures.md 记录，而不是放宽。
