# B3：删除三个无消费点的配置字段

## 1. 元信息

- 来源：review B3（中）。三个字段全仓库无消费点（review 已 grep 确认）：
  - `inner_optimizer.reset_on_global_update`（config.py:118，默认 `true`）——实际 reset/preserve 行为由 adoption 策略硬编码，resolved config 快照里的这个值会误导对照实验读数；
  - `sync.upload_mode`（config.py:66）；
  - `liveness.quorum_policy`（config.py:92）。
- 性质：**不兼容配置变更**（字段删除，出现即拒绝）。运行行为无变化。
- 影响文件：`fs_diloco/core/config.py`、`configs/*.yaml`、`tests/test_config.py`。
- 关联：[bug_fixing_plans/S5](../bug_fixing_plans/S5-config-strategy-grouping.md) 的 L1 与本计划相同——先执行者为准，后执行者跳过。本计划无前置依赖，属 review R0 立即修项。

## 2. 设计决策：删除，而非改造为真实开关

review 建议二选一（删除 / 让 `reset_on_global_update` 成为 preserve/reset 真实开关，且推荐后者）。本计划选择**删除**，理由：真实的 reset/preserve 消融开关是策略作用域的语义（replace/rebase/predict 各自的 preserve 行为不同），应在 S5 的策略分组配置下以精确命名引入（如 `learner.<strategy>.preserve_inner_state`），并作为 reset/preserve 消融实验计划（review R1/R4 方向）的一部分带着测试落地。复活一个位于 `inner_optimizer` 节、名字含糊、历史上从未生效的全局字段，只会延续误导。该决策若被推翻，需先修订本节再动代码。

## 3. 目标与完成谓词

全部满足才可声明完成：

1. 三个字段从 dataclass 定义、运行时消费点与仓库 YAML 中删除；DCF-01 为了输出“字段已移除”必须在 parser tombstone 表保留三个字符串，因此 DCF-04 不能使用全目录零命中的自相矛盾 grep。静态检查应证明命中只存在于 `REMOVED_CONFIG_KEYS`、拒绝测试与说明文档，不存在 dataclass 字段、`getattr`/字典消费点或 `configs/*.yaml`（DCF-04）；
2. YAML 中再出现任一字段时 `resolve_config` 拒绝，错误信息含"字段已移除"与本计划路径（DCF-01）；
3. `configs/` 下全部 YAML 清理完毕且逐一 resolve 成功（DCF-03）;
4. 全量 pytest 通过；一次 tiny run 正常完成（配置面变化不影响运行的管线证据）。

## 4. 范围与非目标

- **范围内**：字段删除、in-repo YAML 清理、拒绝测试。
- **非目标**：
  - preserve/reset 真实开关（见 §2，归属消融实验计划）；
  - `learner.*` 策略字段分组（S5 的 L2–L4）；
  - 历史 run 目录中 resolved config 快照的追改——只读证据不动，但 run_analysis 引用这些快照解读 reset 行为时应注明该字段从未生效（文档同步项）。
- **前提确认（SPECIFY，与 S5 §3 相同）**：先确认 `resolve_config` 对未知键的现行为。若为静默忽略，"删除字段+出现即拒绝"要求先把未知键行为改为拒绝——这是独立的行为变更，需自己的 RED 测试（DCF-02），并成为防止死字段复发的长效机制。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 现状确认 | 复核三字段确实无消费点（含动态访问：`getattr`、字符串键、YAML 模板）；确认未知键现行为并记录 | 无实现 | grep 证据入 progress.md；基线 commit 记录 |
| L1 未知键拒绝（仅当 L0 发现是静默忽略） | DCF-02 先 RED：任意未定义键 → resolve 失败并指出键名 | 未知键拒绝机制 | 全量 pytest（此步可能暴露现有 YAML 里其它拼写残留——逐一处理并记录） |
| L2 字段删除 | DCF-01 先 RED：三字段出现在 YAML → 期望拒绝（当前被接受，RED） | 删除 dataclass 字段；清理 in-repo YAML | DCF-01/03/04 GREEN；全量 pytest |
| L3 管线证据 | — | — | tiny run（`fs_diloco_tiny_local.yaml`）正常完成；resolved config 快照中三字段不存在；证据归档 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| DCF-01 | 死字段拒绝 | 三字段任一出现在 YAML → resolve 失败，错误含"已移除" |
| DCF-02 | 未知键拒绝（条件性） | 任意未定义键 → resolve 失败并指出键名与所在节 |
| DCF-03 | 全配置回归 | `configs/` 下每个 YAML resolve 成功（参数化，常驻） |
| DCF-04 | 静态清扫 | `configs/*.yaml` 与 dataclass/runtime 消费点零命中；`fs_diloco` 中仅允许 parser tombstone 字符串 |

progress.md 每条记录必须列出覆盖的 DCF ID（P8）。

## 7. 验证阶梯

1. **登录节点**：DCF-04 的 YAML 零命中 + parser tombstone 白名单审计、lint、`git diff --check`。
2. **1 节点 compute**：`pytest tests/test_config.py` → 全量 pytest → tiny run。
3. 2/9 节点：不需要。合入时序注意（经验文档 §6.4）：不得在 in-flight 作业仍会重读配置的窗口期合入。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B3/`，规则按 [plans/AGENTS.md](../AGENTS.md)。关键证据：L0 消费点排查记录（含动态访问检查方式）、DCF-01 修复前 RED 输出、tiny run resolved config 快照。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 L0 发现任一"死字段"存在隐蔽消费方，立即停止删除该字段，事实记入 failures.md 并回写 review 报告勘误——其余字段照常处理。

## 10. 文档同步

- docs/README 中的配置示例移除三字段；
- 在 run_analysis 相关结论旁（reports 侧，追加不改写）注明 `reset_on_global_update` 从未生效，历史快照中该值不代表实际行为；
- 若 L1 落地未知键拒绝，在配置文档声明该契约。
