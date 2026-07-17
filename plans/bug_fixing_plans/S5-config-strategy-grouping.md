# S5：配置按策略分组、校验挪入策略类（含 B3 死字段清除）

## 1. 元信息

- 来源：review S5（低）+ B3（中，死字段）。现状：策略专属参数平铺在 `LearnerSection`（config.py:139-145：`post_publish_latest_wait_seconds`、`post_publish_latest_poll_seconds`、`prediction_reconcile_timeout_seconds`）；全部跨字段校验集中在 `resolve_config`（config.py:248-355）；三个字段已确认无消费方：`inner_optimizer.reset_on_global_update`（:118）、`sync.upload_mode`（:66）、`liveness.quorum_policy`（:92）。
- 性质：**含不兼容配置变更**（旧键 fail-closed 拒绝，无自动迁移）。代码行为不变——只有配置面变化。
- 影响文件：`fs_diloco/core/config.py`、策略类模块（S1 交付）、`fs_diloco/runtime/learner.py`、`configs/*.yaml` 全量、配置文档与相应测试。
- 前置依赖：**S1 完成**（校验去处是策略类）。其中 L1（死字段删除）无依赖，可提前单独执行（对应 review R0 的 B3 条目；若已完成则跳过 L1）。

## 2. 目标与完成谓词

全部满足才可声明完成：

1. B3 三个死字段从 dataclass 与全部 in-repo YAML 中删除；YAML 中再出现时被拒绝并给出"字段已移除"错误（CFG-01）。
2. 真正的策略专属字段迁入相应子节；当前盘点只有 prediction timeout，因此迁入 `learner.prediction.*`。不得创建没有字段的 `learner.rebase` 空节；`learner.global_adoption_strategy` 与策略无关字段留在 `learner.*`（归属清单见 §4）。
3. 旧扁平键出现时 resolve 失败，错误信息**包含新键路径**（CFG-02）。
4. 每个策略类实现 `validate(config)`：replace 可为明确 no-op，rebase/predict 承担各自约束；`resolve_config` 中对应的策略专属校验迁出，非策略校验留在 `resolve_config`（CFG-04）。
5. `configs/` 下全部 YAML 迁移完毕且逐一 resolve 成功（CFG-05，参数化测试固化为常驻回归）。
6. 全量 pytest 通过；三种策略各一次 tiny run 正常完成（配置面变化不改行为的管线证据）。

## 3. 兼容性声明（fail-closed，无迁移）

- 与 plan 01 的 DB-09 先例一致：旧键**拒绝**而非静默别名或自动迁移；错误信息必须指出新键路径，使修复成本为一次编辑。
- SPECIFY 阶段必须先确认 `resolve_config` 对未知键的现行为（拒绝/忽略）：若现状是**静默忽略未知键**，则 CFG-02 的实现前提是先把未知键行为改为拒绝——这本身是一个行为变更，需要单独的 RED 测试并在 progress 中显式记录（它同时是防御 B3 类死字段再次出现的机制）。
- 外部/历史 run 目录中的 resolved config 快照不受影响（只读证据，不会被重新 resolve）。

## 4. 规格：字段归属清单

| 字段 | 现位置 | 去处 | 校验去处 |
| --- | --- | --- | --- |
| `global_adoption_strategy` | learner | 不动 | 工厂（非法名拒绝，S1 已建） |
| `prediction_reconcile_timeout_seconds` | learner | `learner.prediction.reconcile_timeout_seconds` | predict 策略 `validate`（>0 等） |
| `post_publish_latest_wait_seconds` / `_poll_seconds` | learner | **留在 `learner.*`**：现实现的 common post-publish 路径在 replace/rebase/predict 三者均消费 | `resolve_config`（策略无关） |
| `poll_latest_during_inner_steps`、`adopt_global_after_upload` | learner | 不动（策略无关开关） | `resolve_config` |
| `inner_optimizer.reset_on_global_update` | inner_optimizer | **删除**（死字段，B3） | — |
| `sync.upload_mode` | sync | **删除**（死字段，B3） | — |
| `liveness.quorum_policy` | liveness | **删除**（死字段，B3） | — |

归属判定的依据（grep 消费方证据）逐字段记入 progress.md，作为 SPECIFY 产物。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 现状盘点 | 确认未知键现行为；逐字段 grep 消费方，产出 §4 归属清单终稿；盘点 `resolve_config` 现有校验并按"策略专属/全局"分类 | 无实现 | 清单入 progress.md；基线 commit 记录 |
| L1 死字段删除（可独立提前） | CFG-01 先 RED：三字段出现在 YAML → 期望拒绝 | 删除 dataclass 字段；清理 in-repo YAML 中的出现；（如需）未知键拒绝机制 | 全量 pytest；`grep -rn "reset_on_global_update\|upload_mode\|quorum_policy"` 仅余历史文档 |
| L2 分组与旧键拒绝 | CFG-02/03 先 RED：旧扁平 prediction timeout 拒绝且提示新键；新键默认值与类型正确 | 新增 `learner.prediction` 子节并迁移 timeout；不创建空 `learner.rebase` | CFG-05 参数化测试覆盖全部 in-repo YAML |
| L3 校验挂接策略类 | CFG-04 先 RED：策略专属反例（如 timeout ≤0）经由策略 `validate` 拒绝 | 策略类 `validate(config)` 落地；`resolve_config` 中对应校验迁出并在启动路径调用 | 校验总集不减少：L0 校验分类清单逐条对账（每条要么留在 resolve_config，要么在某策略 validate，不允许丢失） |
| L4 配置迁移与管线 | — | `configs/*.yaml` 全量迁移 | 三策略 tiny run 各一次正常完成；全量 pytest；文档同步 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| CFG-01 | 死字段拒绝 | 三个 B3 字段出现在 YAML → resolve 失败，错误含"已移除" |
| CFG-02 | 旧扁平键拒绝 | 如 `learner.prediction_reconcile_timeout_seconds` → 失败且错误含 `learner.prediction.reconcile_timeout_seconds` |
| CFG-03 | 新键解析 | 新子节字段默认值、类型、覆盖优先级正确 |
| CFG-04 | 策略校验反例 | rebase/predict 的非法组合被各自 `validate` 拒绝；replace 的 `validate` 明确 no-op；非当前策略的约束不触发 |
| CFG-05 | 全配置回归 | `configs/` 下每个 YAML resolve 成功（参数化，常驻） |
| CFG-06 | 校验对账 | L0 校验分类清单中每条在新代码中有归属（测试或人工对账表，入 artifacts） |

progress.md 每条记录必须列出覆盖的 CFG ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、`git diff --check`、死字段 grep。
2. **1 节点 compute**：CFG 单元测试 → 全量 pytest → 三策略 tiny run 各一次。
3. 2/9 节点：不需要。但**注意时序**（经验文档 §6.4）：本计划合入后，所有旧 YAML 不再可用——不得在任何 in-flight 长作业仍将重读配置的窗口期合入；PBS 脚本中引用的配置名若变化需同步。

## 8. 报告、证据与 Checker

- 报告目录：`reports/imp_plans/bug_fixing/S5/`，规则按 [plans/AGENTS.md](../AGENTS.md)。
- 关键证据：L0 归属清单与校验分类清单、CFG-06 对账表、三次 tiny run 的 resolved config 快照。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 L0 盘点发现某"死字段"实际存在隐蔽消费方（动态 getattr 等），立即停止删除该字段，将事实记入 failures.md 并回写 review 报告勘误。

## 10. 文档同步

- `docs/` 与 README 中出现的配置示例全部更新为新键；
- review 报告 B3/S5 条目标注完成 commit；
- 若未知键行为由忽略改为拒绝，在 docs 的配置说明中声明该契约（这是防止死字段复发的长效机制）。
