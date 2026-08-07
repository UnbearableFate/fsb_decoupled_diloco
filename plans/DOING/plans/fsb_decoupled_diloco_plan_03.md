# 代码与架构审查修复实施计划（Correctness / Measurement / Architecture Freeze）

计划 ID：`fsb_decoupled_diloco_plan_03`

状态：未开始（Phase 0 尚未进入）

实施报告目录：`reports/DOING/fsb_decoupled_diloco_plan_03/`

完成门禁审查目录：`reports/DOING/code_review/fsb_decoupled_diloco_plan_03/<phase-id>/`，`<phase-id>` 取 `phase0`、`phase1`、`phase2`、`phase3`、`plan-complete`。

配套文件：

- requirement matrix：`plans/DOING/plans/fsb_decoupled_diloco_plan_03-requirement-matrix.csv`；
- 输入审查报告：`plans/DOING/reviews/fsb_decoupled_diloco_code_architecture_review.md`；
- 研究路线：`plans/DOING/design/fsb_decoupled_diloco_research_roadmap.md`（本计划实现其 Stage 0 及部分 Stage 2 前置）；
- 前一计划冻结契约：`plans/DONE/plan02/fsb_decoupled_diloco_plan_02.md` 与其 design、requirement matrix。

执行前必须读取：

- 仓库根目录 `AGENTS.md`；
- `plans/AGENTS.md`；
- `plans/ref/实施计划制定与 Agent 执行经验.md`；
- 本计划、requirement matrix 和输入审查报告全文；
- `plans/DONE/plan02/fsb_decoupled_diloco_plan_02.md`（Plan 02 的冻结决策在本计划中默认继续有效，除本计划 §1.5 显式改写的条目外不得推翻）；
- 当前 `plans/00-RESEARCH_PLAN.md` 和 `plans/DOING/design/fsb_decoupled_diloco_research_roadmap.md`；
- 当前 `docs/00-glossary.md`、`docs/02-architecture.md`、`docs/03-runtime-flow.md`、`docs/04-data-flow.md`、`docs/05-code-structure.md`、`docs/06-configuration.md`、`docs/07-operations.md`；
- 与本轮修改相关的 `fs_diloco/runtime/`、`fs_diloco/storage/`、`fs_diloco/protocol/`、`fs_diloco/observability/`、`fs_diloco/modeling/hf_data.py`、`fs_diloco/tools/` 和 `scripts/miyabi/`。

进入计算节点测试、PBS 提交或 Miyabi 实验时使用 skill `miyabi-development`。只做静态源代码检查、单元测试设计和文档编辑时不加载该 skill。

---

## 1. 设计决策与阶段边界

### 1.1 本计划解决什么

输入审查报告确认：当前分支的**协议安全边界较强**（epoch fencing、事务内 leader/membership fence、DB-first authority、bounded discovery 都成立，Critical = 0），但**活性、聚合语义、测量语义和架构可演化性尚未闭合**。本计划把这些缺口按严格串行的四个阶段收口：

1. **Phase 0：缺陷判定与复现门禁**。在改任何生产代码前，对审查报告中的 9 个 High、15 个 Medium 和 10 个架构条目逐条给出 `reproduced` / `rejected-with-evidence` / `deferred-with-justification` 判定，并为 P0 缺陷建立确定性 RED 复现；同时建立虚拟时钟 + in-memory adapter + fault tape 的确定性状态机测试底座。
2. **Phase 1：P0 正确性与测量闭合**。修复 H-01～H-06、H-08、H-09 以及与之直接耦合的 M-01、M-10，使 full/static/HA 与 full/dynamic/HA 成为可信实验平台。
3. **Phase 2：P1 稳态、恢复与可复现性**。修复 H-07 与剩余 Medium 条目，建立 artifact 分类 manifest、guarantee matrix、威胁/故障模型和环境身份冻结。
4. **Phase 3：架构分层与生成式验证**。执行 A-01～A-06、A-08、A-10 的分层抽取和模型化测试，使下一阶段“同算法、不同 transport”的对照实验成为可能。

Phase 1 不改变 outer optimizer 数学；它只改变 **merge weight 的分母定义（processed → effective）**、**selection 的第二阶段（contributor admission）**和**摄取/丢弃的判定条件**。Phase 2 不改变 Phase 1 冻结的语义。Phase 3 是行为等价重构，不得引入任何新的协议语义。

### 1.2 与研究路线和 Plan 02 的关系

- 本计划 = roadmap `Stage 0：Correctness and measurement freeze` 的实施形式，另外提前完成 Stage 2 所需的不变量与状态机底座；
- 本计划**不做** roadmap Stage 1（transport baselines）、Stage 3（fragment streaming 重构）、Stage 4（异步聚合算法）、Stage 5（churn 训练质量闭环）的任何实验结论；
- Plan 02 的冻结决策继续有效：epoch-scoped 目录式 checkpoint/control、`learner_li_<uuid4>` instance ID、`allow_unsolicited_registration=false`、Legacy/Fenced/ReadOnly 三 store 分离、`checkpoint_digest_mode` 默认 `off`、无自动 `qdel`、bootstrap slot 编排；
- Plan 02 的“Phase 2 PASS”不因本计划而失效，但其 `dynamic_control_overhead_ratio` 结论按 H-09 在 Phase 1 重新计算后才可用于论文；在此之前该数字只能作为历史记录引用。

### 1.3 严格 phase gate

- Phase 0 Checker 只有 `PASS` / `BLOCKED`。任一 High finding 既没有确定性复现、也没有可核验的 `rejected-with-evidence` 时为 `BLOCKED`；
- Phase 1 不得在 Phase 0 `PASS` 前开始；
- Phase 1 Checker 允许 `PASS_WITH_FOLLOWUPS`，且**唯一允许的 follow-up 是 8+1 Miyabi regression 长作业尚未观察到完整 terminal 结果**；不得用它掩盖任何已知失败，也不得据此开始 Phase 2；
- Phase 2 不得在 Phase 1 `phase1-completed` 返回 `PASS` 前开始；Phase 2 不提供 staged pass；
- Phase 3 不得在 Phase 2 `PASS` 前开始；Phase 3 的通过条件包含**行为等价证明**（同一 fault tape 下 event lineage 与 selection 序列逐条相同），不接受“测试仍然通过”作为等价证据；
- 每个 phase 结束按 `plans/AGENTS.md` 执行双模型独立审查门禁；plan 结束执行 `plan-complete` 全量审查。

### 1.4 用户可观察完成定义

Phase 0 完成后：

- 每条 High/Medium/架构 finding 在 requirement matrix 中都有判定、证据路径和归属 phase；
- H-01、H-02、H-03、H-05、H-06 各有一个在当前代码上**必然失败**的自动化测试；
- 存在可重放的确定性状态机底座：注入相同 fault tape 两次，得到逐条相同的 selection 序列和 event lineage。

Phase 1 完成后：

- 一个 learner 被 revoke 并由同 stream replacement 接管后，syncer 在有界轮数内选中新 incarnation 并提交新 global version，不再出现无限 fence-retry；
- 被 mid-cycle `replace` 覆盖的训练段不再获得 merge weight；`processed = effective + discarded` 在 DB ledger 中对账为零差；
- proposal 摄取在任何唯一键/ID 冲突下都不会同时丢失旧 pending 并推进 frontier；冲突进入 quarantine 且保留原字节；
- 所有 proposal 与 control artifact 通过版本化 schema 校验；非有限值、非正 token、越界 step 区间、路径逃逸在进入 DB 前被拒绝；
- eligible contributor 数持续大于 `quorum_max` 时，每个 contributor 的选中等待版本数有界，fairness 指标被持久化；
- 一次共享文件系统瞬态错误不会造成永久 drop 或 registration request 删除；
- 权威 token 计数来自 SQLite ledger；停止条件显式声明使用哪一种 token 语义；CSV 不再作为任何门禁分母；
- matched static/dynamic 比较输出 signed delta、workload equivalence 结论和置信区间；异常差异进入 `INCOMPARABLE_REQUIRES_AUDIT` 而不是 PASS。

Phase 2 完成后：

- PBS 不确定状态有持久化 deadline 和 `manual_review` 出口，既不会过早释放容量，也不会永久占用；
- stream replacement 保持数据 cursor/RNG 连续，或在无法保持时给出可核算的 replayed/duplicated token 数；
- payload 采用 create-if-absent 发布，`immutable` 表述与实现一致；
- `init-run` 可幂等修复；archive 具有明确去重契约；CSV 为 per-process 后离线合并；
- schema 具备 CHECK/状态枚举和显式迁移；控制面 publication 幂等；
- `docs/guarantees.md` 给出 mode × guarantee 矩阵，`docs/08-threat-model.md` 给出威胁与故障模型；fragment 在 README 顶层标注 experimental。

Phase 3 完成后：

- `syncer.py`、`learner.py`、`fenced_store.py` 不再同时承担 orchestration、协议决策、I/O 与恢复；
- 协议对象是版本化 dataclass / tagged union，不是 `dict[str, Any]` 与字符串状态；
- 外部系统（POSIX FS、SQLite、PBS、HF、clock、随机源）通过 ports/adapters 接入，存在至少一个 in-memory 替代实现；
- 所有公开 mutator 由 CI 自动枚举并强制 fencing 分类；
- Hypothesis 状态机覆盖 admit/revoke/publish/select/commit/crash 的随机序列，并断言有界步进展。

### 1.5 冲突与选项的冻结选择

审查报告在多处给出可选方案。本计划采用以下选择，实施中不得临场更改：

1. **mid-cycle `replace` 保留**，采用 segment accounting；不采用“禁止 mid-cycle adoption”的简化方案。`learner.mid_cycle_replace_accounting=forbid` 作为可选严格模式存在，但默认 `segment`。理由：adoption 策略是研究变量，禁止会削弱对照。
2. **`tokens_this_update` 在 schema v4 中重定义为 effective token**，同时新增 `processed_tokens_this_interval` / `discarded_tokens_this_interval` / `effective_tokens_this_update`，并用 CHECK 强制 `tokens_this_update = effective_tokens_this_update` 与 `processed = effective + discarded`。不新增第二个语义重复的权重列，不保留静默的旧语义。分析工具按 `schema_meta.schema_version` 区分历史 run 语义。
3. **fairness 默认策略 `oldest_unserved_first`**，可选 `deficit_round_robin`、`rotating_hash`；`stable_id_order` 仅用于复现旧行为，正式实验禁止。理由：确定性、可重放、无需额外浮点状态。
4. **严格 schema 使用仓库内 frozen dataclass + 手写 validator**，不引入 Pydantic 等新运行时依赖。理由：M-15 要求冻结环境身份，新依赖会扩大漂移面。
5. **quarantine 保留原始字节**，位于 `control/quarantine/`，属于 audit 类别，不参与 runtime discovery，不被 cleaner 按文件名删除。
6. **proposal payload 校验默认 `sampled`**；论文主实验必须 `always` 或在报告中显式声明信任边界。checkpoint binary digest 继续沿用 Plan 02 的 `off` 默认。
7. **本计划不实现 fragment version-vector authority 或 fragment resume**。fragment 只接受：experimental 标注、read-result 与 schema 层面的一致化、以及不破坏现状的回归。完整 fragment 协议留给 roadmap Stage 3 的独立 plan。
8. **不引入外部 consensus / fencing service，不改变 outer optimizer 数学**（除 merge weight 分母定义），不改变 staleness 惩罚公式。
9. **指标重命名采用版本断点**：v4 run 使用新名称，历史 run 由 schema version 决定解读方式；只在 summary artifact 中保留一个 `deprecated_aliases` 块用于一次性迁移，不建立长期双名兼容层。
10. **Phase 3 是行为等价重构**。若重构过程中发现新的语义缺陷，记录为 finding 并回到 Phase 1/2 的规则处理，不得在 Phase 3 内顺手改变语义。

### 1.6 已核对的代码证据

Phase 0 开始前，本计划已在目标分支静态核对以下位置，实施 agent 应以此为起点而不是重新搜索：

| Finding | 代码位置 | 已核对事实 |
| --- | --- | --- |
| H-01 | `fs_diloco/storage/sqlite_store.py:947` | `eligible_updates()` 只按 `status/base_global_version/staleness` 过滤，无 `learner_instances/placements/streams` join |
| H-01 | `fs_diloco/storage/fenced_store.py:2038` | `revoke_dead_instances()` 更新 instance/placement/stream/launch_requests，但不终结该 incarnation 的 `pending/selected` update |
| H-01 | `fs_diloco/protocol/merge.py:100` | `preference()` 以 `local_step_end` 为首位，未优先 current `stream_epoch/placement_epoch` |
| H-01 | `fs_diloco/runtime/syncer.py:3647` | 捕获 `DynamicMembershipFenceError` 后把整批 selected 无条件 `reset_selected_to_pending` 并 `continue` |
| H-02 | `fs_diloco/runtime/learner.py:2663`、`2694`、`2727` | mid-cycle `replace` 只更新 `base_global_version` 与 `MidCycleAdoptionTracker`，`losses/interval_tokens/interval_examples` 不重置 |
| H-03 | `fs_diloco/storage/sqlite_store.py:830` | 事务顺序为 frontier 检查 → supersede 旧 pending → `INSERT OR IGNORE` → 无条件推进 frontier → 以 `cur.rowcount>0` 返回 |
| H-04 | `fs_diloco/protocol/merge.py:16` | `normalized_update_weights()` 只断言 `total > 0`，单项 token 非有限/为负不被拒绝，使用内置 `sum` |
| H-05 | `fs_diloco/protocol/merge.py:79`、`120` | 两条 selection 路径都在稳定 ID 排序后直接 `[:quorum_max]` 截断 |
| H-06 | `fs_diloco/storage/atomic_io.py:77` | `safe_read_json()` 把 `OSError` 与 `JSONDecodeError` 一并折叠为 `None` |
| H-08 | `fs_diloco/runtime/syncer.py:3601` | `total_seen_tokens` 累加的是 selected proposal 的 `tokens_this_update` |
| H-09 | `fs_diloco/tools/phase2_matched_evidence.py:81` | overhead 使用 `max(0.0, dynamic - static)/static`，负差被截断为 0 后与 `<0.05` 比较 |
| M-01 | `fs_diloco/protocol/liveness.py:225` | `no_progress_timed_out()` 默认使用 `time.time()` |
| M-02 | `fs_diloco/modeling/hf_data.py:91` | `text_rows_to_blocks()` 把整个 token 流 materialize 为 list，`data.streaming` 不改变该行为 |
| M-04 | `fs_diloco/runtime/learner.py:1567`、`1665` | update UUID 截断为 12 hex |
| M-09 | `fs_diloco/storage/schema.sql` | 全文件 0 个 CHECK 约束；HA/dynamic 表的 4 个 CHECK 位于 `schema_bootstrap.py` |

尚未静态确认、必须由 Phase 0 复现或证伪的是：H-01 的**端到端 livelock 是否真的发生且不自愈**、H-05 的**长跑饥饿幅度**、H-06 的**瞬态错误是否会真实导致 drop**、H-07 的 **PBS 状态歧义窗口是否可触发**。

---

## 2. 权威关系与故障模型变更

### 2.1 token / compute 权威链（Phase 1 新增）

```text
learner 内的 segment ledger
  (processed_tokens, effective_tokens, discarded_tokens, segment_count)
    ↓ 版本化 typed proposal（canonical path 由 identity 推导，不信任 payload 中的 file_path）
SQLite updates row（CHECK 约束；冲突进入 quarantine 而不是静默丢弃）
    ↓ 唯一提交点：commit_full_merge 事务
SQLite token_ledger + global_versions 计数列（权威）
    ↓
summary / metrics JSON、CSV、W&B（telemetry，可丢失，永远不是门禁分母）
```

不变量：**任何 PASS 判定使用的 token 分母必须能追溯到 SQLite ledger row；CSV 或 JSONL 缺失只影响可读性，不改变 authority summary。**

### 2.2 读取结果权威链（Phase 1 新增）

```text
POSIX read
  → ReadResult = Ok(payload, fingerprint)
              | NotFound
              | TransientIo(errno)
              | Malformed(reason)
              | IdentityMismatch(reason)
```

规则：

- `Ok` 之外的结果**不得**直接触发破坏性动作（unlink、drop、supersede）；
- `NotFound` / `TransientIo` 记录 `first_missing_at` 与观察计数，只有跨越 visibility grace 且达到 `min_missing_observations` 次独立观察后才允许 drop；
- `Malformed` / `IdentityMismatch` 移动到 quarantine，保留原字节与诊断，不 drop 同 learner 的其他合法 pending；
- registration request 不得因单次 `OSError` 被删除；
- 该状态机同时适用于 full 与 fragment 的 pointer / payload / control / registration 读取路径。

### 2.3 selection 权威链（Phase 1 修改）

```text
eligible query（dynamic 模式内联 membership fence）
    ↓
Stage 1：per-contributor proposal policy（既有 most_recent / oldest_pending 语义）
    ↓
Stage 2：contributor admission policy（fairness_policy，输出有序 contributor 列表）
    ↓
quorum 截断
    ↓
mark_selected（记录 selection_ledger：wait_versions、deficit）
    ↓
commit_full_merge 内重验 membership fence（Plan 02 既有行为，不变）
```

### 2.4 故障模型增补

Plan 02 §3 的故障模型继续有效，本计划增补以下必须覆盖的失败：

- 共享文件系统瞬态 `ENOENT` / `ESTALE` / `EIO`，以及短暂 visibility 延迟后恢复可见；
- pointer 内容合法但 payload 尚未可见；payload 可见但 hash 不匹配；
- update ID 冲突与 `(learner_id, local_step_end, base_global_version)` 唯一键冲突（含精确重放与语义不一致两种）；
- 同 stream 的旧 incarnation 与新 incarnation 的 pending proposal 并存；
- mid-cycle adoption 发生在 interval 的第一步、中间步和最后一步；
- eligible contributor 数持续大于 `quorum_max` 的长跑；
- PBS live/historical 查询同时不可用、`no_record` 与 accounting lag。

非目标（本计划显式不做）：

- 恶意 / Byzantine learner 防护（在 `docs/08-threat-model.md` 中明确声明不防）；
- fragment HA / dynamic / resume / version-vector authority；
- transport 替换实验与算法研究；
- 自动迁移 v1–v3 live run 到 v4 schema（离线迁移工具属于 Phase 2 的 `OPS-08`，live 在线迁移仍禁止）。

---

## 3. Phase 0：缺陷判定与复现门禁

Phase 0 不修改生产协议行为，只新增测试底座、复现测试和证据脚本。允许的生产代码改动仅限于为测试注入所需的**纯粹依赖注入接口**（如把 `now` 参数化），且不得改变默认行为。

### 3.1 TRI-01 finding 逐条判定

对审查报告的 9 个 High、15 个 Medium、10 个架构条目和 6 个文档条目建立判定表，写入 requirement matrix 与 `reports/DOING/fsb_decoupled_diloco_plan_03/progress.md`：

- `reproduced`：有自动化测试或可核验证据链证明缺陷存在；
- `rejected-with-evidence`：给出代码或测试证据说明该 finding 在当前实现中不成立（必须引用 `file:line` 和测试名）；
- `deferred-with-justification`：确认存在但归入后续 phase 或后续 plan，必须写明归属和理由。

任一 High 条目停留在“未判定”即 `BLOCKED`。

### 3.2 TRI-02 H-01 确定性 livelock 复现

必须构造以下场景并断言**当前代码失败**：

- incarnation A 占用 stream 0，发布 `local_step_end=100` 的 pending proposal；
- A 心跳超时被 `revoke_dead_instances()` 撤销；
- replacement B 复用 stream 0，`stream_epoch+1`，发布 `local_step_end=1` 的 proposal；
- 驱动 syncer merge 循环 N 轮（N 冻结为 5）；
- 断言：存在 current quorum 时，global version 必须在 ≤2 轮内前进，且被选中的 update 属于 B。

同时断言 `no_progress_timeout` **不是**该场景的唯一出口。复现测试必须在 in-memory / 临时目录环境下运行，不依赖 PBS。

### 3.3 TRI-03 H-02 token 归属复现

构造 `inner_steps=2` 的 interval：step 1 后发生 mid-cycle `replace`，step 2 后 publish。断言（当前代码失败）：

- proposal 中的参数变化只能由 step 2 解释；
- merge weight 使用的 token 只等于 step 2 的 token；
- step 1 的 token 出现在 discarded 指标中；
- `processed = effective + discarded`。

### 3.4 TRI-04 H-03 摄取冲突复现

构造 DB 中已有合法 pending `u_old`，唯一键 `(learner, step=100, base=5)`；pointer 换为 `update_id=u_new` 但唯一键相同。断言（当前代码失败）：

- 摄取后仍存在恰好一个合法 pending，或 `u_new` 进入 quarantine；
- frontier 不指向既未入库、也未 quarantine、也非 exact replay 的 proposal；
- exact replay（所有协议字段、hash、identity 完全一致）是幂等的且不破坏既有 pending。

### 3.5 TRI-05 H-05 饥饿复现

在 `stream_pool_size=12`、`quorum_max=8`、所有 contributor 持续 eligible 的确定性模拟中运行 200 个 global version，记录每 contributor 的 selection 次数与最大等待版本数。断言（当前代码失败）：`max_selection_wait_versions` 有界且 ≤ 冻结上限。冻结上限在本测试的 RED 阶段确定并写入 matrix，实施中不得放宽。

### 3.6 TRI-06 H-06 瞬态错误复现

用可注入的文件系统 adapter 对 pointer / payload / registration 读取分别注入单次 `ENOENT`、`ESTALE`、`EIO` 后立即恢复。断言（当前代码失败）：

- 单次瞬态错误不产生 `missing_file/dropped`；
- registration request 不被 unlink；
- 恢复后的正常读取能够继续正常路径。

### 3.7 TRI-07 确定性状态机底座

新增 `fs_diloco/testing/`：

```text
fs_diloco/testing/virtual_clock.py        # monotonic + wall 双时钟，可跳变、可倒退 wall
fs_diloco/testing/fault_tape.py           # 可序列化的 (step, fault_kind, target) 序列，可重放
fs_diloco/testing/in_memory_object_store.py
fs_diloco/testing/in_memory_scheduler.py
fs_diloco/testing/state_machine.py        # admit/revoke/publish/select/commit/crash 的驱动器
```

要求：

- 同一 fault tape 两次执行产生逐条相同的 selection 序列与 event lineage（用 `fs_diloco/tools/compare_event_traces.py` 比对）；
- 底座不进入生产 import 路径的必需依赖（`fs_diloco.testing` 只被测试与 probe 导入）；
- 支持在任意 crash point 中断并从权威状态恢复。

### 3.8 TRI-08 测量基线冻结

在修复前记录当前口径的基线数字，使 Phase 1 之后的变化可解释：

- 现有 `total_seen_tokens` 与实际 processed token 的差值样本；
- Plan 02 记录的 matched static/dynamic 时间（101.949s / 47.348s）在新的 signed + equivalence 口径下的重算结果或“数据不足以重算”的明确结论；
- 现有 selection 分布快照。

### 3.9 TRI-09 既有测试与 Checker 基线

审查报告为静态审查，未重跑测试。Phase 0 必须在目标 commit 上实际执行：

- 全量 `pytest`（记录实际通过数，不引用历史的 `495 passed`）；
- `scripts/miyabi/check_plan01_invariants.py`、`check_plan02_phase1.py --mode phase1-completed`、`check_plan02_phase2.py --mode phase2-completed` 的当前结果。

任何既有失败必须在 Phase 0 记录为 finding 并判定归属，不得默认忽略。

### 3.10 Phase 0 Checker

新增 `scripts/miyabi/check_plan03_phase0.py`。stdout 只能是 `PASS` 或 `BLOCKED`，structured evidence 写入：

```text
reports/DOING/fsb_decoupled_diloco_plan_03/artifacts/
  <timestamp>_phase0-triage_<pass|blocked>.json
```

evidence 至少包含：finding 判定表、每个 RED 测试的名称与失败断言、fault tape 重放一致性结果、pytest 与既有 checker 的实际输出摘要、source/config identity。

---

## 4. Phase 1：P0 正确性与测量闭合

### 4.1 COR-01 / COR-02 / COR-03：动态成员 livelock 三层防线（H-01）

审查报告要求三层同时实施，本计划照此冻结。

**COR-01 — eligible query 只返回 current incarnation。**

新增读模型方法（读路径，不需要 leader token）：

```python
def eligible_updates(
    self,
    current_version: int,
    max_staleness_versions: int,
    *,
    membership_scope: str = "any",   # any | current_members
) -> list[dict[str, Any]]
```

`membership_scope="current_members"` 时使用：

```sql
SELECT u.*
FROM updates AS u
JOIN learner_instances AS li ON li.instance_id = u.learner_instance_id
JOIN placements       AS p  ON p.placement_id = li.placement_id
JOIN streams          AS s  ON s.stream_id    = li.stream_id
WHERE u.status = 'pending'
  AND u.base_global_version <= :current_version
  AND (:current_version - u.base_global_version) <= :max_staleness
  AND li.status IN ('admitted', 'draining', 'drained')
  AND p.current_instance_id      = li.instance_id
  AND p.current_placement_epoch  = li.placement_epoch
  AND s.current_instance_id      = li.instance_id
  AND s.current_stream_epoch     = li.stream_epoch
  AND u.placement_epoch = li.placement_epoch
  AND u.stream_epoch    = li.stream_epoch
ORDER BY u.committed_at ASC
```

`full_update_proposal_source()` 在 `dynamic_mode` 下必须传 `current_members`；static/fragment 路径保持 `any` 并逐字节回归。`drained` 状态保留在集合内，理由与既有摄取路径一致：drain ack 的 final proposal 仍应可被合并，但其 placement/stream 仍必须是 current。

**COR-02 — revoke 与旧 proposal 终结同事务。**

`revoke_dead_instances()`、drain timeout revoke 路径、admission 中的 authorized replacement supersede 路径，都必须在同一 fenced transaction 内追加：

```sql
UPDATE updates
SET status = 'dropped',
    drop_reason = 'revoked_incarnation',
    dropped_by_epoch = :epoch
WHERE learner_instance_id = :instance_id
  AND status IN ('pending', 'selected');
```

并把被丢弃的 token 写入 `token_ledger`（`event_kind='proposal_dropped'`、`discard_reason='revoked_incarnation'`），使 §4.4 的对账仍然成立。返回值必须包含每个 instance 被终结的 update ID 列表，供事件日志与 checker 核对。

**COR-03 — fence retry 不做无条件全批 reset。**

`DynamicMembershipFenceError` 扩展为携带 per-update 拒绝原因：

```python
class DynamicMembershipFenceError(RuntimeError):
    rejections: tuple[MembershipRejection, ...]   # update_id, reason, expected/observed epoch
```

新增 fenced 方法：

```python
def adjudicate_selected_membership(
    self, token: LeaderToken, update_ids: list[str]
) -> dict[str, list[str]]      # {"reset": [...], "dropped": [...]}
```

在单个事务内逐行重查 membership：仍是 current 的行 `selected → pending`；已失效的行 `selected → dropped(drop_reason='membership_fence')` 并写 ledger。syncer 的 `except DynamicMembershipFenceError` 分支改为调用该方法，并记录：

- 事件 `dynamic_membership_commit_retry`，字段增加 `dropped_update_ids`、`reset_update_ids`、`expected_epochs`、`observed_epochs`；
- 指标 `membership_fence_drop_count`、`membership_fence_reset_count`、`membership_fence_retry_count`。

**活性不变量（必须由测试断言）：** 存在 current quorum 且 object store 可用时，连续 fence retry 次数 ≤ `retry_bound = 当前 selected 批次大小`，且此后 global version 必须前进。

### 4.2 COR-04 / COR-05：mid-cycle replace 的 effective token 语义（H-02）

**不变量（ACC）：** proposal 中被 merge 的参数变化必须能由 `effective_tokens_this_update` 所代表的训练段解释；被 `replace` 覆盖的计算只能记为 processed/discarded，不得进入 merge weight。

learner 侧改动（`fs_diloco/runtime/learner.py`）：

- interval accumulator 拆成 **interval 级**与 **segment 级**：`processed_tokens/examples/losses`（整个 interval）与 `segment_tokens/examples/losses`（当前 effective 段）；
- 发生 mid-cycle adoption 且策略语义为“覆盖本地计算”时（`replace`），关闭当前 segment：把 segment 累计量并入 `discarded_*`，重置 segment accumulator，`segment_count += 1`，记录 `last_base_switch_step`；
- 每策略的归属契约必须显式实现并测试：
  - `replace`：覆盖本地权重 → 之前 segment 全部 discarded；
  - `rebase`：本地 delta 被重放到新 base → 之前 segment 仍为 effective，不得计入 discarded；
  - `predict`：按 `reconcile_prediction()` 的实际结果决定；被回滚的部分计 discarded，被保留的部分计 effective。

proposal 新增字段（`format_version` 提升）：

```text
processed_tokens_this_interval
effective_tokens_this_update
discarded_tokens_this_interval
processed_examples_this_interval
effective_examples_this_update
segment_count
last_base_switch_step
effective_local_step_start
```

`tokens_this_update` 按 §1.5(2) 写入 effective 值。`train_loss` 改为 effective 段均值，并新增 `processed_train_loss` 记录整个 interval 均值。

DB（schema v4）在 `updates` 与 `fragment_updates` 增加对应列与约束：

```sql
CHECK(processed_tokens_this_interval = effective_tokens_this_update + discarded_tokens_this_interval),
CHECK(tokens_this_update = effective_tokens_this_update),
CHECK(effective_tokens_this_update > 0),
CHECK(discarded_tokens_this_interval >= 0),
CHECK(segment_count >= 1)
```

`normalized_update_weights()` 继续读取 `tokens_this_update`（此时语义已是 effective），不新增分支。

### 4.3 COR-06：摄取事务顺序与 quarantine（H-03）

`insert_update_metadata()`（`sqlite_store.py`，`FencedSQLiteStore` 通过 `_mutate` 复用同一实现）改为：

```text
validate（schema + identity + membership fence）
  → INSERT（普通 INSERT，不使用 OR IGNORE）
  → 捕获 IntegrityError 并 adjudicate
  → supersede 更旧 pending
  → update frontier
  → commit
```

冲突裁决规则：

| 情况 | 处理 |
| --- | --- |
| 所有协议字段、file hash、identity 与既有 row 完全一致 | exact replay：不 supersede、不改 frontier、返回 `False`，记录 `ingest_exact_replay_count` |
| 唯一键冲突但语义不一致 | 新 row 写入 `proposal_quarantine`，原 pending **保持不变**，frontier **不前进**，返回 `False` |
| `update_id` 冲突但内容不同 | 同上，`conflict_kind='update_id_collision'` |
| 无冲突 | 正常 insert → supersede 更旧 pending → 推进 frontier → 返回 `True` |

新增表：

```sql
CREATE TABLE proposal_quarantine (
    quarantine_id      TEXT PRIMARY KEY,
    update_id          TEXT NOT NULL,
    learner_id         TEXT NOT NULL,
    learner_instance_id TEXT,
    conflict_kind      TEXT NOT NULL,
    existing_update_id TEXT,
    pointer_path       TEXT,
    raw_relative_path  TEXT,
    observed_sha256    TEXT,
    detail             TEXT NOT NULL,
    recorded_by_epoch  INTEGER,
    recorded_at        REAL NOT NULL,
    CHECK(conflict_kind IN (
        'unique_key_conflict','update_id_collision','identity_mismatch',
        'malformed_payload','schema_violation','hash_mismatch'
    ))
);
```

`updates.status` 枚举增加 `quarantined`（用于被隔离后仍需保留 row 的场景），retention / archive / GC 必须识别该状态并且**不得**把 quarantine 原字节当作 orphan 删除。fragment 摄取路径采用同一规则。

**不变量（ING）：** frontier 不得指向既未入库、也未 quarantine、也非 exact replay 的 proposal；任何冲突处理都不得减少合法 pending 的数量。

### 4.4 COR-07 / COR-08：严格 schema 与数值域（H-04）

新增 `fs_diloco/protocol/schemas/`：

```text
schemas/__init__.py
schemas/versions.py     # 每类 artifact 的 format_version 与迁移规则
schemas/proposal.py     # FullProposalV2 / FragmentProposalV2
schemas/control.py      # LatestControlV1 / TerminalV1 / DrainV1 / HeartbeatV2
schemas/membership.py   # RegistrationRequestV1 / AdmissionV1 / LaunchReceiptV1
schemas/errors.py       # SchemaViolation(field, rule, observed)
```

要求：

- 解码后转为 frozen dataclass，DB 与 merge 只接受 typed object；
- 权威性 artifact 采用**未知顶层字段直接拒绝**（fail closed）；telemetry artifact 允许忽略未知字段；
- canonical payload path 由 identity 计算（`run_id`、`learner_instance_id`/`learner_id`、`admission_generation`、`update_id`），**不信任 payload 中的 `file_path`**；实际路径必须落在该 identity 的 canonical 目录内；
- 路径必须是 regular file，不接受 symlink、目录或路径逃逸；
- 强制数值域：

```text
effective_tokens_this_update > 0
processed_tokens_this_interval >= effective_tokens_this_update
inner_steps > 0 且与 (local_step_end - local_step_start) 一致
local_step_start < local_step_end
所有 loss / norm / step-time / resource metric 必须 math.isfinite
created_at <= committed_at 且 committed_at <= now + max_clock_skew_seconds
base_global_version >= 0；placement_epoch / stream_epoch / admission_generation >= 0
file_size_bytes > 0，且与实际 stat 一致
```

- `io.payload_verify_mode` 控制 SHA-256 校验：`off | sampled | always`；`sampled` 按 `payload_verify_sample_rate` 抽样；不匹配即 quarantine（`conflict_kind='hash_mismatch'`）；
- tensor shape / dtype / numel 与 param index 的一致性在加载点验证，失败即 quarantine 而不是崩溃整个 merge。

`normalized_update_weights()` / `normalized_fragment_update_weights()` 改为：

```python
for update in updates:
    weight = raw_update_weight(...)
    if not math.isfinite(weight) or weight <= 0.0:
        raise ValueError(...)   # 携带 update_id 与观测值
total = math.fsum(raw.values())
```

schema v4 在 `updates` / `fragment_updates` 增加 §4.2 的 CHECK，并补充：

```sql
CHECK(inner_steps > 0),
CHECK(local_step_end > local_step_start),
CHECK(base_global_version >= 0),
CHECK(status IN ('pending','selected','applied','dropped','quarantined'))
```

### 4.5 COR-09 / COR-15：两阶段 selection 与公平性（H-05、M-10）

`fs_diloco/protocol/selection.py` 新增，`merge.py` 只保留 Stage 1 语义：

```python
def select_contributors(
    candidates: list[ContributorProposal],
    *,
    policy: str,
    quorum_max: int,
    now_version: int,
    ledger: SelectionLedgerView,
) -> list[ContributorProposal]
```

策略：

- `oldest_unserved_first`（默认）：按 `(last_selected_global_version ASC, produced_effective_tokens_unapplied DESC, contributor_key ASC)`；
- `deficit_round_robin`：以 effective token 为配额单位的 DRR；
- `rotating_hash`：`sha256(run_id, global_version, contributor_key)` 排序，实现无状态轮转；
- `stable_id_order`：现行行为，仅用于复现旧结果，正式实验配置校验拒绝。

`contributor_key` 在 dynamic 模式为 `stream_id`，static 模式为 `learner_id`。

**M-10 修正：** `oldest_pending` 的 age key 明确定义为 `committed_at`（首次入库时间为 tiebreak），不再混用不可跨 incarnation 比较的 `local_step_end`。`select_one_per_dynamic_member()` 的 `preference()` 相应改为：**首先比较 current epoch 一致性，其次按 policy 的 age key，最后 `update_id`**。

新增表与列：

```sql
CREATE TABLE selection_ledger (
    global_version   INTEGER NOT NULL,
    contributor_key  TEXT    NOT NULL,
    update_id        TEXT    NOT NULL,
    wait_versions    INTEGER NOT NULL,
    effective_tokens INTEGER NOT NULL,
    policy           TEXT    NOT NULL,
    selected_at      REAL    NOT NULL,
    PRIMARY KEY(global_version, contributor_key)
);
```

`streams` 与 `learners` 增加 `last_selected_global_version`、`selected_count`、`produced_effective_tokens`、`applied_effective_tokens`。

新增指标（写入 DB 与 syncer metrics）：`per_stream_selection_rate`、`max_selection_wait_versions`、`selection_jain_index`、`selection_entropy`、`applied_over_produced_token_ratio`。

### 4.6 COR-10：结构化文件读取与 visibility grace（H-06）

新增 `fs_diloco/storage/read_result.py`（tagged union，见 §2.2）。改造范围：

- `safe_read_json()` 保留但降级为 telemetry-only helper，并在 docstring 中标注不得用于权威路径；权威路径改用 `read_json_result()`；
- proposal pointer / payload 摄取、selected 后二次检查、registration ingest、control/terminal/heartbeat 读取全部迁移；
- 新增持久化观察状态：

```sql
CREATE TABLE payload_observations (
    observation_target TEXT PRIMARY KEY,   -- update_id 或 request_id
    target_kind        TEXT NOT NULL,
    first_missing_at   REAL,
    observation_count  INTEGER NOT NULL DEFAULT 0,
    last_result        TEXT NOT NULL,
    last_errno         INTEGER,
    last_observed_at   REAL NOT NULL
);
```

- drop 条件：`now - first_missing_at >= sync.ingest.visibility_grace_seconds` **且** `observation_count >= sync.ingest.min_missing_observations`（≥2）**且** 两次观察之间至少间隔一个 scan interval；
- `MALFORMED` / `IDENTITY_MISMATCH`：移动原字节到 `control/quarantine/<kind>/<timestamp>_<id>`，记录 `proposal_quarantine`；
- registration request 只有在明确 `Malformed` 或 TTL 过期后才允许删除，单次 `TransientIo` 一律重试；
- 故障注入必须覆盖 `ENOENT`、`ESTALE`、`EIO` 的短暂恢复与持续失败两种。

### 4.7 COR-11 / COR-12：指标本体与权威计数（H-08）

新增 `fs_diloco/observability/token_ledger.py` 与 schema v4 表：

```sql
CREATE TABLE token_ledger (
    ledger_id           TEXT PRIMARY KEY,
    event_kind          TEXT NOT NULL,
    global_version      INTEGER,
    update_id           TEXT,
    learner_instance_id TEXT,
    stream_id           INTEGER,
    processed_tokens    INTEGER NOT NULL DEFAULT 0,
    effective_tokens    INTEGER NOT NULL DEFAULT 0,
    discarded_tokens    INTEGER NOT NULL DEFAULT 0,
    discard_reason      TEXT,
    recorded_by_epoch   INTEGER NOT NULL,
    recorded_at         REAL NOT NULL,
    CHECK(processed_tokens >= 0 AND effective_tokens >= 0 AND discarded_tokens >= 0),
    CHECK(event_kind IN (
        'proposal_ingested','proposal_selected','merge_applied',
        'proposal_dropped','segment_discarded','stream_replayed'
    ))
);
```

权威计数字段（同时出现在 `global_versions`、`terminal_state` 与 summary artifact）：

```text
processed_tokens_total
unique_data_tokens_estimate
proposal_tokens_produced
proposal_tokens_ingested
proposal_tokens_eligible
proposal_tokens_selected
effective_tokens_applied
discarded_tokens_by_reason      # JSON 对象
replayed_tokens_after_replacement
gpu_seconds
node_seconds
```

重命名：`total_seen_tokens` → `selected_proposal_tokens_committed`（D-02）。schema v4 的 `global_versions` / `terminal_state` 使用新列名；summary artifact 中保留一次性 `deprecated_aliases` 块。

停止条件语义显式化：`sync.stop_after_global_tokens` 在 v4 配置中被拒绝，替换为 `sync.stop_after_effective_tokens_applied`；配置校验必须给出迁移提示而不是静默接受旧名。

**COR-12：** `run_metrics_csv.py` 与所有 CSV/JSONL telemetry 增加 `completeness_level`（`authoritative_export | best_effort | partial`），并且：

- checker 与 matched 比较**只**接受 `authoritative_export`（由 DB ledger 导出）；
- CSV 丢失、行交错或截断不得改变 authority summary，须由测试断言；
- `run_metrics_csv.py` 对 manifest / DB 的 fallback 必须标记降级等级，不得静默降级。

**对账不变量（必须由 checker 计算）：**

```text
effective_tokens_applied + discarded_tokens_total + pending_effective_tokens
    == processed_tokens_total（差值必须为 0）
```

### 4.8 COR-13：matched 比较的可比性门禁（H-09）

新增 `fs_diloco/tools/workload_equivalence.py` 与 `fs_diloco/tools/matched_comparison.py`，替换 `phase2_matched_evidence.py` 的门禁逻辑（保留其 identity 校验部分）。

规则：

1. 永远报告 **signed** `delta_seconds` 与 `signed_ratio`，禁止 `max(0, ...)` 截断；
2. 先运行 workload equivalence checker，任一不等价即 `INCOMPARABLE_REQUIRES_AUDIT`，不进入 overhead 判定。等价维度：
   - `effective_tokens_applied` 与 `processed_tokens_total`（相对差 ≤ 冻结阈值）；
   - committed outer steps 与每轮 selected count 分布；
   - 相同 terminal event anchor（比较区间限定为同一 anchor 之间）；
   - 相同 model / seed / dataset revision / tokenizer hash / source fingerprint；
   - GPU/CPU placement 与节点数；
   - 无 warm cache / 队列 / 启动 / teardown 被单侧计入；
3. 至少 3 次重复，报告均值与 95% 置信区间；
4. 采用 non-inferiority / equivalence test（等价界 ±5%），不使用单样本 ratio 判定；
5. `|signed_ratio| > 0.20` 时无论方向一律输出 `INCOMPARABLE_REQUIRES_AUDIT`；
6. checker 输出三值不变，但 `INCOMPARABLE_REQUIRES_AUDIT` 归入 `BLOCKED`，并在 structured evidence 中给出原因与需审计项清单。

Plan 02 记录的 101.949s / 47.348s 结论必须按新口径重算或标记为“历史记录，不用于论文”。

### 4.9 COR-14：进程内 timeout 使用 monotonic clock（M-01）

- `no_progress_timed_out()` 及所有进程内停滞判定改用 `time.monotonic()`；
- wall clock 只用于跨进程 lease/heartbeat 比较（Plan 02 §2.3 的既有设计不变）与持久审计字段；
- 新增测试：注入 wall clock 向前/向后跳变 ±3600s，断言进程内 timeout 判定不变；
- `fs_diloco/testing/virtual_clock.py` 提供该注入能力。

### 4.10 Phase 1 配置变更

```yaml
sync:
  stop_after_effective_tokens_applied: null   # 取代 stop_after_global_tokens
  selection:
    fairness_policy: oldest_unserved_first     # none|oldest_unserved_first|deficit_round_robin|rotating_hash|stable_id_order
    max_selection_wait_versions: 8
    fairness_report_window_versions: 64
  ingest:
    visibility_grace_seconds: 20.0
    min_missing_observations: 2
    transient_retry_backoff_seconds: 2.0
    max_transient_retry_seconds: 120.0
    quarantine_retention_seconds: 86400.0

io:
  payload_verify_mode: sampled                 # off|sampled|always
  payload_verify_sample_rate: 0.05
  proposal_schema_mode: strict                 # strict|compat

learner:
  mid_cycle_replace_accounting: segment        # segment|forbid

metrics:
  token_ledger: authoritative                  # authoritative|disabled
```

校验规则：

```text
min_missing_observations >= 2
visibility_grace_seconds >= 2 * sync.scan_interval_seconds
max_transient_retry_seconds >= visibility_grace_seconds
transient_retry_backoff_seconds > 0
0 <= payload_verify_sample_rate <= 1；payload_verify_mode=sampled 时 payload_verify_sample_rate > 0
fairness_policy in {none, oldest_unserved_first, deficit_round_robin, rotating_hash, stable_id_order}
membership.mode=dynamic 时 max_selection_wait_versions >= ceil(stream_pool_size / quorum_max)
正式实验要求 proposal_schema_mode=strict 且 metrics.token_ledger=authoritative；否则 checker BLOCKED
配置中出现 sync.stop_after_global_tokens 时 fail closed 并给出改名提示
```

### 4.11 Phase 1 不变量

| ID | 不变量 |
| --- | --- |
| COR-01 | dynamic eligible 集合只包含 current incarnation 的 pending proposal。 |
| COR-02 | revoke / supersede 与其 incarnation 的 pending/selected 终结处于同一事务，且 token 进入 ledger。 |
| COR-03 | fence retry 逐行裁决；存在 current quorum 时连续 retry 有界并最终前进。 |
| COR-04 | `processed = effective + discarded`，逐 proposal 与全 run 均成立。 |
| COR-05 | merge weight 只由 effective token 决定；被覆盖的计算权重为 0。 |
| COR-06 | 任何摄取冲突都不减少合法 pending；frontier 只指向已入库、已 quarantine 或 exact replay 的 proposal。 |
| COR-07 | 权威 artifact 必须通过版本化 schema；canonical path 由 identity 推导；路径逃逸/symlink 被拒绝。 |
| COR-08 | 每个 raw merge weight 有限且为正；总和用 `math.fsum`；DB CHECK 拒绝矛盾行。 |
| COR-09 | eligible contributor > quorum_max 时，每 contributor 的 selection 等待版本数 ≤ 冻结上限。 |
| COR-10 | 单次瞬态 FS 错误不导致 drop 或 registration 删除；malformed 进入 quarantine 并保留原字节。 |
| COR-11 | 权威 token 计数来自 SQLite ledger，对账差为 0；停止条件声明 token 语义。 |
| COR-12 | CSV/JSONL 丢失不改变 authority summary；门禁只接受 `authoritative_export`。 |
| COR-13 | matched 比较输出 signed 值与等价性结论；不可比时输出 `INCOMPARABLE_REQUIRES_AUDIT`。 |
| COR-14 | 进程内 timeout 不受 wall clock 跳变影响。 |
| COR-15 | selection age key 定义唯一且跨 incarnation 可比。 |
| COR-16 | static full 与 fragment 的既有数学、CLI 和结果保持逐字节回归（除本计划显式改写的语义外）。 |

### 4.12 Phase 1 focused 测试

| 组 | 必测场景 |
| --- | --- |
| LIVELOCK | revoke→replacement→下一轮必须选中新 incarnation；revoke 与 selection 的两种并发顺序；批次中只有一个旧 incarnation 时其他合法 proposal 不被回滚；随机 admit/revoke/publish/select/commit 序列的有界步进展。 |
| ACCOUNTING | replace 发生在第 1/中间/最后一个 inner step；rebase 与 predict 的归属差异；`processed=effective+discarded`；multi-segment interval；全 run 对账。 |
| INGEST | exact replay 幂等；唯一键冲突；`update_id` 冲突；frontier crash；old/new pointer 乱序；malformed/quarantine；fragment 同规则。 |
| SCHEMA | 每个数值域反例；未知字段策略；canonical path 推导；symlink/路径逃逸；hash mismatch；tensor shape/dtype 不一致；`format_version` 迁移。 |
| FAIRNESS | `N > quorum_max` 的 1000 版本长跑；异构速度；stream churn；四种策略的确定性与可重放；`oldest_pending` age key 语义。 |
| READRESULT | 瞬态 ENOENT/ESTALE/EIO 后恢复；持续失败到 grace 后 drop；registration 单次错误不删除；quarantine 保留字节；grace 边界的时序。 |
| METRICS | 五类 token 计数守恒；CSV 丢失/截断不改变 authority；`completeness_level` 降级路径；停止条件语义；重命名后的 summary 与 checker 一致。 |
| MATCHED | signed 负差不被截断；workload 不等价拒绝；3 次重复与置信区间；20% sanity bound；identity mismatch。 |
| CLOCK | wall clock ±3600s 跳变；monotonic 单调；lease 判定仍使用 wall clock。 |
| COMPAT | static full 逐字节回归；fragment 回归；legacy schema 只读分析；v1–v3 run 的分析工具语义分支。 |

### 4.13 Phase 1 验证阶梯

- **G0**：范围、dirty worktree、schema/source pin、finding→requirement 映射、Phase 0 RED 测试清单、G3 故障矩阵成本估算；
- **G1**：`git diff --check`、compile/lint、配置静态测试、`bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh`、literal group ID；
- **G2**：计算节点 focused tests + 全量 pytest；确定性状态机 1000 轮；fairness 1000 版本长跑；
- **G3**：崩溃与故障矩阵：摄取冲突、瞬态 FS 错误、mid-cycle adoption、fence retry 各场景在 tiny 配置下至少 10 次；
- **G4**：2 节点共享 FS：真实 revoke→replacement→合并前进；真实瞬态错误注入；
- **G5**：独立 PBS job 人工 restart 下的 token 对账与 fairness 指标；
- **G6**：**8+1 Miyabi regression**：1 syncer + 8 learner 独立作业，至少 10 次 committed merge、至少 1 次真实 replacement、至少 1 次 syncer takeover；token 对账差为 0；fairness 指标在冻结上限内；
- **G6b**：若正式 workload 超过 50 local steps × 10 global steps 基线，按根 `AGENTS.md` 同步验证结论文档。

Phase 1 Checker：`scripts/miyabi/check_plan03_phase1.py --mode phase1-staged|phase1-completed`。`phase1-completed` 必须同时接收与同一 run descriptor 绑定的 matched comparability artifact 和 token ledger 对账 artifact。

---

## 5. Phase 2：稳态、恢复与可复现性

### 5.1 requirement 概览

| ID | 来源 | 契约摘要 |
| --- | --- | --- |
| OPS-01 | H-07 | PBS 不确定性状态机：持久化 `first_scheduler_uncertain_at` / `last_positive_scheduler_evidence_at` / `uncertainty_deadline` / `terminal_evidence_source`；状态统一为 `planned → submitting → submission_unknown → submitted/started → terminal_uncertain → admitted/failed/expired/manual_review`；只有 live+historical 均无记录、超过 zombie window 且无 registration receipt 才释放 reservation；超期仍 `query_failed` 进入 `manual_review`，不静默保留也不自动重提。 |
| OPS-02 | M-03 | stream replacement 的数据连续性：为每 stream 持久化 deterministic global sample index（优先）或 cursor+RNG state；无法保持时必须写 `replayed_tokens_after_replacement` 并在报告中显式列出。 |
| OPS-03 | M-04、D-01 | 完整 UUID 或内容 hash 作为 update ID；payload 采用 `O_EXCL`/link-based create-if-absent 发布；目标已存在即 fail closed；文档中的 "immutable" 表述与实现对齐。 |
| OPS-04 | M-05 | `init_run` 使用 staging root + atomic finalize marker；支持 identity-matched 幂等 resume/repair；半初始化 root 有明确诊断而不是要求人工判断。 |
| OPS-05 | M-06 | archive 每批带 transaction/batch ID 与 row primary key；consumer 契约声明为 at-least-once + 明确去重键；离线分析工具实现该去重。 |
| OPS-06 | M-07 | CSV 改为 per-process 文件 + 离线合并，或单 writer；任何 CSV 永远不是 authority（与 COR-12 一致）。 |
| OPS-07 | M-08 | 每个 config section 实现 `validate()`；覆盖 duration、count、enum 与跨字段不变量；手工构造 `Config()` 也必须经过校验。 |
| OPS-08 | M-09、A-10 | schema v4 补齐 CHECK / 状态枚举 / 必要 FK；`foreign_keys=ON`（若协议允许）；每个 schema 版本有显式 migration 与迁移前 invariant audit；离线迁移工具，不允许 live 在线迁移。 |
| OPS-09 | M-11 | control publication 使用 deterministic command ID；publish ledger 记录 intent 与 result；同 epoch 同语义的重试产生相同 artifact 与 hash。 |
| OPS-10 | M-12 | leader lease acquire 在事务实际取得写锁后重新采样时间；同时记录 DB wait，避免锁等待导致安全预算失真。 |
| OPS-11 | M-13 | commit intent/reservation 先验证再写 checkpoint；fence 失败的 artifact 落 staging 并快速 GC；记录 orphan reason 与字节数。 |
| OPS-12 | M-14、A-09、D-04 | 新增 `docs/guarantees.md` 的 mode × guarantee 矩阵（每格 `guaranteed / best effort / experimental / unsupported` 并链接不变量与测试证据）；README 顶层标注 fragment experimental。 |
| OPS-13 | M-15 | 冻结环境身份：依赖 lock hash、容器/镜像 digest、CUDA/PyTorch 版本、dataset revision、tokenizer hash、driver/runtime，写入 run descriptor 并由 checker 校验。 |
| OPS-14 | A-07 | 为 authority / audit history / telemetry / cache / payload 五类 artifact 建立机器可读 manifest（可丢失、可重建、retention、是否用于 correctness）；cleaner 只按 manifest policy 工作，不再硬编码文件名。 |
| OPS-15 | M-02 | 实现真正的 bounded shuffle buffer + 在线 packing，或把配置项改名为 `dataset_iterable_but_materialized` 并在文档中说明内存行为；二选一，不允许保留误导性命名。 |
| OPS-16 | D-01～D-06 | 文档修正：immutable 表述、token 指标语义、Plan PASS 不等于训练质量 PASS 的三证据域划分、fragment 边界、matched 公式披露、新增 `docs/08-threat-model.md`。 |

### 5.2 Phase 2 不变量

| ID | 不变量 |
| --- | --- |
| OPS-01 | 任一 launch request 要么持有 scheduler 确认的 reservation，要么已终态，要么在 `manual_review`；不存在永久不确定态。 |
| OPS-02 | 同 stream 的 replacement 不重复消费已计入 unique token 的样本，或重复量被精确计量。 |
| OPS-03 | payload 发布是 create-if-absent；同路径二次创建 fail closed。 |
| OPS-04 | `init_run` 对同 identity 可重复执行；不同 identity 一律 fail closed。 |
| OPS-05 | archive 记录具备去重键；重复条目不改变分析结论。 |
| OPS-08 | DB 不接受自相矛盾状态；迁移前后的 invariant audit 均通过。 |
| OPS-09 | 同 epoch 同语义 control publication 幂等，hash 稳定。 |
| OPS-11 | fence 失败不留下引用不明的 orphan；orphan 字节与原因可查询。 |
| OPS-12 | guarantee matrix 每格有明确等级并链接证据，README 与之一致。 |
| OPS-13 | 环境身份 mismatch 的进程不能 acquire/register。 |
| OPS-14 | cleaner 只删除 manifest 允许删除的类别；authority/audit 永不被删。 |

### 5.3 Phase 2 focused 测试

| 组 | 必测场景 |
| --- | --- |
| SCHEDULER | qsub timeout、submission unknown、live no-record、historical lag、qstat outage、重复 PBS job、超期进入 manual_review、zombie window 边界。 |
| DATA | replacement cursor continuity；stream epoch replay；固定 seed 可重复；unique token accounting；bounded streaming 或改名后的内存行为。 |
| OBJECT | create-if-absent 冲突；完整 UUID；staging + finalize；init 幂等与 identity mismatch。 |
| ARCHIVE | crash 造成重复 JSONL 时的去重；batch ID 与 primary key；离线分析一致性。 |
| SCHEMA-MIG | v3→v4 迁移；迁移前 invariant audit 失败即中止；只读打开不触发 DDL；live 在线迁移被拒绝。 |
| PUBLICATION | control command ID 幂等；重试 hash 稳定；fence 失败 artifact 进 staging 并被 GC；orphan 记录完整。 |
| MANIFEST | cleaner 在 manifest 驱动下不删除 authority/audit；telemetry 可采样删除；未知文件类别 fail closed。 |
| IDENTITY | 依赖/镜像/dataset/tokenizer/driver 任一变化都被检出并 fail closed。 |

### 5.4 Phase 2 验证阶梯

- **G7**：1 节点 mock scheduler 状态机全矩阵 + 1000 churn 有界性 + 迁移工具往返；
- **G8**：2 节点独立作业：replacement 的数据连续性证据、control 幂等、manifest 驱动 cleaner 的实机验证；
- **G9**：8+1 Miyabi 验收：至少一次 scheduler 不确定窗口、至少一次 replacement 的 cursor 连续性证据、guarantee matrix 每格有对应 artifact；
- **G9b**：workload 超过 50×10 基线时同步文档与实验报告。

Phase 2 Checker：`scripts/miyabi/check_plan03_phase2.py --mode phase2-completed`。

---

## 6. Phase 3：架构分层与生成式验证

Phase 3 **不改变任何协议语义**。通过条件是行为等价 + 可测试性提升。

### 6.1 requirement 概览

| ID | 来源 | 契约摘要 |
| --- | --- | --- |
| ARC-01 | A-06 | 建立 ports：`AuthorityStore`、`ImmutableObjectStore`、`Scheduler`、`Clock`、`AuditSink`、`TelemetrySink`；adapters：`SQLiteAuthorityStore`、`PosixObjectStore`、`PBSProScheduler`、`JsonlAuditSink`、`WandBTelemetrySink`；每个 port 至少有一个 in-memory 实现。 |
| ARC-02 | A-05 | 协议对象改为版本化 dataclass 与 tagged union（`Proposal`、`LaunchState`、`TerminalState`、`ReadResult`）；DB row 映射只存在于 adapter 层。 |
| ARC-03 | A-01 | 按 use case 拆分 `application/`（learner_cycle、merge_cycle、membership_reconcile、terminal_drain、recovery）与 `domain/`（proposal、membership、publication、selection、token_accounting）；`syncer.py`、`learner.py`、`fenced_store.py` 不再同时承担多层职责。 |
| ARC-04 | A-02 | 依赖方向单向：domain/core 不依赖 runtime/storage adapter；config 的 strategy 约束改为纯数据规则或窄接口 registry。 |
| ARC-05 | A-03 | mutation 只能通过 `FencedTransaction` capability 执行；raw connection 永不暴露给 application；read model 与 write command store 分离；CI 自动枚举 public mutator 并要求 fencing 分类。 |
| ARC-06 | A-04 | 显式命名 `FullProtocolV1`（research-grade）与 `FragmentProtocolExperimentalV0`；抽象共享对象（proposal、version coordinate、publication transaction、latest view、terminal state），fragment 的 version-vector 实现留给后续 plan。 |
| ARC-07 | A-08 | Hypothesis `RuleBasedStateMachine` + 虚拟时钟 + in-memory adapters + crash point 自动枚举 + metamorphic tests（重放、重复 ingest、顺序交换、失效读恢复）+ 有界步 liveness 断言。 |
| ARC-08 | A-10 | 每个 authority schema 版本有显式 migration；command payload 带 schema/command 版本；所有 mutation 经 command handler 产生可重放 audit event；read model 可按版本重建。 |
| ARC-09 | A-06 | 至少证明一次“只替换一个 port”的可行性（例如把 `ImmutableObjectStore` 换成 in-memory 后完整跑通协议测试），为 roadmap Stage 1 的 transport 对照做准备。 |

### 6.2 Phase 3 通过条件

- 同一 fault tape 下，重构前后的 selection 序列、event lineage、committed version 序列逐条相同；
- 全量 pytest 与 Phase 1/2 checker 全部 `PASS`；
- `syncer.py`、`learner.py`、`fenced_store.py` 的单文件行数显著下降，且每个新模块有明确的层归属声明（在 `docs/05-code-structure.md` 中体现）；
- CI 的 mutator fencing 分类检查通过，未分类的公开 mutator 数为 0；
- Hypothesis 状态机在冻结的样本预算内未发现新反例；发现的反例必须按 §1.5(10) 回到 Phase 1/2 规则处理。

Phase 3 Checker：`scripts/miyabi/check_plan03_phase3.py --mode phase3-completed`。

---

## 7. Loop Engineering

### 7.1 Phase 0

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P3-L0 | 冻结 finding 判定口径与复现标准 | triage 表与 matrix 初始化 | 判定表 artifact |
| P3-L1 | 虚拟时钟/fault tape/in-memory adapter 需求 | `fs_diloco/testing/` 底座 | 同 tape 两次重放一致性 |
| P3-L2 | H-01/02/03/05/06 的 RED 测试 | 仅注入所需接口，不改行为 | 五个 RED 测试必然失败的证据 |
| P3-L3 | 基线测量口径 | pytest/checker 实跑与基线记录 | Phase 0 Checker `PASS` |

### 7.2 Phase 1

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P3-L4 | eligible/revoke/retry 三层反例 | COR-01～03 | livelock 有界步进展、fence 指标 |
| P3-L5 | segment 归属反例 | COR-04～05 与 schema v4 列 | 逐 proposal 与全 run 对账 |
| P3-L6 | 摄取冲突反例 | COR-06 与 quarantine | frontier/pending 不变量、fragment 同规则 |
| P3-L7 | 数值域与路径逃逸反例 | COR-07～08 | schema 拒绝矩阵、CHECK 生效 |
| P3-L8 | 饥饿反例 | COR-09、COR-15 与 selection ledger | 1000 版本 fairness 指标 |
| P3-L9 | 瞬态错误反例 | COR-10 与 payload_observations | 三类 errno 注入矩阵 |
| P3-L10 | 指标语义反例 | COR-11～12 与 token ledger | 对账差 0、CSV 丢失不影响 authority |
| P3-L11 | 截断比较反例 | COR-13 与 equivalence checker | signed + CI + INCOMPARABLE 路径 |
| P3-L12 | 时钟跳变反例 | COR-14 | monotonic 断言 |
| P3-L13 | 集群验收 | 独立作业与 checker | G4→G6，docs/reports 同步 |

### 7.3 Phase 2

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P3-L14 | scheduler 歧义反例 | OPS-01 | mock + 真实 PBS 矩阵 |
| P3-L15 | 数据重放反例 | OPS-02、OPS-15 | cursor 连续性与 unique token |
| P3-L16 | 覆盖/半初始化反例 | OPS-03、OPS-04 | create-if-absent 与幂等 init |
| P3-L17 | 重复/矛盾状态反例 | OPS-05、OPS-07、OPS-08 | 迁移 audit 与 config 校验 |
| P3-L18 | 幂等与 orphan 反例 | OPS-09、OPS-10、OPS-11 | publish ledger 与 orphan 记录 |
| P3-L19 | 文档漂移与身份漂移 | OPS-12、OPS-13、OPS-14、OPS-16 | guarantee matrix、manifest、身份 gate |
| P3-L20 | 集群验收 | Phase 2 checker | G7→G9，docs/reports 同步 |

### 7.4 Phase 3

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P3-L21 | 层次违规与未分类 mutator | ARC-01、ARC-04、ARC-05 | CI 分层与 fencing 分类检查 |
| P3-L22 | 无类型协议对象反例 | ARC-02、ARC-06 | tagged union 与 adapter 边界测试 |
| P3-L23 | God module 分解 | ARC-03 | 行为等价（fault tape lineage 相同） |
| P3-L24 | 组合缺陷 | ARC-07、ARC-08、ARC-09 | Hypothesis 状态机、port 替换验证 |

每个 loop 完成后向 `reports/DOING/fsb_decoupled_diloco_plan_03/progress.md` 追加；失败先写 `failures.md`；同一 experiment 连续三次失败后停止局部试错并完成 `code_review.md`。

---

## 8. 性能、可靠性与统计口径

所有阈值必须在对应 loop 的 RED 阶段冻结，实施中不得事后放宽。

### 8.1 Phase 1

- **活性**：存在 current quorum 时，从 revoke 到下一个 committed global version 的 merge 轮数 ≤ 2；连续 fence retry 次数 ≤ 当前 selected 批次大小；`no_progress_timeout` 触发次数在该场景下严格为 0；
- **对账**：`effective_tokens_applied + discarded_tokens_total + pending_effective_tokens - processed_tokens_total == 0`，全 run 严格为 0；
- **摄取**：1000 次冲突注入中，合法 pending 丢失数为 0，frontier 违规为 0，exact replay 幂等率 100%；
- **读取语义**：`ENOENT`/`ESTALE`/`EIO` 各 100 次单次注入中，误 drop 与误删 registration 均为 0；持续失败场景必须在 grace 后确定性 drop；
- **公平性**：`stream_pool_size=12`、`quorum_max=8`、1000 版本长跑中，`max_selection_wait_versions` ≤ 冻结上限，per-stream selection rate 相对偏差 ≤ 10%，Jain index ≥ 0.9；
- **schema 开销**：strict schema 校验使摄取路径 p99 增加 ≤ `min(2ms, 既有摄取 p99 的 25%)`；样本数 ≥ 200，报告 warm-up 与聚合方式；
- **matched 比较**：≥3 次重复，报告 signed ratio 均值与 95% CI，等价界 ±5%，`|signed_ratio| > 0.20` 输出 `INCOMPARABLE_REQUIRES_AUDIT`；
- **回归**：static full 与 fragment 的既有指标与结果不变（除本计划显式改写的 token 语义）。

### 8.2 Phase 2

- 任一 launch request 处于不确定态的时长 ≤ `uncertainty_deadline`，超时后必然进入终态或 `manual_review`；
- replacement 后的 `replayed_tokens_after_replacement` 要么为 0（cursor 连续），要么与实际重复样本数一致（相对误差 0）；
- payload create-if-absent 冲突检出率 100%，静默覆盖为 0；
- 1000 churn 后 active rows / pointers / request files / used pages 的线性斜率不超过 Phase 2 RED 中冻结的门槛；
- manifest 驱动的 cleaner 对 authority/audit 类别的删除数严格为 0。

### 8.3 Phase 3

- 同一 fault tape 下重构前后的 event lineage 差异条目数为 0；
- 未分类公开 mutator 数为 0；
- Hypothesis 状态机在冻结样本预算内新反例数为 0（发现反例即按 §1.5(10) 升级处理，不在 Phase 3 内修语义）。

所有 p95/p99 必须报告样本数、warm-up、聚合方式与缺失字段。Checker 遇到核心指标缺失返回 `BLOCKED`。

---

## 9. 预计代码和文件影响

新增：

```text
fs_diloco/protocol/schemas/__init__.py
fs_diloco/protocol/schemas/versions.py
fs_diloco/protocol/schemas/proposal.py
fs_diloco/protocol/schemas/control.py
fs_diloco/protocol/schemas/membership.py
fs_diloco/protocol/schemas/errors.py
fs_diloco/protocol/selection.py
fs_diloco/storage/read_result.py
fs_diloco/storage/immutable_object.py          # Phase 2
fs_diloco/storage/artifact_manifest.py         # Phase 2
fs_diloco/storage/migrations/v3_to_v4.py       # Phase 2
fs_diloco/observability/token_ledger.py
fs_diloco/modeling/stream_cursor.py            # Phase 2
fs_diloco/core/env_identity.py                 # Phase 2
fs_diloco/testing/virtual_clock.py
fs_diloco/testing/fault_tape.py
fs_diloco/testing/in_memory_object_store.py
fs_diloco/testing/in_memory_scheduler.py
fs_diloco/testing/state_machine.py
fs_diloco/tools/workload_equivalence.py
fs_diloco/tools/matched_comparison.py
fs_diloco/tools/token_ledger_report.py
scripts/miyabi/check_plan03_phase0.py
scripts/miyabi/check_plan03_phase1.py
scripts/miyabi/check_plan03_phase2.py
scripts/miyabi/check_plan03_phase3.py
scripts/miyabi/run_plan03_fault_matrix.pbs
scripts/miyabi/run_plan03_9node_regression.pbs
docs/guarantees.md
docs/08-threat-model.md
tests/test_plan03_phase0_triage.py
tests/test_plan03_membership_liveness.py
tests/test_plan03_token_accounting.py
tests/test_plan03_ingest_conflict.py
tests/test_plan03_proposal_schema.py
tests/test_plan03_selection_fairness.py
tests/test_plan03_read_result.py
tests/test_plan03_metrics_ontology.py
tests/test_plan03_matched_comparability.py
tests/test_plan03_phase2_ops.py
tests/statemachine/test_plan03_model.py
```

修改：

```text
fs_diloco/core/config.py
fs_diloco/core/constants.py
fs_diloco/core/run_descriptor.py
fs_diloco/protocol/merge.py
fs_diloco/protocol/liveness.py
fs_diloco/protocol/membership.py
fs_diloco/protocol/dynamic_terminal.py
fs_diloco/runtime/learner.py
fs_diloco/runtime/syncer.py
fs_diloco/runtime/adoption.py
fs_diloco/runtime/launch_outbox.py
fs_diloco/runtime/pbs_scheduler.py
fs_diloco/storage/sqlite_store.py
fs_diloco/storage/fenced_store.py
fs_diloco/storage/leader_lease.py
fs_diloco/storage/schema.sql
fs_diloco/storage/schema_bootstrap.py
fs_diloco/storage/maintenance.py
fs_diloco/storage/atomic_io.py
fs_diloco/storage/paths.py
fs_diloco/modeling/hf_data.py
fs_diloco/tools/analysis.py
fs_diloco/tools/run_metrics_csv.py
fs_diloco/tools/init_run.py
fs_diloco/tools/clean_run.py
fs_diloco/tools/phase2_matched_evidence.py
configs/*.yaml
scripts/miyabi/check_plan02_phase1.py
scripts/miyabi/check_plan02_phase2.py
scripts/miyabi/*.pbs
README.md
docs/00-glossary.md
docs/02-architecture.md
docs/03-runtime-flow.md
docs/04-data-flow.md
docs/05-code-structure.md
docs/06-configuration.md
docs/07-operations.md
docs/modules/*.md
plans/00-RESEARCH_PLAN.md
plans/ref/实施计划制定与 Agent 执行经验.md
```

Phase 3 另有 `fs_diloco/application/`、`fs_diloco/domain/`、`fs_diloco/ports/`、`fs_diloco/adapters/` 的新增与相应迁移。

测试按 livelock / accounting / ingest / schema / fairness / readresult / metrics / matched / scheduler / data / migration / architecture 拆分，不用一个巨型 runtime test 替代正例、反例与 rollback 检查。

---

## 10. Checker、requirement matrix 与 artifact

完整 requirement 映射在：

```text
plans/DOING/plans/fsb_decoupled_diloco_plan_03-requirement-matrix.csv
```

矩阵中的 `implementation_contract / test_contract / gate / artifact_contract` 是冻结契约，不在实施中覆写。每条 requirement 完成关联测试后把 `status` 更新为 `complete`，并把可复核的报告或 structured artifact 写入 `evidence_path`；占位值 `TBD` 或证据缺失时不得标记 complete。

Checker stdout 只能是：

```text
PASS
PASS_WITH_FOLLOWUPS
BLOCKED
```

`PASS_WITH_FOLLOWUPS` 仅 Phase 1 staged 允许，且唯一允许的 follow-up 见 §1.3。

structured evidence 至少包含：source/config/run descriptor identity 与环境身份、schema 版本与 integrity/PRAGMA、finding 判定表、livelock 有界步证据、token 对账明细、摄取冲突与 quarantine 计数、schema 拒绝矩阵、fairness 指标、瞬态错误注入结果、matched signed/equivalence 结论、scheduler 状态机快照、manifest 与 cleaner 行为、active/physical boundedness、failure event 扫描。

artifact 命名遵循 `plans/AGENTS.md`：

```text
reports/DOING/fsb_decoupled_diloco_plan_03/artifacts/
  YYYYMMDD-HHMMSS_<experiment-id>_<pass|fail|review>.<ext>
```

大型 checkpoint 保留在 run root；reports 只保存 manifest、路径、size、验证结果与必要快照。

---

## 11. 停止、授权与文档同步

立即 `BLOCKED`：

- 发现 stale leader 或非 current incarnation 成功提交业务状态（Critical 级别，须立即停止并升级）；
- token 对账出现非零差且无法解释；
- 摄取冲突导致合法 pending 丢失；
- schema 校验被绕过（未 typed 的 payload 进入 DB）；
- CSV 或其他 best-effort telemetry 被用作 PASS 分母；
- matched 比较在工作量不等价时输出 PASS；
- 迁移过程发现既有数据违反新 CHECK 且无法判定归属；
- PBS `Exit_status=0` 但无真实 workload 输出/状态变化。

需要用户授权：

- 对 live run 执行 v3→v4 迁移或删除 live DB/checkpoint/claim；
- `qdel` 旧 syncer/learner；
- 修改 live job 的 source/config；
- 放宽本计划冻结的任何可靠性/性能阈值或 fairness 上限；
- 把某个 High finding 判定为 `rejected-with-evidence` 或 `deferred-with-justification`（必须在 progress.md 中记录理由与证据，并由用户确认）；
- 开启 `payload_verify_mode=off` 或 `proposal_schema_mode=compat` 运行正式实验。

文档同步：

- Phase 0：只写 reports 与 finding 判定，不把未完成能力写成已实现；
- Phase 1 PASS 后：`docs/00-glossary.md`（token 本体）、`docs/03-runtime-flow.md`、`docs/04-data-flow.md`、`docs/06-configuration.md`、README 的指标与停止条件说明；
- Phase 2 PASS 后：`docs/guarantees.md`、`docs/08-threat-model.md`、`docs/07-operations.md`（scheduler manual_review、manifest 驱动 cleaner、迁移工具）、fragment experimental 标注；
- Phase 3 PASS 后：`docs/02-architecture.md`、`docs/05-code-structure.md`、`docs/modules/*.md` 的分层描述；
- 具体 job/run/数字只写 reports；
- 只有代码经 8+1 节点且 workload 超过 50×10 基线验证时，按根 `AGENTS.md` 更新相应 verified behavior / experiment result。

---

## 12. 发布前自检

### Phase 0

- [ ] `reports/DOING/fsb_decoupled_diloco_plan_03/{progress.md,failures.md,code_review.md,artifacts/}` 已按 `plans/AGENTS.md` 创建；
- [ ] 9 个 High、15 个 Medium、10 个架构、6 个文档条目全部有判定与归属；
- [ ] H-01/02/03/05/06 各有一个在当前代码上必然失败的测试；
- [ ] 虚拟时钟 / fault tape / in-memory adapter 底座可用且重放一致；
- [ ] 全量 pytest 与既有 Plan 01/02 checker 的**实跑**结果已记录；
- [ ] Phase 0 Checker `PASS`。

### Phase 1

- [ ] dynamic eligible query 内联 membership fence，static/fragment 逐字节回归；
- [ ] revoke/supersede 与旧 proposal 终结同事务且写入 ledger；
- [ ] fence retry 逐行裁决，livelock 场景在 ≤2 轮内前进；
- [ ] mid-cycle replace 的 effective/discarded 归属正确，三种 adoption 策略各自的契约有测试；
- [ ] merge weight 只使用 effective token，`processed = effective + discarded` 对账为 0；
- [ ] 摄取事务顺序为 validate→insert→adjudicate→supersede→frontier，冲突不丢 pending；
- [ ] quarantine 保留原字节且不被 cleaner 删除；
- [ ] 版本化 schema 覆盖 proposal/heartbeat/admission/latest/terminal，canonical path 由 identity 推导；
- [ ] 每个 raw merge weight 有限且为正，DB CHECK 生效；
- [ ] 两阶段 selection 与 fairness 指标落库，长跑等待上限满足；
- [ ] 结构化 ReadResult 覆盖所有权威读取路径，单次瞬态错误不产生破坏性动作；
- [ ] token ledger 为权威计数，停止条件语义显式，CSV 降级为 telemetry；
- [ ] matched 比较输出 signed + equivalence + CI，异常差异进入 `INCOMPARABLE_REQUIRES_AUDIT`；
- [ ] 进程内 timeout 全部 monotonic；
- [ ] 8+1 Miyabi regression 通过且 token 对账为 0；
- [ ] Phase 1 completed Checker `PASS`。

### Phase 2

- [ ] scheduler 不确定状态机有 deadline 与 `manual_review` 出口；
- [ ] stream replacement 的数据连续性或重放计量成立；
- [ ] payload create-if-absent，完整 UUID/内容 hash；
- [ ] `init_run` 幂等，identity mismatch fail closed；
- [ ] archive 去重契约与离线分析一致；
- [ ] 每个 config section 有 `validate()`；
- [ ] schema CHECK/枚举/迁移与迁移前 audit 完整；
- [ ] control publication 幂等，lease 时钟在取锁后重采样；
- [ ] orphan artifact 有 staging 与快速 GC；
- [ ] `docs/guarantees.md` 与 `docs/08-threat-model.md` 完成，README 标注 fragment experimental；
- [ ] 环境/dataset/tokenizer 身份冻结并被 checker 校验；
- [ ] artifact manifest 驱动 cleaner；
- [ ] Phase 2 Checker `PASS`。

### Phase 3

- [ ] ports/adapters 建立且每个 port 有 in-memory 实现；
- [ ] 协议对象为版本化 dataclass / tagged union，DB 映射只在 adapter；
- [ ] application/domain 拆分完成，God module 消解；
- [ ] 依赖方向单向，core 不依赖 runtime adapter；
- [ ] mutation 只经 `FencedTransaction`，CI 强制 fencing 分类；
- [ ] full/fragment 协议命名与共享抽象完成（fragment version-vector 明确为后续 plan）；
- [ ] Hypothesis 状态机 + crash 枚举 + metamorphic + 有界步 liveness 断言可用；
- [ ] 同 fault tape 下行为等价证据完整；
- [ ] Phase 3 Checker `PASS`。
