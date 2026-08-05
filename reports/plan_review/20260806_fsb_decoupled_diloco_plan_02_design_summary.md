# `fsb_decoupled_diloco_plan_02_design.md` 核心总结

提炼日期：2026-08-06
来源文档：`plans/DOING/fsb_decoupled_diloco_plan_02_design.md`（状态：已综合两份审查完成设计修订，尚未实现或通过集群验收）
对应计划：`plans/DOING/fsb_decoupled_diloco_plan_02.md`
本摘要仅做提炼，不改动、不评审原设计文档。

## 1. 设计目标与背景

当前 full 模式以单一 SQLite 作为 global version/update 状态的提交点，只适配一个 syncer 和固定 learner 集合。设计要解决五类场景：syncer/learner 作为独立 PBS job 运行、syncer 消失后由另一进程接管、learner 重启/替换/扩容而不复用含混 ID、训练结束不依赖初始化时写死的 learner 数完成闭合、多进程观测故障时不制造无界 qsub 风暴。

目标分三层，直接对应三个实施阶段：

1. **Phase 0** 证明基础原语（SQLite writer lock、共享时钟、固定 cache 的旧 writer 反例、PBS 查询能力、source pinning）的真实能力；
2. **Phase 1** 实现独立作业与 Syncer HA：以 SQLite epoch lease 选出唯一业务 writer，以 epoch-scoped 文件隔离旧进程；
3. **Phase 2** 实现动态成员与有界扩容：分离进程身份、物理位置与数据流，用事务化 admission、launch outbox 和 drain ack 管理生命周期。

**明确不解决**（第 3 节）：fragment 模式的 HA/动态成员/resume；scheduler 层物理 job 的 exactly-once 提交；永久持有 SQLite writer lock 的旧进程自动接管；inner optimizer/dataset iterator offset/RNG 完整恢复；主动缩容/自动删除健康 job；依靠普通共享文件系统 `os.replace` 提供 fencing/CAS；把自动 failover 或动态扩容包装成训练质量提升。这些是当前"一个 SQLite + 普通共享文件系统 + PBS"原语下的显式产品契约，而非遗漏。

设计文档正文开头附有两份既有审查（`20260805_fsb_decoupled_diloco_plan_02_review.md`、`20260805_plan02_review.md`）逐项问题（P0-01~P0-05、P1-01~P1-08、B3~B10、D1~D11、S1~S4）对应的修订决策表，以及若干冲突方案的最终取舍：保留 epoch 目录式布局而非单层长文件名；`learner_li_` 兼容前缀 + mode-aware iterator 而非白名单；bootstrap 走确定性 logical request 而非放开 unsolicited 注册；legacy 与 HA store 完全分离而非用 optional token 兼容；checkpoint SHA-256 默认关闭。

## 2. 关键架构与协议决策

### 2.1 三层状态模型（权威 vs 可查询 vs 便捷缓存）

| 层 | 内容 | 权威性 | 写入者 |
| --- | --- | --- | --- |
| SQLite | leader epoch、global version、update、controller、terminal、membership、launch request | 唯一业务权威 | 当前 leader 的 fenced transaction |
| 当前 epoch canonical artifact | learner 可读的 latest/drain/stop/summary/admission | DB 状态的可验证查询面 | 当前 epoch publisher |
| 固定路径 convenience cache | `control/latest.json`、`stop.json`、`summary.json` | 非权威，可被旧进程污染 | 当前或恢复后的旧 publisher 都可能写 |

核心论证：旧 leader 可能在 lease 检查通过后、`os.replace` 前被暂停，新 leader 接管发布后旧进程恢复仍可覆盖固定路径——普通共享文件系统没有"只有 epoch N 才能替换"的条件写原语。因此验收目标从"固定 cache 永不被污染"改为：旧进程无法写入新 epoch 的 canonical 目录；reader 一旦观察到更高 epoch 永不因低 epoch cache 回退；Checker 可报告 cache pollution 但不得误判为业务回滚。

### 2.2 Run 初始化与 schema 生命周期

新增独立、非竞争的一次性 `init-run` 步骤打破"先建 lease 表还是先 acquire"的循环依赖，避免多 candidate 隐式 DDL 破坏 pre-HA fail-closed 契约。初始化顺序：原子创建 run root → 单事务创建 schema/identity → integrity 检查 → 原子发布 DB → 写 `run_descriptor.json` → 最后写 `bootstrap_complete.json` 门禁标记 → 角色 job 只读校验后才 acquire。

SQLite 打开 API 拆分三条入口：`initialize_new_run()`（唯一 DDL 入口）、`open_existing(expected_identity)`（只打开完成 bootstrap 的 DB）、`open_readonly()`（`mode=ro`，供 analysis/Checker）。`schema_meta.schema_version` 与 `run_state['schema_version']` 同事务写入并须一致，不允许靠"某表碰巧存在"识别 HA schema。JSON 格式版本不整体提升现有 `FORMAT_VERSION`，heartbeat/epoch control/membership artifact 各自独立版本号。

Source/config/资源目录同样按角色隔离：`init-run` 调用 `prepare_authority_dirs()` 建全局命名空间，learner 只能 `prepare_instance_dirs(instance_id)` 建自己的路径；现有会预建所有成员目录的 `prepare_run_dirs()` 不进入新协议。日志/CSV/W&B 按 owner+epoch 隔离，loser candidate 不创建业务 W&B run。

### 2.3 Leader lease 与 fencing（Phase 1 核心协议）

`LeaderToken(run_id, epoch, owner_id)` 不可变；epoch 单调递增且永不复用。Acquire 在 `BEGIN IMMEDIATE` 中完成（读 singleton row → 无 owner 建 epoch 1 / released 建下一 epoch / active 只有超过 `lease_expires_at + max_clock_skew` 才能接管 → 同时写 epoch history 和新 singleton row → commit 后 token 生效）。renew 用独立短超时连接，精确匹配 epoch/owner；leader 自身剩余租约只按 monotonic 时间计算，DB wall-clock expiry 只供其他节点判断 takeover，过期 owner 不能 late renew 复活，只能作为新 candidate 参与下一 epoch。

所有业务写操作必须走 fenced transaction（校验 token → 校验 lease 可开始新 transaction → 短 DB 写 → commit 前再校验 token），transaction 内禁止文件 I/O、checkpoint、模型计算、qstat/qsub、sleep 和长 checksum；原始 writable connection 不得暴露给生产调用方。当前 31 个写方法中只有 6 个已有显式 `BEGIN IMMEDIATE`，需逐方法建 inventory 并迁移：HA full 全部写方法进 `FencedSQLiteStore`；static full/fragment 留在 `LegacySQLiteStore` 并做逐字节回归；analysis 进 `ReadOnlySQLiteStore`；不允许 optional/no-op token（会把"漏传 fence"变成合法路径）。

可用性边界表（第 8.4 节）：transaction 外暂停可自动接管；transaction 内短暂停延迟接管；**transaction 内永久暂停只能安全阻塞、不保证自动可用性**，需要 operator 授权终止旧 job；系统不配置自动 `qdel`。

### 2.4 Checkpoint 与控制面发布协议

路径按 `weights/epochs/eNNNNNNN/<owner-short>/global_vNNNNNN_p<publication-short>.safetensors` 组织，binary 与 `vNNN/stop_gNNN/summary_gNNN` 均为不可变对象，目标已存在直接 fail closed。checkpoint SHA-256 三种显式模式：`off`（默认，DB 只记录 size，不做内容级 bit-rot 检测）、`checker`（离线计算仅写 report）、`always`（commit 前计算并记录）；小型 control JSON 始终 hash。保留目录式 epoch 布局，统一由 `RunPaths` 提供 mode-aware 递归 iterator，避免历史非递归 glob 静默返回空集。

发布顺序：transaction 外算权重/outer state → 写带 publication ID 的 epoch 唯一 binary 并 fsync → fenced transaction 重验 predecessor/token/updates → commit DB（version/update/路径/size/checksum）→ 写 epoch 内不可变 `latest/vNNN.json` → 更新 publication manifest → 原子更新当前 epoch `head.json` → best-effort 更新固定 `control/latest.json`。第 2 步后崩溃产生 orphan binary；第 4 步后崩溃时新 leader 从 DB 行和 checkpoint 重建 canonical artifact，固定 cache 缺失不妨碍恢复。

Learner 不打开业务 SQLite，扫描有界 epoch 查询面选择最高合法 epoch，维护 epoch/owner/global version/terminal generation 四个不可回退水位；固定 cache 仅在 publisher 匹配、版本不回退、checksum 正确时作为快速路径。watchdog 不再把"无进展"等同于 syncer 死亡，而是感知最高合法 epoch heartbeat、claim、scheduler 状态和 canonical repair 窗口，仅在 `syncer_recovery_exhausted`（recovery 总上限耗尽且无合法 leader/candidate）时才受控停止。

### 2.5 Phase 2 身份模型：三个概念分离

| 身份 | 含义 | 生命周期 |
| --- | --- | --- |
| `learner_instance_id` | 一次进程启动 | 每次重启新建 |
| `placement_id/placement_epoch` | host/GPU 位置及其代际 | replacement 时递增 |
| `stream_id/stream_epoch` | 数据 shard/RNG 虚拟流及其代际 | pool 固定，重分配时 epoch 递增 |

`stream_pool_size` 在 run 初始化后不可变，`stream_id ∈ [0, stream_pool_size)` 满足现有 HF dataset shard API 的 `index < num_shards` 约束；`learner_index = stream_id`、`num_learners = stream_pool_size`。replacement 复用同一 stream 时递增 `stream_epoch`，新实例从该 stream 确定性起点重新开始（不保存 iterator offset），标记 `stream_restarted=true`，属于必须披露的数据重复限制。dynamic instance ID 用 `learner_li_<uuid4>` 兼容前缀，但校验不再依赖 `valid_learner_ids(num_learners)` 白名单，改由 path ownership + UUID 格式 + admission row + token 共同判定合法性。

### 2.6 Registration、admission 与 replacement

`allow_unsolicited_registration=false` 始终保持，初始成员也不绕过：first leader 完成 v0 后按 `bootstrap_instances` 创建确定性 logical request（`request_id = sha256(run_id, "bootstrap", slot, config_fingerprint)`），发布 `bootstrap_ready_gNNN.json` 后才允许 learner 注册/初始化 dataset。Admission transaction 必须幂等：相同 request 重放返回相同结果；scale-out 到达时在同一 transaction 内重算 current/reserved capacity，防止跨 request 超发。健康 placement 只能在三种情况下被 replacement：instance 已明确 dead/revoked/expired；request 带 leader 创建的 authorized replacement generation；operator 显式授权。heartbeat stale 只是观测，不自动使旧 proposal 失效。

### 2.7 Membership 竞态与最终 commit fence（Phase 2 关键正确性保证）

Proposal 摄取校验只能拒绝当时已知的 stale proposal，无法覆盖"selected 后才发生 replacement"的竞态，因此 **final global commit transaction 必须重新 join membership**：若 membership 先获得 writer lock，global commit 在 join 时 rollback，syncer 从 DB checkpoint 重算；若 global commit 先提交，membership transition 随后再提交。可接受结果只有这两种线性化路径，不存在"membership 已撤销但旧计算悄悄提交"的第三态。

### 2.8 Capacity observation 与 launch outbox

Observation key 确定化（`merge:<committed_global_version>` 或 controller 分配的 starvation generation），同一 key 只能插入一行，low counter 与该行在同一 fenced transaction 更新，防止 replay/takeover 重复推进计数。Scale-out 创建条件需同时满足：bootstrap 全部结束/deadline 已过、最近 N 个不同 observation 都 low、productive+reserved < desired、admission open 且有空闲 stream、各项预算/cooldown 未超限、current+reserved 不超过 stream pool。Outbox 状态机含 `planned/submitting/submitted/started/admitted/completed` 主链和 `submission_unknown/retryable/cancelled/expired/capacity_fulfilled/failed` 分支；TTL 只约束尚无 scheduler 确认的授权，一旦确认 queued/prologue/running 就持续占用 reserved 直到 scheduler 确认终态，避免"先释放 A 容量再建 B，随后 A/B 同时启动"的超发。

### 2.9 Dynamic close 与 drain acknowledgement

Controller 用持久状态机 `open → draining(close_generation) → closed → terminal` 闭合训练，不再依赖"等待所有初始化 learner ID 出现 stop"。进入 draining 的同一 transaction 关闭新 launch/admission、取消未 admission 的 request、冻结 `max_terminal_version`、发布 `drain_gNNN.json`。每个 current learner 观察到 drain generation 后完成当前 cycle、最多发一份 final proposal、写可重放 ack（`status=drained, close_generation, final_update_id`）；未响应者 timeout 后由 leader 显式 revoke。`input_closed` 需要 admission closed、所有 current instance 都 drained/stopped/revoked、无可 admission 的 launch request、无未过期 registration、final pointer 已摄取、visibility grace 结束这六个条件全部成立。

## 3. 阶段划分

- **Phase 0（可行性证明，第 5 节）**：五组可重复证据——① SQLite writer-lock 探针（`BEGIN IMMEDIATE` 暂停/接管边界）；② 旧 cache writer 探针（故意制造覆盖，证明 reader 仍选高 epoch）；③ 时钟与共享 SQLite 探针（跨节点 skew、busy、journal 配置，扩展 `sqlite_shared_fs_probe.py` 加 `contend` 模式）；④ PBS 能力探针（qsub/qstat 可靠性、job array 可用性，不足则关闭自动 recovery/scale-out）；⑤ Source/config pinning 探针（fingerprint 任一变化必须在 acquire/register 前失败）。
- **Phase 1（独立作业 + Syncer HA，第 6-10 节）**：run 初始化与 schema 生命周期、source/输出隔离、leader lease/fencing、checkpoint/控制面发布协议、recovery submission（默认关闭，仅产生 candidate 不授予 leadership）。
- **Phase 2（动态成员 + 有界扩容，第 11-15 节）**：身份模型分离、固定 virtual stream pool、registration/admission/replacement、membership 竞态与最终 commit fence、capacity observation/launch outbox、dynamic close/drain ack。

验证与发布判定要求严格 **Phase 0 → Phase 1 → Phase 2 串行推进**，Phase 2 不能使用 staged pass，缺少核心 evidence 必须是 `BLOCKED`。

## 4. 主要安全边界

1. **可用性让位于安全性**：writer transaction 内永久暂停时系统不自动接管（无双 writer 保证优先于自动可用性），需 operator 授权终止旧 job；这是设计明确记录的已知限制（第 21 节），未来若要在此故障下无人工介入接管，需要外部 fencing/consensus，不能只调 lease timeout。
2. **固定 cache 非权威、不可信为业务状态**：canonical 判定只认 SQLite + 最高合法 epoch artifact，reader 永不因低 epoch cache 回退。
3. **GC 安全边界**：禁止"目录减 DB 引用直接 unlink"；orphan 先入 `gc_candidates`，经 grace 期后逐文件在 fenced transaction 内重验 token/引用才删除，故障测试须证明 current 文件不变且预期 orphan 确实被删除。
4. **qsub 风暴防护**：observation key 确定化 + reconcile + claim/uncertainty timeout + backoff + 每 observation 和全局预算上限；设计只保证 logical request 最多一个 actor 获得业务 authority，不保证物理 job 只有一个。
5. **Admission 唯一性与容量上限**：`quorum_min <= desired_contributors <= quorum_max <= stream_pool_size` 恒成立；current admitted + scheduler-confirmed reserved 不超过 stream pool。
6. **schema/source 隔离**：pre-HA/fragment 遇 HA 一律 fail closed；source/config/protocol 任一不匹配都在产生业务 row 前失败，且失败进程只能写自己的日志。
7. **默认全部关闭**：`syncer_ha.enabled=false`、`recovery_submission.enabled=false`、`membership.mode=static`、`scaling.enabled=false`；dynamic 要求 full+HA，fragment+HA/dynamic 直接报错；自动 qsub/scale-out 需显式授权，`qdel` 只能由 operator 单独授权并记录，不是配置开关。
8. **有界状态**：正常扫描面（current/recent epoch、instance、未终结 request、每实例一个 proposal pointer、有限 recent observation）与历史 append-only archive 分离，archive 不参与正常 discovery；1000 次 takeover/churn 测试须同时检查行数、文件数、SQLite page/freelist 计数和单轮扫描耗时。

## 5. 验收重点（第 20 节）

**Phase 1 必须证明**：三种打开方式不静默迁移历史 DB 且 schema 双版本一致；31 个写方法全部有明确 store/transaction/test 归属；并发 acquire 只有一个 epoch winner；successor 提交后旧 token 无法写业务 DB；transaction 外/内暂停按边界表处理；旧 writer 可污染固定 cache 但不影响 current canonical 选择；DB commit/control publish 窗口可恢复；epoch 目录下各扫描器实际非空；GC 循环中旧 leader 恢复不能删除 current 文件且 orphan GC 非 no-op；recovery claim/PBS 排队超旧 watchdog 上限时 learner 不被误杀；source mismatch 与 pre-HA/fragment+HA 均 fail closed；1000 次 synthetic 恢复后 active 状态有界；**2 节点真实共享文件系统 + 9 节点独立 PBS job 集群验收通过**。

**Phase 2 必须证明**：stream pool 与当前 dataset API 一致且 1000 次 churn 不越界；unsolicited 关闭时 bootstrap slot 仍能启动初始成员且超预算拒绝；stale/duplicate registration 不能驱逐健康成员；selected 后 membership 竞态只产生两种线性化结果；observation replay 不推进 low counter；logical launch request 最多一个 admitted instance；queued/running request 超 TTL 仍 reserved 且 admitted+reserved 不超 stream pool；健康 learner 可通过 drain ack 闭合，未响应者按 timeout revoke；**9 节点场景覆盖死亡、暂停、scale-out、duplicate physical job 和 terminal closure**（第 14.4 节给出的具体验收峰值形态：1 syncer + 8 learner 稳态 → 终止一个 learner 并由 scheduler 确认结束 → 创建 replacement 恢复 1+8，全程不允许新旧 job 同时占用第 10 个节点）。

Checker stdout 只允许 `PASS`、`PASS_WITH_FOLLOWUPS`、`BLOCKED` 三种输出，Phase 2 不接受 staged pass。

## 6. 已知限制与后续演进（第 21 节）

- 安全性优先于 writer-lock 永久暂停时的自动可用性，这是设计选择而非缺陷；
- 固定 stream pool 限制同一时刻最大贡献者数，改变 pool 大小应创建新 run 而非在线扩容；
- iterator offset 未恢复会产生可观测数据重放，留作后续独立研究问题；
- 自动 recovery 和 scale-out 默认关闭，即便协议实现完成也应先以人工独立 job restart 通过真实集群验收，再逐项开启自动化；是否把这些能力提升为项目默认路线，还需同步 `plans/00-RESEARCH_PLAN.md` 的范围与优先级决策。
