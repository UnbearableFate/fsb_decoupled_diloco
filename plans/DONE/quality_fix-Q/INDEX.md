# quality_fix-Q 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 6 节（系统设计审查——训练准确率，Q1–Q6）中的每个要点提供一份独立计划。本目录的共同底座是两条纪律：**任何质量结论必须以 validation 指标为准**（review P7；local loss 受 Q3 记忆化污染，不再作为主要证据），以及**实验受控三件套**（P6：同 commit、单变量、≥3 seeds）。

## 审计修正后的共享门禁

正式质量对照除同 commit/单变量/≥3 seeds 外，还必须记录 `git_commit`、`git_dirty` 与 `source_fingerprint`；当前未提交工作树不得只靠 HEAD 冒充可复现实验版本。未获提交授权时，同组 fingerprint 完全相同并归档 source manifest/diff 是允许的 fail-closed 证据。

Q4 的统一指标不能把 lm-eval `wikitext` 任务默认口径直接等同于仓库配置的 `data.validation_split`。L0 必须交付专用 validation loss/ppl evaluator，明确 tokenizer、EOS、block、有效 token 加权与 split；`lm-eval-harness` 下游任务保留为可选扩展。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置/关联 |
| --- | --- | --- | --- | --- |
| 1 | [Q1-lr-confound.md](Q1-lr-confound.md) | LR 调度混杂因子 | **委托给 [bug_fix-B/B2](../bug_fix-B/B2-scheduler-decoupling.md) 执行**；质量侧验收追踪 | 阻塞 Q4/Q5 及一切策略消融 |
| 2 | [Q3-data-shuffle.md](Q3-data-shuffle.md) | 数据分片 epoch 级 shuffle，缓解记忆化 | 语义变更 | 与 B2 同批合入最省（同为"作废旧基线"的变更） |
| 3 | [Q4-prediction-validation-eval.md](Q4-prediction-validation-eval.md) L0/L2 | 冻结专用 validation 协议并自动接入独立 1 节点 eval job | 评估基础设施 | 先于所有质量门禁/消融 |
| 4 | [Q6-bf16-publish-quality-guard.md](Q6-bf16-publish-quality-guard.md) | BF16 publish 的质量门禁协议 | 协议 + E1 telemetry/实验 | 依赖 Q4-L0；被 E1 引用 |
| 5 | [Q4-prediction-validation-eval.md](Q4-prediction-validation-eval.md) L1/L3 | 存量评估与新基线策略对照 | 评估实验 | 依赖 Q3/R0 |
| 6 | [Q2-staleness-weighting-evidence.md](Q2-staleness-weighting-evidence.md) | staleness 加权：证据管道 + λ/策略消融 | telemetry + 实验 | 消融 run 在 Q3/Q4 后 |
| 7 | [Q5-terminal-merge-outer-lr.md](Q5-terminal-merge-outer-lr.md) | 尾部小 quorum merge 的噪声评估与可选缩放 | 评估 + 条件实验策略 | 依赖 Q4；需显式捕获 pre-terminal checkpoint |

顺序依据：Q1（=B2）已在当前工作树完成，Q3 是剩余基线作废型变更；随后先建立 Q4 evaluator，再冻结 Q6。存量 checkpoint 可在此时评估，但新策略/λ/terminal 对照必须等待 Q3 后的同 fingerprint 多 seed 基线。**在 Q3 实现、R0 身份落地且 Q4 协议冻结前，不启动新的质量对照 run。**

## 统一约束

- 记录规则以 [plans/AGENTS.md](../AGENTS.md) 为准；报告路径映射：`plans/quality_fix-Q/<Qx>-*.md → reports/imp_plans/quality_fix-Q/<Qx>/`。
- 质量结论门槛：validation loss/ppl（P7 管道）+ ≥3 seeds + 同 commit 单变量；不满足三者的数据只能写"观察"，不得写"结论"（对齐 run_analysis 现有的谨慎口径并制度化）。
- 评估协议统一：固定 validation split、tokenizer、block size、batch 设置，写入一份共享的 eval 协议说明（Q4 的 L0 交付，其余计划引用），避免各计划口径漂移。
- 9 节点 run 仅用于对照实验本身；正确性验证止于 1 节点。

## 2026-07-18 实施状态

Q1 → Q3 → Q4-L0/L2 → Q6 → Q4-L1/L3 → Q2 → Q5 已按共享门禁顺序完成。
Q5 的三 seed post-pre degradation 未跨越 ε=.01，因此 TMN-03/04 按计划为 N/A，
没有增加 outer-LR scaling 开关。Q1–Q6 的全部无条件完成谓词、计划修正和正式证据见
[`reports/imp_plans/20260718_perf-E_quality-Q_completion-audit.md`](../../reports/imp_plans/20260718_perf-E_quality-Q_completion-audit.md)。
