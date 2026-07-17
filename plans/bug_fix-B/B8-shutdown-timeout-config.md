# B8：`wait_for_learner_shutdown` 超时可配置化

## 1. 元信息

- 来源：review B8（低）。syncer.py:1018-1021 硬编码 `timeout = max(30, min(120, 2×heartbeat_interval))`（上限 120s）。超时后 `all_learners_stopped=False` → `finalize_unconsumed_updates` 被跳过（syncer.py:1900-1904）→ 残留 pending 行与 payload（引用驱动 GC 正确地保留它们），终态目录不满足 BND-14 类断言。当前模型 ~20s 收尾未暴露；更大模型最后一次 adoption + 退出可能超过 120s。该函数在 G run 已修过一次（learners 表 stale），是已知薄弱路径。
- 性质：**配置化 + 可观测性增强**。默认值语义略变（上限从 120s 放宽，见 §2），正常 run 行为不变（learner 都在几十秒内确认）。
- 影响文件：`fs_diloco/runtime/syncer.py`、`fs_diloco/core/config.py`、测试。
- 前置依赖：无。

## 2. 规格

- 新配置字段：`liveness.learner_shutdown_timeout_seconds: float | None = None`。`None` → 公式默认 `max(120, 2×heartbeat_interval_seconds)`（保留下限、去掉 120s 上限——原上限正是隐患；30s 下限并入 120）；显式值 → 直接使用。字段归属 `liveness` 节（与 heartbeat/stale/dead 同域）。
- 超时分支增强（syncer.py:1032-1036 的 `learner_shutdown_timeout` 事件）：新增字段列出**未确认 learner 的 id 与各自最后心跳状态/时间**——这是事后诊断"谁没停"的最小必要证据，现事件只有计数。
- 超时后的既有安全行为**不变**：跳过 finalize（避免把可能仍活着的 learner 的 proposal 错误终态化）。终态目录不满足 BND-14 的情况保持可能，但现在有明确证据可查。
- review 建议中的"平均 cycle 时间×2 进公式"：SPECIFY 阶段评估 syncer 侧是否已有现成的 cycle 时长估计（`fastest_next_upload_eta_seconds` 用的 `local_cycle_step_time_seconds_mean` 元数据）；有则纳入公式 `max(120, 2×heartbeat, 2×估计cycle)`，无则不为此新增管道（保持计划小）。决策记入 progress。

## 3. 目标与完成谓词

1. 超时来源可配置，默认公式如 §2，硬编码 `min(120, ...)` 消失（SHT-01/04）；
2. 超时事件包含未确认 learner 明细（SHT-02）；
3. 正常 tiny run 轨迹与基线等价（超时未触发路径无变化，SHT-03）；
4. 全量 pytest 通过。

## 4. 范围与非目标

- **范围内**：超时计算、配置字段、事件增强。
- **非目标**：超时后策略变更（强制 finalize、二次等待等）——安全语义不动；B7（learner 侧 watchdog）独立；`sync_liveness_and_metadata` 本身的正确性（G run 已修，不重开）。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 决策 | 冻结公式与字段名；评估 cycle 估计可得性（§2） | 无实现 | 决策入 progress.md |
| L1 超时计算 | SHT-01 先 RED：显式配置值生效；None → 公式值；非法值（≤0）拒绝 | 字段 + 计算函数（纯函数，可单测） | 单元全绿 |
| L2 事件增强 | SHT-02 先 RED：构造一个 learner 永不确认的场景，断言事件含其 id 与最后心跳信息 | 超时分支实现 | 集成测试（假 run 目录或 tiny run + 人为拖住一个 learner）通过 |
| L3 无回归 | — | — | SHT-03 轨迹等价；全量 pytest；证据归档 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| SHT-01 | 超时来源 | 显式值直用；None → `max(120, 2×heartbeat[, 2×cycle估计])`；≤0 拒绝 |
| SHT-02 | 超时事件明细 | 事件含未确认 learner id 列表与最后心跳状态/时间戳 |
| SHT-03 | 正常路径无回归 | tiny run 轨迹与基线等价 |
| SHT-04 | 静态检查 | 原 `min(120.0, ...)` 硬编码不存在 |

progress.md 每条记录必须列出覆盖的 SHT ID（P8）。

## 7. 验证阶梯

1. **登录节点**：SHT-04 grep、lint。
2. **1 节点 compute**：单元 → 全量 pytest → SHT-02 集成 → tiny run 轨迹等价。
3. 2/9 节点：不需要。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B8/`。若 Checker/analysis 读取 `learner_shutdown_timeout` 事件，确认新字段向后兼容（追加字段，不改既有字段名）。

## 9. 停止与升级规则

按 AGENTS.md。此计划体量小，若 SHT-02 集成场景难以稳定构造（learner 拖住的时序脆弱），允许改用假 run 目录 + 直接调用 `wait_for_learner_shutdown` 的组件级测试，并在 progress 记录理由——不得为此引入 sleep 竞速型测试。

## 10. 文档同步

docs 配置参考新增字段说明（含默认公式与"更大模型需要显式调大"的提示）；review 报告 B8 条目标注 commit。
