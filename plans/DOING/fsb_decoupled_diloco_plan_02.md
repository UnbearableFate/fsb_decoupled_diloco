# 独立作业 Syncer HA 与动态 Learner 成员实施计划

计划 ID：`fsb_decoupled_diloco_plan_02`

状态：已综合两份审查完成设计修订，待 Phase 0 可行性门禁

实施报告目录：`reports/DOING/fsb_decoupled_diloco_plan_02/`

配套文件：

- 人类可读设计文档：`plans/DOING/fsb_decoupled_diloco_plan_02_design.md`；
- requirement matrix：`plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv`；
- 前置审查：`reports/plan_review/20260805_fsb_decoupled_diloco_plan_02_review.md`；
- 仓库接触面审查：`reports/plan_review/20260805_plan02_review.md`。

执行前必须读取：

- 仓库根目录 `AGENTS.md`；
- `plans/AGENTS.md`；
- `plans/ref/实施计划制定与 Agent 执行经验.md`；
- 本计划、配套设计文档和 requirement matrix；
- 当前 `plans/00-RESEARCH_PLAN.md`；
- 当前 `docs/02-architecture.md`、`docs/03-runtime-flow.md`、`docs/04-data-flow.md`、`docs/06-configuration.md`、`docs/07-operations.md`；
- 与本轮修改相关的 `fs_diloco/runtime/`、`fs_diloco/storage/`、`fs_diloco/protocol/`、`fs_diloco/modeling/hf_data.py` 和 `scripts/miyabi/`。

进入计算节点测试、PBS 提交或 Miyabi 实验时使用 skill `miyabi-development`。只做静态源代码检查和文档编辑时不加载该 skill。

---

## 1. 设计决策与阶段边界

### 1.1 本计划解决什么

本计划把当前 full 模式从“一个 syncer + 固定 learner ID 集合”扩展为三个严格串行的阶段：

1. **Phase 0：可行性门禁**。验证 SQLite writer-lock、旧 cache writer、共享时钟、PBS/qstat 和 source pinning 的真实边界；
2. **Phase 1：full 模式 Syncer HA**。支持独立 PBS job、SQLite epoch fencing、DB-first takeover 和可选的 learner-assisted candidate submission；
3. **Phase 2：full 模式动态 Learner 成员**。支持进程 incarnation、固定 virtual stream pool、事务化 membership、幂等 launch outbox、capacity hysteresis 和 drain acknowledgement。

Phase 1 不改变 full merge 数学、staleness 规则或 current-only retention。Phase 2 不改变 outer optimizer、加权平均或 proposal eligibility 数学，只改变成员身份、发现集合、扩容和输入闭合方式。

### 1.2 与研究计划的关系

`plans/00-RESEARCH_PLAN.md` 中“不追求自动 failover”视为此前的范围边界。本计划不静默覆盖它，而采用以下决策：

- 独立 PBS 角色启动和**人工触发的 syncer restart**是 Phase 1 的必做能力；
- learner-assisted 自动 qsub 是 Phase 1 的可选能力，默认关闭，只有 Phase 0 证明 scheduler 能力且用户显式启用时实施；
- 本计划不声称多 syncer 是当前性能瓶颈，也不替代 base-relative displacement 等算法研究；
- 本计划提供的是恢复和动态成员的系统能力，不产生训练质量结论。

完成 Phase 0 后，应在 `plans/00-RESEARCH_PLAN.md` 中同步这一范围决策；在此之前不得把自动 failover 写成当前已支持能力。

### 1.3 严格 phase gate

- Phase 0 Checker 只有 `PASS` / `BLOCKED`；任何 feasibility requirement 没有证据都为 `BLOCKED`；
- Phase 1 不得在 Phase 0 `PASS` 前开始；
- Phase 2 不得在 Phase 1 completed Checker 返回 `PASS` 前开始；
- Phase 1 staged Checker 可返回 `PASS_WITH_FOLLOWUPS`，但只允许继续观察同一长作业，不允许开始 Phase 2；
- Phase 2 不提供 staged pass。

### 1.4 用户可观察完成定义

Phase 1 完成后：

- syncer 和 learner 可以由不同 PBS job 启动，只通过共享 FS/SQLite 协调；
- 同一 run 的 leader epoch 单调递增，不复用；
- 新 epoch 提交后，旧 epoch 不能修改业务 DB，也不能覆盖新 epoch canonical control/checkpoint；
- takeover 从 SQLite committed row恢复，下一提交严格为 `N+1`；
- 旧进程恢复后最多污染自己的旧 epoch目录或全局 convenience cache，learner/Checker 不会采用这些内容；
- DB writer transaction 内无限暂停不会被误报为“仍可自动 takeover”，而是进入明确的 scheduler-kill availability boundary；
- 自动 recovery submission 默认关闭；开启时有 reconciliation、backoff、预算和审计。

Phase 2 完成后：

- `learner_instance_id`、`placement_id`、`stream_id/stream_epoch` 分离；
- virtual stream pool 在 run 内固定，数据 shard/RNG 映射不随 active member 数改变；
- registration、admission、revocation、stream allocation 和 membership history由 leader-fenced transaction维护；
- final global commit transaction重新验证 incarnation、placement和stream；
- logical launch request最多 admission一个 learner；
- capacity observation幂等，连续 low windows不会因重放重复计数；
- dynamic close通过 drain directive/ack闭环，不依赖固定 expected learner ID。

### 1.5 两份审查存在冲突时的选择

本计划采用以下推荐选择，并将其视为后续实现不得临场改变的冻结项：

- 保留 **epoch-scoped目录式 checkpoint/control**，不改为单层长文件名；目录隔离提供更清晰的 writer ownership。代价是必须把 maintenance、Checker、probe和analysis中的非递归 glob统一迁移到 `RunPaths` 的递归、mode-aware iterator，并用非空断言防止静默 no-op；
- dynamic instance ID采用 `learner_li_<uuid4>`，复用现有 `LEARNER_ID_PREFIX`；同时仍移除各模块硬编码 glob和固定 learner白名单，不能只依赖兼容前缀掩盖扫描缺口；
- `allow_unsolicited_registration=false`保持不变；初始 learner不走安全旁路，而由 first leader创建确定性的 bootstrap launch requests，operator/job array以 bootstrap slot领取；
- HA full使用独立 `FencedSQLiteStore`，legacy full/fragment使用 `LegacySQLiteStore`；不使用 nullable token或 no-op sentinel把两种写语义混在一个 store中；
- checkpoint binary默认**完全不计算 SHA-256**；使用唯一 publication path + 必填 size，目标碰撞直接fail closed。只有显式选择 `checker` 或 `always` 模式才计算大文件digest；小型 JSON control artifact仍始终保存 SHA-256；
- 不保留任何自动 `qdel`配置。是否终止旧 job是带审计的 operator动作；
- 初始编排优先使用 Phase 0确认可用的 PBS job array；若 array不可用，则使用由 manifest列出的独立 learner jobs。两者都使用 bootstrap slot和同一个 run descriptor，不退回单一 `mpirun` 角色混合作为正式独立作业验收。

---

## 2. 权威状态、查询面与 writer

### 2.1 Phase 1 权威链

```text
SQLite syncer_leader row
    └── 产生单调 LeaderToken(run_id, epoch, owner_id)

epoch-scoped weight/outer payload
    ↓
同一 SQLite 中带 token 的 business transaction
    ├── global_versions / update transitions
    ├── controller_state / terminal_state
    └── control_publications manifest
    ↓
当前 epoch canonical control artifacts
    ├── latest/head.json + immutable latest/vNNN.json
    ├── terminal/stop_gNNN.json
    └── terminal/summary_gNNN.json
    ↓
control/latest.json、stop.json、summary.json
    └── 仅为 convenience cache，可被旧 writer污染，不是权威
```

SQLite transaction 是训练状态的唯一提交点。learner 不打开 SQLite，而是：

1. 从 bounded `control/syncer_epochs/` 选择最高合法 epoch；
2. 读取该 epoch 自己目录中的 canonical head/terminal artifact；
3. 只把全局固定 cache作为快速路径，并校验 `published_by_epoch/owner_id`；
4. 固定 cache低于最高 epoch或字段/hash不一致时忽略。

### 2.2 为什么采用 epoch canonical artifact

普通共享 FS 的 `os.replace` 没有 fencing token/CAS。旧 leader可能在写前检查 lease 后被 `SIGSTOP`，待新 leader完成后再恢复并替换固定 JSON。因此本计划不再要求“旧进程永远不能改固定 cache hash/mtime”，而要求：

- 旧进程只能继续写自己的 epoch目录；
- 新 epoch canonical path不与旧 epoch共享；
- 当前 reader只采用最高合法 epoch；
- 固定 cache被污染时可以由当前 leader修复，但正确性不依赖修复及时发生。

### 2.3 SQLite writer-lock availability boundary

安全性定义为：**新 epoch 已在 SQLite 中提交后，旧 epoch不能提交业务 transaction。**

同一 SQLite 的 `BEGIN IMMEDIATE` 提供 transaction serialization，但带来明确边界：

- 旧 leader在 transaction 外暂停：lease到期后 candidate可以取得 writer lock并 takeover；
- 旧 leader在 write transaction 内暂停：它持有 SQLite writer lock，新 candidate必须等待；
- 已持锁的旧 transaction恢复后，其 commit在线性顺序上发生在新 epoch acquire之前；
- 已过期 owner不得开始新的业务 transaction或 renew；
- 若旧 writer无限暂停，必须由 scheduler/operator终止旧进程释放 lock；默认不自动 `qdel`。

本计划不宣称仅用一个 SQLite 文件即可在“旧 writer永久持锁”时同时获得安全性和自动可用性。

### 2.4 Writer 规则

- `init-run`是唯一可创建 authority目录、正式 SQLite和bootstrap marker的角色；candidate不得执行 schema DDL；
- candidate只打开只读 bootstrap视图和 `LeaderLeaseStore`，不得获得任意业务 mutator；
- active leader的每个业务 transaction在 `BEGIN IMMEDIATE` 后校验 token；
- transaction内不得执行文件 I/O、qstat/qsub、sleep、模型计算或长时间 checksum；
- checkpoint worker只写 epoch唯一 binary，不写 DB/control cache；
- control publisher只写当前 token对应的 epoch目录；全局固定 cache仅在 canonical artifact完成后 best-effort更新；
- `prepare_run_dirs()`必须拆成 initializer/leader使用的 `prepare_authority_dirs()` 和 learner使用的 `prepare_instance_dirs(instance_id)`；learner不得创建 `control/weights/optim` 或其他非自身 identity目录；
- learner只写自己的 instance目录、heartbeat、proposal pointer和payload；dynamic实例名为 `learner_li_<uuid4>`；
- Checker/analysis只读 live run，不修复 DB/control文件；
- candidate、leader epoch、learner instance分别写独立 JSONL/CSV，不共享单 writer文件。

---

## 3. 故障模型与非目标

必须覆盖：

- Python exception、`SIGKILL`、`SIGSTOP/SIGCONT`；
- 节点/job消失和 queued/prologue/running/finished/unknown scheduler状态；
- checkpoint完成前后、DB transaction前后、canonical artifact前后、固定 cache替换前后的崩溃；
- qsub成功但 receipt/DB 尚未记录；
- duplicate physical PBS jobs；
- learner暂停、永久死亡、同 placement新 incarnation；
- shared FS metadata短暂延迟、SQLite busy和有界节点时钟偏差。

明确假设：

- 已 fsync 的普通文件和 committed SQLite transaction不会被底层静默破坏；
- shared FS 对 `mkdir` 和同目录 `os.replace` 提供项目当前依赖的原子可见性；
- Miyabi wall-clock skew可测且小于配置上限；lease等待使用 wall clock，进程内停滞计时使用 monotonic clock；
- PBS不保证 exactly-once qsub；系统只保证 logical request at-most-one admission；
- 不是 Byzantine/恶意 actor模型；
- source root在 live run期间不可变，并可由 fingerprint验证。

非目标：

- fragment HA、fragment dynamic membership、fragment resume；
- 主动 scale-in、自动 qdel健康 job；
- inner optimizer/iterator exact replay；
- scheduler层物理 exactly-once submission；
- 外部 consensus/fencing service；
- socket/RPC/inotify correctness path；
- 自动迁移 pre-HA live run到 fenced runtime。

---

## 4. 模式、兼容性与 schema bootstrap

### 4.1 模式矩阵

| 模式 | Phase 1 | Phase 2 |
| --- | --- | --- |
| full + `syncer_ha.enabled=false` + static | 保留 legacy single-syncer，默认路径 | 保留回归 |
| full + `syncer_ha.enabled=true` + static | 正式 HA 路径 | 保留回归 |
| full + HA + dynamic | 不在 Phase 1启用 | Phase 2正式路径 |
| fragment + HA/dynamic | fail closed | fail closed |
| fragment + HA disabled + static | 保持现状并回归 | 保持现状 |

默认值：

- `coordination.syncer_ha.enabled=false`；
- `recovery_submission.enabled=false`；
- `membership.mode=static`；
- `scaling.enabled=false`。

### 4.2 Bootstrap顺序

不得继续使用“任意进程 connect 后立即 executescript/ALTER”的无条件流程，也不得让 candidate 在 lease 表尚不存在时同时承担 schema 创建和 acquire。新 run 采用独立、一次性的 `init-run` 控制面步骤：

1. launcher/operator 在提交任何角色前运行 `python -m fs_diloco.tools.init_run`；
2. initializer 原子创建新的 run root；若目录已存在则只允许对 source/config/schema 完全匹配且没有业务 row 的 incomplete bootstrap 做显式恢复，否则 fail closed；
3. initializer 在同目录临时 SQLite 中以单个 transaction 创建完整目标 schema、写 `schema_meta/run identity`，执行 integrity/PRAGMA 检查，关闭连接后原子发布正式 DB；
4. initializer 最后写 `control/bootstrap_complete.json`，其中固定 `run_id`、schema/protocol version、source/config checksum和bootstrap generation；不得记录会随业务 transaction变化的整个 DB 文件 hash；
5. candidate/learner 先只读校验 complete marker 和 SQLite identity；marker 缺失或 checksum 不一致时不得 acquire/register；
6. base HA schema 已存在后，candidate 才能对 `syncer_leader` 执行 acquire；第一个成功 leader 以 fenced business transaction 初始化 v0；
7. pre-HA DB + HA enabled：fail closed并提示离线迁移不在本计划范围；
8. pre-HA DB + HA disabled：走 legacy兼容路径；
9. fragment DB + HA/dynamic：fail closed；
10. analysis/checker使用 read-only URI，永不触发 DDL。

bootstrap 不由多个 PBS candidate 竞争执行。对 incomplete bootstrap 的删除、替换或离线修复属于显式 operator 操作；live run 启动后 schema 不允许在线迁移。

现有 `connect()` 必须拆成三个没有隐式语义的入口：

- `initialize_new_run()`：只允许 `init-run` 调用，负责 DDL；
- `open_existing(expected_identity)`：只打开已经完成 bootstrap 的 DB，不执行 DDL/ALTER；
- `open_readonly()`：analysis/Checker专用，SQLite URI `mode=ro`，不执行 DDL/ALTER。

initializer在同一 transaction同时写 `schema_meta.schema_version` 和 `run_state['schema_version']`。HA open要求两者与 marker完全一致；表是否“碰巧存在”不能作为 HA 身份判据。fragment/static legacy writer通过独立 `LegacySQLiteStore` 保留当前行为，不能打开 `FencedSQLiteStore`。

版本建议：

- legacy current schema：`user_version=1`（通过离线识别适配现有 user_version=0）；
- HA schema：`user_version=2`；
- dynamic membership schema：`user_version=3`；
- `PROTOCOL_VERSION` 和 DB schema version不混用；不全局提升当前 `FORMAT_VERSION`。新文件类型各自从1开始定义 `SYNCER_HEARTBEAT_FORMAT_VERSION`、`CONTROL_EPOCH_FORMAT_VERSION`、`MEMBERSHIP_FORMAT_VERSION`，历史 reader只按对应artifact类型做兼容判断。

### 4.3 Source/config pinning

HA正式作业必须使用不可变 source root：

- 推荐 commit-specific worktree、只读代码快照或容器；
- 初始化时写 `control/run_source_manifest.json`，包含 commit、dirty flag、source fingerprint、Python入口和依赖环境摘要；
- `control/run_config.resolved.yaml` 是后续 candidate/learner唯一配置输入；
- `init-run`同时写不可变 `control/run_descriptor.json`，包含 `run_id/shared_root`、resolved config路径与checksum、source identity、mode、bootstrap slot数量和protocol/schema版本；PBS job只通过环境变量取得 shared root，再读取该 descriptor；
- PBS script在 import `fs_diloco` 前比较 expected/current commit、dirty fingerprint、resolved config checksum、protocol/schema version；
- 不匹配时不 acquire、不 register，直接 fail closed并写 candidate/learner独立日志。

正式 HA run默认要求 clean commit。若确需 dirty source，必须先创建不可变快照并以快照 hash作为 source identity，不能引用继续变化的主工作树。

---

## 5. Phase 0：可行性门禁

Phase 0 不改生产协议，只新增探针、supervisor和证据脚本。

### 5.1 FEAS-01 SQLite writer-lock probe

场景：

1. A连接同一 shared DB并 `BEGIN IMMEDIATE`；
2. A写一行但不 commit，然后 `SIGSTOP`；
3. B尝试 acquire transaction；
4. 证明 B在 A持锁时只能 busy/等待，不能成为第二 writer；
5. `SIGKILL` A后，B可 acquire、integrity正常、只看到 transaction前状态。

通过条件：实测结果与 2.3 availability boundary一致；任何 IOERR/integrity异常为 `BLOCKED`。

### 5.2 FEAS-02 old cache writer probe

场景：old writer通过 lease check后暂停；new epoch发布 canonical + fixed cache；old恢复覆盖固定 cache。

通过条件：

- 反例稳定证明固定 cache可以被旧 writer覆盖；
- reader仍选择 new epoch canonical；
- Checker报告 convenience cache污染但业务状态不失败；
- new leader修复固定 cache后恢复一致。

### 5.3 FEAS-03 clock/shared SQLite probe

- 至少两个计算节点读取相互时钟偏差；
- rollback journal、FULL synchronous、busy timeout符合预期；
- A commit后B reopen看到相同值；
- 扩展现有 `scripts/miyabi/sqlite_shared_fs_probe.py`，增加 `contend` 子命令，在跨节点 N进程 acquire/renew争抢下记录 busy次数、锁等待分布和 starvation；不新建功能重复的第二套 shared-SQLite probe；
- 压力下无 unexplained locked/IOERR/integrity failure。

### 5.4 FEAS-04 PBS capability probe

验证 compute node是否允许 `qsub/qstat`、可查询的 job字段、request ID如何进入 `Job_Name`/`Variable_List`、queued/prologue/running识别、job array能力和 job ID规范化。若自动 submission能力不足，只阻断自动 recovery/scaling，不阻断人工独立 job restart；若 array不可用，初始learner使用descriptor manifest列出的独立作业。

### 5.5 FEAS-05 source pinning probe

同 source/config/run descriptor通过；commit、dirty fingerprint、resolved config或descriptor任一变化都在 Python runtime启动前失败，且不创建 leader epoch、membership或业务 row。

### 5.6 Phase 0 Checker

新增 `scripts/miyabi/check_plan02_feasibility.py`。stdout只能输出 `PASS` 或 `BLOCKED`，structured evidence写入：

```text
reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/
  <timestamp>_phase0-feasibility_<pass|blocked>.json
```

---

## 6. Phase 1：Syncer lease、epoch publication与恢复

### 6.1 配置

```yaml
coordination:
  syncer_ha:
    enabled: false
    lease_duration_seconds: 90.0
    renew_interval_seconds: 10.0
    max_clock_skew_seconds: 2.0
    heartbeat_interval_seconds: 5.0
    heartbeat_stale_after_seconds: 30.0
    lease_busy_timeout_ms: 5000
    candidate_acquire_poll_seconds: 5.0
    candidate_wait_seconds: 180.0
    learner_recovery_wait_seconds: 1800.0
    canonical_repair_wait_seconds: 120.0
    max_retained_epoch_dirs: 32
  recovery_submission:
    enabled: false
    claim_timeout_seconds: 120.0
    reconciliation_interval_seconds: 60.0
    uncertainty_timeout_seconds: 300.0
    backoff_initial_seconds: 60.0
    backoff_max_seconds: 900.0
    max_attempts_per_observation: 3
    max_outstanding_candidates: 1
    claim_retention_seconds: 3600.0
    candidate_pbs_script: scripts/miyabi/run_syncer_candidate.pbs

io:
  checkpoint_digest_mode: off
```

校验：

```text
renew_interval > 0
lease_duration >= 5 * renew_interval
heartbeat_interval <= renew_interval
heartbeat_stale_after >= 3 * heartbeat_interval
lease_duration >= heartbeat_stale_after + 2 * max_clock_skew
0 < lease_busy_timeout_ms <= renew_interval * 1000
0 < candidate_acquire_poll <= renew_interval
candidate_wait >= lease_duration + max_clock_skew
learner_recovery_wait >= candidate_wait
canonical_repair_wait >= 2 * heartbeat_interval
max_outstanding_candidates >= 1
checkpoint_digest_mode in {off, checker, always}
```

`candidate_wait_seconds` 是 loser candidate在放弃前等待 leadership 的上限。candidate先只读观察 epoch heartbeat；只有疑似过期时才按带jitter的 `candidate_acquire_poll_seconds`尝试 DB acquire，不能在健康 leader存在时周期性争抢 writer lock。`learner_recovery_wait_seconds` 覆盖 claim、PBS排队、candidate启动和首次 canonical repair；正式值由 Phase 0/G5队列证据冻结。上面时间值是 probe起点，不是未经测量的正式默认。

### 6.2 Schema

新增或扩展：

```sql
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    protocol_version INTEGER NOT NULL,
    mode TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE syncer_leader (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    renewed_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    heartbeat_seq INTEGER NOT NULL
);

CREATE TABLE syncer_epochs (
    epoch INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    last_renewed_at REAL NOT NULL,
    final_state TEXT,
    final_at REAL,
    superseded_by_epoch INTEGER,
    source_fingerprint TEXT NOT NULL,
    config_sha256 TEXT NOT NULL
);

CREATE TABLE controller_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL,
    generation INTEGER NOT NULL,
    reason TEXT,
    requested_at REAL,
    max_terminal_version INTEGER,
    updated_by_epoch INTEGER NOT NULL,
    updated_by_owner_id TEXT NOT NULL
);

CREATE TABLE terminal_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    stop_reason TEXT NOT NULL,
    final_version INTEGER NOT NULL,
    total_seen_tokens INTEGER NOT NULL,
    finalized_by_epoch INTEGER NOT NULL,
    finalized_by_owner_id TEXT NOT NULL,
    finalized_at REAL NOT NULL
);

CREATE TABLE control_publications (
    kind TEXT NOT NULL,
    logical_generation INTEGER NOT NULL,
    published_by_epoch INTEGER NOT NULL,
    published_by_owner_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(kind, logical_generation, published_by_epoch)
);

CREATE TABLE gc_candidates (
    relative_path TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL,
    owning_epoch INTEGER NOT NULL,
    publication_id TEXT NOT NULL,
    state TEXT NOT NULL,
    not_before REAL NOT NULL,
    recorded_by_epoch INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    deleted_at REAL
);
```

`global_versions` 增加 `commit_epoch/commit_owner_id/publication_id/weight_size_bytes/optim_size_bytes` 以及 nullable `weight_sha256/optim_sha256`；HA v0也记录 epoch 1。binary digest模式为 `off|checker|always`，默认 `off`：

- `off`：publisher、resume和completed Checker都不计算checkpoint SHA-256；通过唯一path、DB记录size、实际file size、safetensors header/loadability和DB引用一致性验证；
- `checker`：publisher不计算，completed Checker只把digest写入report artifact，不回写live DB；
- `always`：publisher在DB commit前计算并持久化digest，显式接受额外critical-path成本。

现有proposal payload的 `io.compute_sha256` 语义不被该字段改变。control JSON、source/config manifest等小文件仍强制 SHA-256。`updates` 增加 nullable `selected_by_epoch/applied_by_epoch/dropped_by_epoch`，legacy/fragment路径保持可读。

### 6.3 Leader token与 transaction规则

`LeaderToken(run_id, epoch, owner_id)` 不可变。`owner_id` 使用：

```text
<pbs-job-or-local>:<hostname>:<pid>:<uuid4>
```

Acquire：

1. `BEGIN IMMEDIATE`；
2. row不存在则创建 epoch 1；
3. released row立即允许 `epoch+1`；
4. active row仅在 `now > lease_expires_at + max_clock_skew` 后允许 takeover；
5. 插入 epoch history、更新 singleton、commit。

Renew：独立短超时 connection，只允许完全匹配 owner；`busy_timeout <= renew_interval`；expiry后不能原地复活。leader在每次成功renew时记录 `monotonic_at_last_successful_renew`，自身判断剩余租约只使用 monotonic elapsed；DB中的 wall-clock expiry只供其他节点做takeover判断，NTP跳变不能延长leader自认的租约。

业务 transaction：

1. `BEGIN IMMEDIATE`；
2. 校验 row中的 epoch/owner仍匹配；
3. 新 transaction还必须确认未超过 lease安全边界；
4. 完成所有短 DB写；
5. commit前再次确认 row匹配；
6. 因本 transaction持有 writer lock，successor acquire只能在线性顺序上发生在其 commit/rollback之后。

原始 `SQLiteStore.execute()` 和直接暴露的 writable `store.conn`不得成为绕过 fencing的生产接口。读操作与 test-only inspection接口分离。store明确拆分：

- `LegacySQLiteStore`：HA disabled的static full和fragment；不得接受 leader token；行为与Plan 01基线逐字节回归；
- `FencedSQLiteStore`：仅HA full/dynamic；所有公开mutator必须要求 `LeaderToken`；
- `ReadOnlySQLiteStore`：analysis/Checker；SQLite层强制只读。

P1-L2开始时必须重新生成并冻结 mutator inventory。当前接触面为31个写方法，其中6个已有显式 `BEGIN IMMEDIATE`、25个为autocommit或transaction helper；不能只修改 `commit_full_merge()`：

```text
full/common:
  set_run_state, upsert_global_version, _set_run_state_in_transaction,
  initialize_full_run, commit_full_merge, upsert_learner,
  update_learner_status, insert_update_metadata, mark_updates_selected,
  mark_updates_applied, reset_selected_to_pending,
  reset_all_selected_to_pending, prepare_full_resume, drop_updates,
  drop_obsolete_updates, drop_ineligible_updates,
  finalize_unconsumed_updates, drop_superseded_updates,
  delete_archived_rows, clear_gc_pending_paths

fragment legacy:
  upsert_fragment_definition, upsert_fragment_version,
  insert_fragment_update_metadata, mark_fragment_updates_selected,
  mark_fragment_updates_applied, reset_fragment_selected_to_pending,
  reset_all_fragment_selected_to_pending, drop_fragment_updates,
  drop_obsolete_fragment_updates, drop_ineligible_fragment_updates,
  drop_superseded_fragment_updates
```

inventory必须记录每个方法的新owner store、transaction边界、token要求、调用点和对应RED test。full/common中供legacy full复用的方法不得靠optional token分支；应由明确实现/adapter保持两条写路径可审查。

### 6.4 Epoch checkpoint与 control布局

```text
weights/epochs/e000007/<owner-short>/global_v000018_p<publication-short>.safetensors
optim/epochs/e000007/<owner-short>/outer_v000018_p<publication-short>.safetensors

control/syncer_epochs/e000007_<owner-short>/
  heartbeat.json
  latest/
    head.json
    v000018.json
  terminal/
    stop_g000001.json
    summary_g000001.json
```

规则：

- binary目标路径由随机 `publication_id`唯一化，目标已存在一律视为collision并 fail closed；小型immutable control artifact已存在且 checksum不同则 fail closed；
- DB保存binary相对路径、size和可选checksum，以及control相对路径和必填checksum；不从文件名推断 authority；
- `head.json` 只在同 epoch目录内原子替换；
- new leader先从 DB重建自己 epoch的 `latest/vN.json + head.json`，再发布 heartbeat；
- `control/latest.json/stop.json/summary.json` 为 best-effort镜像；
- 目录式epoch布局保持不变；`RunPaths`新增 `iter_epoch_weights/iter_epoch_optim/iter_instance_heartbeats/iter_instance_pointers/iter_instance_payloads` 等mode-aware递归iterator，maintenance、Plan 01 Checker、publication crash probe、liveness、analysis和metrics不得保留自有glob；每个测试必须断言期望非空的扫描面确实非空；
- maintenance禁止“目录扫描结果减DB引用后立即unlink”。明确orphan写入 `gc_candidates`，或由reconciler在 `lease_duration + max_clock_skew` grace后登记；已登记path永远不得再被业务transaction引用；
- deletion worker每个文件删除前用短fenced transaction重新校验token、candidate状态、grace和全部DB引用，失败即停止本轮。校验后即使暂停，目标也只可能是已冻结的旧publication path；new epoch使用不同目录/publication ID；
- GC测试必须在旧leader的扫描/删除循环中间暂停，new epoch提交后恢复旧进程，并证明current weight/outer路径存在且checksum或size不变，同时实际清理的预期orphan数量大于0；
- 任何 old epoch恢复只能影响其旧目录/全局镜像，不能创建或修改 new epoch canonical path。

### 6.5 Learner current-epoch reader

learner维护：

- `highest_observed_epoch/owner_id`；
- 当前 epoch最后 heartbeat seq前进的 local monotonic时刻；
- 当前 global version单调水位；
- terminal/control generation单调水位。

冷启动扫描 bounded epoch目录，读取合法 heartbeat/terminal marker，选择最高 epoch；运行中 lower epoch永不使状态回退。若固定 cache的 publisher低于最高 epoch，直接读取 current epoch canonical head。

现有 learner watchdog必须改为 epoch/recovery-aware：

- syncer liveness只由“最高合法epoch heartbeat seq在本地monotonic时间是否前进”判断，不能用global version长期不变替代heartbeat停滞；
- 只要当前heartbeat仍合法，no-progress计时不得触发 `syncer_unresponsive`；
- heartbeat已stale但存在未过期claim，或qstat确认candidate处于queued/prologue/running/submission_unknown且仍在 `learner_recovery_wait_seconds` 内时，learner继续等待和本地可安全工作，不因原600秒默认watchdog退出；
- `control/stop.json` 的“文件存在”不是停止依据；必须通过最高epoch、owner、generation和canonical hash校验；
- learner观察到更高epoch但canonical head尚未出现时，最多以 `canonical_repair_wait_seconds` 为一个告警窗口。窗口到期只增加 `cache_rejected_lower_epoch_count/canonical_repair_wait_count`、重新扫描并允许recovery流程继续，不得直接退出或接受低epoch cache；
- recovery总等待超过冻结上限且没有合法leader/candidate时，写明确 `syncer_recovery_exhausted` 受控停止原因，不能复用含混的 `syncer_unresponsive`。

### 6.6 Syncer日志、metrics与W&B

```text
logs/candidates/<owner>.jsonl
logs/syncers/e000007_<owner-short>.jsonl
metrics/syncer_epochs/e000007_<owner-short>.csv
```

candidate只写 candidate log。W&B在 acquire + source/config验证 + resume成功后初始化。takeover使用同一 logical run时必须把 epoch作为字段；terminal summary由最终 completed Checker或 final leader根据 DB terminal row写，不接受旧 epoch覆盖。

### 6.7 Recovery claim与scheduler reconciliation

Observation key：

```text
sha256(run_id, highest_epoch, heartbeat_seq, heartbeat_fingerprint)
```

布局：

```text
control/syncer_launch_claims/<observation-key>/
  attempt_000001.lock/
    claim.json
    submission.json
```

所有 learner先扫描现有 attempt：

- 最新 attempt仍在 claim timeout内：不创建下一 attempt；
- 有 receipt且 job queued/prologue/running：无论wall-clock TTL多久都保留outstanding/reserved状态且不重提，只有qstat确认job终态/不存在后才能释放；
- qsub后无 receipt：等待 uncertainty timeout并先按 request fingerprint做 qstat reconciliation；
- 只有不超过 max attempts、没有 outstanding candidate且 backoff到期时，竞争 `max_attempt+1` 的 atomic mkdir；
- qsub成功后写 receipt；重复 physical job由 leader lease吸收；
- stop/terminal generation已合法发布时不创建 claim；
- candidate在健康heartbeat存在时只读等待，不打开写transaction；本计划没有自动 `qdel`路径。

### 6.8 Phase 1 bounded state

active live set：current leader epoch、current control publication、DB current checkpoint、未过 retention的 candidate claim、仍可能恢复的旧 job epoch目录。归档：

```text
metrics/syncer_epoch_history.jsonl
metrics/recovery_submission_history.jsonl
```

归档 fsync并在 DB/FS引用解除后清理。至少做 1000 synthetic takeover/claim cycle，证明 active row、epoch目录、claim目录、used pages在 warm-up后有界；历史 JSONL允许增长但不参与 runtime discovery。

---

## 7. Phase 1 不变量、测试和 Gate

### 7.1 核心不变量

| ID | 不变量 |
| --- | --- |
| HA-01 | epoch单调递增且不复用。 |
| HA-02 | successor epoch提交后，旧 token不能提交业务 transaction。 |
| HA-03 | HA full的全部业务mutator只存在于 FencedSQLiteStore，DB transaction是 global version/update/controller唯一提交点。 |
| HA-04 | 每个 committed version唯一、连续并记录 commit epoch/owner。 |
| HA-05 | checkpoint和canonical control path按 epoch隔离，所有扫描通过RunPaths递归iterator且不得静默返回空。 |
| HA-06 | 固定 latest/stop/summary不是权威，污染不影响 reader/Checker。 |
| HA-07 | takeover从 DB current row恢复并从 N提交 N+1。 |
| HA-08 | DB commit后/control publish前崩溃可由新 leader修复。 |
| HA-09 | heartbeat与model latest分离，learner watchdog理解epoch、recovery claim和canonical repair窗口。 |
| HA-10 | expired owner不能开始新业务 transaction或 renew。 |
| HA-11 | DB writer-lock内暂停阻塞takeover但不产生双 writer。 |
| HA-12 | old epoch不能覆盖或GC current epoch checkpoint/canonical control；learner不能创建authority目录。 |
| HA-13 | claim/qsub不是 leadership authority。 |
| HA-14 | 同 observation/attempt只有一个 mkdir winner。 |
| HA-15 | recovery有reconciliation/backoff/budget，queued/running job在scheduler确认结束前一直计outstanding。 |
| HA-16 | source/config/run descriptor mismatch actor不能 acquire或写业务状态。 |
| HA-17 | candidate/epoch log和metrics为独立 writer。 |
| HA-18 | init-run受控DDL；pre-HA fenced resume、incomplete bootstrap与fragment HA fail closed；只读open不修改DB。 |
| HA-19 | LegacySQLiteStore的full/fragment静态回归保持现有数学、CLI与结果。 |
| HA-20 | active HA控制面和binary live set有界。 |

### 7.2 Focused测试

| 组 | 必测场景 |
| --- | --- |
| LEASE | 首次/并发 acquire、expiry/skew、late renew、release、stale release、reopen、1000竞争。 |
| SCHEMA | new run双版本标记、incomplete bootstrap、pre-HA resume拒绝、read-only open不产生表/ALTER、artifact format独立版本。 |
| FENCE | 31-method inventory、autocommit消除、global/update/run/controller/liveness/maintenance、raw execute逃逸、fragment legacy逐字节回归。 |
| DIRS | learner只创建instance目录；authority目录只由init/leader创建；dynamic/static路径正反例。 |
| PUB | 两 epoch同 target、checkpoint暂停、DB commit/control窗口、旧固定 cache覆盖、canonical选择、publication collision、off/checker/always digest。 |
| GLOB | directory epoch下weight/outer/heartbeat/pointer/payload/analysis/Checker扫描非空且实际发现预期对象。 |
| GC | old在扫描/删除循环中间暂停；new提交后old恢复；current文件不变且预期orphan删除数大于0。 |
| WATCHDOG | heartbeat有进展但version不变；claim/PBS排队超过旧600秒；lower-epoch stop；canonical repair超窗不误退出。 |
| LOCK | transaction外 SIGSTOP takeover；transaction内 SIGSTOP等待；kill旧 owner后接管。 |
| CLAIM | 8 learner同 attempt、相邻 attempt抑制、queued/prologue、qsub/receipt窗口、budget。 |
| SOURCE | commit/config/fingerprint mismatch全部 fail closed。 |
| TERMINAL | old before stop/summary/GC、new epoch finalization、old恢复污染固定 cache但不污染 authority。 |
| BOUNDED | 1000 epoch/claim循环的 active rows、dirs、used pages。 |

### 7.3 验证阶梯

- **G0**：scope、dirty worktree、schema/source pin、31-method inventory、job array/manifest编排、`miyabi-development` skill可用性和G3故障矩阵成本估算；
- **G1**：`git diff --check`、compile/lint、配置静态测试、`bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh`、literal group ID；
- **G2**：计算节点 focused tests、全部 pytest、1000竞争/有界性；
- **G3**：1节点复用现有 `publication_failpoint` 和 `scripts/miyabi/publication_crash_probe.py`，补充HA暂停点；tiny配置下每场景至少10次，并在G0记录预计wall time；
- **G4**：2节点 shared-FS takeover，分别覆盖 transaction外暂停和 writer-lock边界；
- **G5**：独立 PBS job人工 restart；若 Phase 0 PBS capability通过，再验 learner-assisted candidate；
- **G6**：9节点正式验收，1 syncer + 8 learner独立 job，至少一次真实跨job takeover、累计至少10个 committed merge；
- **G6b**：若正式 workload超过 50 local steps × 10 global steps基线，按仓库指令同步验证结论文档。

Phase 1 Checker：`scripts/miyabi/check_plan02_phase1.py --mode phase1-staged|phase1-completed`。

---

## 8. Phase 2：动态成员、stream pool和扩容

### 8.1 Identity

- `learner_instance_id = learner_li_<uuid4>`：每次进程启动唯一，复用 `LEARNER_ID_PREFIX` 并拥有自己的路径；
- `placement_id = <hostname>:<gpu-uuid-or-cpu-id>`：物理位置；
- `placement_epoch`：同 placement的 incarnation代际；
- `stream_id ∈ [0, stream_pool_size)`：固定 virtual data shard/RNG流；
- `stream_epoch`：stream被重新分配给新 instance时递增，fence旧 proposal。

stream不再单调无限增长。`stream_pool_size` 在 run初始化后不可变，dynamic iterator调用：

```text
learner_index = stream_id
num_learners = stream_pool_size
seed = training.seed + deterministic(stream_id)
```

因此 shard定义不随 active count变化。replacement复用 stream时，默认从该 stream确定性序列起点重新开始；本计划不恢复 iterator offset，必须在 telemetry/报告中标记 `stream_restarted=true`。

兼容前缀不等于继续保留固定成员白名单。dynamic heartbeat/request validator按UUID、path ownership、admission row和token验证，不调用 `valid_learner_ids(num_learners)`；liveness、maintenance、analysis和metrics统一通过 `RunPaths` iterator发现实例。

### 8.2 配置

```yaml
membership:
  mode: static
  stream_pool_size: 8
  bootstrap_instances: 8
  initial_membership_deadline_seconds: 1800.0
  registration_scan_interval_seconds: 2.0
  registration_request_ttl_seconds: 120.0
  heartbeat_stale_after_seconds: 120.0
  heartbeat_dead_after_seconds: 300.0
  revocation_grace_seconds: 60.0
  expired_retention_seconds: 600.0
  max_active_instance_records: 16
  allow_unsolicited_registration: false
  allow_healthy_placement_replacement: false
  reuse_stream_for_same_placement: true

scaling:
  enabled: false
  desired_contributors: 8
  low_contributor_threshold: 6
  consecutive_low_windows: 2
  productive_window_count: 2
  startup_grace_seconds: 180.0
  productive_upload_grace_factor: 2.0
  productive_upload_grace_min_seconds: 60.0
  productive_upload_grace_max_seconds: 600.0
  cooldown_seconds: 300.0
  max_pending_launch_requests: 2
  max_total_launch_requests: 16
  launch_request_ttl_seconds: 900.0
  capacity_observation_retention_count: 64
  scheduler_reconcile_interval_seconds: 30.0
  starvation_observation_seconds: 120.0
  learner_pbs_script: scripts/miyabi/run_dynamic_learner.pbs

terminal:
  admission_close_policy: global_target_or_launch_budget
  drain_ack_timeout_seconds: 300.0
  registration_visibility_grace_seconds: 10.0
  proposal_visibility_grace_seconds: 20.0
  max_terminal_merges: 1
  allow_preclose_admission_during_drain: false
```

校验：

```text
dynamic要求 full + HA；禁止 fragment
stream_pool_size >= bootstrap_instances
quorum_min <= desired_contributors <= quorum_max <= stream_pool_size
current admitted instance数 <= stream_pool_size
max_active_instance_records >= stream_pool_size
heartbeat_dead_after > heartbeat_stale_after
expired_retention >= revocation_grace
initial_membership_deadline >= registration_request_ttl
max_pending_launch_requests <= max_total_launch_requests
launch_request_ttl >= 2 * scheduler_reconcile_interval
low_threshold < desired_contributors
consecutive_low_windows >= 2
capacity_observation_retention_count >= consecutive_low_windows + productive_window_count
必须有 global target/manual close/deadline/有限 launch budget之一
```

`max_active_instance_records` 是current + grace的存储上限，不是可同时贡献的成员数。dynamic resolver以 `stream_pool_size`派生数据分片兼容字段；显式CLI `--num-learners`在dynamic模式拒绝，不能成为membership authority。`--learner-id`改为static模式必填、dynamic模式拒绝；dynamic进程自行生成instance ID并通过 `--bootstrap-slot` 或 `--launch-request-id`取得admission授权。

### 8.3 Dynamic schema

新增：

```sql
CREATE TABLE learner_instances (
    instance_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL,
    placement_epoch INTEGER NOT NULL,
    stream_id INTEGER,
    stream_epoch INTEGER,
    admission_token_hash TEXT,
    launch_request_id TEXT,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    gpu_identity TEXT,
    status TEXT NOT NULL,
    registered_at REAL NOT NULL,
    admitted_at REAL,
    last_seen REAL,
    last_proposal_at REAL,
    last_contributed_observation_seq INTEGER,
    drained_generation INTEGER,
    stopped_at REAL,
    expired_at REAL,
    status_reason TEXT,
    admitted_by_epoch INTEGER,
    UNIQUE(placement_id, placement_epoch)
);

CREATE TABLE placements (
    placement_id TEXT PRIMARY KEY,
    current_placement_epoch INTEGER NOT NULL,
    current_instance_id TEXT,
    reusable_stream_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE streams (
    stream_id INTEGER PRIMARY KEY,
    current_stream_epoch INTEGER NOT NULL,
    current_instance_id TEXT,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE registration_requests (
    instance_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    launch_request_id TEXT,
    placement_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    processed_by_epoch INTEGER,
    rejection_reason TEXT
);

CREATE TABLE launch_requests (
    request_id TEXT PRIMARY KEY,
    observation_key TEXT,
    bootstrap_slot INTEGER,
    role TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    not_before REAL,
    submission_attempts INTEGER NOT NULL,
    pbs_job_id TEXT,
    scheduler_state TEXT,
    scheduler_observed_at REAL,
    admitted_instance_id TEXT,
    expires_at REAL,
    reservation_released_at REAL,
    last_error TEXT
);

CREATE TABLE capacity_observations (
    observation_key TEXT PRIMARY KEY,
    observation_seq INTEGER UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    global_version INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    eligible_contributors INTEGER NOT NULL,
    selected_contributors INTEGER NOT NULL,
    productive_instances INTEGER NOT NULL,
    reserved_launch_capacity INTEGER NOT NULL,
    low_capacity INTEGER NOT NULL,
    close_generation INTEGER NOT NULL,
    recorded_by_epoch INTEGER NOT NULL
);
```

`updates` 增加：

```text
learner_instance_id
placement_id
placement_epoch
stream_id
stream_epoch
admission_generation
admission_token_hash
selected_membership_generation
```

### 8.4 Registration与安全 replacement

learner在构建 data iterator前：生成 instance ID → 计算 placement → 原子写 request → 等待 admission/rejection → 获得 stream/epoch/token → 再初始化 RNG/data。

初始成员也不属于 unsolicited registration。first leader完成v0后，在同一fenced transaction按 `bootstrap_instances` 创建确定性request：

```text
request_id = sha256(run_id, "bootstrap", slot, config_fingerprint)
reason = bootstrap
bootstrap_slot = 0..bootstrap_instances-1
state = external_submitted
```

leader随后发布 `control/bootstrap_ready_g000001.json`。job array或独立launcher给每个初始learner传唯一 `--bootstrap-slot`；learner可以先启动但必须等待ready artifact，再用对应request注册。每个slot最多admit一个instance，超出bootstrap预算后无request注册仍按 `allow_unsolicited_registration=false`拒绝。尚在scheduler queued/running的bootstrap slot计reserved capacity；initial membership deadline前不触发scale-out。

bootstrap request与scale-out request共用at-most-one admission和scheduler审计，但 `reason=bootstrap` 不消耗 `max_total_launch_requests` 或scale cooldown；它始终计入current/reserved capacity上限。

Admission transaction：

1. 验证 leader token、run/source/config/request hash和 TTL；
2. 必须关联有效bootstrap/scale-out launch request或带审计的operator replacement授权；`allow_unsolicited_registration=false`时不存在初始成员旁路；
3. request重放返回相同结果；
4. fulfilled launch request的duplicate job被拒绝；
5. scale-out request在admission时重新计算current/reserved capacity；若其他instance/request已满足desired或stream pool上限，则当前request转 `capacity_fulfilled` 并拒绝late job，防止跨request超发；
6. placement有健康 current时默认拒绝，不允许普通duplicate驱逐；
7. 只有 current已 dead/revoked/expired，或 request带明确 authorized replacement generation时才 supersede；
8. 分配空闲 stream，或对已显式 revoke的旧 instance复用 stream并增加 stream_epoch；
9. 插入 instance并存 token hash；
10. 更新 placement/stream/launch request；
11. commit后发布 admission canonical artifact。

过期/unresolved request转为 rejected/expired并归档，processed request从 discovery目录删除，确保 request surface只与 unresolved + grace相关。

### 8.5 Proposal与 final commit membership fence

proposal携带 instance、placement epoch、stream epoch和 token。摄取时验证并持久保存；selector按 current stream去重。

最终 `commit_full_merge()` 必须在同一 transaction中：

1. 验证 leader token和 predecessor；
2. 重新读取 selected update rows；
3. join instance/placement/stream；
4. 确认 instance仍 admitted/current、placement和stream epoch匹配、token hash匹配；
5. 确认 selected stream/placement唯一；
6. 验证 staleness/weight；
7. 插入 global version并原子更新 update状态。

若 membership先变化，global commit rollback，syncer丢弃已计算的内存 theta/outer并从 DB current checkpoint恢复后重选；若 global commit先取得 writer lock，则 membership transition在线性顺序上发生在其后。

### 8.6 Capacity observation与scale-out

Observation key：

- merge：`merge:<committed_global_version>`；
- starvation：由 controller transaction在 `next_starvation_observation_at` 到期时分配持久 `starvation_generation`，不是按各进程本地时间直接 floor。

同 observation transaction更新 `observation_seq` 和 `consecutive_low_count`。重复 key不新增 row、不增加 low count。

`productive_instances`：current admitted instance满足以下任一条件：

- 最近 `productive_window_count` 个唯一 observation中贡献过；
- 仍在 startup grace；
- heartbeat fresh，且 `now - last_proposal_at` 未超过 `clamp(last_cycle_step_time_mean * inner_steps * factor, min, max)`。

没有可靠 step-time/last proposal时使用 startup grace；缺失字段不得把实例永久算 productive。

触发 scale-out需同时满足：bootstrap requests已全部admitted/terminal，或已过 `initial_membership_deadline_seconds`且scheduler reconciliation完成；最近 N个唯一 observation都 low；productive + reserved < desired；pending/max total/cooldown满足；admission open；有空闲 stream；`current admitted + reserved <= stream_pool_size`。request ID由 `(run_id, observation_key, ordinal, config_fingerprint)`确定，一次 observation最多一个 request。

只在DB保留最近 `capacity_observation_retention_count` 条已处理observation；更旧row先fsync归档到history，再由fenced GC删除。low counter、productive window和Checker都不得依赖已经归档的隐式全表扫描。

### 8.7 Launch outbox

状态：

```text
planned -> submitting -> submitted -> started -> admitted -> completed
external_submitted(bootstrap) -> started -> admitted -> completed
submitting/submitted -> submission_unknown -> reconciled|retryable
nonterminal -> failed|expired|cancelled|capacity_fulfilled
```

qsub/receipt/DB crash window允许物理重复，但 admission transaction对 `launch_request_id`最多一个 winner。outbox先 reconcile再 retry。`launch_request_ttl_seconds`只约束尚无scheduler确认的 planned/submission_unknown授权；一旦qstat确认job queued/prologue/running，无论wall-clock TTL多久都持续计reserved capacity，直到scheduler确认terminal/absent并完成reconciliation，不能因TTL释放容量后再创建跨request duplicate。所有 job启动前执行 source/config gate。

### 8.8 Dynamic drain acknowledgement

Controller state：

```text
open -> draining(close_generation) -> closed -> terminal
```

进入 draining 后：

- 不创建 launch request；
- 所有未 admission logical request转 cancelled，后续 physical job registration被拒绝；
- 默认不 admission pre-close request；
- current learner读取 current epoch `drain_gNNN.json`；
- learner完成已经开始的 local cycle，最多发布一份 final proposal，然后停止发布；
- learner写 heartbeat/ack：`status=drained, close_generation, final_update_id`；
- 未响应实例到 drain timeout后由 leader显式 revoke；
- takeover从 DB controller state重建同一 generation，不能创建新 generation。

进入 draining 的同一 transaction 必须冻结 `max_terminal_version`：target-driven close取配置的 global target；manual/budget/deadline close取 `min(global_target, current_version + max_terminal_merges)`。因此 final proposal不等于保证合并，terminal drain永远不得提交超过该上限；剩余合法 proposal最终标记为 `stopped_unconsumed`。takeover必须沿用已持久化的上限。

`input_closed` 当且仅当：

```text
admission_state == closed
AND every current instance is drained/stopped/revoked
AND no logical launch request can still admit
AND no unexpired registration request remains
AND final pointer ingestion completed
AND registration/proposal visibility grace completed
```

随后在 `max_terminal_version` 预算内执行严格 terminal drain，再提交 DB terminal state和 current epoch stop/summary canonical artifact。future/stale/incarnation fence不因尾部 quorum降低而放宽。

### 8.9 Phase 2 bounded state

runtime discovery只通过共享 `RunPaths` iterator扫描：current/grace instance、unresolved registration、nonterminal launch request、每 current instance一个 pointer、最近 `capacity_observation_retention_count` 条observation。scheduler仍确认queued/running的request不能仅因TTL归档。过期 identity和request先 fsync归档到：

```text
metrics/learner_instance_history.jsonl
metrics/registration_history.jsonl
metrics/launch_request_history.jsonl
metrics/membership_event_history.jsonl
metrics/capacity_observation_history.jsonl
```

1000 churn后 active DB rows、pointer、request文件、used pages和单轮 scan项目不得随历史线性增长。

---

## 9. Phase 2 不变量、测试与 Gate

### 9.1 核心不变量

| ID | 不变量 |
| --- | --- |
| MEM-01 | `learner_li_<uuid4>` instance每次启动唯一，dynamic CLI不接受static learner ID。 |
| MEM-02 | placement、instance、stream/stream_epoch不混用；dynamic发现不使用固定learner白名单或散落glob。 |
| MEM-03 | stream pool固定且有界。 |
| MEM-04 | data shard/RNG映射只由固定 stream pool决定。 |
| MEM-05 | 同 placement最多一个 current epoch。 |
| MEM-06 | 同 stream最多一个 current stream epoch。 |
| MEM-07 | 健康 placement不能被普通duplicate registration驱逐。 |
| MEM-08 | bootstrap/scale-out registration都必须有request；重放幂等，过期或超预算request不能admission。 |
| MEM-09 | old instance恢复不能重新成为 current。 |
| MEM-10 | heartbeat不决定已摄取 proposal是否提交。 |
| MEM-11 | final global commit transaction重验 membership/stream。 |
| MEM-12 | 一次 merge每 current stream/placement最多一份。 |
| MEM-13 | logical launch request最多一个 admitted instance。 |
| MEM-14 | capacity observation key幂等。 |
| MEM-15 | 连续 low count只基于不同 observation。 |
| MEM-16 | cooldown/pending/reserved/max total共同限制扩容；queued/running job在scheduler确认结束前不释放reserved。 |
| MEM-17 | draining后不再创建或 admission新 logical capacity。 |
| MEM-18 | healthy learner通过 drain ack停止发布。 |
| MEM-19 | input_closed满足完整 ack/revoke/request/final visibility谓词。 |
| MEM-20 | dynamic active state和递归discovery面逻辑/物理有界且非静默空集，static/fragment兼容回归通过。 |

### 9.2 Focused测试

| 组 | 必测场景 |
| --- | --- |
| ID/STREAM | `learner_li_`校验、dynamic CLI、同 host重启、pool exhausted、stream复用/epoch、iterator mapping、restart标记、所有扫描面非空。 |
| REG | 8个bootstrap slot无scale request成功admit、第9个无request拒绝、request重放/过期/path/source mismatch、healthy placement拒绝、authorized replacement。 |
| COMMIT | selection后/读 tensor后/outer后 revoke或supersede，验证 commit或membership只有一个先发生。 |
| OUTBOX | qsub前后 crash、receipt前后 crash、queued超过TTL仍reserved、scheduler absent后释放、unknown scheduler、duplicate job、leader takeover。 |
| SCALE | initial deadline前不扩容、单次 low、连续两个不同 low、重复 observation、6→7 reset、starvation、cooldown、reserved与stream上限。 |
| DRAIN | global/manual/budget/deadline close、healthy ack、timeout revoke、late proposal、queued job、takeover。 |
| BOUNDED | 1000 churn active rows/pointers/requests/observations/used pages；iterator实际发现数量非零且符合预期。 |
| COMPAT | legacy static full结果、fragment HA/dynamic拒绝、historical analysis只读。 |

### 9.3 验证阶梯

- **G7**：1节点 synthetic state machine、mock scheduler、1000 churn；
- **G8**：2节点独立 syncer/learner job，replacement、stream复用、syncer takeover、drain ack；
- **G9**：最多9个并发计算节点的dynamic正式验收：1 syncer + 8 bootstrap learner达到稳定态；永久终止一个learner并由scheduler确认释放后，连续两个唯一low observation创建一个replacement request，恢复到1+8；另一个learner短暂停顿；注入duplicate physical job并完成dynamic close。扩容不会与被终止job同时占用第10个节点；
- **G9b**：正式 workload超过仓库 50×10基线时同步文档和实验报告。

Phase 2 Checker：`scripts/miyabi/check_plan02_phase2.py --mode phase2-completed`。

---

## 10. Loop Engineering

### 10.1 Phase 0

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P0-L0 | 冻结审查问题和probe口径 | supervisor/probe骨架 | feasibility artifact + Checker |
| P0-L1 | writer-lock/cache反例 | 可重复故障注入 | 1/2节点证据，确定availability边界 |
| P0-L2 | clock/PBS/source假设 | capability probes | 能力矩阵和 Phase 1 feature flags |

### 10.2 Phase 1

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P1-L0 | bootstrap/pre-HA/fragment、job编排、31-method inventory | 三种open API、schema/artifact version、run descriptor | analysis只读、CLI/编排冻结和兼容回归 |
| P1-L1 | lease竞争/expiry/lock | LeaderLeaseStore | 1000竞争、kill/reopen、clock injection |
| P1-L2a | 25个autocommit/隐式DDL反例 | Legacy/Fenced/ReadOnly store与显式事务边界 | 31-method checklist、fragment逐字节回归 |
| P1-L2b | stale mutator成功反例 | 全HA mutator fence注入 | AST/API审查、rollback和raw escape覆盖 |
| P1-L3 | checkpoint/control clobber和空glob | epoch binary + canonical control + RunPaths iterator | existing failpoints、old cache反例、非空扫描、reader repair |
| P1-L4 | old finally/GC/watchdog污染 | controller/terminal DB state、gc ledger、epoch-aware watchdog | stop/summary/循环中暂停GC/W&B/recovery wait矩阵 |
| P1-L5 | source/log并发 | source gate和epoch writer paths | mismatch、candidate loser、takeover logs |
| P1-L6 | claim storm/qsub窗口 | reconcile/backoff/budget | mock和真实PBS能力允许路径 |
| P1-L7 | active state增长 | epoch/claim archive与GC | 1000 cycle logical/physical boundedness |
| P1-L8 | 集群验收 | independent launchers/checker | G3→G6，docs/reports同步 |

### 10.3 Phase 2

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | CHECK/PERSIST |
| --- | --- | --- | --- |
| P2-L0 | stream超界、动态shard、CLI/glob反例 | bounded stream pool、instance validator、mode-aware iterator | iterator/RNG/restart和非空discovery测试 |
| P2-L1 | bootstrap失败/stale/duplicate registration | bootstrap requests和admission/replacement transaction | slot预算、source/path/TTL/cache repair |
| P2-L2 | membership selected race | commit-time membership join | failpoints和DB线性顺序 |
| P2-L3 | O(history) discovery | active set + archive/GC | 1000 churn rows/files/pages |
| P2-L4 | outbox crash matrix | request/receipt/reconciliation | duplicate admission拒绝 |
| P2-L5 | duplicate low observation | idempotent capacity controller | merge/starvation/takeover |
| P2-L6 | healthy learner永不闭合 | drain directive/ack | timeout/revoke/late publish/terminal |
| P2-L7 | 集群chaos | dynamic PBS/checker/analysis | G7→G9，docs/reports同步 |

每个 loop完成后向 `reports/DOING/fsb_decoupled_diloco_plan_02/progress.md`追加；失败先写 `failures.md`；同一 experiment连续三次失败后停止局部试错并完成 `code_review.md`。

---

## 11. 性能、可靠性与统计口径

### 11.1 Phase 1

- `lease_renew_failures=0`（normal run）；
- renew p99 `< lease_duration/4`，样本只计真实 renew transaction，至少100样本；
- business transaction p99 `< renew_interval/2`，max必须记录；
- takeover protocol latency从旧 lease过期边界到新 epoch DB commit，不含 PBS queue，门槛 `<= 2*renew_interval + 10s`；
- transaction内 writer-lock pause单独报告，不计入自动 takeover latency；
- lease/heartbeat控制面 CPU time和阻塞 critical-path wall time分别报告，不把后台重叠 wall time简单相加；
- 健康leader存在时启动候选观察者，`sqlite_commit_seconds` p99相对无候选matched run回归门槛在P1-L1 RED测试冻结，candidate写transaction尝试数应为0；
- checkpoint digest默认 `off`，publisher、resume和Checker均不得新增全量hash读取；`checker`模式单独报告离线hash wall time，`always`模式单独报告publish关键路径hash wall time；normal publish p99与Plan 01 matched baseline的允许回归在P1-L3 RED测试冻结；
- fixed cache污染允许出现，但 current canonical adoption错误严格为0；
- stale epoch业务 commit数严格为0。

### 11.2 Phase 2

- 每 request admitted instance `<=1`；
- 任意时刻 current admitted instances + scheduler-confirmed reserved capacity `<= stream_pool_size`；
- 每 observation key DB row严格为1；
- cooldown内 scale-out request `<=1`；
- admission closed后新 launch/admission均为0；
- input_closed成立后在 `2*scan_interval + proposal_visibility_grace` 内进入 terminal drain，不等待完整 no-progress timeout；
- 1000 churn在前100次 warm-up后，对 active rows/pointers/request files/used pages拟合线性斜率；斜率门槛在 P2-L0 RED test中冻结，不得事后放宽；
- dynamic额外控制面 critical-path wall time `< matched static run complete time * 5%`。matched run必须具有相同 source/config/model/data/seed/global target，唯一差异为 membership/scaling开关。

所有 p95/p99报告样本数、warm-up、聚合方式和缺失字段。Checker遇到核心指标缺失返回 `BLOCKED`。

---

## 12. 预计代码和文件影响

新增：

```text
fs_diloco/storage/schema_bootstrap.py
fs_diloco/storage/leader_lease.py
fs_diloco/storage/fenced_store.py
fs_diloco/protocol/control_epoch.py
fs_diloco/protocol/membership.py
fs_diloco/protocol/dynamic_terminal.py
fs_diloco/runtime/pbs_scheduler.py
fs_diloco/runtime/launch_outbox.py
fs_diloco/tools/init_run.py
fs_diloco/tools/launch_independent_run.py
scripts/miyabi/check_plan02_feasibility.py
scripts/miyabi/check_plan02_phase1.py
scripts/miyabi/check_plan02_phase2.py
scripts/miyabi/run_syncer_candidate.pbs
scripts/miyabi/run_dynamic_learner.pbs
scripts/miyabi/run_2node_syncer_takeover_regression.pbs
scripts/miyabi/run_9node_dynamic_resilience.pbs
```

修改：

```text
fs_diloco/core/config.py
fs_diloco/core/constants.py
fs_diloco/modeling/hf_data.py
fs_diloco/storage/schema.sql
fs_diloco/storage/sqlite_store.py
fs_diloco/storage/paths.py
fs_diloco/storage/maintenance.py
fs_diloco/protocol/liveness.py
fs_diloco/protocol/merge.py
fs_diloco/runtime/syncer.py
fs_diloco/runtime/learner.py
fs_diloco/runtime/adoption.py
fs_diloco/tools/analysis.py
fs_diloco/tools/run_metrics_csv.py
configs/*.yaml
scripts/miyabi/sqlite_shared_fs_probe.py
scripts/miyabi/check_plan01_invariants.py
scripts/miyabi/publication_crash_probe.py
scripts/miyabi/*.pbs
scripts/miyabi/*.sh
README.md
plans/ref/实施计划制定与 Agent 执行经验.md
docs/02-architecture.md
docs/03-runtime-flow.md
docs/04-data-flow.md
docs/05-code-structure.md
docs/06-configuration.md
docs/07-operations.md
docs/modules/*.md
```

测试按 lease/fence/control/source/membership/stream/outbox/scaling/terminal/boundedness拆分，不用一个巨型 runtime test替代 transaction正例、反例和rollback检查。

---

## 13. Checker、requirement matrix与 artifact

完整 requirement映射在：

```text
plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv
```

矩阵中的 `implementation_contract/test_contract/gate/artifact_contract` 是冻结契约，不在实施中覆写。每条requirement完成关联测试后，把 `status` 更新为 `complete`，并把可复核的报告或structured artifact写入 `evidence_path`；占位值 `TBD` 或证据缺失时不得标记complete。

Checker stdout只能是：

```text
PASS
PASS_WITH_FOLLOWUPS
BLOCKED
```

`PASS_WITH_FOLLOWUPS` 仅 Phase 1 staged允许。structured evidence至少包含：source/config/run descriptor identity、schema双版本/integrity/PRAGMA、31-method mutator inventory、epoch history、version→epoch、control manifest/hash、各递归discovery面的expected/observed数量、固定 cache污染、writer-lock边界、watchdog/recovery等待、claim/job/reserved map、membership/stream generation、bootstrap slots、capacity observations、terminal generation、active/physical boundedness和 failure event扫描。

大型 checkpoint保留 run root；reports只保存manifest、路径、size、验证结果和必要快照；只有显式启用 `checker|always` 时才包含checkpoint checksum。

报告路径以scoped `plans/AGENTS.md`为准：`reports/DOING/fsb_decoupled_diloco_plan_02/`。进入Phase 0时同时把ref文档中残留的 `reports/imp_plans/<plan-id>/` 示例更新为 `reports/DOING/<plan-id>/`，不能创建第三套报告位置。

---

## 14. 停止、授权与文档同步

立即 `BLOCKED`：

- SQLite出现 IOERR/integrity failure或无法解释的跨节点 lock行为；
- fixed cache被误当作 authority；
- successor epoch提交后 stale token仍能业务 commit；
- source/config mismatch actor acquire/admit成功；
- duplicate logical request admission两个 learner；
- commit transaction接受非 current incarnation/stream；
- healthy learner未 ack/revoke却声明 input_closed；
- 重复 observation导致提前 scale-out；
- PBS `Exit_status=0` 但无真实 workload输出/状态变化。

需要用户授权：

- `qdel`旧 syncer/learner；
- 删除 live DB/checkpoint/claim；
- 修改 live job source/config；
- pre-HA迁移；
- 开启自动 recovery submission/automatic scale-out正式作业；
- 放宽可靠性/性能阈值。

文档同步：

- Phase 0：只写 reports和 research-plan范围决策，不把能力写成已实现；
- Phase 1 PASS后：README、architecture、runtime/data flow、configuration、operations、module docs；
- Phase 2 PASS后：dynamic identity、stream pool、membership/outbox、scaling、drain closure和模式矩阵；
- 具体 job/run/数字只写 reports；
- 只有代码经 9节点且 workload超过 50×10基线验证时，按根 `AGENTS.md` 更新相应 verified behavior/experiment result。

---

## 15. 发布前自检

### Phase 0

- [ ] `reports/DOING/fsb_decoupled_diloco_plan_02/{progress.md,failures.md,code_review.md,artifacts/}` 已按 `plans/AGENTS.md` 创建；
- [ ] `miyabi-development` skill在进入计算节点工作前可用；
- [ ] writer-lock pause边界有1/2节点证据；
- [ ] fixed cache stale writer反例可重复；
- [ ] reader只采用最高 epoch canonical；
- [ ] clock/shared SQLite/PBS/source能力均有 structured evidence；
- [ ] feasibility Checker `PASS`。

### Phase 1

- [ ] init/open-existing/open-readonly三条路径无隐式DDL，schema双版本一致；
- [ ] 31个current mutator均映射到Legacy/Fenced store、transaction和RED test；
- [ ] 所有HA业务 mutator transaction内 fence，fragment不用optional/no-op token；
- [ ] raw writable escape hatch已移除或封闭；
- [ ] learner只创建自己的instance目录；
- [ ] old epoch不能改 current epoch checkpoint/control；
- [ ] RunPaths递归扫描在maintenance/Checker/probe/liveness/analysis/metrics中非空且一致；
- [ ] old epoch在GC循环中恢复不能删除current checkpoint，且测试确实删除预期orphan；
- [ ] fixed cache污染不影响 learner/Checker；
- [ ] learner watchdog按epoch heartbeat/claim/recovery判断，lower-epoch stop和长PBS排队不误杀；
- [ ] candidate/epoch log单 writer；
- [ ] claim reconciliation/backoff/budget完整且默认关闭；
- [ ] transaction外 SIGSTOP takeover和 transaction内 availability boundary均通过；
- [ ] active epoch/claim状态有界；
- [ ] Phase 1 completed Checker `PASS`。

### Phase 2

- [ ] stream pool固定、有界并与 iterator API一致；
- [ ] initial bootstrap slots在无scale request时可admit且超预算拒绝；
- [ ] dynamic CLI拒绝static learner-id/num-learners权威输入；
- [ ] healthy placement不能被 duplicate驱逐；
- [ ] registration TTL/replay/source gate完整；
- [ ] final commit重验 membership/stream；
- [ ] observation幂等和 low counter正确；
- [ ] logical request最多一个 admission；
- [ ] queued/running request超过TTL仍计reserved，admitted+reserved不超过stream pool；
- [ ] healthy learner drain ack闭环完整；
- [ ] 1000 churn逻辑/物理状态有界；
- [ ] static full/fragment兼容回归通过；
- [ ] Phase 2 Checker `PASS`。
