# Q3：数据分片 epoch 级 shuffle

## 1. 元信息

- 来源：review Q3（中）。`wikitext_batches`（hf_data.py:75-119）+ `_batched_blocks`（hf_data.py:60-72）：WikiText-2 train ~2.4M tokens，8 learner contiguous 分片后每片 ~290 个 1024-block；5000 步 ≈ 对同一 290 block 按**固定顺序**循环 ~280 遍（顺序模循环，无任何 shuffle）。后果：(a) local train loss 大量成分是记忆化；(b) 固定循环序 + 固定 seed 使不同策略的 batch 序列在首次 adoption 时刻错位后完全发散，放大异步调度这一混杂因子；(c) 8 分片互不相交，merge 平均的是 8 个记忆不同子集的模型。
- 性质：**语义变更（基线作废型）**——合入后所有历史 loss 不可比。**建议与 [B2](../bug_fix-B/B2-scheduler-decoupling.md) 同批合入**，两次基线作废合并为一次。
- 影响文件：`fs_diloco/modeling/hf_data.py`、测试。
- 前置：无代码前置。

## 2. 设计规格

- `_batched_blocks` 增加 epoch 级重排：第 e 个 epoch 使用由 `(training.seed, learner_index, e)` 经固定 64-bit mixing 派生的 permutation；把各 epoch permutation 串成无限 block stream，再按固定 `micro_batch_size` 切批。若 block 数不能整除 batch，最后一批跨 epoch 边界，仍保持固定 batch shape；“每个 epoch 完整 permutation”是 stream 语义，不把跨界 batch 错算为 epoch 内重复；
- 确定性保持：同 (seed, learner_index) → 逐 batch 完全可复现（多 seed 纪律 P6 依赖此性质）；
- 分片方式（contiguous、互不相交）**不动**——分片重叠/全局采样属 00 §4.6 数据升级方向（非目标）；
- synthetic 路径不动；新增 `data.shuffle_blocks: bool = true`（默认开启）；`false` 必须逐 batch 复现旧 modulo 序列。seed 取 `training.seed`，显式传入 data iterator，不在 `DataSection` 复制第二个 seed。

## 3. 目标与完成谓词

1. 每个 epoch 是 block 集合的一个完整 permutation：无重复、无遗漏（DSH-01）；相邻 epoch 顺序不同（DSH-02）；
2. 确定性：同 (seed, learner, epoch) 逐 batch 相同；不同 learner/epoch 的 permutation 互异（DSH-03）；
3. `shuffle_blocks=false` 逐字节复现旧序列（回归锚，DSH-04）；
4. tiny run 正常完成；全量 pytest 通过；
5. 基线作废声明与新基线安排（与 B2 合并）记入 run_analysis。

## 4. 范围与非目标

- **范围内**：block 级 epoch shuffle、开关、确定性测试。
- **非目标**：分片重叠、全局采样、更大数据集（00 §4.6）；validation 指标管道（P7，Q4 的 L0 承载）；tokenize/分块逻辑改动。
- **对照污染声明（P6）**：合入后 local loss 语义变化（记忆化成分下降，数值上 loss 可能整体升高——这是预期，不是回退）。必须与 B2 同批建立新基线。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 冻结 | 跨 epoch 固定 batch stream、开关命名、稳定 64-bit seed mixing；baseline source fingerprint | 无实现 | 决策入 progress.md |
| L1 单元 | DSH-01–04 先 RED | epoch shuffle 实现 | 单元全绿；全量 pytest |
| L2 管线 | — | — | tiny run 正常完成；loss 曲线形态变化记录（预期：下降更慢、更平滑）；与 B2 的同批合入协调记录 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| DSH-01 | 完整性 | 将 batch 展平并按 epoch 长度切片后，每片恰为全集 permutation；不可整除反例同样成立 |
| DSH-02 | 重排性 | 相邻 epoch 顺序不同（排除恒等 permutation 的退化实现） |
| DSH-03 | 确定性 | 同参数逐 batch 相同；learner/epoch 间 permutation 互异 |
| DSH-04 | 旧行为锚 | 关闭开关 → 与旧实现逐 batch 一致 |

progress.md 每条记录必须列出覆盖的 DSH ID（P8）。

## 7. 验证阶梯

登录节点静态 → 1 节点 pytest + tiny run。9 节点：不需要门禁；新基线 run（B2+Q3 合入后）自然覆盖。

## 8. 报告、证据与升级规则

报告目录 `reports/imp_plans/quality_fix-Q/Q3/`。按 AGENTS.md 三连败升级。

## 9. 文档同步

docs 数据管道章节补 shuffle 语义与开关；review 报告 Q3 条目标注 commit；00 §4.6 记录"短期项已做，分片重叠/数据升级仍开放"。
