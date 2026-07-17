# quality_fix-Q 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 6 节（系统设计审查——训练准确率，Q1–Q6）中的每个要点提供一份独立计划。本目录的共同底座是两条纪律：**任何质量结论必须以 validation 指标为准**（review P7；local loss 受 Q3 记忆化污染，不再作为主要证据），以及**实验受控三件套**（P6：同 commit、单变量、≥3 seeds）。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置/关联 |
| --- | --- | --- | --- | --- |
| 1 | [Q1-lr-confound.md](Q1-lr-confound.md) | LR 调度混杂因子 | **委托给 [bug_fix-B/B2](../bug_fix-B/B2-scheduler-decoupling.md) 执行**；质量侧验收追踪 | 阻塞 Q4/Q5 及一切策略消融 |
| 2 | [Q3-data-shuffle.md](Q3-data-shuffle.md) | 数据分片 epoch 级 shuffle，缓解记忆化 | 语义变更 | 与 B2 同批合入最省（同为"作废旧基线"的变更） |
| 3 | [Q6-bf16-publish-quality-guard.md](Q6-bf16-publish-quality-guard.md) | BF16 publish 的质量门禁协议 | 实验协议（无代码） | 被 [perf_fix-E/E1](../perf_fix-E/E1-publish-path-io.md) 引用 |
| 4 | [Q4-prediction-validation-eval.md](Q4-prediction-validation-eval.md) | 用 validation eval 检验 prediction 启发式 | 评估实验 | 依赖 P7 eval 接入（工具已存在）；结论解释受 Q1 约束 |
| 5 | [Q2-staleness-weighting-evidence.md](Q2-staleness-weighting-evidence.md) | staleness 加权：证据管道 + λ 消融 | telemetry + 实验 | 消融 run 应在 B2/Q3 之后 |
| 6 | [Q5-terminal-merge-outer-lr.md](Q5-terminal-merge-outer-lr.md) | 尾部小 quorum merge 的噪声评估与可选缩放 | 评估 + 可选实验策略 | 依赖 P7 eval |

顺序依据：Q1（=B2）与 Q3 是**基线作废型变更**，必须最先、且尽量同批合入——每次此类变更都作废历史对照，攒在一起只付一次"重建基线"成本；Q6 是 E1 实验的门禁，无代码、随时可交付；Q4/Q2/Q5 是消费新基线的评估与消融，排在其后。**在 B2+Q3 合入并跑出新基线前，不启动任何 Q4/Q2/Q5 的对照 run。**

## 统一约束

- 记录规则以 [plans/AGENTS.md](../AGENTS.md) 为准；报告路径映射：`plans/quality_fix-Q/<Qx>-*.md → reports/imp_plans/quality_fix-Q/<Qx>/`。
- 质量结论门槛：validation loss/ppl（P7 管道）+ ≥3 seeds + 同 commit 单变量；不满足三者的数据只能写"观察"，不得写"结论"（对齐 run_analysis 现有的谨慎口径并制度化）。
- 评估协议统一：固定 validation split、tokenizer、block size、batch 设置，写入一份共享的 eval 协议说明（Q4 的 L0 交付，其余计划引用），避免各计划口径漂移。
- 9 节点 run 仅用于对照实验本身；正确性验证止于 1 节点。
