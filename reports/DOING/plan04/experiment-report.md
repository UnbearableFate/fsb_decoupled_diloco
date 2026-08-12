# plan04 Full Protocol 实验报告

## 结论

Plan04 的两种 baseline 和七项 Full Protocol 实验均已完成。每项 Full Protocol 实验只使用三个通过条件：最终接管训练的 Syncer 正常退出、训练达到 25 个 global step、最终平均 loss < 3.5。七项实验均满足三个条件。

实验统一使用固定版本的 GPT-2 和 WikiText-2。Full Protocol 使用 200 个 local step × 25 个 global step、8 个独立 Learner job 和 merge 阈值 4。DDP baseline 使用 5,000 step；periodic-average baseline 使用 200 × 25 step。

## 结果

| 实验 | Supervisor | Final Syncer | Final loss | Training time | 状态 |
|---|---:|---:|---:|---:|---|
| Normal | `2540861.opbs` | `2540864.opbs` | `3.196270` | `605.51s` | PASS |
| Stagger 4+4 | `2541158.opbs` | `2541161.opbs` | `3.197345` | `617.38s` | PASS |
| Stagger 3+3+2 | `2541231.opbs` | `2541236.opbs` | `3.167654` | `647.61s` | PASS |
| Learner fault，simultaneous | `2541648.opbs` | `2541649.opbs` | `3.288647` | `1008.25s` | PASS |
| Learner fault，4+4 | `2541824.opbs` | `2541825.opbs` | `3.305553` | `909.07s` | PASS |
| Syncer failure | `2541871.opbs` | `2541896.opbs` | `3.243430` | `934.19s` | PASS |
| Dual Syncer | `2541938.opbs` | `2541950.opbs` | `3.245163` | `982.39s` | PASS |

所有最终 Syncer 的 PBS `Exit_status` 均为 `0`，所有 terminal authority 的 `final_version` 均为 `25`。每个完成的 Full Protocol run 只保留 1 个 version 25 model weight 和 1 个对应 outer optimizer state。

## 场景观察

- Stagger 4+4 的两批首次提交 offset 为 `0.07s` 和 `30.00s`。
- Stagger 3+3+2 的三批首次提交 offset 为 `0.07s`、`32.02s` 和 `60.00s`。只有前三个 Learner 时，authority 尚未发布 global version。
- 两项 Learner fault 实验均在约 60 秒删除 1 个已 admission 的 bootstrap Learner。Capacity service 随后分别 admission replacement `2541665` 和 `2541839`。
- Syncer failure 在约 60 秒删除 primary `2541872.opbs`，约 30 秒后提交 successor `2541896.opbs`。Successor 成为 epoch 2，并正常完成训练。
- Dual Syncer 在约 30 秒提交 candidate `2541950.opbs`。Candidate 运行时 authority 仍只有 primary epoch；重叠约 30 秒后删除 primary。Candidate 随后成为 epoch 2，并正常完成训练。

## Baseline

| 模式 | PBS | Final loss | Training time |
|---|---:|---:|---:|
| DDP，5,000 step | `2540694.opbs` | `2.935435` | `1127.79s` |
| Periodic average，200 × 25 step | `2540695.opbs` | `2.998504` | `1040.56s` |

Normal 的最终 loss 相对 DDP 和 periodic-average baseline 分别增加 `8.89%` 和 `6.60%`。训练时间分别减少 `46.31%` 和 `41.81%`。这些比较只用于诊断，不影响 PASS/FAIL。

## Source lineage

根据用户要求，queue、wall-time 和实验判定工具的修改不废弃已完成的训练结果。只有训练或协议功能逻辑变化才需要重跑受影响实验。

- `7145197f3209fa67727bb0d458d0db38a81eb86d`：baseline、Normal、Stagger 4+4 和 Stagger 3+3+2。
- `a064ef8837ca033db883fe60879c4578b921b09c`：Learner simultaneous fault。与上一 target 相比，只修改 queue、wall-time 和诊断 oracle。
- `319081c8d57411a3e1b8ee724c82f810f25a3228`：Learner staggered fault、Syncer failure 和 Dual Syncer。与上一 target 相比，只修改实验判定工具、测试和文档。

最新 source target 在 Miyabi compute job `2541809.opbs` 上通过 Ruff、focused pytest `271 passed`、完整 pytest `599 passed` 和 website lint/test。Validation artifact 为 `artifacts/validation_319081c.json`。

## 诊断说明

旧实验 artifact 中的 replacement succession、terminal fence、capacity launch 和 baseline comparison 检查继续作为诊断信息保留。它们不覆盖三个终态通过条件。所有有效失败、基础设施无效 run 和根因处理记录在 `failures.md`。

## 证据索引

- 正式 gate、source lineage 与通过公式：`formal_manifest.json`
- Requirement 验收：`requirements.csv`
- 实施里程碑：`progress.md`
- 失败与无效实验：`failures.md`
- 结构化实验和 validation 证据：`artifacts/`
- 统一指标：`runs/summary.csv`
