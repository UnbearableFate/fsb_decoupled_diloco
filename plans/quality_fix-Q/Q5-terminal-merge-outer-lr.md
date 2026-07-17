# Q5：尾部小 quorum merge 的噪声评估与可选 outer lr 缩放

## 1. 元信息

- 来源：review Q5（低）。terminal drain 允许 selected=2/3 的尾部 merge（B run 尾部 7、5、2），outer step 对 2 个 learner 的平均照常走 lr=0.7+momentum 全步长；对最终 checkpoint 的影响从未评估。plan 01 §3.4 本就允许把缩放作为显式实验策略记录。
- 性质：**评估优先，改动可选**——先测影响，数据支持时才引入 `selected/quorum_max` 缩放（默认关闭的实验策略）。
- 影响文件：（可选阶段）`fs_diloco/runtime/syncer.py`、配置；评估阶段零代码。
- 前置：[Q4](Q4-prediction-validation-eval.md) L0 的 eval 协议；评估对象可用既有 run 的归档/终态 checkpoint。

## 2. 目标与完成谓词

1. 影响评估完成：对至少一个含小 quorum 尾部 merge 的 run，比较"最后一次 partial merge 前后" checkpoint 的 validation 指标（前一版本 checkpoint 若已被 current-only GC 删除，则用新 run 在停止前显式保留一份——评估设计在 SPECIFY 冻结）（TMN-01）；
2. 决策成文：影响可忽略 → 记录负结论、计划完成；影响可见 → 进入可选阶段（TMN-02）；
3. （可选阶段）`sync.terminal_merge_outer_lr_scaling: bool = false`：终态 drain 期 merge 的 outer lr 乘 `selected/quorum_max`；语义测试 + 对照 run（TMN-03/04）。

## 3. 范围与非目标

- **范围内**：评估、决策、可选缩放开关。
- **非目标**：正常（非 terminal）merge 的 outer lr 策略；quorum 语义改动；drain 规则改动（B4/S3 的领域）。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 评估设计 | checkpoint 可得性核查；对照口径冻结（Q4 协议） | 无代码 | 设计入 progress.md |
| L1 影响评估 | — | 无代码 | TMN-01 数据 + TMN-02 决策入 reports |
| L2（条件触发）缩放实现 | 缩放语义单元测试先 RED（仅 drain 期、仅开关开启时生效） | 开关 + 缩放 | 全量 pytest；tiny run 轨迹等价（开关关闭时） |
| L3（条件触发）对照 | P6 三件套设计 | 无代码 | 开/关成对 run + validation 对比（TMN-04） |

## 5. 测试矩阵与通过条件

| ID | 项目 | 通过条件 |
| --- | --- | --- |
| TMN-01 | 影响评估 | partial merge 前后 validation 对比数据齐备 |
| TMN-02 | 决策 | 继续/停止的判断有数据支撑并成文 |
| TMN-03 | 缩放语义 | 仅 drain 期且开关开启时缩放；关闭时逐字节旧行为 |
| TMN-04 | 对照 | 成对 run validation 对比入 run_analysis |

## 6. 验证阶梯

登录节点静态 → 1 节点（eval job、单元测试、tiny run）→ 9 节点仅 L3 对照（若触发）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/quality_fix-Q/Q5/`。低优先级计划：若 L1 得出"影响可忽略"，立即完结，不进入 L2——不为低收益改动增加协议表面积。

## 8. 文档同步

评估结论入 run_analysis；若缩放落地，docs 的 terminal drain 章节与 plan 01 §3.4 注记该显式策略。
