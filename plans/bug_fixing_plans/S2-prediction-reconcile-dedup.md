# S2：prediction reconcile 去重 + 事件轨迹等价工具

## 1. 元信息

- 来源：review S2（中）。`fs_diloco/runtime/learner.py` 中同一段 prediction reconcile 逻辑存在两份拷贝：inner-step poll 内（1556-1589）与 cycle 末等待（1657-1716），差异仅为后者多一个 `reconcile_waited_seconds` 事件字段。
- 性质：**行为保持重构**。附带交付本目录全部计划共享的基础设施：事件轨迹等价工具。
- 影响文件：`fs_diloco/runtime/learner.py`；新增 `fs_diloco/tools/compare_event_traces.py` 与其测试。
- 前置依赖：无。后续 S1 将把本计划的 helper 吸收进 prediction 策略类，接口设计需为此留出余地（闭包无关、状态经参数传入传出）。

## 2. 目标与完成谓词

全部满足才可声明完成：

1. reconcile 核心逻辑（调用 `rebase_local_delta_onto_global` → 发出 `global_prediction_reconciled` → 清空 prediction 状态）在 learner.py 中只存在**一份**；两个调用点只保留各自差异（是否传入 waited 秒数、随后的 adoption 尾部事件归属不变）。
2. 事件轨迹等价工具 `fs_diloco/tools/compare_event_traces.py` 交付并自测通过，非法输入/配置以 exit 2 区分于“不等价”的 exit 1（REC-01）。
3. tiny prediction run 的归一化事件轨迹与基线 commit 等价（REC-04）。
4. 测试矩阵 REC-01–REC-06 全部通过；全量 pytest 通过。

## 3. 范围与非目标

- **范围内**：提取 reconcile helper；定义并封装 prediction 状态四元组；交付轨迹等价工具。
- **非目标**：
  - B6（GC 竞态有界重试推广到 adopt/rebase 加载路径）——helper 内部保留单一 payload 加载入口作为将来插入重试的缝，但本轮不实现重试；
  - S1 的策略接口化——本轮只做函数级去重，不引入类层次；
  - prediction 数学语义（预测公式、momentum 代理）的任何改动。

## 4. 规格

### 4.1 事件轨迹等价工具（先行交付，供 S1/S3/S5 复用）

- 输入：两个 run 目录（或日志目录），角色过滤（learner/syncer）。
- 归一化：从 JSONL 事件流提取有序 `(actor, event_name, 稳定字段子集)` 序列。默认丢弃：时间戳、耗时/秒数类字段、速率、主机名、PID、绝对路径、run ID、update_id 中的运行时间前缀。保留：事件名、版本号、step、token 计数、reason、布尔标志。字段取舍和可忽略的观测事件由**显式、可版本化的 profile** 配置；对比输出必须打印 profile 名称或路径。
- 输出：等价 → exit 0；不等价 → exit 1 并打印首个分歧位置的两侧上下文；输入目录、JSONL 或 profile 非法 → exit 2，不得误报成行为差异。
- 自测：同一轨迹对比等价；注入乱序/字段改动/事件缺失后对比必须报告不等价。
- 局限声明：多进程 tiny run 中 learner 间相对顺序受调度影响，对比按**每 actor 单独序列**进行，不断言跨 actor 交错顺序。同一 actor 的 liveness ingestion、quorum wait 和 terminal-drain/grace 分支也可能因合法调度而变化；任何用于硬门禁的 profile 必须先在同代码、同配置的重复 run 上证明投影可重复。不可重复的整段轨迹只作诊断，不能据此拒绝重构。

### 4.2 reconcile helper

- 形态：模块级、闭包无关的显式状态函数。prediction 状态（`reference_flat`、`carried_tokens`、`update_id`、`base_version` 四元组）以一个轻量 dataclass 进出；函数会按现语义修改模型并发事件，因此不得误称为无副作用纯函数——这是 S1 迁移的直接前提。
- 职责边界（保证事件顺序逐字节不变）：
  - helper 内：`rebase_local_delta_onto_global` 调用、`global_prediction_reconciled` 事件（`reconcile_waited_seconds` 仅在调用方传入时携带）、状态清空；
  - helper 外（调用点保留）：inner-poll 点的共享 adoption 尾部（learner.py:1630-1647 的 `global_adopted` + preserved/reset 分支）与 cycle 末点自带的尾部事件（1707-1716）；`global_prediction_reconcile_wait_started`、abandon-on-stop（1649-1656）与超时 `TimeoutError`（1671-1674）均留在 cycle 末调用点。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 轨迹工具 | 写自测：等价样例、乱序反例、字段漂移反例、非法输入反例先 RED | 实现 `compare_event_traces.py` 与 profile 机制 | REC-01 GREEN；用真实 tiny run 日志作冒烟输入；工具用法写入 progress.md |
| L1 基线冻结 | 记录基线 commit + dirty 状态；确认 launcher 的实际 learner 数与配置一致；同代码重复跑一次以验证所选 profile 可重复 | 在基线上以 **1 learner** 跑 tiny prediction run（`configs/fs_diloco_tiny_predict_local.yaml`，固定 seed） | profile、重复性输出、归一化轨迹与原始日志存入 artifacts，作为 L2 的对照物 |
| L2 提取 helper | REC-02/REC-03 单元测试先行：状态四元组清空、两调用点事件字段集合差异仅 `reconcile_waited_seconds` | 提取 helper，两个调用点改写 | REC-02–REC-05 GREEN；重跑 tiny prediction run，REC-04 轨迹等价；全量 pytest |
| L3 边界加固 | 超时路径与 abandon 路径反例先行 | 如 L2 已覆盖则无实现 | REC-05/REC-06 GREEN；证据与未覆盖风险写入 progress.md |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| REC-01 | 轨迹工具自测 | 等价样例 exit 0；乱序/字段漂移/缺事件反例 exit 1 且指出首个分歧；非法输入/profile exit 2 |
| REC-02 | helper 单元 | reconcile 后四元组全部清空；返回新 global version；事件字段完整 |
| REC-03 | 双调用点字段集合 | 两点发出的 `global_prediction_reconciled` 字段集合之差恰为 `{reconcile_waited_seconds}` |
| REC-04 | 轨迹等价 | tiny prediction run（同 seed 同配置）重构前后归一化轨迹等价 |
| REC-05 | 超时路径 | 等待无新 latest 时仍抛 `TimeoutError`，且状态未被部分清空 |
| REC-06 | abandon 路径 | stop.json 存在时发出 `global_prediction_abandoned_on_stop`，不进入 reconcile |

progress.md 每条记录必须列出覆盖的 REC ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、`git diff --check`。
2. **1 节点 compute**：REC 单元测试 → 全量 pytest → tiny prediction run 轨迹等价（REC-04）。
3. 不需要 2 节点与 9 节点：改动不触及磁盘协议、DB 与 syncer。

## 8. 报告、证据与 Checker

- 报告目录：`reports/imp_plans/bug_fixing/S2/`，规则按 [plans/AGENTS.md](../AGENTS.md)。
- artifacts 必须含：基线与重构后两次 tiny run 的 commit、配置、归一化轨迹文件、对比工具输出。
- 无需独立 Checker；REC-04 的轨迹等价即本计划的核心验收证据。

## 9. 停止与升级规则

按 AGENTS.md：同一 REC ID 连续失败三次升级 code_review.md。轨迹对比因**非确定性字段**反复失败时，修正做法是把该字段纳入 profile 的丢弃清单并记录理由，不是放宽整体等价判定。

## 10. 文档同步

- 轨迹工具的用法（一段即可）写入工具模块 docstring；本目录 INDEX.md 已声明其共享地位，无需另立文档；
- 不触及 docs/ 与研究计划。
