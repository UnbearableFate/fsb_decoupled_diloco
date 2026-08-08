# Plan 03：统一 Fenced Full Runtime、修复审查问题并移除旧 writer

计划 ID：`fsb_decoupled_diloco_plan_03_unified_ha`

状态：执行中（2026-08-08，P0 completion candidate，等待 phase review）

配套 requirement matrix：

```text
plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv
```

实施记录与完成门禁遵循 `plans/AGENTS.md`：

```text
reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/
reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/<phase-id>/
```

审查依据：

- `plans/DOING/reviews/fsb_decoupled_diloco_code_architecture_review.md`；
- `plans/DONE/plan02/fsb_decoupled_diloco_plan_02.md` 及其设计、证据和最终审查；
- `plans/ref/实施计划制定与 Agent 执行经验.md`；
- 当前 tracked 源码、配置、测试、launcher、PBS 脚本和文档。

本计划的静态复审基线是 commit
`7e4205adfdbcb561c752493dfbeba7976de204d7`。下文的path/symbol anchor只用于解释这次复审；执行时必须重新定位，并把实际plan branch point的完整commit ID写入`progress.md`。不得用会移动的分支名代替branch point。

---

## 0. 目标、最终结果与范围

### 0.1 最终结果

本计划完成三项结果：

1. full static 和 full dynamic 只使用一条 fenced candidate runtime；单 candidate 也必须经过 initializer、leader lease、单调 epoch、事务内 token 校验和 epoch-scoped control publication。
2. 对上次审查的 40 条 finding 逐条复现、反证或修复，优先闭合 proposal、membership、token、filesystem 和 scheduler 的安全性与活性问题。
3. 从生产路径删除 classic full writer 和 fragment V0 writer；旧完成 run 仍可只读 inspect/export，旧代码通过冻结 tag 保留。

最终权威链为：

```text
learner immutable payload / cycle receipt
    ↓ fixed atomic pointer
typed decode + identity/integrity validation
    ↓
fenced SQLite command transaction                 ← 唯一业务提交点
    ├── accepted proposal / disposition
    ├── membership / stream cursor
    ├── selection credit
    ├── token ledger
    ├── publication intent / committed version
    └── terminal / control publication ledger
    ↓
epoch-scoped canonical control
    ↓
fixed latest/stop/summary convenience cache       ← 可重建、非权威
```

第一次取得 lease 通常得到 epoch 1；同一 candidate 进程重启或重新取得 lease 时 epoch 仍必须递增，不能假定“单 candidate 永远是 epoch 1”。

### 0.2 最终支持矩阵

| 模式 | 最终状态 | 保证边界 |
|---|---|---|
| full + static + 1 candidate | 支持 | 完整 leader fencing；固定 learner ID + logical launch + per-process attempt generation，安全支持PBS rerun和显式replacement |
| full + static + 多 candidate | 支持 | single-active-leader failover |
| full + dynamic | 支持 | leader fencing + incarnation/placement/stream/admission fencing |
| learner-assisted recovery submission | 可选，默认关闭 | 只负责提交 candidate，不授予领导权 |
| classic full writer | 删除 | 仅冻结 tag 可运行 |
| fragment V0 writer | 删除 | 仅冻结 tag 可运行 |
| fragment HA | 不实现 | 下一计划重新设计 version-vector authority |
| 完成的 v1/v2/v3 full/fragment run | 只读支持 | inspect/export/eval，不写原 run |
| 未完成的旧 run 原地 resume | 不支持 | 明确 fail closed |
| torch distributed baselines | 保持支持 | 不改变DDP/periodic-average协议或正式配置语义；允许共享schema marker机械更新 |

“统一 runtime”指 full static/dynamic 共用 candidate 生命周期、publication、selection、token 和 terminal application service；它不要求 static DB 人为创建 dynamic registration/placement/stream 表。dynamic membership 是 authority v4 的可选 schema feature，见 §3.2。

### 0.3 明确非目标

- 不实现 fragment HA、version vector、RPC、对象存储或新 outer optimizer。
- 不把整个仓库一次性搬到新的 clean-architecture 目录树。
- 不支持把未完成的旧 authority DB 迁成可继续训练的 v4 DB；旧 token、cursor 和 proposal 语义不足以安全补造 v4 状态。
- 本计划不实现可恢复的 Hugging Face iterable streaming。v4 对 `data.streaming=true` fail closed；indexed/materialized 数据仍受支持。
- 不以三 seed 训练质量研究阻塞本次正确性/删码计划。数值有限性、确定性 oracle 和现有回归必须通过；正式质量比较另列 follow-up。
- 不删除 `fs_diloco/baselines/`、`torch_baseline_*.yaml` 或两个 torch baseline PBS 脚本。

### 0.4 本次复审修正的计划错误

本修订已纠正原计划中的下列问题，执行者不得恢复旧表述：

1. 分支起点不能再写成 `codex/better_docs`；当前分支图已变化，必须记录实际 commit。
2. `495 collected` 已不是当前树的可靠基线；当前新增了 torch baseline 测试。P0 必须在冻结 commit 上重新收集，后续只比较记录值。
3. 仓库已经存在通用 static HA launcher `fs_diloco/tools/launch_independent_run.py`，不得再平行实现一套 `launch_run.py`。
4. H-01 不只可能在 final commit 重试：dynamic `mark_updates_selected()` 已有 current-membership 复检，并会对 stale candidate 抛出 generic `RuntimeError`，所以当前更直接的失败可能是 syncer 退出。P0 必须分别复现 selection-time 和 commit-time race。
5. static 与 dynamic 不需要共用完全相同的物理表；强行给 static 合成 placement/stream 行会扩大迁移和故障面。
6. conflict/malformed proposal 可能根本无法插入 `updates`，因此不能靠 `updates.status='quarantined'` 表示；必须使用独立 observation/disposition/quarantine 表。
7. 旧 v3 token/cursor 信息不足，不能承诺“离线迁移后继续训练”；本计划只迁配置，不迁可恢复 run state。
8. 当前有8个fragment-enabled config和5个fragment PBS；`fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml`及对应PBS是历史fragment实验的full control，不是fragment runtime，应单独归档而不是误计或迁成新的正式full配置。
9. 当前新增 `fs_diloco/baselines/` 和 4 个 torch baseline config；所有共享 config/modeling/data 改动必须有 baseline 回归。
10. 不引入全仓纯格式化提交，也不把新引入的 pyright 设成永久阻塞门禁；只格式化本计划实际修改的文件。
11. 性能阈值不能观察结果后自动放宽为“median + 3pp”；本计划预先冻结 10% non-inferiority margin，噪声大时增加样本或报告 INCONCLUSIVE。
12. 当前已有完整的 `docs/00..07` 和 module 文档；本计划更新现有文档，只新增一份兼容/迁移文档，不再创建重复的 `protocol-v2.md` 等文档。
13. P0 的 RED reproduction 与“全量测试通过”原先冲突；本计划规定 strict xfail + `--runxfail` 证据，P2/P3 修复时移除 xfail。
14. `pending + selected <= contributors` 原门槛错误：checkpoint I/O 期间同一 contributor 合法地可以同时有 1 个 selected 和 1 个新 pending。正确上界是每 contributor 各至多一个，即总数 `<= 2M`。

---

## 1. 已核实的当前代码基线

本节是执行前事实层。若执行 branch point 相对静态复审基线发生变化，P0 必须生成 drift 报告；不能默默沿用数字或行号。

### 1.1 仓库表面

| 项目 | 当前事实 | 计划影响 |
|---|---|---|
| 测试 | 58 个 tracked `test_*.py` 文件；最后一次 Plan 02 证据是 495 tests，但之后加入 3 个 torch baseline 测试文件 | P0 重新 collect/run，不硬编码数量 |
| fenced mutator | `_BOUND_MUTATORS` 当前 42 项 | 生成逐项 `keep / merge / delete` 映射，不只做集合相等 |
| fragment | 11个`test_fragment_*.py`、8个fragment-enabled config、5个fragment PBS、3个protocol模块、4张`fragment_*`表；另有1个full no-fragment历史control config/PBS pair | P5删除前逐项迁移共享断言、单列control pair并保留legacy reader |
| HA config | 只有 4 个 tiny config 显式包含 `coordination.syncer_ha` | 所有 full config 都要迁到强制 leader config |
| config 递归面 | `configs/5000/fs_diloco_gpt2_wikitext2_8l_200x25steps.yaml` 位于子目录 | inventory 必须递归 `configs/**` |
| launcher | 已有 `init_run.py`、`launch_independent_run.py` 和 Phase 1/2 专用 launcher | 扩展既有通用 launcher；专用 launcher只保留验收用途 |
| console scripts | 已有 syncer/learner/inspect/eval 等入口和 torch baseline 入口；没有 init/launch/migrate 入口 | P4 增加 console alias，但保留现有 `python -m` 入口 |
| schema | `schema.sql` 无 CHECK；`schema_bootstrap.py` 通过 base DDL + ALTER 构造 v2/v3 | v4 使用完整 fresh DDL，不继续叠 ALTER |
| 版本 | `PROTOCOL_VERSION=3`、`HA_SCHEMA_VERSION=2`、`DYNAMIC_SCHEMA_VERSION=3`、通用 `FORMAT_VERSION=1` | 建 producer/consumer inventory；已有 wire 只递增并拆分用途，新显式 schema 从 1 开始 |
| 工具 | dev 依赖只有 pytest/ruff；无 Hypothesis、pytest-timeout、pyright 配置 | 只把 Hypothesis/pytest-timeout 作为必需 dev 依赖；pyright 可选 |
| baselines | `fs_diloco/baselines/` 复用 `resolve_config`、`hf_data` 和 learner optimizer helper | 提取共享 modeling helper并强制回归，不误删 |

### 1.2 审查 finding 的准确落点

| Finding | 当前符号/位置 | 已核实事实 |
|---|---|---|
| H-01 | `SQLiteStore.eligible_updates` | 未按 current dynamic membership 过滤 |
| H-01 | `FencedSQLiteStore.mark_updates_selected` | selection 时已经 current-check；任一 stale row 导致整批 generic `RuntimeError` |
| H-01 | `revoke_dead_instances`、authorized replacement、drain timeout | 多条 revocation 路径都没有统一终结该 incarnation 的 pending/selected rows |
| H-01 | `run_syncer` 的 `DynamicMembershipFenceError` 分支 | commit-time race 会把整批 selected reset 为 pending |
| H-02 | `run_learner` mid-cycle replace | 改 base，但不清 `losses/interval_tokens/interval_examples` |
| H-03 | `insert_update_metadata` | 先 supersede，再 `INSERT OR IGNORE`，最后无条件推进 frontier |
| H-04/H-05 | `protocol/merge.py` | token 未逐项拒绝 non-finite/negative；quorum 截断可固定偏向低 ID |
| H-06 | `safe_read_json`、`Path.exists()` 调用 | transient I/O、not-found 和 malformed 被混为一次性永久失败 |
| H-07 | `LearnerLaunchOutbox.reconcile` | 已知 job ID 的 `no_record` 会立即 failed；query uncertainty 没有完整 deadline |
| H-08 | `total_seen_tokens` | 实际是 committed selected proposal token，不是物理 processed token |
| H-09 | `phase2_matched_evidence.py` | `max(0, dynamic-static)` 会隐藏巨大负差异和不可比 workload |
| M-02/M-03 | `modeling/hf_data.py` | streaming flag 仍 materialize；replacement 从 stream 起点重启 |
| M-04 | `write_update` | `uuid4().hex[:12]` + replace publication，不是真正 create-if-absent |
| M-05 | `tools/init_run.py` | final root 先创建，后续失败留下不可重试的半初始化目录 |

### 1.3 H-01 的修正判定

静态证据只能证明 stale proposal 可以进入 selector。当前有两条不同故障路径：

```text
revoke before selection
→ stale row 仍 eligible
→ selector 可能选择 stale row
→ mark_updates_selected current-check 失败
→ 当前 syncer 可能直接退出

revoke after selection
→ checkpoint 已写
→ commit_full_merge membership fence 失败
→ 整批 reset pending
→ stale row 可能再次进入选择
```

P0 必须分别确认这两条；不得把尚未发生的“确定性 livelock”当作既成事实。无论表现为 crash 还是 retry loop，修复目标都是 current quorum 存在时 stale incarnation 不阻塞合法 batch。

---

## 2. 权威、writer 与故障模型

### 2.1 writer 规则

| Writer | 允许写入 | 禁止写入 |
|---|---|---|
| initializer | staging run root、descriptor、完整 v4 schema、bootstrap marker、artifact policy | global version、proposal |
| launcher/orchestrator | launcher-owned immutable launch plan、per-request qsub intent/receipt和partial-failure evidence | authority DB、leader/control state、自动qdel |
| operator request tool | expected-state-hash绑定的immutable resolution request | authority DB直写、无reason mutation、隐式admission/qdel |
| active leader | 自己 epoch 的 checkpoint/control；带 token 的 authority command | raw SQL、其他 epoch control、无 token mutation |
| learner | 自己的 payload、proposal pointer、cycle-receipt pointer、heartbeat/registration | authority DB、leader/control authority |
| checker/analysis | read-only/query-only DB 和 artifact | authority/run-state repair或migration、隐式 DDL |
| cleanup | completed run 内由 policy + DB live references + completion evidence共同允许的对象 | authority/audit、live/resumable run、未知归属路径 |

### 2.2 故障模型

本计划保证：

- benign crash、SIGKILL、进程暂停和 stale-but-non-malicious process；
- qsub receipt 丢失、qstat live/historical 延迟和有界 outage；
- shared filesystem 的 transient `ENOENT/ESTALE/EIO`、可见性延迟和 torn temp object；
- fixed cache 丢失、旧 epoch cache 覆盖和可检测损坏；
- 配置声明范围内的 clock skew。

本计划不保证 compromised learner、伪造 tensor/metrics、恶意 symlink race 或底层 FS/SQLite 同时永久损坏。proposal path、digest 和 shape 仍 fail closed，但这不是认证协议。

若旧leader在SQLite write transaction内部被无限期SIGSTOP，它仍持有OS/file lock；successor必须安全等待，不能靠lease超时绕过SQLite互斥。恢复需要scheduler/operator终止旧job或进程自然退出，且本计划不自动`qdel`。自动failover/RTO只承诺旧进程停在transaction外或其lock最终释放；safety仍要求stale commit=0。

static 不创建假的 dynamic placement/stream 行，但也不能只凭可复用 learner ID 接受任意进程。base schema 保存 `static_contributor_bindings(learner_id, logical_launch_id, attempt_id, binding_generation, status)`：launcher 的 durable array receipt 提供 stable logical launch identity；每次process start/rerun生成新的attempt ID并写registration pointer，leader在首次绑定或确认旧attempt已terminal后递增generation、发布binding artifact，learner在此之前不能训练/发布。新logical job还需要显式replacement command；scheduler uncertainty时阻塞/manual review。static proposal/receipt携带attempt+generation fence，旧进程恢复后不能提交。dynamic继续使用instance/placement/stream fence。

### 2.3 publication/commit 生命周期

文件 I/O 不能位于 SQLite transaction 内。一次 merge 的固定顺序是：

```text
try_select_batch(command)                       # 短事务；创建 durable selection_batch
→ load/validate payload
→ aggregate/outer step
→ prepare_publication_intent(command)           # 短事务，绑定 epoch/version/selection
→ create-if-absent weight + outer artifacts     # 事务外
→ commit_merge(command)                         # BEGIN IMMEDIATE 后重验 leader/membership
→ publish canonical epoch control
→ repair fixed cache
```

v0 initialization也必须由取得lease的leader通过同一publication-intent/commit/control链完成（selection为空且target version=0）；initializer只建schema/descriptor，不能保留另一条unfenced v0 writer。

`publication_intents` 至少有 `prepared / committed / abandoned`。candidate takeover 必须按 DB 状态 reconcile：

- committed intent 的 artifact 是 authority live set；
- prepared 但未 commit 的 artifact 是 orphan candidate；
- selected 但没有 prepared intent 的 batch 由 successor 分类：invalid rows终结，still-current rows回 pending；
- 当前 leader 遇到 membership fence 失败时把 intent 标成 abandoned 并登记 same-epoch GC；
- successor 只能依据 authority 判定，不依据 fixed cache 猜测 commit。

`selection_batches` 至少有 `selected / prepared / committed / abandoned`，并保存 command ID、selected row identity 和目标 version。`try_select_batch` 必须在同一事务内建立 batch并把 rows 标为 selected；没有 durable batch 的 selected row 是 schema/integrity error。selection credit 只在 `commit_merge` 时消耗。

所有可重试业务/control命令的command ID由`run_id + command kind + logical target/generation + owner epoch`确定性推导。同一ID重放相同immutable request返回既有结果；同一ID携带不同request hash立即fail closed。successor repair使用自己的epoch和显式`repairs_publication_id`，不能冒充旧epoch命令。

### 2.4 显式 fenced command API

不得把 generic transaction capability 暴露给 application，也不得保留 `LeaderBoundSQLiteStore.__getattr__()`。目标接口形如：

```python
leader = authority.open_leader(token)

selection = leader.try_select_batch(command)
intent = leader.prepare_publication(command)
commit = leader.commit_merge(command)
leader.bind_or_replace_static_attempt(command)
leader.retire_incarnation(command)
leader.begin_terminal_close(command)
leader.finalize_terminal(command)
```

每个 public command 自己定义 transaction 边界并在取得 `BEGIN IMMEDIATE` 后重验 token/lease。跨 concern 的原子不变量由一个粗粒度 command 完成，例如：

- `retire_incarnation` 同时撤销成员、释放 placement/stream、终结 proposal，并把受影响的 prepared selection/publication 标为 abandoned；
- `bind_or_replace_static_attempt` 同时推进binding generation、终结旧attempt active proposal，并abandon受影响selection/publication；
- `commit_merge` 同时写 global version、应用 proposal、更新 fairness credit/token fate/capacity observation；
- `begin_terminal_close` 原子冻结close generation/current fences并关闭新input；
- `finalize_terminal` 在drain结束后终结所有未消费proposal、outstanding token fate和prepared intent。

application 不持有 raw connection，也不能组合任意 SQL transaction。

---

## 3. 协议、schema、配置和兼容性

### 3.1 版本冻结

版本号按 artifact 独立治理；已有 artifact 只递增，第一次获得独立 schema 的 artifact 从 1 开始：

```text
PROTOCOL_VERSION                  = 4
AUTHORITY_SCHEMA_VERSION          = 4
CONFIG_SCHEMA_VERSION             = 1   # 旧 config 未显式版本化
PROPOSAL_FORMAT_VERSION           = 2
PROPOSAL_POINTER_FORMAT_VERSION   = 2
CYCLE_RECEIPT_FORMAT_VERSION      = 1
PROGRESS_POINTER_FORMAT_VERSION   = 1
LEARNER_HEARTBEAT_FORMAT_VERSION  = 2
SYNCER_HEARTBEAT_FORMAT_VERSION   = 2
CONTROL_FORMAT_VERSION            = 2
MEMBERSHIP_FORMAT_VERSION         = 2
LAUNCH_RECORD_FORMAT_VERSION      = 2
RUN_DESCRIPTOR_FORMAT_VERSION     = 2
SOURCE_MANIFEST_FORMAT_VERSION    = 2
ACTOR_ATTESTATION_FORMAT_VERSION  = 1
ARTIFACT_POLICY_FORMAT_VERSION    = 1
PARAM_INDEX_FORMAT_VERSION        = 1   # wire 未改变，不为配合 protocol 人为 bump
```

删除通用 `FORMAT_VERSION` 前必须先生成 producer/consumer inventory，并把每个 retained artifact 迁到自己的常量；legacy decoder 可以保留局部 alias。不得为了“统一数字”给未改变的 param index 等 wire format 人为 bump。文档统一称 `FullProtocolV4`；`FullUpdateProposalV2` 只表示 proposal wire format v2，不能写成“Protocol V2”。

### 3.2 fresh authority v4 DDL

新增两份完整 DDL：

```text
fs_diloco/storage/schema_v4.sql            # full base/leader/proposal/token/publication
fs_diloco/storage/schema_v4_dynamic.sql    # dynamic membership/scheduler extension
```

规则：

- 新 initializer 直接创建完整 v4 表，不再执行旧 `schema.sql` 后叠 `ALTER TABLE`；
- static 和 dynamic 都写 `schema_version=4`，同时写 `mode`、canonical `features_json` 和有序DDL bundle的 `ddl_sha256`；open时逐项重算，不只相信metadata；
- static 不创建 dynamic learner registration/placement/stream/admission/launch 表；candidate recovery outbox 属于 common leader feature，不能误塞进 dynamic extension；
- dynamic adapter 使用 extension tables 做 current-incarnation query；
- v4 authority只保存normalized run-root-relative artifact path + identity/hash，不保存staging或任意absolute payload path；adapter在canonical root下解析并拒绝`..`/absolute escape；
- application 传入 typed `StaticMembershipScope | DynamicMembershipScope`，不能在 runtime 里拼两套 SQL；
- `PRAGMA foreign_keys=ON`、`journal_mode=DELETE`、`synchronous=FULL`、busy timeout 都由 open helper 验证；
- legacy DDL 不允许由 v4 runtime 执行。

accepted proposal 的 `updates.status` 只允许：

```text
pending / selected / applied / dropped
```

删除从未写入的 `UPDATE_STATUS_FAILED`。quarantine/conflict 属于 observation disposition，不加入 `updates.status`。

核心新表：

```text
proposal_observations
proposal_conflicts
proposal_visibility
cycle_receipts
contributor_progress              # static=learner_id，dynamic=stream_id
static_contributor_bindings       # base feature；不是 synthetic dynamic placement
token_fates
token_rollups
selection_state
selection_batches
publication_intents
archive_batches
control_publications
artifact_publications
```

关键 CHECK：

```sql
-- cycle_receipts
CHECK(processed_tokens_this_cycle > 0)
CHECK(effective_tokens_this_cycle >= 0)
CHECK(local_discarded_tokens_this_cycle >= 0)
CHECK(processed_tokens_this_cycle
      = effective_tokens_this_cycle + local_discarded_tokens_this_cycle)

-- accepted updates；zero-effective cycle 只能有 receipt，不能有 proposal
CHECK(effective_tokens_this_update > 0)
CHECK(retained_tokens_since_base >= effective_tokens_this_update)
CHECK(cycle_seq >= 1)
CHECK(inner_steps > 0)
CHECK(local_step_start >= 0)
CHECK(local_step_end = local_step_start + inner_steps)
CHECK(data_cursor_start >= 0 AND data_cursor_end > data_cursor_start)
CHECK(base_global_version >= 0)
CHECK(status IN ('pending','selected','applied','dropped'))
```

finite float、tensor shape/dtype、path 和 digest 仍由 typed decoder/adapter 验证；SQLite CHECK 不能替代这些检查。

### 3.3 Proposal V2 与 disposition

`FullUpdateProposalV2` 至少携带：

```text
proposal_format_version
run_id / stable_contributor_key / cycle_seq / cycle_id / update_id
cycle_receipt_id / cycle_receipt_sha256
base_global_version
local_step_start / local_step_end / inner_steps
processed_tokens_this_cycle
effective_tokens_this_update
local_discarded_tokens_this_cycle
retained_tokens_since_base
data_cursor_start / data_cursor_end
contributor_fence                 # static launch binding 或 dynamic membership fence
payload_size / payload_sha256 / tensor schema identity
created_at
```

mutable proposal/progress pointer是独立versioned envelope，只含run/contributor、target identity/hash、pointer sequence和`published_at`。pointer时间不属于proposal immutable identity，也不参与exact replay比较。

规则：

- update ID 使用完整 UUID4；payload path 由identity推导并以run-root-relative canonical form存储，不信任任意`file_path`；
- payload 以 temp + fsync + same-directory hard-link/create-no-replace 发布，并 fsync parent directory；目标已存在时 fail closed；fixed pointer 仍使用 atomic replace + parent fsync；
- v4 proposal 的 SHA-256 必填，leader 在 merge 前重新计算；
- regular-file、non-symlink、canonical parent、size、safetensors key/shape/numel/dtype 必须匹配；
- JSON 有大小上限；v2 proposal/v1 receipt 默认拒绝unknown field，未来扩展必须 bump对应artifact version；所有int/float做严格类型和finite验证（`bool`不能冒充`int`）。learner wall-clock字段只作audit，不参与fence、grace或selection。

摄取事务顺序：

```text
decode/validate outside transaction
→ BEGIN IMMEDIATE
→ revalidate identity/current membership
→ lookup update_id and logical unique key
→ exact replay / conflict adjudication
→ ordinary INSERT accepted update
→ supersede lower-cycle-seq pending of same stable contributor only after INSERT success
→ record terminal observation disposition
→ advance frontier to observation_id
→ commit
```

精确定义：

- exact replay：相同 `update_id` 且全部 immutable protocol fields、cycle ID、size、digest 一致；
- proposal logical key 固定为 `(run_id, stable_contributor_key, cycle_seq)`；dynamic stable key 是 stream ID，不是易变 instance ID；
- 相同 logical key 但不同 update ID：conflict；
- 相同 update ID 但任一 immutable field/digest 不同：identity collision；
- transient read：非 terminal disposition，不推进 frontier；
- frontier同时保存last terminal cycle sequence和observation ID，只能单调前进；pointer old/new/replay重排不能使frontier回退，proposal也不能越过缺失receipt sequence摄取；
- conflict/malformed：写 `proposal_observations` + `proposal_conflicts`，旧 pending 保持有效；
- quarantine 保存 bounded diagnostic、fingerprint 和原路径。小 JSON 可复制 bounded bytes；大 tensor 不复制、不 unlink，由 retention policy 保留原 object。

### 3.4 cycle receipt、token 语义和 data cursor

原计划的“proposal 参数完全由 `effective_tokens_this_update` 解释”过强：同一 learner 在没有采纳新 global 时，后续绝对参数快照仍包含更早周期的 retained work。v4 冻结以下更准确语义：

- `effective_tokens_this_update`：本次 publication cycle 新产生且没有被 mid-cycle replace 覆盖的 token；只用于本次 merge weight 和 effective-token 指标。
- `local_discarded_tokens_this_cycle`：本周期内被 replace 覆盖的 token。
- `retained_tokens_since_base`：当前参数相对 base 保留的累计本地训练 token；只用于 lineage/audit，不替换 merge-weight 公式。
- merge 数学仍是 `effective_tokens_this_update / staleness_penalty`，不借本计划改变 outer optimizer 或改成累计 token 权重。

这里的token fate是“本cycle token是否直接作为某个proposal的merge weight”分区，不是参数影响的因果归因。一个upload-skipped/superseded cycle的训练效果可能被后续absolute snapshot作为retained ancestry携带；因此`effective_dropped/reported_unpublished`不能表述为“对最终参数零贡献”，`effective_applied`也不能表述为“所有产生影响的物理token”。authority同时报告direct-weight fate与`retained_tokens_since_base/carried ancestry`，论文/文档不得用前者单独推导训练效率或样本利用率。

每个完成的 learner cycle 发布一个 typed immutable `cycle_receipt` 和固定 progress pointer。receipt identity 固定为 `(run_id, stable_contributor_key, cycle_seq)`，并携带 `previous_receipt_id/hash`；proposal 必须引用对应 receipt。顺序为：

```text
optional tensor + proposal metadata create
→ cycle receipt create + progress pointer
→ proposal pointer（若本周期发布 proposal）
```

receipt 包含 cycle ID/sequence、processed/effective/local-discarded、cursor range、`proposal_expected`、planned update ID、planned payload digest和contributor fence。引用必须无环：先确定tensor digest，再构造receipt bytes/hash，最后构造引用receipt hash的proposal metadata；随后按上述顺序create-no-replace。receipt不反向引用proposal-metadata hash。即使failure simulation跳过upload，也必须发布receipt。receipt path由stable key + cycle sequence推导；progress pointer和proposal pointer都只是discovery hint。authority除读取pointer外，还可对每个current contributor只探测“next expected receipt”这一个canonical path，不能历史全扫。progress pointer可从sequence `k`跳到`k+n`，但authority必须沿hash chain从最后已裁决sequence连续catch up；缺任一中间receipt时等待visibility/reconcile，不能越洞推进cursor或ledger。receipt声明的canonical proposal可直接发现，避免“receipt已写、pointer前crash”永久丢失。

因此 token ledger 的精确范围是“在 terminal cutoff 前被 authority 连续摄取并裁决的 durable cycle receipts”，不是无法观察的物理 GPU 工作：

```text
authority_adjudicated_reported_processed
= local_discarded
 + direct_weight_applied
 + direct_weight_dropped_by_reason
 + direct_weight_quarantined_or_conflicted
 + direct_weight_reported_unpublished
 + direct_weight_outstanding_pending_selected_or_prepared
```

terminal 时 `direct_weight_outstanding_pending_selected_or_prepared=0`。硬崩溃发生在 cycle receipt 前的工作不进入等式；每个突然丢失的 incarnation 单独报告，run 级上界求和：

```text
per_lost_incarnation_unreported_upper_bound <= one configured cycle token budget
run_unreported_upper_bound = sum(per-lost-incarnation upper bounds)
```

这个上界依赖“每个 cycle 边界都发布 receipt”，不能通过猜测 heartbeat 吞吐量推导。revocation cutoff 之后才出现的 stale receipt 只记 audit disposition，不推进 cursor、不能产生 proposal application；checker 必须把这类 post-fence work 与 authority-accepted ledger 分开。

base `contributor_progress` 保存last receipt ID/hash、last cycle sequence和cursor，并在current-fenced contiguous receipt成功ingest时推进：static key使用learner ID，dynamic key使用stream ID。static restart control与dynamic replacement admission都带`resume_cursor`、last receipt hash和next cycle sequence。receipt前crash每个lost incarnation最多replay一个cycle；receipt后proposal pointer前crash记为reported-unpublished，不replay已确认cursor。

v4 只实现 indexed-resumable adapter：

- synthetic 和 materialized/indexed dataset 能从 `(stream_id, block_cursor)` 确定性定位；
- `data.streaming=true` 配置直接拒绝，并给出后续 iterable-resumable 计划提示；
- `build_batch_iterator` 的共享改动必须保持 torch baseline 的 rank shard 不重叠与确定性测试。

### 3.5 配置 v4

删除：

```yaml
init:
  resume: ...
coordination:
  syncer_ha:
    enabled: ...
fragments: ...
```

改为强制 leader 配置：

```yaml
coordination:
  leader:
    lease_duration_seconds: 90
    renew_interval_seconds: 10
    max_clock_skew_seconds: 2
    heartbeat_interval_seconds: 5
    heartbeat_stale_after_seconds: 30
    lease_busy_timeout_ms: 5000
    business_busy_timeout_ms: 60000
    candidate_acquire_poll_seconds: 5
    candidate_wait_seconds: 180
    learner_recovery_wait_seconds: 1800
    canonical_repair_wait_seconds: 120
    max_retained_epoch_dirs: 32
  recovery_submission:
    enabled: false

maintenance:
  archive_batch_rows: 256
  recent_batch_dedup_count: 64
  hot_receipts_per_contributor: 64
  hot_observations_per_contributor: 64
  quarantine_records_per_contributor: 64
  publication_orphan_grace_seconds: 120
```

这些是待P0冻结的保守起点，不能在实现中散落magic number。validator至少要求positive bounds，且`publication_orphan_grace_seconds >= lease_duration_seconds + 2*max_clock_skew_seconds`；G6只验证预先冻结值，不能观察结果后放宽。若证据支持收紧，只能作为后续变更并重新跑对应门禁。

所有dataclass section都有纯`validate()`；`load_config`、直接构造后进入runtime、descriptor load和CLI override后都必须调用同一顶层`validate(profile)`。profile由调用entrypoint显式传入`full_v4`或`torch_baseline`，并与`torch_baseline.enabled`交叉校验；先跑共享约束再跑profile约束。不得要求torch baseline满足leader/membership运行约束，也不得让full config借baseline profile绕过它们。

ambiguous `sync.stop_after_global_tokens` 删除，改成明确的 `sync.stop_after_direct_weight_tokens_applied`。迁移工具只有在旧metric与新direct-weight token可证明等价（例如不允许pre-publication replace且旧计数正是committed selected proposal token）时才自动映射；否则输出blocking diagnostic，要求用户显式确认新语义。

迁移工具名中的 `v3→v4` 指 protocol family；旧 config 本身没有显式 schema version，不能把它伪写成“config schema v3”。

### 3.6 兼容策略

新增：

```text
fs_diloco/legacy/config_v1_v3.py
fs_diloco/legacy/run_reader.py
fs_diloco/legacy/fragment_v0.py
```

这些模块只能：

- query-only 打开旧 SQLite；
- 解析旧 resolved config、latest/summary/archive；
- 为 inspect/export/eval 提供旧 fragment index/schedule 的纯函数。

它们不能返回production writer、不能执行DDL、不能被runtime import。SQLite必须以URI`mode=ro`打开并设置`PRAGMA query_only=ON`；inspect只读；export/eval的输出必须写到调用者显式指定、位于旧run root之外的新目录。旧`total_seen_tokens`等字段按`legacy_*`原义导出并附semantic version，不能伪换算成v4 direct-weight/processed/causal metric。测试比较旧fixture tree的path/inode/size/mtime/hash在操作前后不变，architecture test检查import boundary。

不新增 `migrate_run_v3_v4.py`。旧未完成 run 的唯一安全选择是：

1. 在冻结 tag/独立旧环境中继续旧实验；或
2. 从旧 checkpoint 明确开始一个“新 v4 run”，版本从新 lineage 起算并记录 parent provenance；这不是 resume，本计划不提供自动工具。

---

## 4. 核心不变量

requirement matrix 必须与以下 ID 一一对应。既有 `HA-*`、`MEM-*` 和 review 的 `M-*` 已占用，本计划继续使用独立前缀。

### 4.1 Authority 与 mode

- **AUTH-01**：committed global version单调连续；v0 predecessor为NULL，v>0 predecessor恰为v-1；一个target version至多一个committed publication。
- **AUTH-02**：同一时刻最多一个 leader epoch 能成功提交业务 mutation。
- **AUTH-03**：stale token 在 successor commit 后不能 mutation。
- **AUTH-04**：fixed cache 缺失/污染不改变 authority，且可从 authority 修复。
- **AUTH-05**：authority 缺失时不能凭 cache 恢复。
- **AUTH-06**：committed weight 与 outer-state theta 完全一致。
- **AUTH-07**：checkpoint/control path epoch + owner + publication ID 唯一。
- **AUTH-08**：所有 public write command 显式、带 token、短事务；raw connection 不可达。
- **AUTH-09**：单 candidate 仍走 initializer/lease/epoch/fencing。
- **AUTH-10**：automatic recovery submission 关闭不妨碍人工 successor。
- **AUTH-11**：旧writer若暂停在SQLite write transaction内，successor安全等待lock释放；不得以lease超时绕过互斥或声称自动RTO。
- **MODE-01**：static/dynamic 共用 leader/publication/application protocol；只有 dynamic 启用 membership schema feature。
- **MODE-02**：static current logical-launch binding含per-process attempt+generation fence；同一learner ID的旧/重复physical process不能提交。

### 4.2 Proposal 与 filesystem

- **PROP-01**：proposal/cycle receipt 在 application 前已变为 typed object。
- **PROP-02**：payload path 由 identity 推导，不能 path escape。
- **PROP-03**：payload 是 canonical root 下 regular non-symlink file。
- **PROP-04**：完整 UUID + create-if-absent，目标不可覆盖。
- **PROP-05**：新 accepted row 成功 insert 前不 supersede 旧 pending。
- **PROP-06**：frontier 引用 terminal `proposal_observation`，不能引用不存在的 update/quarantine。
- **PROP-07**：冲突不能由 `INSERT OR IGNORE` 吞掉。
- **PROP-08**：每 contributor 至多 1 pending + 1 selected；quiescent active 总数 `<=2M`。
- **PROP-09**：transient visibility 不产生永久 drop。
- **PROP-10**：stable malformed/conflict 有独立、可审计、bounded quarantine disposition。
- **FS-01**：read result 是 `OK / NOT_FOUND / TRANSIENT_IO / MALFORMED / IDENTITY_MISMATCH`。
- **FS-02**：not-found/malformed terminal disposition 满足 grace + 稳定重复观察。
- **FS-03**：短暂 ENOENT/ESTALE/EIO 恢复后 drop 数为 0。
- **FS-04**：size/digest/tensor schema mismatch fail closed。
- **FS-05**：GC 不删除 committed/selected/pending/publication-intent/current-control reference。

### 4.3 Dynamic membership 与 scheduler

- **DMB-01**：dynamic eligible proposal 只来自 current incarnation。
- **DMB-02**：所有 retire/revoke 路径与该 incarnation pending/selected 终结同事务。
- **DMB-03**：selection 和 final commit 都重验 instance/placement/stream/generation/token。
- **DMB-04**：同一 stream/placement 每次 merge 至多贡献一次。
- **DMB-05**：logical launch request 至多 admission 一个 instance。
- **DMB-06**：current quorum 持续存在且 storage 最终可用时，有限 attempt 内 commit 或明确 terminal。
- **DMB-07**：fence conflict 返回 invalid IDs；invalid 终结，still-current 才可回 pending。
- **DMB-08**：drained instance 只保留声明的 final proposal；revoked/stopped instance不保留 active proposal。
- **DMB-09**：terminal input close 后不得 admission。
- **DMB-10**：replacement 复用 stream 时 stream epoch 严格递增并携带 resume cursor。
- **SCHED-01**：qsub 成功但 receipt 丢失能按 request identity reconcile。
- **SCHED-02**：`no_record` 进入 uncertainty，不等于立即 failed。
- **SCHED-03**：uncertainty 有持久 wall-clock deadline 和 evidence source。
- **SCHED-04**：deadline 后进入 failed/expired/manual_review，不无限占用隐式状态。
- **SCHED-05**：uncertain/manual_review 不自动重提或重复 admission。
- **SCHED-06**：不自动 qdel 已接受 job。

所有改变 `learner_instances.status` 为 `revoked/stopped/expired` 或清空 current placement/stream 的代码只能调用同一个 `retire_incarnation` authority command，禁止复制 SQL。

### 4.4 Token、selection 与 data

- **TOK-01**：proposal 的 merge weight 只使用本周期未被覆盖的 positive finite effective token；retained ancestry 单独记录。
- **TOK-02**：publication前任何inner-poll/cycle-end replace之前的token进入local-discarded，并清空有效loss/token/example accumulator。
- **TOK-03**：rebase/predict 保留的 segment 不重复计数。
- **TOK-04**：每个 raw weight positive finite，使用稳定求和；NaN/Inf/negative 接受数为 0。
- **TOK-05**：authority 已连续摄取/裁决的 completed-cycle ledger 精确守恒；每个突然丢失 incarnation 的 receipt 前 gap 单独给出一个 cycle 上界，run 级求和。
- **TOK-06**：processed/produced/ingested/selected/direct-weight-applied/dropped/local-discarded/replayed与carried ancestry分开；direct fate不冒充因果贡献。
- **TOK-07**：stop criterion 明确绑定direct merge-weight applied token，不冒充processed或causal token。
- **TOK-08**：删除 CSV/W&B 不改变 authority token summary。
- **SEL-01**：per-contributor proposal choice 与 contributor admission 分两层。
- **SEL-02**：`quorum_max<N` 的稳定 eligible 集合无饥饿。
- **SEL-03**：selection credit 只与成功 global commit 同事务更新。
- **SEL-04**：crash/failed commit 不消耗 credit。
- **SEL-05**：顺序只依赖 authority state + stable key，不依赖 wall time。
- **SEL-06**：相同 event tape 得到相同 selected lineage 和 tensor reduction order。
- **DATA-01**：indexed stream 由 `(stable_contributor_key,cursor,seed,dataset identity)` 决定；static key是learner ID，dynamic key是stream ID。
- **DATA-02**：static restart 和 dynamic replacement 都从 base contributor progress 恢复；dynamic 额外重验 stream epoch。
- **DATA-03**：每个 lost incarnation 的 hard-crash replay 小于等于一个 cycle，并进入 replay ledger。
- **DATA-04**：v4 明确拒绝 `data.streaming=true`，不再伪装为 bounded streaming。

公平选择键冻结为：

```text
(last_selected_committed_version_or_minus_one, stable_contributor_key)
```

dynamic key 是 `stream_id`，static key 是 `learner_id`。选出 subset 后按 stable key 排序再做 tensor reduction，以保证 `quorum_max>=N` 的旧行为和浮点顺序不变。不得把 wall-clock `first_eligible_at` 放进确定性选择键。

### 4.5 Terminal、audit、environment 与兼容性

- **TERM-01**：normal terminal先冻结close generation和current contributor fences、关闭admission/input，再drain并发布final terminal；static/dynamic使用同一顺序。
- **TERM-02**：terminal drain 不放宽 future/staleness/contributor/digest fence。
- **TERM-03**：terminal 后 pending/selected、outstanding token fate和prepared publication intent都有唯一终态。
- **AUDIT-01**：artifact policy + DB publication ledger 能机器判定 authority/audit/telemetry/cache/payload/temp。
- **AUDIT-02**：archive使用immutable batch object；retry按`batch_id + content hash`幂等，consumer按`record_kind + primary_key`去重。
- **AUDIT-03**：cleanup 不删除不可重建 authority/audit。
- **AUDIT-04**：recovery hot set经rollup/archive/prune后有界；immutable audit batch/partition可线性增长，但不参与启动/discovery扫描。
- **AUDIT-05**：telemetry文件是single-writer/per-actor；不存在多进程共享CSV append，离线export不反写authority。
- **CLOCK-01**：进程内 elapsed timeout 用 monotonic；跨进程 lease/scheduler deadline 用 wall clock并声明 skew。
- **INIT-01**：正式 shared FS 已在 P0 证明不支持 directory `RENAME_NOREPLACE`。initializer 必须用 same-parent staging + exclusive `mkdir(final)` identity reservation，逐对象 hard-link create-no-replace、fsync，再最后发布 create-no-replace `.complete` marker；reader 只承认 identity/hash完整的 marker，因而半初始化 final directory 不可见且既有 final 不能被覆盖。
- **ENV-01**：descriptor 冻结 source/lock/model/tokenizer/dataset revision；actor runtime attestation 可核验。
- **LEGACY-01**：旧 run reader query-only，production runtime 不能 import legacy writer/DDL。
- **BASE-01**：torch DDP/periodic-average config语义、sharding、optimizer schedule和正式PBS行为不因本计划改变；共享schema marker更新不算协议变化。

---

## 5. Phase 0：冻结、triage、oracle 和可测底座

Phase ID：`P0-freeze-oracles`

P0中的pytest、FS probe和性能标定属于runtime/实验工作，执行时即触发`miyabi-development` skill及仓库PBS规则；不要等到P6才加载。

### 5.1 任务

1. 确认当前正在独立 Plan 03 branch 上；记录 clean `branch_point`。如果执行环境不是独立 branch，才从包含本修订计划的 clean commit 新建 branch。
2. 在该 commit 创建 annotated tags：
   - `archive/classic-full-v1-final`
   - `archive/fragment-v0-final`
   - tag已存在时只允许验证其恰好指向同一commit；禁止force-move。
3. 生成 source/config/PBS/test/schema inventory；必须递归扫描并明确排除 torch baseline 删除范围。
4. 在 Miyabi compute node 上重新运行 `pytest --collect-only` 和完整测试，记录实际数量、环境和 artifact；登录节点不运行 pytest/torch。
5. 冻结 42 mutator 的迁移表：`old_name, concern, keep|merge|delete, new_command, reason`。
6. 建立 deterministic event tape：固定 v0 theta、proposal tensors/IDs、arrival、membership、selection、outer optimizer；classic 和 static HA 在无 failure、无 mid-cycle replace、`quorum_max>=N` 下比较共同 semantic projection：selected IDs/order、weight、merged theta、outer state和version predecessor必须exact/bitwise相同；不比较预期不同的epoch/path/timestamp包装字段。
7. 对 9 High + 15 Medium + 10 architecture + 6 docs 共 40 条 finding 写 triage JSON：`reproduced / rejected-with-evidence / deferred-with-justification`。
8. 建立 `tests/support/`：virtual clock、fault tape、tmp authority、fake PBS、deterministic IDs。
9. 增加 dev 依赖 `hypothesis`、`pytest-timeout` 和 pytest marker；不做全仓格式化。pyright 只在 P0 证明选定新模块零 baseline error 后才可加为辅助门禁。
10. 冻结性能方法：paired 2-learner tiny workload、交替/随机化arm顺序、相同timer anchor、signed delta、10% margin、固定seed的one-sided 95% paired-bootstrap upper bound。先做5 pairs；不足以判定时每批加5、最多20 pairs。P0只测噪声/可行性，不能依据observed effect改变margin、CI方法或上限。
11. 在正式shared filesystem的临时目录探测same-directory hard-link/create-no-replace、directory no-replace rename、dir-fd/openat(O_NOFOLLOW)、parent-directory fsync和SQLite DELETE-journal lock行为；只操作`mktemp -d`得到的精确路径。P0 实测 directory `RENAME_NOREPLACE` 返回 `EINVAL`，因此冻结为：exclusive `mkdir(final)` 预留 identity，逐对象 hard-link 后 fsync，最后 hard-link create-no-replace 发布 `.complete` marker；逐 crash prefix、重试和 collision 必须证明半成品对 reader 不可见且不能覆盖别的 identity。其他原语仍须通过；禁止静默退化为覆盖写。
12. 冻结§3.5 maintenance retention起点、推导公式和G6测量口径；后续不得根据10k结果放宽阈值。

### 5.2 四个必须动态判定的 finding

| Finding | RED 命题 |
|---|---|
| H-01a | revoke-before-select 后 stale row 被 selector 选中会导致 selection-time abort，合法 current batch未前进 |
| H-01b | revoke-after-select 后 final fence conflict 不能只处置 invalid row，存在 whole-batch retry 风险 |
| H-05 | `N=8, quorum_max=3` 连续 1000 轮时高 ID contributor 饥饿 |
| H-06 | payload read 注入一次 EIO/ESTALE/ENOENT 会产生永久 drop/abort |
| H-07 | 已知 PBS ID 的 live+historical no-record 被立即 failed，或 query failure 无 deadline |

P0 把已复现case提交为`xfail(strict=True)`，并另用`pytest --runxfail`保存真实失败输出；被反证的case改成普通passing characterization，不保留会XPASS的xfail。P2/P3修复对应行为时必须删除xfail；最终不得保留核心invariant xfail。

### 5.3 Gate

- branch point/tag/source identity 可解析；
- 当前完整测试在 compute node 通过，实际 collected 数已记录；
- deterministic classic/static-HA semantic anchor bitwise相同。仅epoch/path/timestamp等非语义包装差异可在projection说明中排除；selected/weight/theta/outer/predecessor差异必须使P0 BLOCKED并先修订计划，不能只写disposition后继续删classic；
- 40 条 finding 全部 triage；上述 RED case 均有失败证据或反证；
- mutator/classic/fragment/baseline inventory 完整；
- 测试底座和依赖可用；
- shared-FS capability probe通过，或fallback/计划修订已在实现前冻结；
- 未改变生产协议语义。

---

## 6. Phase 1：typed boundary、fresh schema 与显式 authority commands

Phase ID：`P1-typed-foundation`

### 6.1 工作单元

1. 新增 typed objects：`FullUpdateProposalV2`、`CycleReceiptV1`、`ProposalDisposition`、`ContributorFence`（static binding/dynamic membership variants）、`SelectionCandidate/Batch`、`PublicationIntent`、`ReadResult`、`LaunchState`、`TerminalState`。
2. 把 JSON/DB row decode 限制在 boundary；merge、selection、token service 不再接收 `dict[str, Any]`。
3. 实现完整 base/dynamic v4 DDL、schema metadata/DDL hash/open validation，以及base static logical-launch binding；旧 schema 只读。
4. 实现 `LeaderAuthority` explicit commands 和 read model；迁移 42-mutator 表中的 retained commands，删除 `__getattr__` dispatch 和 raw connection escape。
5. 实现统一 config validator 和版本常量；保留v1-v3 query-only legacy config decoder。
6. 从 `runtime.learner` 把 optimizer/scheduler 构建 helper 提取到 `modeling`，让 `fs_diloco/baselines` 不再反向 import runtime。

### 6.2 关键测试

```text
tests/protocol/test_proposal_v2.py
tests/protocol/test_cycle_receipt_v1.py
tests/storage/test_schema_v4.py
tests/storage/test_leader_authority_commands.py
tests/storage/test_contributor_progress.py
tests/storage/test_static_contributor_binding.py
tests/architecture/test_authority_surface.py
tests/architecture/test_dependency_boundaries.py
tests/test_config.py
tests/test_torch_baseline_*.py
```

负例覆盖 zero/negative/NaN/Inf、step/cursor mismatch、path escape、symlink、unknown field/version、direct SQL、stale token、mode/schema-feature mismatch。

### 6.3 Gate

- fresh static/dynamic v4 DB 均可初始化、reopen、integrity check；
- application/protocol 不接收 raw proposal dict；
- retained public mutation 全部映射到 explicit fenced command；
- static DB 不依赖 dynamic tables，但旧/重复logical launch受base binding generation fence；
- P0 golden semantic projection 完全不变；
- torch baseline config/focused tests通过；
- v4 runtime 尚未切换，旧正式入口仍可作为 oracle。

---

## 7. Phase 2：proposal、membership、filesystem 与 publication 正确性

Phase ID：`P2-correctness-measurement`

### 7.1 Safe ingest 与 immutable object

实现 §3.3 的 typed validation、ordinary INSERT、observation disposition、exact replay/conflict、frontier FK、mandatory digest 和 create-if-absent。

至少覆盖：

- same ID/same bytes replay；
- same ID/different bytes collision；
- same logical key/different ID conflict；
- insert/supersede/frontier 每个 crash point；
- pointer old/new/replay reorder；
- symlink/path escape/oversized JSON/wrong tensor schema；
- collision 时旧 pending 仍可选。

### 7.2 H-01 的完整修复

不要只改 `eligible_updates()`。同一工作单元完成：

1. dynamic eligible query join current instance/placement/stream，并验证 update row 自身 fence；
2. 新建唯一 `retire_incarnation` command，供 heartbeat-dead、authorized replacement、drain timeout、stopped/expired 等所有路径调用；同事务终结 pending/selected；
3. `try_select_batch` 在一个事务内分类 invalid IDs并创建 `selection_batch`，合法数不足 quorum 时不留下 partial selected；
4. final commit error 返回 structured invalid IDs；invalid 已 dropped/终结，still-current 才 reset pending；
5. drained 不是 revoked：只有 matching `final_update_id` 可保留并继续 current-fenced ingest；
6. 同一 selected batch 中一个 stale row 不得使其他合法 row无限重试。

通过条件：

- revoke-before-select 和 revoke-after-select 两条 RED 转 GREEN；
- current quorum 已存在时，处置一次 stale conflict 后下一次可提交 attempt 前进；
- stale incarnation successful commit=0；
- retired incarnation active proposal=0；
- generic selection-time `RuntimeError` 不再终止 syncer。

### 7.3 Structured FS visibility

禁止用`Path.exists()`作协议判定。object adapter优先用canonical dir-fd + `openat(O_NOFOLLOW)` + `fstat`，并以`lstat/open/stat`结果分类errno；平台缺少原语时做pre/post inode/type一致性检查并落入§2.2 threat boundary。visibility observation持久化到DB，键至少包含object identity、pointer signature/fingerprint和update/cycle ID；同一signature使用upsert聚合first/last/count/last-result，不能每次poll追加一行。pointer前进后旧signature按bounded retention归档。

terminal disposition 条件：

- NOT_FOUND 跨过配置 grace 且至少 3 次稳定观察，中间没有成功；
- MALFORMED 至少 2 次相同 bounded fingerprint 且跨过 grace；
- TRANSIENT_IO 不直接 drop；超过 operator deadline 进入 manual_review/error terminal，不伪装成 missing；
- identity/digest/shape mismatch 立即 fail closed 并写诊断，但不 unlink 原 object。

### 7.4 publication intent / same-epoch orphan

在 checkpoint 写前提交 prepared intent；final commit 成功才转 committed。membership/leader fence 失败将 intent 标为 abandoned，允许在 lease-safe grace 后清理同 epoch orphan。crash/restart 根据 intent 状态幂等 reconcile。

### 7.5 Gate

- H-01/H-03/H-04/H-06/M-04/M-13 对应测试全部 GREEN；
- proposal/membership/visibility state machine 的开发 profile 和 phase profile 通过；
- publication crash matrix 的受影响点通过；
- active accepted proposal 在 quiescent boundary 满足 `pending+selected<=2M`；
- P0 不含语义变更的 golden semantic anchor仍 bitwise相同。

---

## 8. Phase 3：token、fair selection、cursor、scheduler 和 operational robustness

Phase ID：`P3-operational-robustness`

### 8.1 Segment/cycle accounting

`TrainingSegmentAccumulator` 在任何发生于publication之前的 `replace`（inner poll或cycle-end adoption）时：

1. 关闭旧 segment并计入 local-discarded；
2. 清空有效 loss/token/example/grad accumulator；
3. 把 `interval_start_step` 移到 replace 后边界；
4. 用新 base 开始 segment；
5. 最后一个 step 后或cycle-end replace导致effective segment为空时不发布zero-token proposal，只发布cycle receipt。

rebase/predict 的 retained work 不丢弃、不重复。所有 cycle（包括 upload skip）发布 receipt，authority 建立 §3.4 ledger。

最小反例：

```text
step1 → replace → step2 → publish
step1 → cycle-end replace → receipt-only
```

第一例断言proposal tensor/loss/effective token只对应step2，step1进入local-discarded；processed=step1+step2。第二例断言effective=0、processed=local-discarded且没有proposal。

### 8.2 Fair selection

先 per-contributor 选 proposal，再按 persistent service credit 选 contributor。credit 只在成功 `commit_merge` 更新；failed selection/intent/commit 不消耗。

门禁：

| 组 | N | quorum_max | 要求 |
|---:|---:|---:|---|
| A | 8 | 8 | selected set、stable reduction order与旧实现逐轮相同 |
| B | 8 | 3 | 1000 rounds；count偏差<=1；max wait<=ceil(N/3)+1；Jain>=0.95 |

### 8.3 Data cursor

将 indexed iterator 改为显式 block cursor；contiguous receipt ingest 原子推进 base contributor progress。static restart按learner ID恢复，dynamic replacement按stream ID恢复并返回resume cursor/last receipt hash/next cycle sequence。测试覆盖progress pointer跳号、receipt chain缺洞、receipt archive后续链、proposal publish、upload skip、receipt前后crash、static restart、dynamic replacement、重复physical job和takeover。

`data.streaming=true` validator RED→GREEN（明确拒绝）；不再保留必须完成“100k streaming RSS”才能过 plan 的虚假门禁。

### 8.4 Scheduler uncertainty

launch state 至少包含：

```text
planned → submitting → submission_unknown → submitted → started
        → terminal_uncertain → admitted | failed | expired | manual_review
```

持久字段：first uncertain wall time、last positive evidence、deadline、evidence source、manual reason。live/historical no-record、registration receipt 和 request fingerprint 共同 reconcile；uncertain/manual_review 保留 anti-duplicate tombstone，operator action有审计。

新增`fs_diloco/tools/resolve_scheduler_uncertainty.py`：默认dry-run；只有`--apply --expected-state-sha256 --reason`齐全时才create-no-replace写operator request。允许动作仅为`confirm_job_id / mark_failed / mark_expired / record_external_cancel_evidence`；admission仍只能来自合法registration fence。active leader摄取request并用explicit fenced command比较expected state后转移；tool本身不得连接authority DB、不得直接admit或qdel。stale request安全拒绝并保留audit。

### 8.5 Clock、initializer、archive、artifact、environment

- process elapsed timeout 统一用 injected monotonic clock；lease/scheduler persistent deadline继续用 wall clock，并在 SQLite lock 后重新采样；
- terminal close artifact冻结每个current contributor fence；learner最多完成当前cycle并在drain ack声明final cycle sequence/update ID。drain只接受这些pre-close fence的contiguous receipt和matching final proposal，超出声明或来自new/stale attempt的一律拒绝；hard-crash contributor按per-incarnation gap上界终结；
- initializer 在与 final root 同 parent/mount 的 `<run>.staging.<uuid>` 完成全部文件/DB/marker/fsync并关闭SQLite handle；descriptor 中写 final logical path而不是 staging path。发布时 exclusive `mkdir(final)` 并先写入 create-no-replace identity reservation；把 staging 中每个 immutable object以 hard-link create-no-replace装入 final，fsync相应目录，最后以同目录 hard-link create-no-replace发布 `.complete` marker并fsync parent。reader在marker前视final为不存在；marker后重新加载descriptor/DB并核对reservation、manifest hash和logical path。retry只允许恢复同identity且未complete的reservation，其他collision fail closed；失败可留下可解释的 reserved partial final 和非权威 staging，但不能覆盖或暴露半初始化run；
- cycle receipt、terminal observation、token fate和旧version metadata先写`audit/batches/<kind>/<batch_id>` immutable create-no-replace object并fsync，再在一个fenced transaction中更新`archive_batches`/cumulative rollup并prune hot rows。同batch ID若hash相同视为retry，不同则fail closed；不再多进程append共享archive文件。只有active leader的fenced maintenance可把已closed batch objects压成immutable partition + hashed manifest；manifest commit并重新验hash后才可GC已被完全覆盖的source batches。generic cleanup无此权限。较老DB batch rows折叠为manifest cursor；analysis仍按record kind/primary key去重；
- runtime telemetry改为`metrics/<actor-kind>/<actor-id>/<attempt-id>.jsonl`等per-actor single-writer文件；共享CSV只由离线export生成到显式输出目录，不能由多个learner append；CSV/W&B仍非权威；
- initializer 发布 versioned artifact policy；DB只逐项登记 correctness-relevant publication，不能要求 learner 为每个 telemetry 文件写 authority row；
- descriptor 冻结 source/`uv.lock`/model/tokenizer/dataset revision；每个 actor写 runtime attestation。GPU driver/queue/job ID 属于 actor/run evidence，不要求 initializer 在无 GPU 环境伪造。

### 8.6 Matched comparison

重写 checker，先验证source/config/model/data/seed/cursor/outer target/processed与direct-weight token/carried ancestry/selected count/failure tape/timer anchor/resource allocation，再输出：

```text
comparison_status = COMPARABLE | INCOMPARABLE | BLOCKED
signed_delta_ratio
paired_raw_repeats
confidence_interval
```

不得用 clipped ratio 作为 gate；任何方向 absolute delta >20% 自动 INCOMPARABLE 并审计 workload。

### 8.7 Golden rebaseline

P2/P3 会有意改变 replace token 和 `quorum_max<N` selection。生成 `unified_v4_trace.json` 及逐 case 归因报告：

- 无 replace、无 quorum 截断 case 必须与 P0 `torch.equal`；
- 只允许被已接受 finding解释的 selected/weight变化；
- 其他漂移按缺陷处理，不能直接写进 golden。

### 8.8 Gate

- H-02/H-05/H-07/H-08/H-09 和 M-01..M-03/M-05..M-12/M-15 的 accepted finding 均有 GREEN 测试或明确 disposition；
- authority 已连续裁决的 receipt ledger terminal balance=0；
- static/dynamic 每个 suddenly lost incarnation replay <= one cycle，run级gap/replay上界为逐incarnation求和；
- scheduler duplicate admission=0，uncertainty 在 deadline内有明确状态；
- wall-clock jump不改变 process timeout；
- init 每个 object-link/fsync/complete-marker crash point都不会让reader接受半成品，也不会覆盖既有 final；同identity retry可完成，异identity collision fail closed，descriptor中的logical path在complete后有效；
- recovery hot DB/files在rollup/archive/prune后有界，audit增长不进入启动扫描；
- unified v4 trace 和归因报告完成；
- torch baseline data/optimizer/protocol tests保持通过。

---

## 9. Phase 4：强制 fenced runtime cutover 与配置/PBS 迁移

Phase ID：`P4-mandatory-fenced-runtime`

### 9.1 Syncer/learner cutover

`run_syncer` 最终固定为：

```text
load immutable descriptor
→ wait bootstrap complete
→ acquire candidate lease
→ open LeaderAuthority
→ initialize-v0-or-resume-v4-authority
→ common merge application
→ terminal/release
```

删除生产分支：

- `ha_mode` / `syncer_ha.enabled`；
- runtime `SQLiteStore(...)` writer construction；
- classic `config.init.resume`；
- fixed latest/stop 作为 authority；
- learner 创建 authority 目录。

learner 无论static/dynamic都先加载descriptor/source gate、使用epoch control reader并取得current contributor fence，**之后**才import/load torch model和分配GPU；未admitted actor只能写自身registration/日志。static验证attempt/generation binding，dynamic再验证instance/placement/stream admission。fixed cache只作可修复convenience view。

### 9.2 复用既有 launcher

扩展 `fs_diloco/tools/launch_independent_run.py`：

- default dry-run；
- initializer 完成后提交 syncer candidate；
- static 提交 rerunnable array；dynamic 每个 bootstrap slot独立 qsub并逐项保存 receipt；
- static array receipt按index生成stable logical launch ID；每次process start生成attempt ID，learner在leader发布current attempt/generation binding前等待；PBS rerun复用logical ID但必须取得新generation，新logical job走显式replacement；
- 任一 partial submission 非零退出、保留 accepted job ID、不自动 qdel；
- 所有 walltime 必须由调用者显式给出并符合仓库最短实用 walltime规则，且不得短于 10 分钟。

新增 console aliases：

```text
fs-diloco-init-run
fs-diloco-launch-run       # 指向 launch_independent_run:main
fs-diloco-migrate-config-v3-to-v4
```

保留 `python -m fs_diloco.tools.init_run`、`python -m fs_diloco.tools.launch_independent_run`、`python -m fs_diloco.syncer/learner`，因为现有 PBS/文档使用它们。无需新增 `fs-diloco-syncer-candidate`。

### 9.3 Config migration

迁移工具：

- 默认只输出 structured diff；
- 除下一条显式`--in-place`外，只有给出`--output`才写文件，且output目标存在时拒绝；
- repository-owned tracked config 允许显式 `--in-place --expected-sha256 <old>`；两参数缺一不可，先在内存迁移并完成v4 round-trip validate，再atomic replace。`--in-place` 与 `--output` 互斥；
- fragment config 返回 unsupported，不静默转 full；
- ambiguous token stop 返回 blocking diagnostic；
- 完成后 round-trip v4 validate；
- 不写旧 run root。

仓库自有 full config 在 archive tag 之后**原路径就地更新**，避免主线同时维护 v3/v4 两套正式 config。迁移矩阵记录 `old commit:path → new commit:path`、语义差异和是否需重新基线。

inventory 必须递归覆盖：

```text
configs/**/fs_diloco_*.yaml
scripts/local/**
scripts/miyabi/**
```

并明确：

- `configs/5000/...200x25steps.yaml` 不遗漏；
- `...no_fragment_50x10.yaml`及`run_9node_no_fragment_gpt2_wikitext2_50x10.pbs`作为历史fragment-control pair在P5从正式mainline归档，不迁v4、不计作fragment-enabled/PBS；
- `torch_baseline_*.yaml` 与 torch baseline PBS 不迁 leader config；但因其复用 `Config`，仍必须补齐/验证共享 config schema version和removed-key规则。

每个 retained full PBS 都改为 initializer + candidate + learner roles，执行前通过 `bash -n scripts/miyabi/*.pbs` 和 literal group ID 检查。`run_plan01_regression.pbs` 迁成 v4 回归锚点。

### 9.4 Gate

静态扫描 production runtime：

```text
ha_mode                         = 0
syncer_ha.enabled               = 0
runtime raw SQLiteStore writer  = 0
learner prepare_run_dirs        = 0
classic resume authority        = 0
```

行为：

- 1 candidate static；
- static learner同logical launch rerun恢复，以及旧generation恢复后的successful commit=0；
- 2 candidate takeover；
- dynamic replacement；
- error terminal + successor；
- fixed cache corruption/repair；
- authority missing fail closed；
- recovery submission disabled + manual successor；
- existing launcher static/dynamic dry-run和partial-receipt tests。
- admission拒绝/等待路径在torch import和GPU allocation之前完成（import sentinel + CUDA allocation counter为0）。

所有 repository-owned full config/PBS 完成迁移、Plan 01 v4 regression通过后，P5 才获准删除 classic/fragment writer。

---

## 10. Phase 5：删除 classic/fragment writer、保留 legacy reader并收敛模块

Phase ID：`P5-delete-classic-refactor`

### 10.1 删除与保留

删除 production writer：

- classic syncer loop/init/resume/publish-stop；
- classic learner fixed latest/stop authority branch；
- fragment learner/syncer loop、fragment mutation store、fragment runtime config/PBS；
- `protocol/fragment_codec.py`、`fragment_index.py`、`fragment_scheduler.py` 中仅 runtime 需要的部分；
- old layered schema/bootstrap path和 dynamic proxy mutator dispatch。

保留并收敛：

- `fs_diloco/{syncer,learner,analysis,eval_lm_harness}.py` 公共 `python -m` shim；只更新已删除 export，不因其“兼容”字样误删；
- read-only old full/fragment analysis/export/eval；fragment 所需纯函数移到 `fs_diloco/legacy/fragment_v0.py`；
- deterministic golden data，不保留可调用 classic production runtime；
- torch baselines及其 config/PBS/checker。

### 10.2 测试和配置删除记账

删除前对每个 classic/fragment test做：

```text
migrate-to-unified | retain-legacy-reader | delete-obsolete
```

`test_fragment_store.py` 等包含的 shared SQLite/GC/terminal invariant 必须先迁到 full v4 tests。artifact 记录删除用例数、迁移断言数、参数化 case净变化；不要求测试总数只能增加，但禁止无解释下降。

8个fragment-enabled config和5个fragment PBS从主线删除；另将1个full no-fragment历史control config及其1个PBS按§9.3单独归档，删除清单不能把它们伪计成fragment runtime。旧fragment run的四张表仍由legacy query-only reader识别，v4 DDL不创建这些表。

### 10.3 有限、就地的模块拆分

本计划不搬空现有包。目标结构基于当前仓库：

```text
fs_diloco/
├── core/                 # v4 config/version/descriptor
├── protocol/             # typed proposal, token, selection, membership, control
├── storage/              # authority v4, lease, object IO, maintenance
├── runtime/
│   ├── syncer.py         # CLI/composition/main loop
│   ├── learner.py        # CLI/composition/training loop
│   └── services/         # ingest, merge, publication, terminal, scheduler reconcile
├── modeling/             # model/data/shared optimizer helpers
├── observability/
├── legacy/               # query-only v1-v3 readers
├── baselines/            # unchanged protocol consumer
└── tools/
```

架构门禁按职责，不按任意行数：

- syncer/learner entrypoint不拼 SQL、不直接调用 qstat/qsub；
- protocol不 import runtime/storage adapters/PBS/Path；
- baselines不 import runtime learner；
- legacy不被 runtime import；
- external filesystem/PBS/clock/authority fault semantics经窄 adapter；
- 不保留两套 proposal ingest/merge/terminal implementation。

### 10.4 文档同步

更新现有：

```text
README.md
docs/README.md
docs/00-glossary.md
docs/01-overview.md
docs/02-architecture.md             # guarantee + failure model
docs/03-runtime-flow.md
docs/04-data-flow.md
docs/05-code-structure.md
docs/06-configuration.md
docs/07-operations.md
docs/modules/*.md
```

只新增：

```text
docs/08-compatibility-and-migration.md
```

README 只描述 full static/dynamic 和 single-/multi-candidate deployment；fragment V0 标记为 archived/unsupported并记录tag及其不可变full commit ID，避免只依赖本地tag name。具体 job ID、性能数字和实验结果写 `reports/`，不写进稳定 docs。满足仓库“9 节点且超过 50×10 baseline 后同步文档”的条件时，必须同步对应 verified behavior。

### 10.5 Gate

- production config无法表达 classic/fragment runtime；
- classic/fragment writer symbol只存在于 frozen tag，不存在当前 tracked runtime；
- legacy reader query-only且通过旧 full/fragment fixtures；
- unified runtime、baseline、analysis/eval完整回归通过；
- import/command surface/dead-entry scan通过；
- 删除清单与测试净变化 artifact 完成。

---

## 11. Phase 6：验收阶梯、性能、Checker 和最终审查

Phase ID：`P6-acceptance-final-review`

任何 PBS/runtime 验证到来时按触发规则使用 `miyabi-development` skill；提交前执行仓库规定的 `bash -n`、literal group ID 和 evidence-based walltime 检查。登录节点只运行静态检查。

### 11.1 G0：冻结与成本

- clean review-target commit；base 是 target ancestor；
- source/config/schema/DDL hash冻结；
- requirement matrix 无缺行；
- PBS job 数、节点、预计 runtime、最短实用 walltime和依据写入 `progress.md`；walltime 至少 10 分钟，默认明显过长时用 `qsub -l walltime=...` 显式缩短；
- 所有测试/实验预先定义 success evidence，不能只看 PBS exit code。

### 11.2 G1：登录节点静态门禁

```bash
git diff --check
python -m compileall -q fs_diloco
ruff check fs_diloco tests scripts/miyabi
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
```

另做 removed-key/classic/fragment writer/raw SQL/import boundary/docs link/group ID scan。`ruff format --check` 只对本计划修改/新增 Python 文件或 P0 已证明 clean 的目录运行；不以无关存量格式阻塞。pytest/torch不在登录节点运行。

### 11.3 G2：compute-node focused + full tests

关联组至少覆盖：proposal/disposition、membership retire/race、token/cycle receipt、fair selection、FS visibility、scheduler uncertainty、cursor、authority command、terminal、legacy reader、cleanup、config migration、torch baselines。

每个 accepted behavior bug 有 pre-fix RED artifact；正例、负例、rollback 同组通过；核心 invariant xfail=0；完整 collected 数变化与P0 inventory对账。

### 11.4 G3：生成式状态机

使用同一 invariant 定义、不同 profile：

| profile | examples | max transitions | 作用 |
|---|---:|---:|---|
| dev | 25 | 50 | 内环 |
| phase | 200 | 200 | 工作单元 |
| gate pure model | 1000 | 300 | P6 |
| gate SQLite adapter | 200 | 150 | P6 |

动作包含ingest/replay/conflict/select/retire/dynamic replace/static attempt bind+replace/commit/crash/restart/drain/FS fault/scheduler ambiguity。violation=0，失败例可deterministic replay。不要对每个SQLite example强行500 transitions造成无意义数小时门禁。

### 11.5 G4：publication crash matrix

至少覆盖：tensor/proposal metadata temp+fsync+create、cycle receipt、progress pointer、proposal pointer、static binding control、selection batch、publication intent、weight、outer、precommit fence、version insert、proposal transition、DB commit、canonical control、fixed cache、archive batch/manifest。v0 lifecycle和N>0 merge lifecycle各覆盖适用点，每点至少10次。

恢复只能到事务前/后；selected最多应用一次；next version=N+1；prepared/abandoned orphan可解释并可安全清理；stale leader commit=0。

### 11.6 G5：真实 tiny pipeline

| learners | candidates | membership | fault |
|---:|---:|---|---|
| 1 | 1 | static | none |
| 2 | 1 | static | none |
| 2 | 1 | static | learner crash + same logical launch rerun；old generation resume |
| 2 | 2 | static | active syncer crash |
| 2 | 1 | dynamic | learner replacement |
| 2 | 2 | dynamic | syncer + learner failure |

检查 terminal/summary/DB/current checkpoint/token ledger/cursor/active rows/temp/orphan和error events；不得只看 stop reason。

### 11.7 G6：10,000-cycle boundedness

先标定实际耗时；超过 10 分钟则使用独立 PBS job，不放进默认 pytest。

quiescent/maintenance boundary 的逻辑门禁：

- 每 contributor `pending<=1`、`selected<=1`，总数 `<=2M`；
- retired incarnation active proposal=0；
- current authority weight/outer reference pair=1；
- fixed proposal/progress pointer各不超过 current contributor数；
- prepared intent、scheduler uncertainty、quarantine和epoch dirs都受配置 retention约束。

物理门禁只针对recovery hot set：warm-up/retention稳定后`live_pages=page_count-freelist_count` slope的95% upper`<0.01 page/cycle`；active/recovery file slope约0；discovery/recovery scan与历史长度不线性增长。immutable audit batch/partition允许按事件线性增长，但必须按batch/hash幂等、可compact/轮转且不在启动路径全量扫描；分别报告hot bytes与audit bytes。物理retained checkpoint可包含有界old epochs，不能错误要求磁盘永远只有一对文件。

### 11.8 G7：2-node shared-FS/SQLite

覆盖lock contention、leader commit/successor reopen、old syncer在transaction外与transaction内两种SIGSTOP、old static learner SIGSTOP/SIGCONT、cache delete/repair、transient visibility和qstat live/historical。transaction外应takeover；transaction内successor必须等待，测试随后显式终止旧进程再前进。要求integrity、busy retry有界、RPO 0、stale syncer/learner commit 0、duplicate application 0。

### 11.9 G8：Miyabi 8+1 static

- 8 learners + 1 candidate node；
- learner training、update tensor、syncer compute/publish均FP32；
- 至少 60 local steps/cycle、20 global versions；
- distinct contributor、ledger balance、no starvation/transient drop、terminal/current-only authority、all final heartbeats、Checker PASS。

### 11.10 G9：Miyabi 8+1 dynamic failure soak

- learner training和update/publish tensor为BF16，CPU syncer以FP32 compute；8 bootstrap learners；至少60 local steps/cycle、120 global versions；
- permanent learner loss、replacement复用 stream、duplicate physical job、active syncer crash、successor、old syncer恢复、terminal drain；
- current quorum下持续前进、old proposal不阻塞、single admission、cursor replay有界、stale commit=0、bounded authority、Checker PASS。

G9 任一时刻保持8 learner allocations + 1 candidate allocation。为在单candidate节点保留被SIGSTOP的旧进程并启动successor，正式配置默认使用已由既有证据支持的CPU syncer placement；P0/G0必须重新确认两进程内存预算。SIGSTOP只能注入在SQLite transaction外，并由instrumentation证明，否则预期行为是successor安全等待writer lock。active/successor由该allocation内test supervisor编排；不得为满足failover悄悄扩成10节点。若CPU内存预算或supervisor方案不可行，必须把拓扑改为10节点、重新估时并在提交前修订本计划和成本记录。

duplicate learner actor在同一learner allocation内只走registration/admission gate，未获admission前禁止加载model/GPU state，因此可验证single admission而不额外占GPU；permanent-loss后的replacement复用已释放allocation。若实现会让未admitted actor先占GPU，该实现本身不符合cutover gate。

G8/G9 两个 9-node job覆盖正式 static/dynamic、FP32/BF16和超过 50×10 文档同步门槛；不再预设 30+ 个9-node作业。

### 11.11 G10：paired performance/comparability

classic 在冻结 tag的独立 worktree/venv/run root运行；两臂不共享DB/run目录。使用2 learners tiny workload，arm顺序按P0预注册方案交替/随机化；先5 paired repeats，无法判定时以5为一批增加到最多20，超过上限仍不确定则INCONCLUSIVE。优先在少量长allocation内顺序执行多pair，每个arm使用fresh run root；共同model/dataset cache在timer前按同一规则prewarm，不执行privileged system-cache drop。timer不包含PBS排队，allocation/启动差异仍单独报告。

两组性能比较都关闭failure injection和mid-cycle replace；否则旧classic token含义不能与v4一一映射，直接INCOMPARABLE。硬门禁：

- workload equivalence=PASS；
- raw signed delta、paired values、timer anchor和CI完整；
- paired median absolute delta >20% 时 INCOMPARABLE并审计workload，不得PASS；
- clipping不存在。

signed overhead定义为`(candidate_seconds-baseline_seconds)/baseline_seconds`。预注册non-inferiority margin：unified single-candidate相对classic、dynamic相对static的paired median overhead均`<=10%`，且P0冻结的one-sided 95% paired-bootstrap upper bound也必须`<=10%`才PASS。median或upper bound超过10%时阻塞并归因优化；点估计达标但CI不足以判定时按上限增加repeats，不能放宽margin。质量统计研究不混入此性能状态。

### 11.12 Checker

使用一个实现避免七份逻辑漂移：

```text
scripts/miyabi/check_plan03.py --phase <phase-id> --mode staged|completed
```

P0 只允许 `PASS/BLOCKED`。stdout 只打印：

```text
PASS
PASS_WITH_FOLLOWUPS
BLOCKED
```

详细 JSON 至少包含 plan/phase/source/environment/schema identity、requirements map、metrics、errors和evidence paths。correctness requirement和G10预注册性能门禁不能用 `PASS_WITH_FOLLOWUPS`；该状态只用于明确仍运行的非门禁长作业或补充性性能/质量follow-up。

### 11.13 phase/plan review

每个 phase 按 `plans/AGENTS.md` 冻结 review-target、完成 Codex/Claude 独立审查、处置 finding并形成 phase-final commit。P6 后对 target 中全部 tracked `fs_diloco/` 做 current-state完整审查；汇总 remediation、修复、验证和必要的增量复审完成前不得宣布 plan 完成。

---

## 12. Finding 处置映射

| Finding | Phase | 预定处置 |
|---|---|---|
| H-01 | P0/P2 | selection-time + commit-time复现；current eligibility + central retire + structured retry |
| H-02 | P3 | segment reset + cycle receipt/effective token |
| H-03 | P2 | insert-first/disposition/frontier FK |
| H-04 | P1/P2 | typed proposal + fresh constraints + mandatory integrity |
| H-05 | P3 | persistent oldest-unserved credit |
| H-06 | P2 | typed read + persisted visibility grace |
| H-07 | P0/P3 | scheduler uncertainty state machine |
| H-08 | P3 | precise token ontology + authority ledger |
| H-09 | P3/P6 | workload equivalence + signed paired comparison |
| M-01/M-12 | P3 | monotonic process clock + post-lock wall sample |
| M-02/M-03 | P3 | reject fake streaming + indexed durable cursor |
| M-04 | P2 | full UUID + create-if-absent |
| M-05 | P0/P3 | shared-FS fallback：same-parent staging + exclusive final reservation + hard-linked immutable objects + create-no-replace complete marker + post-complete self-check |
| M-06/M-07 | P3/P5 | archive dedup；telemetry非权威/单writer或per-process |
| M-08/M-09 | P1 | config validator + fresh schema constraints |
| M-10 | P3 | stable authority service credit |
| M-11/M-13 | P2/P3 | deterministic control ID + publication intent/abandoned GC |
| M-14 | P0/P5 | archive/remove fragment writer；legacy read-only |
| M-15 | P3 | descriptor identity + actor attestation |
| A-01..A-10 | P1/P5 | 有限就地拆分、explicit commands、typed protocol、legacy boundary |
| D-01..D-06 | P2/P5/P6 | 真 immutable、精确token名、分域结果、fragment边界、signed perf、failure model |

P0 若把 finding 判为 `rejected-with-evidence`，matrix 必须改成对应证据和“不修改”；不能仍按表中预定方案静默改行为。defensive hardening若继续做，严重度降级并说明不改变已验证语义。

---

## 13. 推荐 commit / 工作单元顺序

```text
C00 plan branch point / reports / matrix scaffolding
C01 archive tags + current inventory + 42-command disposition
C02 RED strict-xfail reproductions + test support + fresh baseline
C03 deterministic classic/static oracle + performance method freeze
C04 typed proposal/receipt/config/version boundary
C05 fresh v4 base/dynamic DDL
C06 explicit LeaderAuthority commands；remove proxy dispatch
C07 safe ingest/disposition/create-if-absent
C08 current membership eligibility + central retire + structured selection/commit retry
C09 typed FS visibility + publication intents/orphan reconciliation
C10 segment/cycle token ledger + indexed cursor
C11 persistent fair selection + matched checker
C12 scheduler/clock/staged init/archive/artifact/environment
C13 unified syncer/learner cutover
C14 reuse launcher + config/PBS migration + Plan01 v4 regression
C15 delete classic writer
C16 delete fragment writer + legacy readers + test-deletion accounting
C17 bounded module extraction + docs + checker
C18 local/2-node/9-node acceptance and performance evidence
C19 final current-state review remediation
```

`C10/C11` 完成后立即生成 unified v4 golden；不能像旧计划那样把 rebaseline 排到后续 unrelated data work之后。`C15/C16` 在 C13/C14 完整通过前不得开始。

---

## 14. 预计文件落点

优先修改/新增：

```text
fs_diloco/core/config.py
fs_diloco/core/constants.py
fs_diloco/core/run_descriptor.py

fs_diloco/protocol/proposal.py
fs_diloco/protocol/cycle_receipt.py
fs_diloco/protocol/contributor.py
fs_diloco/protocol/membership.py
fs_diloco/protocol/token_accounting.py
fs_diloco/protocol/selection.py
fs_diloco/protocol/read_result.py
fs_diloco/protocol/control_epoch.py
fs_diloco/protocol/liveness.py

fs_diloco/storage/authority.py
fs_diloco/storage/leader_lease.py
fs_diloco/storage/schema_v4.sql
fs_diloco/storage/schema_v4_dynamic.sql
fs_diloco/storage/object_store.py
fs_diloco/storage/paths.py
fs_diloco/storage/maintenance.py

fs_diloco/runtime/syncer.py
fs_diloco/runtime/learner.py
fs_diloco/runtime/actor_bootstrap.py
fs_diloco/runtime/pbs_scheduler.py
fs_diloco/runtime/launch_outbox.py
fs_diloco/runtime/services/*
fs_diloco/modeling/training.py
fs_diloco/baselines/train.py

fs_diloco/legacy/config_v1_v3.py
fs_diloco/legacy/run_reader.py
fs_diloco/legacy/fragment_v0.py

fs_diloco/tools/init_run.py
fs_diloco/tools/launch_independent_run.py
fs_diloco/tools/migrate_config_v3_v4.py
fs_diloco/tools/check_workload_equivalence.py
fs_diloco/tools/resolve_scheduler_uncertainty.py
fs_diloco/tools/analysis.py
fs_diloco/tools/run_metrics_csv.py
fs_diloco/tools/clean_run.py

scripts/miyabi/check_plan03.py
```

明确不新增平行的 `fs_diloco/tools/launch_run.py` 或可继续训练的 `migrate_run_v3_v4.py`。

---

## 15. 完成定义

只有全部满足才标记完成：

1. production full static/dynamic只有统一 candidate/lease/fenced authority runtime。
2. classic full和fragment V0没有 writer、正式 config、PBS或可调用 production入口。
3. 完成的旧 full/fragment run仍可 query-only inspect/export/eval；旧未完成 run明确拒绝 resume。
4. 40 条 finding 全部有 triage和最终 disposition；所有 accepted Critical/High已修，Medium已修或有证据延期。
5. Proposal V2、cycle receipt、fresh authority v4和explicit command boundary生效。
6. selection-time/commit-time stale incarnation、ingest conflict、pre-publication replace token、fairness、FS transient、scheduler uncertainty均有pre-fix RED和最终GREEN。
7. authority连续摄取并裁决的receipt ledger守恒；每个突然丢失incarnation的hard-crash gap按一个cycle上界、run级求和，不伪称物理processed精确值。
8. static restart/dynamic replacement cursor连续，per-incarnation replay在声明上界内；fake streaming配置fail closed。
9. P0/full regression、state machines、crash matrix、tiny、10k、2-node、G8 static、G9 dynamic和G10 performance均通过。
10. stale leader commit、stale static binding/dynamic incarnation commit、duplicate admission、version fork、theta mismatch、authority-from-cache recovery均为0。
11. quiescent active proposal满足每contributor最多1 pending+1 selected；recovery hot DB/files/discovery有界；线性audit archive单独计量且不参与恢复扫描。
12. matched comparison先证明可比并报告signed paired statistics；两组median和one-sided 95% upper bound均通过10%门槛；不得clip或以异常负差异自动PASS。
13. 所有 repository-owned full config/PBS完成v4迁移；fragment删除清单和测试净变化可审计。
14. torch baseline config、tests、正式PBS和协议行为无回归。
15. requirement matrix 100%映射到测试、Checker requirement和artifact。
16. README、docs/00..07、module docs和compatibility/migration文档与最终代码一致；正式9节点超baseline结果按仓库规则同步。
17. phase review、plan current-state审查、remediation、必要增量复审、plan-final commit和cleanup manifest完整。

若 P4 之前发现迁移/删码范围不可行，可以在任何删除动作前修订计划为“只修 accepted findings、保留 classic writer”；一旦 C15 开始，不允许通过重新启用 classic 作为同一 run root 的 runtime fallback。
