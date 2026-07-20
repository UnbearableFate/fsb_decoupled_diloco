# E3：fragment 物化策略——危险缺省修正与成本剥离

## 1. 元信息

- 来源：review E3（中）。`should_materialize_fragment_full`（syncer.py:275-284）：`materialize_full_every_events=None 或 ≤0` → **每个事件都物化**完整 498MB FP32 checkpoint——"不配置"是最贵行为，违反直觉。50×10 fragment 配置显式 `=1`：每次本应只写 63MB 的 fragment merge 附带 498MB 全模型物化，足以解释 fragment 比 full 慢 32% 的相当部分，且使"fragment 减少 I/O"的对照失真。物化 I/O 与协调等待（00 §4.4 认定的主因）叠加，需分开计量。
- 性质：配置语义修正（**不兼容变更**：缺省行为改变）+ 受控消融实验。
- 影响文件：`fs_diloco/core/config.py`、`fs_diloco/runtime/syncer.py`、fragment 配置、metrics。
- 前置：无。

## 2. 设计决策

- 沿用仓库 fail-closed 先例：`fragments.enabled=true` 时 `materialize_full_every_events` **必填正整数**，`None/≤0` 拒绝并在错误信息中说明旧缺省语义已移除（比报告给的"缺省=10"更不易误用：任何数值都应是有意选择）。resume 语义（若允许全模型 checkpoint 缺失时冷启）在 SPECIFY 阶段确认——若某些恢复路径依赖"最近物化不早于 N 事件"，校验需给出下界提示而不是任意值均可。
- telemetry：沿用已有 `materialize_full_seconds`，新增 `materialized_bytes`/`materialized_this_event`，与 fragment 发布耗时分列（P5，为消融提供口径）。
- 审计补充：事件 0、配置周期和达到显式 outer target 时物化仍不足以保证 `input_exhausted`/其他终止原因的 latest materialized checkpoint 对应最终 fragment state。所有正常终止路径必须强制一次最终物化；否则 Q4/Q5 eval 会评估旧模型。

## 3. 目标与完成谓词

1. 缺省语义修正：fragment 模式缺该字段或 ≤0 → 配置拒绝（MAT-01）；`should_materialize_fragment_full` 不再包含"None→每事件"分支（MAT-02 静态检查）；
2. telemetry 使用现有命名 `materialize_full_seconds`，新增 `materialized_bytes`/`materialized_this_event`，并在 tiny fragment run 可见（MAT-03）；
3. 消融完成：`=1` vs `=10`（其余配置同 commit 单变量，P6）成对 fragment run，把物化 I/O 从协调等待中剥离的定量结论写入 run_analysis（MAT-04）；
4. in-repo fragment 配置全部显式赋值；全量 pytest 通过。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 语义盘点 | 确认物化 checkpoint 的全部消费方（resume、learner startup、analysis/eval、Checker）与所有 stop reason | 无实现 | 周期任意正整数；初始化与正常终止强制物化，resume 依赖 fragment checkpoint 而非 materialized full |
| L1 校验修正 | MAT-01 先 RED（当前 None 被接受且最贵） | 必填正整数校验；分支移除；in-repo 配置补值 | MAT-02 静态检查；全量 pytest |
| L2 telemetry | 字段冻结 | 物化计时/字节/是否发生上报；终止调用显式 `force_materialize=true` | tiny fragment run 与 input_exhausted 反例中 final materialized 权重对应最终 event（MAT-03） |
| L3 消融 | 成对实验设计冻结（resolved config diff 审查） | 无代码 | `=1` vs `=10` 成对 run；剥离结论 + commit 入 run_analysis（MAT-04） |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| MAT-01 | 校验 | fragment 模式缺字段/None/0/负数 → 拒绝，错误信息含理由与下界 |
| MAT-02 | 静态 | "未配置→每事件物化"分支不存在 |
| MAT-03 | telemetry/终态 | 物化耗时/字节/布尔值与 fragment 发布耗时分列；任意正常 stop 后 final materialized checkpoint 对应最新 event |
| MAT-04 | 消融 | 成对 run 单变量核查通过；物化 I/O 占比结论有数据支撑 |

## 6. 验证阶梯

1. 登录节点：MAT-02 grep、lint。
2. 1 节点：pytest → tiny fragment run。
3. 9 节点：仅 L3 消融（G7/G8 纪律）。注意与 B4（fragment terminal drain）的时序：若 B4 未修，消融 run 的完整时间数据仍被尾部空等污染——建议 B4 先行，或消融结论只使用 interval 级指标并注明。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/perf_fix-E/E3/`。按 AGENTS.md 三连败升级。

## 8. 文档同步

docs fragment 章节：物化字段必填语义、选值指引（物化间隔 × 事件频率 = 全模型 checkpoint 新鲜度）；review 报告 E3 条目标注 commit。
