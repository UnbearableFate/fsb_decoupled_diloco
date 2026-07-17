# S1：`run_learner` 三策略提炼为 GlobalAdoptionStrategy

## 1. 元信息

- 来源：review S1（中）——本目录回报最大的一项。`run_learner`（learner.py:1411-1970，约 560 行）内联 replace / rebase / prediction 三种 global adoption 策略：`rebase_reference_flat`、`carried_delta_tokens`、`last_published_anchor_update_id`、`prediction_reference_flat`、`prediction_carried_tokens`、`prediction_update_id`、`prediction_base_version` 七个平行状态变量加 `rebase_enabled`/`prediction_enabled` 两个布尔开关，靠 if/elif 链维持互斥；每新增一种策略需在 ≥4 处插分支。
- 性质：**行为保持重构**。事件名、事件字段、磁盘协议、DB schema、配置语义一律不变。
- 影响文件：`fs_diloco/runtime/learner.py`；新增策略模块（建议 `fs_diloco/runtime/adoption.py`）与其测试；可能新增一份 tiny rebase 配置。
- 前置依赖：S2 已交付（轨迹等价工具 + reconcile helper 已是可迁移的显式状态函数）。建议 S4 先行合入，避免与其 learner.py 改动冲突。

## 2. 目标与完成谓词

全部满足才可声明完成：

1. 存在 `GlobalAdoptionStrategy` 抽象与 replace / rebase / predict 三个实现类，策略由配置经唯一工厂构造（STR-01）。
2. 静态谓词：§1 列出的 7 个状态变量与 2 个布尔开关在 `run_learner` 函数体内出现次数为 **0**（STR-08，grep 可查）；策略状态只存在于策略对象私有字段。
3. `global_adopted`、`inner_training_state_preserved`、`inner_optimizer_reset` 三个事件由**统一的 adoption 收尾路径**发出，不再散布于各分支（STR-02–04 断言事件顺序）。
4. 三种策略各自的 tiny run 归一化事件轨迹与基线 commit 等价（STR-05/06/07）。
5. 全量 pytest 通过；tiny run 上 `scripts/miyabi/check_plan01_invariants.py` 维持 PASS。

## 3. 权威关系与故障模型

- 策略对象封装的是 **learner 进程内易失状态**。现行为：进程崩溃即丢失 reference/prediction 状态，重启后从常规 resume 路径重新 adopt——重构后必须保持一致，策略对象不引入任何持久化。
- 收敛副作用不变量：inner-step 之外对模型参数的写入只允许发生在策略触发的 adoption 路径中（现状即如此，重构后由结构保证）。
- 磁盘协议、payload 格式、latest 语义均为非目标，任何策略实现不得直接绕过现有 storage 层函数。

## 4. 范围与非目标

- **范围内**：`run_learner`（full 模式）的三策略状态与分支迁移；统一事件收尾；策略工厂。
- **非目标**：
  - fragment learner 主循环（S3 计划处理其重复块；fragment adoption 与 full global strategy 语义不同，不纳入本策略类）；
  - B6 GC 竞态重试推广——策略类共享单一 payload 加载入口，为 B6 预留缝，但不实现；
  - B2/Q1 scheduler 语义、S5 配置分组、任何新策略。
- 兼容性：无配置变化；策略名与现 `learner.global_adoption_strategy` 取值一一对应。

## 5. 接口规格

```python
class GlobalAdoptionStrategy:
    name: str

    def wants_inner_poll(self, config) -> bool: ...
    # 对应现 should_poll_during_inner_step 条件（learner.py:1546-1550）：
    # replace 恒可 poll；rebase/predict 仅在 reference 存在时可 poll。

    def on_newer_latest(self, ctx, latest) -> AdoptionOutcome: ...
    # 现 inner-poll 分支体（1551-1647）：replace→adopt_global；
    # rebase/predict→rebase_local_delta_onto_global + 各自事件与状态清空。

    def on_cycle_end(self, ctx) -> AdoptionOutcome | None: ...
    # 现 cycle 末等待（1657-1716）：predict 的 reconcile-wait + 超时；其余策略为 no-op。

    def before_publish(self, ctx) -> None: ...
    # rebase 清除已被本次 proposal 覆盖的旧 anchor；predict 断言 reconcile 已完成。

    def on_after_publish(self, ctx, publish_result) -> StrategyAction: ...
    # 现 publish 后分支（1820-1943）：建立 rebase anchor / prediction reference。

    def on_local_tokens(self, tokens: int) -> None: ...
    # reference 存在时累计 carried tokens；runner 不读取策略私有字段。

    def on_stop(self, ctx) -> None: ...
    # 现 abandon-on-stop（1649-1656）。
```

- `AdoptionOutcome` 携带：新 global version、新 latest metadata、重置后的 `tokens_since_global_load`、`preserve_inner_state: bool`、`reason`。`StrategyAction` 可携带 adoption outcome，或携带“未 adopt 但需 reset optimizer”的原因（prediction started）；二者不得同时出现。**统一 adoption 收尾**由 runner 完成：发 `global_adopted`；按 `preserve_inner_state` 发 `inner_training_state_preserved`（带 `inner_training_state_metrics`）或重建 optimizer/scheduler 并发 `inner_optimizer_reset`。prediction-start 的 reset 也走单一 action 收尾，但不伪造 `global_adopted`。
- `ctx` 只提供模型、I/O、logger 与当前 runner 状态所需的显式依赖；策略专属 reference/token/update-id 状态不得放回 ctx。
- 现 publish 前防御检查（1729-1734）移入 `before_publish`，从 `run_learner` 移除；训练 token 累计统一经 `on_local_tokens`，否则无法满足 STR-08 的私有状态要求。
- 互斥由构造保证：run_learner 只持有一个策略实例，不再有 enabled 布尔。

## 6. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 基线冻结 | 确认三种策略各有可用 tiny 配置：replace=`fs_diloco_tiny_local.yaml`、predict=`fs_diloco_tiny_predict_local.yaml`；rebase 若无 tiny 配置则新增 `fs_diloco_tiny_rebase_local.yaml`（新增配置先在基线 commit 上跑通再冻结） | 基线 commit 上跑三种 tiny run（固定 seed） | 三份归一化轨迹 + commit + 配置入 artifacts |
| L1 接口与 replace | STR-01/02 先 RED：工厂分派、replace 类的 adopt+reset 事件顺序 | 新建策略模块；replace 类落地；run_learner 的 replace 路径切换到策略调用 | STR-05 轨迹等价；全量 pytest；progress 记录 |
| L2 rebase 迁移 | STR-03 先 RED：publish 后建 anchor、newer latest 时 rebase+preserve、状态清空 | rebase 类迁移，删除对应内联分支与变量 | STR-06 轨迹等价；`tests/test_learner_rebase.py` 全通过 |
| L3 predict 迁移 | STR-04/09 先 RED：prediction 建立、reconcile（复用 S2 helper）、超时、abandon-on-stop | predict 类迁移，删除对应内联分支与变量 | STR-07 轨迹等价；S2 的 REC 矩阵回归通过 |
| L4 清扫与静态断言 | STR-08 先 RED（此时变量仍有残留即 RED） | 移除全部策略局部变量、布尔开关、防御分支；收尾事件归一 | STR-08 GREEN；三份轨迹等价复跑；tiny run 上 invariant Checker PASS；全量 pytest |

每个 loop 的 PERSIST 均含：测试命令、关键日志路径、轨迹对比输出、下一 loop 的已知风险。

## 7. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| STR-01 | 策略工厂 | 三个合法策略名→对应类；非法名在启动时拒绝 |
| STR-02 | replace 单元 | on_newer_latest→adopt_global；outcome 触发 reset 收尾；事件顺序与现实现一致 |
| STR-03 | rebase 单元 | publish 后 anchor 建立；newer latest→rebase、preserve 收尾、状态清空 |
| STR-04 | predict 单元 | publish 后 prediction 建立；reconcile 清空状态；超时抛 `TimeoutError` |
| STR-05 | replace 轨迹等价 | tiny run 归一化轨迹与基线一致 |
| STR-06 | rebase 轨迹等价 | 同上（rebase tiny 配置） |
| STR-07 | predict 轨迹等价 | 同上（predict tiny 配置） |
| STR-08 | 静态清扫 | 7 状态变量 + 2 布尔在 `run_learner` 体内出现 0 次 |
| STR-09 | stop 路径 | predict 策略 on_stop 发出 `global_prediction_abandoned_on_stop`，字段不变 |

progress.md 每条记录必须列出覆盖的 STR ID（P8）。

## 8. 验证阶梯

1. **登录节点**：lint、`git diff --check`、STR-08 grep。
2. **1 节点 compute**：STR 单元测试 → 全量 pytest → 三种 tiny run 轨迹等价 → `check_plan01_invariants.py` 对 tiny run 目录 PASS。
3. **2 节点/9 节点**：不需要——协议与磁盘布局未变。下一次 9 节点实验落在重构后 commit 上时在 run_analysis 注明 commit（P6）。
4. 性能不设门槛也不做口头承诺：本计划不改热路径计算；如观察到 tiny run 明显变慢，作为事实记入 progress 并在扩大规模前排查。

## 9. 报告、证据与 Checker

- 报告目录：`reports/imp_plans/bug_fixing/S1/`，规则按 [plans/AGENTS.md](../AGENTS.md)。
- 核心验收证据 = STR-05/06/07 三份轨迹对比输出 + STR-08 grep 输出 + Checker PASS，全部入 artifacts。
- 每个 loop 结束时 `git stash`/临时提交留存可回退点；三策略迁移不得挤在单一提交里（回退粒度 = loop）。

## 10. 停止与升级规则

- 按 AGENTS.md 三连败升级。特别地：**轨迹不等价即停**——不允许通过"调整归一化 profile 丢弃更多字段"来吸收真实行为差异；每次 profile 变更必须在 failures.md 记录字段名与非确定性来源证明。
- 若迁移中发现现实现存在**依赖分支顺序的隐藏行为**（例如事件在特定交错下缺失），先记入 failures.md 并在 review 报告补充条目，经确认后允许把该行为定义为 bug 并偏离等价（需在 progress 中显式声明偏离点），不得静默"顺手修复"。

## 11. 文档同步

- 策略接口各钩子的稳定语义与调用时机写入模块 docstring；docs/ 如有 learner 生命周期描述则同步；
- 完成后在 review 报告的 R3 条目标注 commit；S5（配置分组）自此解除依赖阻塞。
