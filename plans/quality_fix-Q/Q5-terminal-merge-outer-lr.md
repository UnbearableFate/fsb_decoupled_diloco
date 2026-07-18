# Q5：尾部小 quorum merge 的噪声评估与可选 outer lr 缩放

## 1. 元信息

- 来源：review Q5（低）。terminal drain 允许 selected=2/3 的尾部 merge（B run 尾部 7、5、2），outer step 对 2 个 learner 的平均照常走 lr=0.7+momentum 全步长；对最终 checkpoint 的影响从未评估。plan 01 §3.4 本就允许把缩放作为显式实验策略记录。
- 性质：**评估优先，改动可选**——先测影响，数据支持时才引入 `selected/quorum_max` 缩放（默认关闭的实验策略）。
- 影响文件：`fs_diloco/runtime/syncer.py`、配置与 Q4 validation evaluator（TMN-01
  证据捕获/前驱解析）；若条件触发，另含可选 outer-LR scaling 路径。
- 前置：[Q4](Q4-prediction-validation-eval.md) L0 的 eval 协议；评估对象可用既有 run 的归档/终态 checkpoint。

## 2. 目标与完成谓词

1. 影响评估完成：新增默认关闭的研究证据开关 `sync.capture_terminal_predecessor_for_eval`；仅在 input-closed 且 selected<quorum_min 的 terminal merge 前，把当前 weight 以 hardlink（失败时 copy）冻结到 run 内 `eval_checkpoints/`，记录 source version/checksum/selected/quorum。它不是第二权威，不参与 resume/latest/GC（TMN-01）；
2. 至少 3 seeds 的 pre/post checkpoint 使用 Q4 同协议评估。决策阈值预先冻结为：paired post-pre validation loss 的均值或任一 seed 劣化超过 Q6 新 FP32 baseline noise ε 才视为“影响可见”；否则记录负结论并完成（TMN-02）；
3. （可选阶段）`sync.terminal_merge_outer_lr_scaling: bool = false`：终态 drain 期 merge 的 outer lr 乘 `selected/quorum_max`；语义测试 + 对照 run（TMN-03/04）。

正式 TMN-01 工作负载必须让 learner 输入关闭先于全局 stop target：使用 `training.completion_mode=local_or_global`，并移除或设为确定不可达的 global target。每个 seed 只有同时出现 `terminal_input_closed`、`terminal_drain_selected(selected_count<quorum_min)` 和 capture manifest 才有效；先到 `stop_after_outer_steps` 的 run 明确排除。原计划未冻结这一触发条件，导致首批三个 run 均在 v50 提前停止而没有可评估前驱，现已修正。

## 3. 范围与非目标

- **范围内**：评估、决策、可选缩放开关。
- **非目标**：正常（非 terminal）merge 的 outer lr 策略；quorum 语义改动；drain 规则改动（B4/S3 的领域）。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 评估设计 | 证明旧 run 前版本已被 current-only GC；冻结证据快照目录/manifest/hardlink fallback 与 ε 判据 | 默认关闭的 capture helper + GC 非权威性测试 | 开关关闭零额外文件；开启仅 terminal partial 前捕获一次/多次 manifest |
| L1 影响评估 | 先做触发预检：input-closed 必须先于 global stop，且 terminal selected<quorum_min；未触发的 run 排除而非当作“无影响” | 无代码 | 3 个有效 seed 的 TMN-01 数据 + TMN-02 决策入 reports |
| L2（条件触发）缩放实现 | 缩放语义单元测试先 RED（仅 drain 期、仅开关开启时生效） | 开关 + 缩放 | 全量 pytest；tiny run 轨迹等价（开关关闭时） |
| L3（条件触发）对照 | P6 三件套设计 | 无代码 | 开/关成对 run + validation 对比（TMN-04） |

## 5. 测试矩阵与通过条件

| ID | 项目 | 通过条件 |
| --- | --- | --- |
| TMN-01 | 捕获/评估 | 3-seed partial merge pre/post validation 数据齐备；manifest/checksum/selected/quorum 可复算；capture 不改变 DB/latest |
| TMN-02 | 决策 | 继续/停止的判断有数据支撑并成文 |
| TMN-03（条件） | 缩放语义 | 仅 TMN-02 触发时实施；仅 drain 期且开关开启时缩放，关闭时逐字节旧行为；未触发则按计划 N/A |
| TMN-04（条件） | 对照 | 仅 TMN-02 触发时实施；成对 run validation 对比入 run_analysis；未触发则按计划 N/A |

## 6. 验证阶梯

登录节点静态 → 1 节点（eval job、单元测试、tiny run）→ 9 节点仅 L3 对照（若触发）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/quality_fix-Q/Q5/`。低优先级计划：若 L1 得出"影响可忽略"，立即完结，不进入 L2——不为低收益改动增加协议表面积。

## 8. 文档同步

评估结论入 run_analysis；若缩放落地，docs 的 terminal drain 章节与 plan 01 §3.4 注记该显式策略。
