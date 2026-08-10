# FS-Based Decoupled DiLoCo 全面代码与架构审查报告

审查对象：`UnbearableFate/fsb_decoupled_diloco` 分支 `codex/fsb_decoupled_diloco_plan_02`  
审查日期：2026-08-07（Asia/Tokyo）  
仓库链接：<https://github.com/UnbearableFate/fsb_decoupled_diloco/tree/codex/fsb_decoupled_diloco_plan_02>  
仓库内最后一份已读取的最终修复提交：`10f371b4e3475e5045d6e8b0632ba85ecf98496d`

## 1. 结论

这不是一个“只有 happy path 的研究脚本”。当前代码已经建立了若干正确而且难度较高的系统边界：DB-first authority、epoch-unique publication、事务内 leader fencing、动态成员的 placement/stream/admission fencing、可修复的 learner-facing cache、current-only retention，以及独立 PBS 作业启动和故障证据留存。仓库文档也明显优于一般研究原型，能够解释运行目录、协议角色、恢复语义和已知实验结论。

但是，当前版本仍不适合直接冻结为论文主实验基线。审查确认了：

| 类别 | 数量 | 结论 |
|---|---:|---|
| Critical | 0 | 未发现能够直接绕过 epoch/leader fencing、让 stale leader 成功提交新权威状态的路径 |
| High | 9 | 其中 5 项可影响训练活性或聚合正确性，4 项会削弱实验结论可信度 |
| Medium | 15 | 主要涉及恢复边界、数据语义、时间源、幂等性、状态约束和可复现性 |
| 架构/文档债务 | 10 | 当前大型模块和隐式 dict 协议会显著提高下一阶段研究迭代风险 |

最应优先修复的三个问题是：

1. **动态成员换代后，旧 incarnation 的 pending proposal 可能被反复选中，导致 syncer 永久 livelock。**
2. **mid-cycle `replace` 会覆盖前半段训练结果，但 proposal 仍把前半段 token 计入 merge weight。**
3. **proposal 摄取先 supersede 旧 pending、再执行 `INSERT OR IGNORE`；唯一约束冲突时会同时丢失旧 pending 并推进 frontier。**

建议把这三项以及严格 proposal schema、选择公平性、共享文件系统瞬态错误处理、指标语义统一，定义为下一轮实验前的 P0 门禁。

## 2. 审查方法与限制

### 2.1 覆盖范围

本次审查交叉阅读了以下主要区域：

- 配置与运行身份：`fs_diloco/core/config.py`、`run_descriptor.py`、`constants.py`
- learner：`fs_diloco/runtime/learner.py`、`adoption.py`
- syncer：`fs_diloco/runtime/syncer.py`、`syncer_ha.py`
- 权威状态和存储：`sqlite_store.py`、`fenced_store.py`、`leader_lease.py`、`schema_bootstrap.py`、`schema.sql`、`maintenance.py`、`atomic_io.py`
- 动态成员与终止：`membership.py`、`dynamic_terminal.py`、`liveness.py`
- merge 和 outer optimizer：`merge.py`、`outer_optim.py`
- 数据和模型：`hf_data.py`、`hf_model.py`
- PBS 控制面：`pbs_scheduler.py`、`launch_outbox.py`、独立启动工具和 PBS 脚本
- 分析和观测：`metrics.py`、`analysis.py`、`run_metrics_csv.py`
- 设计文档、研究计划、Phase 0–2 证据、现有代码审查报告和相关测试

此外，比较分支与 `master` 后可见该分支领先 53 个提交，并对 Plan 02 涉及的主要新增/修改文件进行了重点追踪。

### 2.2 限制

本报告是**源码、文档、测试和已留存运行证据的静态审查**。由于当前执行环境不能直接通过 GitHub 域名 materialize 仓库，本次没有独立运行 pytest、Miyabi 作业或故障注入。仓库自身的最终修复记录报告了 `495 passed in 25.23s`，但该结果是仓库留存证据，不是本次审查重新执行得到的结果。

这一区分很重要：现有测试可以证明许多已枚举场景通过，却不能自动排除跨状态机组合缺陷。本报告最严重的动态成员 livelock 就属于“单独的 revoke、selection 和 final fence 都正确，但组合后的 retry 策略不活”的问题。

### 2.3 严重度定义

- **Critical**：可以破坏权威状态安全性、跨 epoch 成功写入，或导致不可恢复的数据/模型静默损坏。
- **High**：可以稳定触发 livelock、错误聚合、有效计算统计错误，或使论文主要结论无效。
- **Medium**：在特定故障、规模或配置下导致恢复不完整、数据重复、资源泄漏、难以诊断或可复现性下降。
- **Low/Architecture**：主要增加维护成本、误用概率和未来扩展风险。

## 3. 值得保留的设计

### 3.1 DB-first authority 是正确的恢复边界

full-mode 将 committed SQLite row 作为权威状态，而把固定 `latest.json`、`stop.json` 和 `summary.json` 视为可重建 cache。这比“最后写出的文件就是 checkpoint”清晰得多，也让 crash matrix 能围绕明确的 commit point 验证。

### 3.2 最终业务提交重新验证 leader 和 membership fence

动态成员的 proposal 在摄取时验证 admission，在最终 global commit 事务中又重新验证 incarnation、placement epoch、stream epoch、admission generation/token。这个双重验证是必要的，能够阻止“摄取后、提交前被替换”的旧 learner 成功参与新版本。

### 3.3 epoch-unique publication 限制 stale leader 的破坏面

旧 leader 即使继续完成慢 I/O，也只能在旧 epoch namespace 中留下 orphan artifact；新 epoch 的事务 fencing 决定权威状态。这一方向比依靠固定路径上的最后写者获胜更可靠。

### 3.4 bounded discovery 和 reference-driven retention 已形成系统贡献雏形

full 模式每 learner 一个固定 pointer，fragment 模式每 `(learner, fragment)` 一个固定 pointer；活跃 DB 与归档历史分离；checkpoint/payload 由引用关系回收。这些机制比单纯“使用共享文件系统传参数”更有研究价值。

### 3.5 文档能够披露负面结果和模式差异

研究计划明确记录 fragment payload 更小但端到端更慢、fragment resume 尚未实现、固定 local-step 与固定 outer-step 不等价等事实。这种披露应继续保留，并扩展为正式的保证矩阵。

## 4. High 严重度问题

## H-01 动态成员换代后，旧 proposal 可造成确定性 livelock

**类型：活性错误；置信度：高**

### 证据链

1. `SQLiteStore.eligible_updates()` 只根据 `status`、`base_global_version` 和 staleness 查询 pending update，没有 join `learner_instances`、`placements`、`streams` 来过滤当前 incarnation。
2. `revoke_dead_instances()` 会撤销 learner instance 并释放 placement/stream，但不会把该 incarnation 的 pending/selected update 一并终结。
3. `select_one_per_dynamic_member()` 在同一 stream/placement 冲突时，默认按 `(local_step_end, committed_at, update_id)` 偏好较“新”的 proposal，而不是优先 current `stream_epoch`/`placement_epoch`。
4. `commit_full_merge()` 会正确地在最终事务中拒绝已不是 current member 的 proposal。
5. syncer 捕获 `DynamicMembershipFenceError` 后，会把整批 selected update 重置为 pending，然后进入下一轮。
6. `tests/test_plan02_phase2_dynamic.py` 已经覆盖“selected update 所属成员被 revoke 后，最终 commit 必须失败”，但没有覆盖 syncer 的 `reset_selected_to_pending → reselect` 活性循环。

### 可复现失败路径

- incarnation A 占用 stream 0，发布 `local_step_end=100` 的 pending proposal；
- A 失去 heartbeat，被 revoke；
- replacement B 复用 stream 0，`stream_epoch+1`，当前只训练到 step 50；
- eligible query 同时返回 A 和 B；
- selector 因 A 的 `local_step_end` 更大而选 A；
- final commit 因 membership fence 拒绝 A；
- catch 分支把 A 重新设为 pending；
- 下一轮再次选择 A。

如果该批次因 A 而整体回滚，global version 不前进，A 也不会因 version staleness 自然过期。因此该循环可以无限持续，最终只由 `no_progress_timeout` 终止，训练无法恢复。

### 修复建议

采用三层防线，不要只改一处：

1. **eligible query 只返回 current incarnation：**

```sql
SELECT u.*
FROM updates AS u
JOIN learner_instances AS li
  ON li.instance_id = u.learner_instance_id
JOIN placements AS p
  ON p.placement_id = li.placement_id
JOIN streams AS s
  ON s.stream_id = li.stream_id
WHERE u.status = 'pending'
  AND li.status IN ('admitted', 'draining')
  AND p.current_instance_id = li.instance_id
  AND p.current_placement_epoch = li.placement_epoch
  AND s.current_instance_id = li.instance_id
  AND s.current_stream_epoch = li.stream_epoch
  AND u.placement_epoch = li.placement_epoch
  AND u.stream_epoch = li.stream_epoch;
```

2. **revoke 与旧 proposal 终结放入同一事务：**

```sql
UPDATE updates
SET status = 'dropped', drop_reason = 'revoked_incarnation'
WHERE learner_instance_id = :instance_id
  AND status IN ('pending', 'selected');
```

3. **fence retry 不要把全部 selected 无条件 reset：**重新查询每个失败 row 的 membership；无效 row 直接 drop，仍有效的 row 才 reset。记录 `membership_fence_drop_count` 和具体 epoch 差异。

### 必须新增的测试

- A(step 100) revoke → B(step 1) 同 stream replacement → syncer 下一次 merge 必须选择 B 并前进版本。
- revoke 与 selection 并发的两种顺序。
- selected batch 中只有一个旧 incarnation 时，不得导致其他合法 proposal 被无限回滚。
- 模型化测试：对 admit/revoke/publish/select/commit 的随机序列断言“存在 current quorum 时最终必有进展”。

---

## H-02 mid-cycle `replace` 会给已被覆盖的计算分配 merge weight

**类型：聚合语义错误；置信度：高**

### 证据链

full learner 的一个 upload interval 内维护：

- `losses`
- `interval_tokens`
- `interval_examples`
- `interval_start_step`
- `base_global_version`

当 `poll_latest_during_inner_steps=true` 且 adoption strategy 为 `replace` 时，learner 会在 interval 中途加载新 global，覆盖本地模型并重建 inner optimizer。代码会更新 `base_global_version`，并通过 `MidCycleAdoptionTracker` 记录 adoption 次数和 `base_switched_at_step`；但是不会清空 `losses`、`interval_tokens` 和 `interval_examples`。

最终 proposal 中的参数只包含“最后一次 replace 之后”的本地训练效果，前半段训练已经被覆盖；然而 `tokens_this_update`、平均 loss 和 examples 仍覆盖整个 interval。syncer 的 `normalized_update_weights()` 直接按 `tokens_this_update / staleness_penalty` 赋权，并不使用 `base_switched_at_step` 修正。

### 影响

- merge weight 高估该 proposal 的有效本地计算量；
- `total_seen_tokens` 和 update utilization 高估实际进入模型的 token；
- 不同 adoption 策略的质量/效率比较不公平；
- 同一 proposal 的 `base_global_version` 与 token 归属不再满足清晰的不变量。

### 修复建议

定义并强制如下不变量：

> proposal 中被 merge 的参数变化，必须能够由 `effective_tokens` 所代表的训练段解释；被 replace 覆盖的计算只能记为 processed/discarded，不得进入 merge weight。

可选实现：

1. `replace` 发生时结束当前 segment；把已累计量记入 `discarded_by_mid_cycle_replace_tokens`，然后重置有效 interval accumulator。
2. proposal 增加：
   - `processed_tokens_this_interval`
   - `effective_tokens_this_update`
   - `discarded_tokens_this_interval`
   - `segment_count`
   - `last_base_switch_step`
3. merge weight 使用 `effective_tokens_this_update`。
4. 简化方案：禁止 `replace` 在 upload interval 中途发生，只允许 cycle boundary adoption；这会降低响应性，但语义最清楚。

### 必须新增的测试

构造两步 interval：step 1 后 replace、step 2 后 publish。断言 proposal 参数只受 step 2 影响，merge weight 也只等于 step 2 token；step 1 token 必须进入 discarded 指标。

---

## H-03 `INSERT OR IGNORE` 之前先 supersede，会丢失合法 pending proposal

**类型：状态机/事务顺序错误；置信度：高**

### 证据链

`SQLiteStore.insert_update_metadata()` 当前事务顺序为：

1. 验证 frontier 不是同一个 `update_id`；
2. 将同 learner 的其他 pending row 全部标记为 `dropped/superseded`；
3. 执行 `INSERT OR IGNORE INTO updates ...`；
4. 无论 insert 是否发生，都更新 `proposal_frontiers`；
5. commit，并以 `cur.rowcount > 0` 返回。

`updates` 表还有 `UNIQUE(learner_id, local_step_end, base_global_version)`。

### 可复现失败路径

- DB 已有合法 pending `u_old`，唯一键为 `(learner, step=100, base=5)`；
- pointer 被替换为不同 `update_id=u_new`，但仍具有同一唯一键；
- 事务先把 `u_old` 标为 superseded；
- `INSERT OR IGNORE` 因唯一键冲突忽略 `u_new`；
- frontier 被推进为 `u_new`；
- 结果是没有 pending update，且后续同 pointer 会因 frontier 命中而不再处理。

这也会在 update ID 冲突、异常重放或手工恢复错误时发生。

### 修复建议

把“是否接受新 proposal”放在任何破坏性变更之前：

1. 用普通 `INSERT`，捕获 `sqlite3.IntegrityError`。
2. 冲突后查询现有 row：
   - 若所有协议字段、文件 hash 和 identity 完全一致，作为 exact replay，保持原 frontier；
   - 若语义不一致，标记为 `quarantined/conflict`，不要 supersede 旧 pending；
   - 不允许静默 ignore。
3. 只有 insert 成功后，才 supersede 更旧 pending 并更新 frontier。
4. fragment update 路径采用同样规则。

推荐事务顺序：`validate → insert candidate → adjudicate conflict → supersede older → update frontier → commit`。

---

## H-04 proposal 缺少严格的版本化 schema 和数值域验证

**类型：完整性/数值安全；置信度：高**

当前 protocol 大量使用 `dict[str, Any]`，校验分散在 learner、ingest、SQLite 和 merge 层。可见路径虽然检查部分 identity 和必填字段，但没有形成一处可审计的严格 schema，尤其缺少以下统一约束：

- `tokens_this_update > 0`、`inner_steps > 0`
- 所有 loss、norm、timestamp、resource metric 必须 finite
- `local_step_start < local_step_end`，且跨度与 `inner_steps` 一致
- `created_at <= committed_at`，并限制异常未来时间
- file path 必须位于该 learner/admission 的 canonical payload 目录
- path 必须是 regular file，不是 symlink/目录
- file size 和 SHA-256 在摄取或使用前必须验证
- tensor shape、dtype、numel 与 parameter index 一致
- `base_global_version`、epoch、generation 不得为负
- 未知字段的兼容策略和 `format_version` migration 规则

`normalized_update_weights()` 只验证所有 raw weight 的总和为正；若单个 token 为负、NaN 或极大值，可能产生负权重、NaN 或数值失真。

### 修复建议

- 建立 `protocol/schemas/`：`UpdateProposalVn`、`HeartbeatVn`、`AdmissionVn`、`LatestControlVn`、`TerminalVn`。
- 解码后转为 frozen dataclass/Pydantic-like typed object，再进入 DB。
- canonical path 由 identity 计算，不信任 payload 中任意 `file_path`。
- 每个 raw weight 单独要求 `math.isfinite(weight) and weight > 0`；使用 `math.fsum`。
- DB 增加 CHECK，例如：

```sql
CHECK(tokens_this_update > 0),
CHECK(inner_steps > 0),
CHECK(local_step_end > local_step_start),
CHECK(base_global_version >= 0),
CHECK(status IN ('pending','selected','applied','dropped','quarantined'))
```

- hash 验证可配置为 `always / sampled / disabled`，但论文主实验必须启用或明确说明信任边界。

---

## H-05 `quorum_max` 下存在确定性的 contributor ID 饥饿

**类型：公平性/训练偏差；置信度：高**

`select_one_per_learner()` 在 `most_recent_per_learner` 下先按 learner ID 排序，再截取前 `quorum_max`；动态路径最终按 `(stream_id, learner_id)` 排序后截断。只要 eligible contributor 数持续大于 `quorum_max`，低 ID/低 stream ID 可以反复进入每个 batch，高 ID contributor 的 proposal 则不断被自己的新 pointer supersede，却从未被应用。

### 影响

- 动态 stream pool 大于目标 quorum 时，一部分数据 shard 可能长期不进入训练；
- token-weighted merge 并不能修复“从未被选中”的 contributor；
- churn/heterogeneity 实验可能把选择偏差误解释成速度或 stale 效果；
- selection fairness 与模型数据分布耦合。

### 修复建议

将“每 contributor 选哪个 proposal”和“本轮选哪些 contributor”拆成两个阶段：

1. per-contributor proposal policy；
2. contributor admission policy。

第二阶段采用以下任一可审计方案：

- deficit round robin；
- oldest-unserved-first；
- 以 global version 为种子的 rotating hash；
- age × staleness × contribution deficit score。

新增指标：

- per-stream selection rate
- max selection wait versions
- Jain fairness index
- selection entropy
- applied/produced token ratio per stream

---

## H-06 一次共享文件系统读失败会被当成永久协议失败

**类型：故障语义错误；置信度：高**

`safe_read_json()` 把 `OSError` 和 JSON parse error 都折叠为 `None`。动态 registration ingest 对非 dict 结果立即 `unlink`。normal proposal 路径对一次 `Path.exists()==False` 立即标记 `missing_file/dropped`；selected 后再次检查仍采用一次观察即永久 drop。共享文件系统中的瞬态 lookup/read 错误、短暂 visibility 延迟和真正 malformed payload 因此没有区分。

动态 terminal 路径已经存在 visibility grace 概念，但正常 proposal 摄取/选择没有同等级保护。

### 修复建议

返回结构化读取结果：

```text
OK(payload, fingerprint)
NOT_FOUND
TRANSIENT_IO(errno)
MALFORMED(reason)
IDENTITY_MISMATCH(reason)
```

并采用状态机：

- `NOT_FOUND/TRANSIENT_IO`：记录 `first_missing_at`，经过 visibility grace 且多次独立观察后才 drop；
- `MALFORMED`：移动到 quarantine，保留原字节和诊断；
- registration request 不得因一次 OSError 被删除；
- destructive unlink/drop 前要求稳定文件年龄或重复观测；
- 故障注入覆盖 `ESTALE`、`EIO`、`ENOENT` 短暂恢复。

---

## H-07 PBS scheduler ambiguity 状态机可能错误释放或永久占用容量

**类型：控制面活性/资源泄漏；置信度：中高**

`LearnerLaunchOutbox.reconcile()` 的关键行为：

- 已有 job ID 且查询为 `finished`/`no_record` 时立即标记 failed 并释放 reservation；
- `query_failed`/`unknown` 不进入统一 deadline 处理，可长期保持原状态；
- live qstat 与 historical qstat 的“尚不可见”窗口没有明确的 zombie/uncertain 期限；
- request TTL 对已有 job ID 的不确定状态没有形成闭环。

结果可能是：

- accounting lag 被误认为 job 已消失，过早允许重复 replacement；
- qstat 长期失败时 reservation 永久占用，scale-out 停止；
- `no_record` 在不同 PBS 配置下含义不稳定。

### 修复建议

为每个 request 持久化：

- `first_scheduler_uncertain_at`
- `last_positive_scheduler_evidence_at`
- `uncertainty_deadline`
- `terminal_evidence_source`

状态机统一为：`planned → submitting → submission_unknown → submitted/started → terminal_uncertain → admitted/failed/expired/manual_review`。

只有在 live+historical 查询均无记录、超过明确 zombie window，并且没有 registration receipt 时才释放。超过 deadline 但仍 query_failed 时进入 `manual_review`，不能静默保留也不能自动重提。

---

## H-08 token 指标命名和分母不足以支撑论文结论

**类型：实验有效性；置信度：高**

syncer 的 `total_seen_tokens` 实际只在 global commit 成功前加上 selected proposal 的 `tokens_this_update`。它并不等于：

- 所有 learner 实际处理的 token；
- 所有已发布 proposal token；
- 去重后的数据 token；
- 对最终参数真正有效的 token；
- GPU-hours 或有效算力。

结合 H-02，某些被 replace 覆盖的 token 甚至会进入 selected token，但没有体现在 proposal 参数中。因此 `stop_after_global_tokens`、update utilization、accepted-token efficiency 和静态/动态比较都可能使用了不同于读者直觉的分母。

此外，共享 CSV 是 best-effort telemetry，不应承担权威 denominator。`run_metrics_csv.py` 等工具对 manifest/DB 的 fallback 逻辑也需要明确“完整性等级”。

### 修复建议：建立指标本体

至少分别记录：

- `processed_tokens_total`
- `unique_data_tokens_estimate`
- `proposal_tokens_produced`
- `proposal_tokens_ingested`
- `proposal_tokens_eligible`
- `proposal_tokens_selected`
- `effective_tokens_applied`
- `discarded_tokens_by_reason`
- `replayed_tokens_after_replacement`
- `gpu_seconds`、`node_seconds`

每个 stop criterion 必须声明使用哪个 token 语义。论文图表同时给出 quality-vs-unique-token、quality-vs-effective-token 和 quality-vs-GPU-hour，而不能只使用 `total_seen_tokens`。

权威计数应来自 SQLite event/commit ledger；CSV/JSONL 只作为可丢失 telemetry。

---

## H-09 matched static/dynamic 性能门禁会隐藏不可比运行

**类型：研究方法错误；置信度：高**

仓库文档记录 matched static/dynamic 运行分别约为 101.949 秒和 47.348 秒，并按冻结公式将“正 overhead”截断为 0，从而通过 `<5%` 门槛。即使动态路径确实更快，超过 2 倍的差异也首先意味着需要审计：

- timer anchor 是否一致；
- cache/JIT/数据下载是否一致；
- learner 是否处理了相同数量的有效 token；
- selected contributor 和 quorum 节奏是否一致；
- GPU/CPU placement 是否一致；
- 失败/替换过程是否改变了工作量；
- 是否有 warm cache、队列、启动或 teardown 被计入一侧。

`max(0, dynamic/static - 1)` 只能回答“有没有正 overhead”，不能证明两个运行可比，也不能在差异异常时自动给 PASS。

### 修复建议

- 始终报告 signed delta，不截断原始差异；
- 先运行 workload equivalence checker，再计算 overhead；
- 比较区间限定为相同 terminal event anchor；
- 同时报告 processed/effective tokens、outer steps、selected count、GPU-seconds；
- 至少 3 次重复并给置信区间；
- 采用 non-inferiority/equivalence test，而不是单样本 ratio；
- 当绝对差异超过预设 sanity bound（例如 20%）时，无论方向都进入 `INCOMPARABLE_REQUIRES_AUDIT`。

## 5. Medium 严重度问题

| ID | 问题 | 影响 | 建议 |
|---|---|---|---|
| M-01 | `no_progress_timed_out()` 使用 `time.time()` | NTP/时钟跳变可提前或推迟终止 | 进程内 timeout 全部改用 `time.monotonic()`；wall time 只用于持久审计 |
| M-02 | `data.streaming=true` 的实现仍会 materialize rows/tokens/blocks | 对大型或无限 iterable dataset 可能 OOM，配置名误导 | 实现真正 bounded shuffle buffer 和在线 packing，或改名为 `dataset_iterable_but_materialized` |
| M-03 | replacement 复用 stream 时从数据流开头和初始 RNG 重启 | 重复样本、数据分布偏移，dynamic quality 比较失真 | 为每 stream 持久化 data cursor、shuffle/RNG state 或确定性 global sample index |
| M-04 | update UUID 只保留 12 个 hex 字符，payload 写入使用 replace 语义 | 大规模长跑下碰撞概率高于完整 UUID；碰撞会破坏“immutable payload”假设 | 使用完整 UUID/内容 hash；以 `O_EXCL`/link-based create-if-absent 发布 immutable object |
| M-05 | `init_run` 失败后可能留下半初始化 root，而重试拒绝已有目录 | 运维需要手工判断和清理，易误删证据 | 使用 staging root + atomic finalize marker；支持 identity-matched idempotent resume/repair |
| M-06 | archive 采用“append+fsync 后 DB prune”，crash 可造成 JSONL 重复 | history 至少一次，不是 exactly once；离线分析需自行去重 | 每批 archive 带 transaction/batch ID 和 row primary key；consumer 明确去重 contract |
| M-07 | CSV 多进程 append 缺少权威同步/完整性语义 | 行交错、尾部截断、丢失会污染分析 | per-process logs 后离线合并；或单 writer；CSV 永远不作为 authority |
| M-08 | 配置字段的 runtime type/domain 检查不完整 | YAML 弱类型和手工构造 `Config()` 可绕过假设 | 每 section 建 `validate()`；对 duration、count、enum、cross-field invariant 全覆盖 |
| M-09 | `schema.sql` 缺少 FK、CHECK 和严格 status enum | 应用 bug 可写入自相矛盾状态，恢复时难以区分 | 增加 constraints；迁移时先 audit 旧数据；开启 `foreign_keys=ON`（若协议允许） |
| M-10 | dynamic selector 的 `oldest_pending` preference 先偏好更小 local step，再看 commit time | 与名称“oldest pending”不完全一致，跨 incarnation 时更混乱 | 明确定义 age key；通常以 `committed_at`/first_seen 为主，不使用不可比 local step |
| M-11 | 同 epoch/同语义的 control publication 幂等规则分散 | retry 可能产生不同 artifact/hash，修复逻辑复杂 | 为 control command 分配 deterministic command ID；publish ledger 记录 intent/result |
| M-12 | leader lease acquire 的 wall-clock snapshot 与 DB lock wait 分离 | 极端锁等待后获得 token 时本地安全预算可能偏紧/偏松 | 在事务实际获得写锁后重新采样时间；同时记录 DB wait |
| M-13 | membership fence 在 checkpoint 写后失败会留下 orphan artifacts | H-01 可放大为持续 I/O 和存储增长 | commit intent/reservation 先验证；失败 artifact 放 staging 并快速 GC；记录 orphan reason |
| M-14 | fragment 模式没有与 full 同等级的 resume/HA/authority 语义 | 顶层“系统支持 fragment”容易被误读为同等保证 | 将 fragment 标为 experimental；完成 version-vector authority 后再进入主结论 |
| M-15 | 环境身份未与 source identity 同等级冻结 | 依赖、CUDA/PyTorch/HF dataset revision 漂移可改变实验 | 记录 lock hash、container/image digest、dataset revision、tokenizer hash、driver/runtime |

## 6. 架构层面的不足

## A-01 `syncer.py`、`learner.py` 和 storage 模块已成为 God modules

分支变更中 `fenced_store.py` 约 3,192 行新增，`syncer.py` 增加约 1,233 行，`learner.py` 增加约 598 行；当前单文件同时处理 orchestration、协议决策、I/O、状态转移、metrics 和恢复。其直接后果是：

- 状态机边界只能靠局部阅读理解；
- 故障注入需要 patch 深层函数；
- full/fragment/static/dynamic/HA 组合产生大量条件分支；
- 单个修改容易跨越安全、活性和观测边界。

建议按 use case 分解：

```text
application/
  learner_cycle.py
  merge_cycle.py
  membership_reconcile.py
  terminal_drain.py
  recovery.py

domain/
  proposal.py
  membership.py
  publication.py
  selection.py
ports/
  authority_store.py
  object_store.py
  scheduler.py
  clock.py
adapters/
  sqlite_authority.py
  posix_object_store.py
  pbs_scheduler.py
  hf_dataset.py
```

## A-02 依赖方向反转不彻底

`core` 层仍会触达 storage/runtime 细节，配置验证也依赖具体 adoption 实现。理想方向应是：domain/core 不依赖 runtime/storage adapter；runtime 组合 port 和 adapter。

建议把 config 的 strategy constraint 变成纯数据规则，或由 strategy registry 通过窄接口注册，不让 core import runtime implementation。

## A-03 `LeaderBoundSQLiteStore` 的代理/手工方法绑定容易发生 fencing 漏洞

当前通过 wrapper/proxy、共享 connection 和方法委派把 leader token 注入 store。随着 SQLiteStore 新增 mutation，开发者必须记得：

- 在 wrapper 中暴露；
- 选择正确 transaction helper；
- 进行 lease safety check；
- 进行 epoch fence；
- 不让调用者绕回 raw connection。

这类“每个新方法都靠人工分类”的设计会随规模增长失效。

建议：

- mutation command 只能通过一个 `FencedTransaction` capability 执行；
- raw connection 永不暴露给 application；
- read model 和 write command store 分离；
- CI 自动枚举 public mutator，要求每个 mutation 有 fencing classification。

## A-04 full 与 fragment 是两套未统一的协议

full 拥有 DB-first resume、HA、dynamic membership、epoch controls；fragment 仍依赖不同 latest 语义、独立循环和较弱恢复。这不是简单 feature flag，而是两个系统。

短期应显式命名：

- `FullProtocolV1`：research-grade
- `FragmentProtocolExperimentalV0`：无 HA/无 crash-consistent resume

长期抽象共同对象：proposal、version coordinate、publication transaction、latest view、terminal state。full 的 version coordinate 是 scalar，fragment 是 version vector，而不是在单体 loop 中复制代码。

## A-05 协议对象是无类型 dict，状态值是字符串

大量 `dict[str, Any]` 和字符串 status 使非法状态可以传播到很深的位置才失败。建议以版本化 dataclass + tagged union 表示：

```python
Proposal = FullProposalV2 | FragmentProposalV2
LaunchState = Planned | Submitting | Submitted | Started | Admitted | Failed
TerminalState = Open | Draining | Closed | Terminal
ReadResult = Ok | NotFound | TransientError | Malformed
```

DB row 映射应只存在于 adapter 层。

## A-06 外部系统没有形成清晰 ports/adapters

POSIX FS、SQLite、PBS、Hugging Face、W&B、clock 和 random source 直接进入主循环，导致：

- 难以对比 FS transport 与 RPC/object-store transport；
- 难以做 deterministic state-machine simulation；
- 难以验证 PBS 之外的 scheduler；
- benchmark 代码与生产 protocol 耦合。

下一阶段研究要比较“同算法、不同 transport”，因此 port 抽象已经不是一般代码洁癖，而是实验设计所必需。

## A-07 authority、audit log 和 telemetry 的边界仍不够清楚

当前有 SQLite、epoch JSON、history JSONL、CSV、per-process JSONL、W&B。文档说明 SQLite 权威，但部分分析和 cleanup 仍需要知道哪些历史不可重建。此前仓库也实际修复过 cleaner 删除 authority archive 的问题。

建议为每类 artifact 加机器可读 manifest：

| 类别 | 可丢失 | 可重建 | retention | 用于 correctness |
|---|---|---|---|---|
| authority | 否 | 部分 | 强制 | 是 |
| audit history | 否 | 否 | 归档 | 是/复核 |
| telemetry | 是 | 否 | 可采样 | 否 |
| cache/view | 是 | 是 | current-only | 否 |
| payload | 条件 | 取决于引用 | 引用驱动 | 是 |

cleaner 只根据 manifest policy 工作，而不是硬编码文件名。

## A-08 测试策略仍以场景枚举为主，缺少生成式状态机验证

现有 495 tests 和大量 Phase checker 是优势，但主要覆盖已想到的路径。H-01 展示了组合缺陷：revoke test、selector test、final fence test 各自通过，整体仍可 livelock。

建议增加：

- Hypothesis RuleBasedStateMachine；
- deterministic virtual clock；
- in-memory authority/object/scheduler adapters；
- crash point 自动枚举；
- metamorphic tests：重放、重复 ingest、顺序交换、失效读恢复；
- liveness bounded-step assertion，而不只断言最终 transaction rollback。

## A-09 缺少一份 mode × guarantee 的正式矩阵

顶层 README 同时描述 full、fragment、static、dynamic、HA，但不同组合的保证并不等价。建议新增 `docs/guarantees.md`：

| 模式 | 单 writer safety | stale leader fencing | crash-consistent resume | dynamic membership | bounded discovery | deterministic data continuity |
|---|---|---|---|---|---|---|
| full/static/no-HA | … | N/A | … | N/A | … | … |
| full/static/HA | … | … | … | N/A | … | … |
| full/dynamic/HA | … | … | … | … | … | 当前否 |
| fragment/static | … | N/A | 当前否 | N/A | … | … |

每个单元必须是 `guaranteed / best effort / experimental / unsupported`，并链接到 invariant 和 test evidence。

## A-10 schema migration 和 domain command 没有独立版本治理

当前 schema bootstrap 已有版本概念，但业务 mutation 仍散布在 store 方法。随着 dynamic/fragment 扩展，迁移和命令兼容会变得困难。

建议：

- 每个 authority schema 版本有显式 migration；
- command payload 带 schema/command version；
- migration 先做 invariant audit，再切换 marker；
- 所有 mutation 经过 command handler，产生可重放 audit event；
- read model 可随版本重建。

## 7. 文档需要修正的地方

### D-01 “immutable payload” 的表述强于实现

当前 payload 使用随机短 ID 加 atomic replace 写入目标路径。正常情况下不会覆盖，但实现没有 create-if-absent 保证。文档应改为“由协议约定不覆盖”，或者实现真正的 immutable create 后再保留强声明。

### D-02 `total_seen_tokens` 名称应改为精确语义

建议改为 `selected_proposal_tokens_committed`，直到新的指标本体完成。所有历史图表需要写明 denominator。

### D-03 Plan 02 PASS 不等于训练质量 PASS

README 已有一句说明，但建议把系统正确性、控制面性能和训练质量分别列为三个证据域，禁止 checker 在缺少 eval metric 时输出笼统的 overall PASS。

### D-04 fragment 的保证需在顶层立即可见

不要只在研究计划中说明 fragment resume 未完成。README 的 feature list 旁应直接标注 experimental boundary。

### D-05 matched 性能公式必须披露 signed value 和 comparability checks

不能只给 clipped positive overhead。文档应同时保存原始 duration、signed ratio、工作量 equality 和异常差异解释。

### D-06 需要威胁模型和故障模型

明确区分：

- benign crash / pause / scheduler retry；
- transient shared-FS errors；
- partial/corrupt writes；
- stale but non-malicious process；
- malicious/compromised learner（当前大概率不防）；
- clock skew assumptions；
- node/FS durability assumptions。

否则“fenced”“durable”“immutable”容易被理解为比实际更强的保证。

## 8. 建议的修复优先级

## P0：在任何新主实验前完成

1. 修复 H-01 dynamic old-incarnation livelock。
2. 修复 H-02 mid-cycle replace effective-token accounting。
3. 修复 H-03 ingest conflict transaction order。
4. 实现 H-04 strict proposal schema 和 positive finite weight checks。
5. 修复 H-05 contributor fairness。
6. 实现 H-06 structured FS read result 和 visibility grace。
7. 建立 H-08 指标本体，重命名历史指标。
8. 把 H-09 matched gate 改为 workload-equivalence + signed comparison。

完成标准：不仅 unit test 通过，还需要 state-machine test、fault injection 和至少一次 8+1 Miyabi regression。

## P1：在论文系统实验前完成

1. PBS uncertainty deadline/state machine。
2. dynamic stream data cursor continuity。
3. authority/audit/telemetry manifest。
4. schema CHECK/migration。
5. 真正 immutable object creation。
6. monotonic timeout 和 init idempotence。
7. 冻结 environment/dataset identity。

## P2：在 fragment 成为主贡献前完成

1. 提取 protocol domain 和 ports/adapters。
2. 实现 fragment version-vector authority 与 resume。
3. 重构 learner/syncer God modules。
4. 增加 storage/RPC/object-store 可替换 adapter。
5. 统一 full/fragment publication transaction 抽象。

## 9. 必需的新增测试矩阵

| 类别 | 最低测试 |
|---|---|
| Dynamic membership | revoke+replacement+old pending；同 stream epoch 竞争；final fence retry；重复 PBS job；drain 中 replacement |
| Proposal ingest | exact replay；update ID 冲突；唯一键冲突；frontier crash；old/new pointer reorder；malformed/quarantine |
| Filesystem | transient ENOENT、ESTALE、EIO；迟到可见性；symlink/path escape；hash mismatch；payload collision |
| Adoption | replace/rebase/predict 在每个 inner step 发生；有效 token 守恒；optimizer state 守恒/重置 |
| Selection | `N > quorum_max` 长跑公平性；heterogeneous speed；stream churn；oldest policy 语义 |
| Publication | 每个 write/rename/DB commit 点 crash；fence failure 后 orphan GC；cache repair |
| Scheduler | qsub timeout、submission unknown、live no-record、historical lag、qstat outage、rerun same PBS ID |
| Metrics | processed/produced/selected/effective/discarded token 守恒；CSV 丢失不改变 authority summary |
| Data | replacement cursor continuity；stream epoch replay；固定 seed 可重复；unique token accounting |
| Scale | 1/2/9 节点；长周期 bounded rows/files/pages；metadata contention；syncer duty cycle |

建议为状态机定义以下核心不变量：

1. 每个 committed global version 恰有一个 predecessor。
2. committed selected update 在 commit 时必须属于 current membership fence。
3. 同一 stream 每个 merge 至多贡献一次。
4. 只要存在 current quorum 且 object store 最终可用，系统在有限步内前进或进入明确 terminal state。
5. `effective_tokens_applied + discarded_tokens + pending_effective_tokens` 与已处理 segment token 对账。
6. frontier 不得指向既未入库、也未 quarantine、也非 exact replay 的 proposal。
7. cache 丢失不得改变 authority；authority 丢失必须 fail closed。

## 10. 推荐的目标架构

```text
LearnerApplication
  ├── DataStreamPort
  ├── ModelTrainerPort
  ├── ProposalPublisherPort
  └── ControlViewPort

SyncerApplication
  ├── MembershipService
  ├── ProposalInboxService
  ├── FairSelectionService
  ├── AggregationService
  ├── PublicationService
  └── TerminalService

Domain
  ├── VersionCoordinate (Scalar | Vector)
  ├── MembershipFence
  ├── Proposal
  ├── SelectionBatch
  ├── PublicationIntent
  └── TokenAccounting

Ports
  ├── AuthorityStore
  ├── ImmutableObjectStore
  ├── Scheduler
  ├── Clock
  ├── AuditSink
  └── TelemetrySink

Adapters
  ├── SQLiteAuthorityStore
  ├── LustrePosixObjectStore
  ├── PBSProScheduler
  ├── JsonlAuditSink
  └── WandBTelemetrySink
```

关键原则是：

- safety invariant 位于 domain/application transaction boundary，不散落在 adapter；
- FS、PBS、SQLite 的异常被翻译成 typed result，不直接变成 `None`；
- full/fragment 共享 command 和 publication lifecycle，只替换 version coordinate 和 payload partition；
- 每个 experiment 只替换一个 port，从而实现“同算法、不同 transport”的可信对照。

## 11. 最终判断

当前代码已经具备成为一篇 ML systems 论文 artifact 的基础，但还处于“协议安全边界较强、活性与测量语义尚未完全闭合”的阶段。

可以保留并继续强化的核心是：

- shared-storage authority；
- epoch-fenced syncer HA；
- independent PBS job membership；
- bounded proposal surface；
- DB-first recovery 和审计证据。

当前不应直接作为论文定论的部分是：

- dynamic membership 已完全无活性缺陷；
- `total_seen_tokens` 代表实际训练计算；
- static/dynamic overhead 已由单次 clipped ratio 证明；
- fragment 已在系统层面优于 full；
- 所有 payload 都由实现强制 immutable；
- fragment 与 full 拥有同等级恢复保证。

修完 P0 后，full/static/HA 与 full/dynamic/HA 可以成为下一轮实验主线；fragment 应继续标记为 experimental，直到其等待结构、version-vector recovery 和训练质量证据闭合。

## 12. 主要代码定位

- `fs_diloco/runtime/syncer.py`
- `fs_diloco/runtime/learner.py`
- `fs_diloco/runtime/adoption.py`
- `fs_diloco/runtime/launch_outbox.py`
- `fs_diloco/runtime/pbs_scheduler.py`
- `fs_diloco/protocol/merge.py`
- `fs_diloco/protocol/membership.py`
- `fs_diloco/protocol/liveness.py`
- `fs_diloco/protocol/dynamic_terminal.py`
- `fs_diloco/storage/sqlite_store.py`
- `fs_diloco/storage/fenced_store.py`
- `fs_diloco/storage/leader_lease.py`
- `fs_diloco/storage/atomic_io.py`
- `fs_diloco/storage/schema.sql`
- `fs_diloco/storage/schema_bootstrap.py`
- `fs_diloco/storage/maintenance.py`
- `fs_diloco/modeling/hf_data.py`
- `fs_diloco/tools/run_metrics_csv.py`
- `tests/test_plan02_phase2_dynamic.py`
- `tests/test_merge.py`
- `README.md`
- `docs/02-architecture.md`
- `docs/03-runtime-flow.md`
- `docs/04-data-flow.md`
- `docs/05-code-structure.md`
- `docs/06-configuration.md`
- `docs/07-operations.md`
- `plans/00-RESEARCH_PLAN.md`
- `plans/DOING/fsb_decoupled_diloco_plan_02.md`
