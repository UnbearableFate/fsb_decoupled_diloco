# E6：adoption 停顿计量

## 1. 元信息

- 来源：review E6（低）。replace 模式每次 adoption：读 498MB checkpoint → 逐参数 copy → optimizer/scheduler 重建（learner.py:435-444、1896），约每 cycle 一次、亚秒到秒级，8 learner 累计可观但非主导。
- 性质：**纯 telemetry**。改进本身委托他处：读文件减半归 [E1](E1-publish-path-io.md)（BF16 publish）；重建消除归 preserve 类策略与 [B2](../bug_fix-B/B2-scheduler-decoupling.md)（scheduler 恢复语义）。本计划只负责让这项成本**可见、可归因**——review P5 教训的最小应用。
- 影响文件：`fs_diloco/runtime/learner.py` 事件字段、分析脚本。
- 前置：无。若 S1（策略重构）已完成，计量点在统一 adoption 收尾处，实现更简。

## 2. 目标与完成谓词

1. adoption 相关事件（`global_adopted` 及 fragment 对应事件）携带 `adoption_pause_seconds`（含读文件、copy、重建三段或至少总时长；分段粒度 SPECIFY 冻结）（APT-01）；
2. 分析侧可汇总：per-learner 每 cycle 平均停顿、占 cycle 比例进入 run 分析输出（APT-02）；
3. 基线数据留档：一次 tiny run + 引用最近 9 节点 run 估算的对照行，写入报告（APT-03）；
4. 正常路径无回归：tiny run 轨迹与基线等价（新字段在 profile 声明）。

## 3. 范围与非目标

- **范围内**：计量、分析汇总、基线留档。
- **非目标**：任何 adoption 性能改动（归 E1/B2/策略计划）；预取/异步 adoption 之类新机制（若数据显示停顿占比显著，作为 follow-up 提案写入报告，由 00 矩阵裁决）。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L1 字段 | 分段与字段名冻结；APT-01 单元先 RED | 计时与事件字段 | tiny run 字段可见且数值合理 |
| L2 汇总 | 汇总口径冻结 | 分析脚本加列 | APT-02/03；轨迹等价；全量 pytest |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| APT-01 | 字段 | adoption 事件含停顿时长，量级与文件大小/带宽自洽 |
| APT-02 | 汇总 | 分析输出含 per-learner 平均停顿与 cycle 占比 |
| APT-03 | 基线 | 基线数据入报告，供 E1/B2 完成后对照 |

## 6. 验证阶梯

登录节点静态 → 1 节点 pytest + tiny run。无需多节点。

## 7. 报告与文档同步

报告目录 `reports/imp_plans/perf_fix-E/E6/`。metrics 字段表补新字段；review 报告 E6 条目标注 commit。
