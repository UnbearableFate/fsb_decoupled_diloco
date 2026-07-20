# B10：mid-cycle adoption 的 proposal 元数据标注

## 1. 元信息

- 来源：review B10（低）。replace + `poll_latest_during_inner_steps` 下，inner step 中途 adoption 把 `base_global_version` 更新为新版本（learner.py:1630），但该 cycle 前半段在旧 base 上训练，`tokens_this_update` 仍统计整个 interval。proposal 是绝对参数快照，数值无害；失真的是 staleness 加权（按新 base 计算，实际混合 base）与 `tokens_since_global_load` 的解释。当前正式配置未使用该组合，风险为零——本计划是防未来组合的**可解释性**修复，非行为修复。
- 性质：**元数据新增**（proposal meta 与事件补充字段），merge 数学与 staleness 加权**不变**。
- 影响文件：`fs_diloco/runtime/learner.py`、（视 SPECIFY 结论）`fs_diloco/storage/sqlite_store.py` 的 metadata 白名单、分析脚本、测试。
- 前置依赖：无。若 S1 已完成，计数器归属 replace 策略类。

## 2. 规格

- learner 在每个 upload interval 内维护计数：`mid_cycle_adoption_count`（本 interval 内 inner-poll adoption 次数）与 `base_switched_at_step`（最近一次切换前已完成的 interval 内 step 数，1-based；若第 N 个 step 后切换则为 N）；每次 adoption 只计一次，不按版本跨度计数；
- proposal 发布时写入 pointer metadata 两个新字段；下一 interval 开始时清零；
- 无 mid-cycle adoption 时字段为 `0`/`null`（字段恒在，便于分析侧无条件读取）；
- **存储盘点结论与修正**：full proposal 的所谓 meta.json 实际是会被下一次发布覆盖的 `updates/latest/learner_*.json` pointer；`insert_update_metadata` 又使用列白名单，归档 JSONL 来自 DB 行而非原始 meta 透传。因此“只写 meta、零 schema”无法提供持久可解释性。必须给 `updates` 表新增两个可空/默认兼容列，并在幂等 schema migration、ingest、归档行中透传；fragment 表不加列（本计划明确排除 fragment/rebase/predict）。旧 run 缺列由现有 connect-time migration 补齐；
- 计数器在**每个 cycle/interval 开始时重置**，而非只在成功 publish 后清零；这样 upload skip/crash-simulation 的下一 interval 不会继承无 proposal 的旧计数。发布函数收到该 interval 的显式快照，发布后 adoption 不计入已经结束的 interval；
- 语义澄清写入 docs：staleness 加权把 proposal 视为"整个 interval 基于 base_global_version 训练"，mid-cycle adoption 时这是近似；新字段量化近似程度，**不**改变加权本身。若未来实验启用 replace+poll 组合并需要精确加权，那是新的协议设计（显式非目标）。

## 3. 目标与完成谓词

1. 两个字段按 §2 语义出现在 meta.json（MCA-01/02）；
2. staleness/merge 路径零变化：merge 相关单元测试无 diff，tiny run（不启用 poll）轨迹与基线等价（MCA-03）；
3. 启用 replace+poll 的 tiny run 中：发生 mid-cycle adoption 时字段非零且与事件日志中的 adoption 次数一致（MCA-04）；
4. 全量 pytest 通过。

## 4. 范围与非目标

- **范围内**：计数、meta 字段、docs 澄清、分析侧读取（如 analysis 汇总表加一列，成本允许时）。
- **非目标**：改变 staleness 加权或 token 统计口径；为 rebase/predict 路径添加同类标注（它们的 reference 语义不同，mid-cycle reconcile 已有专属事件）；replace+poll 组合的启用建议。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 入库路径确认 | 查清 meta 字段入库/归档透传行为（§2 必查项）并冻结存储决策 | 无实现 | 结论入 progress.md；基线 commit |
| L1 计数与字段 | MCA-01/02 先 RED：单元级构造 interval 内 adoption，断言字段值与清零 | 计数器 + meta 写入 | 单元全绿 |
| L2 无回归与集成 | MCA-04 场景配置（tiny + replace + poll_latest_during_inner_steps + 短 interval） | — | MCA-03 轨迹等价；MCA-04 集成通过；全量 pytest |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| MCA-01 | 字段语义 | interval 内 N 次 adoption → count=N、step 为最近一次；下一 interval 开始时清零（upload skip 同样不泄漏） |
| MCA-02 | 缺省值 | 无 adoption → count=0、step=null，字段恒在 |
| MCA-03 | 无回归 | 不启用 poll 的 tiny run 轨迹与基线等价（新增 meta 字段在 profile 中显式声明） |
| MCA-04 | 启用组合集成 | replace+poll tiny run：meta 字段与事件日志计数一致 |

progress.md 每条记录必须列出覆盖的 MCA ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、`git diff --check`。
2. **1 节点 compute**：单元 → 全量 pytest → MCA-03/04 两个 tiny run。
3. 2/9 节点：不需要。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B10/`。无 Checker 变更（meta 追加字段向后兼容）。

## 9. 停止与升级规则

按 AGENTS.md。若 L0 发现 meta 字段无法零 schema 变更地到达分析侧，且加列成本超出本计划体量，允许把"分析侧可读"降级为 follow-up 并在 progress 声明——字段先落 meta.json 即达成主要目标。

## 10. 文档同步

docs 的 proposal 元数据字段表新增两字段；staleness 加权的近似语义澄清（§2 末条）写入协议文档；review 报告 B10 条目标注 commit。
