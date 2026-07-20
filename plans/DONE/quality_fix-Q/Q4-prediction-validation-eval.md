# Q4：prediction 启发式的 validation 级验证

## 1. 元信息

- 来源：review Q4（中）。`predict_next_global_weight`（learner.py:555-699）以 `momentum×-(1-μ)` 作历史聚合位移代理、`previous_total_update_tokens` 估计自身权重，构造预测全局权重并**在其上继续训练**至 reconcile；预测误差期间的梯度取自偏离真实轨迹的点，reconcile 迁移参数但不修正这些梯度已写入的 optimizer moments（H 保留 moments）。H 的系统指标与 local loss 最好，但按 Q1（LR 混杂）/Q3（记忆化）该信号不足以支撑质量结论。
- 性质：**评估基础设施 + 实验**。现有 lm-eval harness 负责下游 task，但其 `wikitext` task split/tokenization 不能自动证明等于本仓库 `data.validation_split`，因此 L0 新增专用 validation loss/ppl evaluator；lm-eval 保留为可选扩展。
- 交付物：统一 eval 协议（供全 Q 系列复用）+ 既有 checkpoint 的评估数据 + prediction 策略的质量判断依据。
- 前置：无（既有 checkpoint 评估可立即做）；**对照性结论**受 [Q1](Q1-lr-confound.md) 约束——B2 前的 run 之间只能做"同批横评"，不能归因到策略机制。

## 2. 目标与完成谓词

1. **统一 eval 协议冻结并成文**（本计划 L0，全目录引用）：使用 run resolved config 的 `data.validation_split`、model tokenizer、与训练一致的 text→tokens+EOS→non-overlap block；HF causal-LM shift 后按有效 predicted token 加权 loss，`ppl=exp(loss)`；固定 batch/dtype，并记录 dataset revision/cache、checkpoint sha256 与完整 source identity（PVE-01）；
2. 既有终态 checkpoint 首批评估完成：F、H、rebase-preserve（v50/v49）及可取得的 replace 基线 run，数据入 `reports/`（PVE-02）；
3. 评估结果与 local loss 的分歧/一致性分析成文：若 validation 与 local loss 排序不一致，即为 Q1/Q3 混杂的直接证据，写入 run_analysis（PVE-03）；
4. B2+Q3 新基线建立后：prediction vs replace/rebase 的受控对照（同 commit、单变量、≥3 seeds、validation 指标）完成，形成 prediction 是否可作默认策略的判断（PVE-04）；
5. eval 自动接入：训练 job 完成后由提交器创建 `afterok` 依赖的独立 1 节点 eval job，避免 9 节点作业尾部空置八节点；结果原子写 `metrics/validation_eval.json` 并在 run 冻结后追加到 `control/summary.json`。job success 还必须验证非空 blocks/predicted tokens、有限 loss/ppl 与 checkpoint identity（PVE-05）。

## 3. 范围与非目标

- **范围内**：eval 协议、存量 checkpoint 评估、新基线后的受控对照、（视 SPECIFY 决策）eval 自动接入。
- **非目标**：prediction 数学的改进（预测公式、reconcile-moments 修正——若 PVE-04 显示质量损失，改进方案作为新研究计划立项）；lm-eval-harness 任务集扩展（loss/ppl 之外的下游任务，后续可选）。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 协议 | 专用 evaluator 正反例先 RED：shift/token weighting、空 split、非有限 loss、checkpoint/config mismatch | evaluator + 1-node PBS + 共享协议文档 | 一个存量 checkpoint 完整 validation 试跑；与同模型直接 HF loss 小样人工核对 |
| L1 存量评估 | 存量 checkpoint 清单与可用性核查（run 目录、GC 后遗留） | 无代码 | F/H/rebase-preserve/replace 评估数据 + PVE-03 分析入 reports |
| L2 自动接入 | 冻结 dependent-job 变量、结果 schema 与 summary 原子更新 | login 提交器：train job + `afterok` 1-node eval；不在 9-node job 内执行 eval | tiny/1-node 验证；依赖失败时不得产生伪成功 validation 字段（PVE-05） |
| L3 受控对照 | 等待 B2+Q3 新基线；实验设计冻结（P6 三件套） | 无代码 | 成对多 seed run + eval；PVE-04 判断成文 |

## 5. 测试矩阵与通过条件

| ID | 项目 | 通过条件 |
| --- | --- | --- |
| PVE-01 | 协议 | 协议文档存在且被试跑验证；参数完备可复现 |
| PVE-02 | 存量评估 | ≥4 个 checkpoint 的 validation loss/ppl 数据入 reports，含评估命令与 job 证据 |
| PVE-03 | 分歧分析 | validation 与 local loss 排序对比成文 |
| PVE-04 | 受控对照 | P6 三件套满足；prediction 质量判断有 validation 数据支撑 |
| PVE-05 | 自动链路 | train 成功才运行 eval；结果非空/有限/identity 匹配并原子进入 summary；失败保持缺失而非 null-success |

## 6. 验证阶梯

登录节点静态（PBS `bash -n`）→ 1 节点 eval job（存量评估即在此层完成）→ 9 节点仅 L3 对照 run（G7/G8 纪律）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/quality_fix-Q/Q4/`。评估 job 必须满足经验文档 §5（Exit_status=0 不算证据：核对输出非空、指标字段存在）。按 AGENTS.md 三连败升级。

## 8. 文档同步

eval 协议入 reports 共享文档并在本目录 INDEX 引用；run_analysis 的"仍需 validation"待办（出现 6 次）逐条标注闭合；review 报告 Q4/P7 条目标注 commit。
