# `fsb_decoupled_diloco_plan_02.md` 可执行性审查

审查日期：2026-08-05  
审查对象：`plans/DOING/fsb_decoupled_diloco_plan_02.md`  
审查方式：基于当前工作树的静态设计、代码、schema、文档和 PBS launcher 对照；未进入 Miyabi 计算节点，未运行 pytest，未提交或取消 PBS 作业。

## 1. 结论

**当前计划不适合直接进入实现，建议状态为 `BLOCKED_FOR_PLAN_REVISION`。**

计划的总体方向是合理的：它保留了 SQLite 事务作为 full global version 的唯一提交点，明确区分权威状态与可重建 cache，使用 epoch 唯一 checkpoint 路径，Phase 1/Phase 2 严格串行，并给出了较完整的故障矩阵、Checker 和 artifact 纪律。这些都与当前 full 路径的 DB-first publication/resume 基础相符。

但计划目前仍有五个会影响正确性或使验收条件无法成立的阻塞问题：

1. 固定 `latest.json` / `stop.json` / `summary.json` 无法仅靠“写前 lease 检查”抵御 `SIGSTOP → takeover → SIGCONT` 的旧 writer；
2. 旧 leader 若在同一 SQLite 的 write transaction 中被 `SIGSTOP`，会持有 writer lock，使新 leader 无法通过同一 DB 的 `BEGIN IMMEDIATE` takeover；
3. dynamic terminal 的 `draining → input_closed` 缺少让健康 learner 停止继续发布并确认 drain 的协议，存在闭环等待；
4. 单调增长的 `stream_id` 没有可执行的数据分片映射，而当前 iterator 要求固定 `learner_index < num_learners`；
5. dynamic proposal 只规定摄取时校验成员身份，没有把 incarnation/stream 校验纳入最终 global commit transaction，无法证明 selected-race 下的 MEM-05/MEM-18。

因此：

- **Phase 1**：完成下面的 P0/P1 设计修订和前置探针后可以实施；
- **Phase 2**：在 Phase 1 `PASS` 之外，还必须先冻结 stream、membership commit 和 drain-ack 三个协议，当前版本不能顺利实施；
- 建议把当前大计划拆成 `02A Syncer HA` 与 `02B Dynamic Membership` 两份独立计划，先完成 02A 的真实跨作业 `PASS`，再发布 02B。

## 2. 与当前仓库对齐良好的部分

### 2.1 权威链方向正确

当前 full publication 已经是：写 weight/outer → SQLite 单事务提交 global version 与 update 状态 → 写 `latest.json`。对应实现位于：

- `fs_diloco/runtime/syncer.py:398-518`；
- `fs_diloco/storage/sqlite_store.py:271-435`。

计划在此基础上增加 `commit_epoch/owner_id` 和 transaction 内 fencing，而不是引入第二套模型权威，方向正确。

### 2.2 Epoch 唯一 checkpoint 是必要且可落地的

当前 checkpoint 只由 version 命名：`RunPaths.global_weight_path()` / `outer_optim_path()` 位于 `fs_diloco/storage/paths.py:119-123`。计划改为 epoch/owner/version 唯一路径，可以消除旧 writer 覆盖新 writer binary 的问题，也与 DB 保存实际路径的现有模式兼容。

### 2.3 Phase gate、Checker 和证据纪律较好

计划明确禁止在 Phase 1 仅得到 `PASS_WITH_FOLLOWUPS` 时开始 Phase 2，并区分 staged/completed Checker；同时保留失败先记录、连续三次失败升级全面审查、PBS 成功必须证明 workload 真正执行等规则。这部分可以保留。

### 2.4 PBS 静态门禁与仓库规则一致

计划要求 `bash -n`、literal group ID、非空 workload artifact。当前已有 PBS 文件使用字面 group `xg24i002`，没有发现 `<group_id>` 占位符。正式新增脚本仍需在提交前重新执行全量静态检查。

## 3. 阻塞问题（P0）

### P0-01：固定 JSON cache 的“防旧 writer”保证在 `SIGSTOP` 模型下不成立

**计划位置**：5.4、5.7、5.10、DUAL-06/07；尤其是计划行 368-370、451-459、521-530、643-645。

计划对 DB 外 cache 的保护是“写前 `ensure_lease_remaining()`”，并让 reader 拒绝低 epoch cache。该设计可以**检测并忽略**旧 cache，但不能阻止下面的时序：

```text
old leader: lease check PASS
old leader: SIGSTOP（发生在 os.replace 前）
new leader: takeover，发布 current stop/summary
old leader: SIGCONT，继续 os.replace 固定 stop.json/summary.json
```

共享文件系统的普通 `os.replace` 没有 fencing token/CAS 条件；旧进程恢复后仍能替换固定文件。计划自己在 PUB-HA-06..08 承认 lower-epoch cache 可能覆盖，但 DUAL-07 又要求旧进程不能改变 summary 的 hash/mtime，两者矛盾。当前 `finally` 确实会无条件写 stop、summary、maintenance 和 W&B，见 `fs_diloco/runtime/syncer.py:2894-2963`，所以这个窗口是实际集成点，不是纯理论问题。

**必须修改**：

1. 把 terminal/controller 权威先提交到 SQLite，例如显式 `run_terminal` / `controller_state` row，记录 epoch/owner/generation；
2. 将 canonical `latest/stop/summary` 改为 epoch-scoped immutable artifact，例如 `control/epochs/e.../<owner>/stop.json`；
3. 固定 `control/stop.json` 等只作为可能被污染的 convenience cache，reader/Checker 必须从 DB 或最高合法 epoch canonical artifact 判定；
4. 若仍要求固定文件 hash/mtime 永不被旧 writer 改写，则必须引入外部 fencing/CAS 或先由 scheduler 确认旧进程已终止；仅靠现有共享 FS 原语不能满足；
5. W&B terminal summary 也不能靠一次写前检查获得 CAS 语义，应只由 completed epoch 的离线汇总器写，或使用 epoch-scoped run/字段并由最终 Checker选择合法 epoch。

应新增 failpoint：`after_cache_lease_check_before_replace`，并把 `latest`、`stop`、`summary` 三类分别覆盖。

### P0-02：同一 SQLite 中暂停的 write transaction 会阻塞 lease takeover

**计划位置**：5.2、5.4、PUB-HA-04、DUAL-09；计划行 274-324、344-370、607、646。

计划把 lease row 和业务表放在同一个 SQLite，并让 acquire 与业务 transaction 都使用 `BEGIN IMMEDIATE`。当前仓库也只有一个 DB：`control/syncer_metadata.sqlite3`，连接使用 rollback journal 和 60 秒 busy timeout，见：

- `fs_diloco/storage/paths.py:98-100`；
- `fs_diloco/storage/sqlite_store.py:58-73`。

如果旧 leader 在 transaction 已取得 writer lock 后被 `SIGSTOP`，该 lock 不会因 lease wall-clock 到期而自动释放。新 candidate 无法取得 `BEGIN IMMEDIATE` 来更新 `syncer_leader`，因此无法 takeover。独立 SQLite connection 只能避免同一 connection 被 checkpoint 占用，不能绕过另一个连接/进程持有的 DB writer lock。

把 lease 放到另一个 SQLite 虽能让新 leader获选，却会破坏“业务 transaction 与 fencing row 同事务校验”的原子性：旧业务 transaction 恢复时可能仍按旧快照提交。因此不能把它当作简单修复。

**必须修改**：

1. 在 P1-L0 增加真实探针：进程 A `BEGIN IMMEDIATE` 后 `SIGSTOP`，进程 B 尝试 lease acquire，记录 busy/timeout、恢复和 kill 后行为；
2. 明确可提供的可用性边界：
   - DB transaction 外暂停：允许自动 takeover；
   - DB writer transaction 内无限暂停：安全地不产生第二 writer，但 takeover 必须等待旧 transaction 释放，必要时经授权 `qdel`/kill 旧 job；
3. 将业务 transaction 保持短小，并记录 transaction p99/max；禁止在 transaction 中做文件 I/O、qstat、sleep 或模型计算；
4. 修改 PUB-HA-04/DUAL 的通过条件，不能声称在旧 writer lock 永久存在时仍自动 takeover；
5. 若产品目标坚持该场景下无人工 kill 的自动 HA，需要超出当前“仅 SQLite + 普通 FS”边界的外部 fencing/lease 服务，应另立设计。

### P0-03：Dynamic terminal 缺少 drain 通知和 learner acknowledgement，存在闭环等待

**计划位置**：9.11；计划行 1090-1133。

计划要求 `input_closed` 同时满足 `admission_state == closed` 和“没有 current instance 仍能发布”，但进入 `draining` 后只停止新 launch/registration，并只 revoke dead/superseded instance，没有告诉健康 current learner 在哪个边界停止发布、如何确认最终 pointer 已可见。

当前 full learner 主要通过 `stop.json` 或本地 completion 条件退出，见 `fs_diloco/runtime/learner.py:2246-2261, 2361-2363, 2487-2506`；`global_only` learner 到达本地 horizon 后还会等待 global stop。若 stop 只在 `input_closed` 之后发布，健康 learner 可继续发布，而 syncer 永远无法证明“no current instance can still publish”。

**必须修改**：增加独立于 terminal `stop.json` 的 drain generation 协议：

```text
open
  -> draining(close_generation, publish_cutoff)
  -> 每个 current instance 观察 generation
  -> 完成/放弃当前明确规定的 publication 边界
  -> 写 final pointer + drained heartbeat/ack(generation, final_update_id)
  -> syncer 最终摄取并显式 revoke/close instance
  -> closed/input_closed
  -> terminal drain
  -> authoritative stop/summary
```

需要定义未响应 learner 的 deadline/revocation、ack 重放、旧 generation ack、takeover 后重建、final visibility grace 起点，并增加 healthy learner 的 DTERM 测试，而不只测试 dead/superseded。

### P0-04：单调 `stream_id` 与当前固定数据 shard API 不兼容

**计划位置**：9.1、9.5；计划行 772-781、923-930。

计划让新 placement 获得单调增长的 stream ID，并把它用于 dataset shard/iterator seed。当前代码要求：

```python
dataset.shard(num_shards=num_learners, index=learner_index, contiguous=True)
```

见 `fs_diloco/modeling/hf_data.py:149-167`，调用点见 `fs_diloco/runtime/learner.py:2194-2199`。这里要求 `learner_index < num_learners`，并假设整个 run 的 `num_learners` 固定。动态 churn 后的单调 stream 可能超过固定 shard 数；如果随 active member 数改变 `num_shards`，同一 stream 的数据定义又会在运行中变化。

**必须在 Phase 2 前选择并冻结一种语义**：

- 固定大小的 virtual stream pool，`stream_id ∈ [0, virtual_stream_count)`，只回收已经无 active/pending/selected 引用的 stream；或
- 每个 stream 使用完整 dataset + 独立确定性 seed，不再声称互斥 dataset shard；或
- 设计可持久恢复的动态 shard allocator。

还要明确同 placement 新 incarnation 复用 stream 时是否从头重放数据、是否恢复 iterator offset/RNG；本计划当前不保存 inner optimizer 和 iterator 状态，若选择从头开始，应把数据重复作为明确限制和研究证据字段。测试至少加入：stream ID 超过初始 desired count、1000 churn 后分配有界、同 stream replacement、不同 active stream 不冲突。

### P0-05：成员身份只在摄取/selector 校验，未进入最终 global commit transaction

**计划位置**：9.6、9.8、MEMT-05/06；计划行 958-984、997-1008、1224-1225。

计划要求 proposal 在摄取时验证 instance/token/placement/stream，并让 selector 按 stream 去重；但没有规定 `updates` 表保存哪些 incarnation 字段，也没有要求 `commit_full_merge()` 在插入 global version 的同一 transaction 内重新校验 membership currentness 和 stream 唯一性。

当前 `commit_full_merge()` 只检查 update ID 唯一、`learner_id` 唯一、状态、前驱和 staleness，见 `fs_diloco/storage/sqlite_store.py:271-429`。selected 后还要在 transaction 外读取 tensor、聚合和执行 outer step，见 `fs_diloco/runtime/syncer.py:2639-2726`。在此期间若成员被 supersede/revoke，只靠摄取时校验无法证明旧 incarnation 不进入下一次 commit。

**必须修改**：

1. `updates` 增加并持久保存 `learner_instance_id/placement_id/placement_epoch/stream_id/admission_generation`，必要时保存 token hash而非明文；
2. dynamic selection transaction 记录 membership snapshot/generation；
3. final global commit transaction 重新 join `learner_instances/placements/controller_state`，确认每份 selected update 仍允许提交，并在 DB 层断言 current stream/placement 唯一；
4. 校验失败必须 rollback global commit，丢弃已计算的内存 theta/outer，按 DB current checkpoint重新选择和计算；
5. membership transition 与 global commit 必须由同一 DB 的 transaction serialization 保证先后关系；
6. 增加 failpoint：selection 后、tensor load 后、outer step 后分别 supersede/revoke，再检查只允许“旧成员完整先提交”或“成员变更先提交并使 global commit rollback”两种结果。

## 4. 高优先级计划缺口（P1）

### P1-01：自动恢复 claim 不能可靠抑制重复 qsub

计划行 497-515 用“各 learner 本地观察到的 stale elapsed / interval”生成 attempt。不同 learner 的首次观察时刻不同，可能同时落入相邻 attempt 并各自赢得一个 `mkdir`；已有 queued/prologue candidate 时，当前规则也没有强制先做 scheduler reconciliation、全局 backoff 或总预算。`recovery_claim_retention_seconds` 出现在配置中，但正文没有给出它如何阻止后续 attempt。

建议把 observation key 固定为 `(run_id, highest_epoch, heartbeat_seq, heartbeat_fingerprint)`，增加共享 submission state、scheduler state、`not_before`、指数 backoff、max attempts/max outstanding candidates；每次新 attempt 前先检查所有未过 retention 的 receipt/qstat。无法可靠查询时可允许物理重复，但必须有严格资源预算。`recovery_submission_enabled` 应明确默认 `false`，只有显式授权/配置后才能自动 qsub。

### P1-02：Pre-HA fail-closed 与当前 connect-time 自动 DDL 的改造顺序未定义

计划行 324 要求显式 schema version 且不得静默升级 pre-HA run。当前 `connect()` 在 identity/protocol 检查前就执行完整 schema 和幂等 `ALTER TABLE`，见 `fs_diloco/storage/sqlite_store.py:58-73`；`run_syncer()` 也在读取 run identity 前构造 `SQLiteStore`，见 `fs_diloco/runtime/syncer.py:2407-2415`。

计划必须先定义 bootstrap API：只读识别空 DB / pre-HA DB / HA DB / fragment DB，再决定创建、打开或 fail closed；建议使用 `PRAGMA user_version` 加独立 schema metadata。还需明确：

- HA disabled 的默认值和 single-syncer full 行为；
- fragment single-syncer 如何继续使用未 fenced mutator；
- v0 的 `commit_epoch/commit_owner_id`；
- protocol/format/schema 三种版本分别如何提升；
- analysis 只读打开历史 DB 时不得触发迁移。

### P1-03：独立/自动新作业没有强制加载同一份 source 和 config

计划只在 artifact 中记录 source fingerprint，并在 Phase 2 admission cache 中返回 fingerprint；Phase 1 candidate takeover 还缺少等价的启动前 source gate。当前 PBS launcher 从 live `PROJECT_ROOT` import，长期 run 期间 checkout 一旦变化，新 candidate 可能用不同代码接管同一 DB。

建议每个 run 在初始化时冻结 immutable source/config manifest，并让 candidate/learner PBS 脚本在 import runtime 前核对 expected commit、dirty fingerprint、resolved config checksum 和 protocol/schema version；不一致立即退出且不 acquire/admit。更稳妥的方案是提交时使用不可变代码快照、容器或 commit worktree，而不是长期引用可变主工作树。

### P1-04：多 syncer 会破坏当前单 writer 日志/CSV/W&B 假设

当前所有 syncer 写同一 `logs/syncer.jsonl` 和 `metrics/syncer_metrics.csv`，并在 acquire 之前初始化 logger/W&B：

- `fs_diloco/runtime/syncer.py:2415, 2429-2435`；
- `fs_diloco/runtime/syncer.py:2810-2865`。

多个 candidate/旧 leader恢复时，这些路径会变成并发 writer；JSONL/CSV 追加不再满足当前文档的单 writer 契约。建议改为：

```text
logs/syncers/e{epoch}_{owner}.jsonl
logs/candidates/{owner}.jsonl
metrics/syncer_epochs/e{epoch}_{owner}.csv
```

分析工具按 DB epoch history 合并；固定汇总文件由 completed Checker 离线生成。W&B 只在 acquire 成功后初始化，明确 takeover 是 resume 同一 run 还是 epoch 子 run，candidate loser 不创建业务 run。

### P1-05：Scale-out observation 缺少幂等 window identity 和可实现公式

计划行 1058-1088 中的 `W` 未定义，upload ETA 的采样字段/公式未冻结；`capacity_observations` 只有自增 ID，没有 semantic window key 或 unique constraint。leader takeover/retry 可能把同一 global version/window 写两次，随后被误认为“连续两个 low window”并提前扩容。

建议增加 `observation_key UNIQUE`、`kind(merge|starvation)`、window generation/start/end、source global version、close generation，并在同一个 fenced transaction 中更新 low-window counter和创建 deterministic launch request。冻结 `W`、ETA公式、missing telemetry行为，以及 merge observation 与 starvation observation 如何互斥/去重。

### P1-06：Registration 可以无条件替换同 placement 的健康成员，且 request 无 TTL

计划行 932-943 规定只要同 placement 有旧 current instance就 supersede/revoke。即使 `allow_unsolicited_registration=false`，仍需定义 manual/recovery request 的授权关系；否则重复 job 或陈旧 registration 可驱逐健康 learner。`registration_requests/<uuid>.json` 也没有 request TTL/processed tombstone/清理顺序，无法证明“bounded request surface”。

建议：健康 current placement 默认拒绝新 registration，除非 request 明确带 authorized replacement generation，或旧实例已 stale/dead/revoked；为 registration 增加 created/deadline/source fingerprint/PBS job/launch request 校验、processed state和 GC 条件，并加入“stale request不能驱逐健康 current”反例。

### P1-07：实施报告路径与 scoped 指令冲突，且 requirement matrix 实际缺失

`plans/AGENTS.md:7-12` 要求使用计划文件名作为稳定标识。当前文件名是 `fsb_decoupled_diloco_plan_02.md`，计划却写 `reports/DOING/02/`（计划行 3-7、1460、1471-1476）。应二选一：

- 将计划文件重命名为 `plans/DOING/02.md`；或
- 将报告目录统一为 `reports/DOING/fsb_decoupled_diloco_plan_02/`。

此外，计划行 1413 声称同目录已有 `02-requirement-matrix.csv`，但当前文件不存在。正文示例只映射 6 条，而共有 HA-01..20 和 MEM-01..20。开始实施前必须创建完整 40 行矩阵，并把新增的 P0/P1 requirement 一并纳入。

### P1-08：Phase 1 的 leader history/cache/claim/log 还没有有界生命周期

Phase 2 详细设计了 1000 churn，但 Phase 1 的 `syncer_epochs`、epoch heartbeat、claim attempt、candidate receipt、epoch log/metrics 如何归档和删除没有完整 live-set 规则。多次恢复后它们会随历史增长。应定义 active/recent/audit retention、archive identity、删除先后和至少一个 1000-takeover 或等价合成有界性测试。

## 5. 可改进项（P2）

### P2-01：性能门槛缺少可重复计算口径

计划行 1387-1405 给出了 3%/5%、p99 和“不随历史线性增长”，但未定义：

- renew/heartbeat 后台线程的 wall time 是求和、critical-path 还是 CPU time；
- `complete_training_time` 从哪两个事件计算；
- Phase 2 “额外控制面开销”如何与 baseline 配对；
- p95/p99 的样本边界、warm-up、最小样本数；
- “同数量级”“无单调线性增长”的数值判据。

应在大作业前冻结字段、聚合公式、baseline fingerprint和 Checker 缺失字段行为。

### P2-02：当前研究路线与 Plan 02 的优先级需要显式协调

当前 `plans/00-RESEARCH_PLAN.md:66` 仍写“不追求自动 failover”，`88` 和 `219` 又把 base-relative displacement oracle列为下一阶段优先工作；同时 `112` 确实支持逐步开展独立 PBS 作业和角色级重启。Plan 02 直接扩展到自动 syncer failover、dynamic membership和自动 scale-out，明显扩大了近期范围。

这不是代码不可行的直接证据，但在实施前应记录一条明确决策：Plan 02 是否正式取代“不做自动 failover”的旧边界，以及为什么现在优先于 displacement/联合完成谓词。否则计划完成后文档会出现相互矛盾的项目主线。

### P2-03：建议拆分计划，减少一次性协议面

当前计划同时改配置、schema、所有 business mutator、checkpoint path、cache reader、learner watchdog、PBS scheduler、membership、data stream、terminal state和分析工具，且两个阶段的完成状态不同。建议拆分：

1. `02A`：independent-job launch + source pinning + manual syncer restart；
2. `02B`：SQLite lease/epoch DB fencing + epoch canonical artifacts；
3. `02C`：learner-assisted candidate submission（默认关闭）；
4. `03`：dynamic membership + bounded stream pool + drain ack；
5. `04`：automatic scale-out/outbox。

若仍保留两阶段单文件，至少应把 P1-L0 改为硬性的 architecture feasibility gate；在 P0-01/P0-02 探针和设计审查通过前，不允许进入大规模 store mutator 重构。

## 6. 建议的修订顺序

### R0：只修计划，不写生产代码

1. 决定固定 cache/terminal 的 canonical authority；
2. 写清 SQLite writer-lock 下的可用性边界；
3. 定义 dynamic drain-ack state machine；
4. 定义 bounded stream pool 或其他数据流语义；
5. 把 membership currentness 加入 global commit contract；
6. 修正报告目录并补齐 requirement matrix；
7. 记录与 `00-RESEARCH_PLAN.md` 的优先级决策。

### R1：Phase 1 feasibility probes

在任何大改动前先实现并持久保存以下小探针：

- SQLite `BEGIN IMMEDIATE + SIGSTOP` writer-lock/takeover probe；
- cache lease-check 后暂停、旧 epoch rename 的反例；
- clock skew/shared SQLite/qsub-from-compute/qstat 字段探针；
- source/config fingerprint mismatch 的 candidate fail-closed probe；
- queued candidate 跨 attempt 的 claim/backoff probe。

只有这些探针证明修订后的 contract 可满足，才进入完整 Phase 1 loops。

### R2：Phase 1 实施

优先顺序建议为：schema bootstrap → leader lease → transaction fencing → epoch checkpoint/canonical cache → process-scoped logs → manual independent-job takeover → 自动 claim（可选、默认关闭）→ 1/2/9 节点。

### R3：Phase 2 协议冻结后实施

先用 pure state-machine tests 完成 stream allocation、registration、membership-commit race和 drain ack，再接真实 learner/数据 iterator/PBS outbox。不要先实现 scale-out，再补 terminal 和数据流语义。

## 7. 修订后“可开始实施”的最低门槛

以下项目全部满足后，可以把本审查结论从 `BLOCKED_FOR_PLAN_REVISION` 改为 `READY_FOR_PHASE1`：

- [ ] epoch canonical terminal/cache 设计能处理 lease-check 后 `SIGSTOP`；
- [ ] 接受并测试 SQLite writer-lock 的 availability boundary，或引入经验证的外部 fencing；
- [ ] pre-HA/HA/fragment schema bootstrap 与默认配置语义明确；
- [ ] candidate 使用 immutable source/config 或启动前严格 fingerprint gate；
- [ ] claim 有 pending reconciliation、backoff 和资源预算；
- [ ] syncer log/CSV/W&B 改为 owner/epoch scoped；
- [ ] report path统一，完整 requirement matrix存在；
- [ ] P1-L0 探针命令、通过条件和 artifact路径已经写入计划。

Phase 2 还需额外满足：

- [ ] stream 到 dataset/RNG 的映射有界且可执行；
- [ ] global commit transaction 内重验 membership/stream/incarnation；
- [ ] healthy learner drain notice/ack/timeout/revocation闭环完整；
- [ ] capacity observation幂等，连续 low-window 不会被重放伪造；
- [ ] stale/duplicate registration不能驱逐健康 current instance。

## 8. 最终判断

该计划不是“方向错误”，而是已经接近 implementation specification，但其中几条最强 HA/dynamic 声明超出了当前原语实际能保证的范围。先修正上述权威边界、锁可用性、动态数据流和 terminal 闭环后，Phase 1 可以成为本项目现有 DB-first full reference 的自然延伸；若不修订就直接实现，最可能出现的结果是大量单元测试通过，但 `SIGSTOP`、旧 finally、dynamic closure 或真实独立 PBS job 场景在后期才暴露结构性失败。
