# Plan 03：统一 Fenced Syncer Runtime、删除经典模式并闭合全面审查问题

计划 ID：`fsb_decoupled_diloco_plan_03_unified_ha`

状态：待执行

建议执行分支：从 `codex/fsb_decoupled_diloco_plan_02` 最新冻结提交创建独立分支，例如：

```text
codex/fsb_decoupled_diloco_plan_03_unified_ha
```

审查依据：

- 上次全面审查：`fsb_decoupled_diloco_code_architecture_review.md`
- 当前 Plan 02 设计与实现记录
- `plans/ref/实施计划制定与 Agent 执行经验.md`
- `plans/AGENTS.md`
- 当前主要协议：full classic、full static HA、full dynamic HA、fragment classic
- 上次审查确认的 9 个 High、15 个 Medium、10 项架构债务和 6 项文档问题
- 候选计划 `plans/DOING/candidate_plans/fsb_decoupled_diloco_plan_03.md`（分阶段修复方案，本计划吸收其证据表、triage 门禁和冻结决策）

配套文件：

```text
plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha.md              ← 本文
plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv
```

`plans/AGENTS.md` 按“计划文件名去掉 `.md`”推导报告路径。本仓库已把计划移入 `plans/DOING/plans/`，plan-id 推导规则不变：

```text
plan-id   = fsb_decoupled_diloco_plan_03_unified_ha
报告      = reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/
完成门禁  = reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/<phase-id>/
```

---

## 0. 执行摘要

本计划同时完成两件事：

1. **删除生产代码中的经典 Syncer 模式。**
2. **修复上次全面审查中影响安全性、活性、聚合语义和实验可信度的问题。**

最终主线只保留一套 full-mode runtime：

```text
FullProtocolV2
    ↓
必须经过 initializer
    ↓
Syncer candidate 获取 leader lease
    ↓
获得单调 epoch
    ↓
所有业务 mutation 经过 transaction 内 fencing
    ↓
epoch-unique checkpoint/control publication
    ↓
learner 读取 canonical epoch view
```

“只有一个 Syncer”不再是经典模式，而是统一 runtime 的退化情况：

```text
candidate 数量 = 1
自动 recovery submission = false
epoch = 1，持续持有 lease
```

因此必须区分两个概念：

- **强制保留：** lease、epoch、fencing、DB-first recovery、canonical epoch control。
- **仍然可选：** learner-assisted 自动 `qsub` recovery、备用 candidate 数量、dynamic membership、自动 scale-out。

本计划不把 fragment HA 一起实现。当前 fragment 只有经典路径，恢复与 full 不等价。为避免在一次更新中同时承担“删除 classic + 修复 P0 + fragment version-vector HA”三个高风险目标，最终主分支采取以下决策：

- full static：支持，强制统一 HA runtime；
- full dynamic：支持，强制统一 HA runtime；
- fragment V0：从主运行入口移除，保存在冻结 tag/归档分支；
- 以后重新引入 fragment 时，必须直接实现于统一 fenced authority 之上，不能恢复经典控制路径。

---

## 0A. 修订说明与已核实的代码基线

本节是执行前必须先读的事实层。以下内容已在 `codex/better_docs` 工作树上直接核对，执行者不需要重新推导；与本节冲突的任何叙述以本节为准。

### 0A.1 分支起点

`codex/fsb_decoupled_diloco_plan_02` 是 `codex/better_docs` 的祖先，二者相差 1 个提交（`bc71b67 better docs`），本计划文件本身就在这个提交上。因此：

- Plan 03 branch 从 **当前 `codex/better_docs` 的 tip** 创建，而不是从 plan02 tip 创建（否则会丢失本计划文件）；
- plan branch point 记录为该 commit ID，作为第一个 phase 审查的 base。

### 0A.2 已核实的审查证据（file:line）

| Finding | 证据位置 | 已核实的事实 |
|---|---|---|
| H-01 | `fs_diloco/storage/sqlite_store.py:947` | `eligible_updates()` 只按 `status/base_global_version/staleness` 过滤，无任何 membership join |
| H-01 | `fs_diloco/storage/fenced_store.py:2038` | `revoke_dead_instances()` 撤销 instance、清 placement/stream、failed launch_requests，但**不终结**该 instance 的 pending/selected updates |
| H-01 | `fs_diloco/runtime/syncer.py:3647-3684` | `DynamicMembershipFenceError` 分支对**整批** selected 调用 `reset_selected_to_pending`，重载 checkpoint 后 `continue` |
| H-02 | `fs_diloco/runtime/learner.py:2663-2665, 2694-2698, 2727-2750` | mid-cycle replace 只记录 tracker 并改写 `base_global_version`，`losses/interval_tokens/interval_examples` 累加器不重置 |
| H-03 | `fs_diloco/storage/sqlite_store.py:830-935` | `insert_update_metadata()` 顺序为 frontier 检查 → `UPDATE ... status='dropped', drop_reason='superseded'` → `INSERT OR IGNORE` → 无条件推进 `proposal_frontiers` |
| H-04 | `fs_diloco/storage/schema.sql` / `schema_bootstrap.py` | `schema.sql` 中 CHECK 约束数为 0；`schema_bootstrap.py` 仅 4 个 |
| H-05 | `fs_diloco/protocol/merge.py:56-86, 89-122` | `select_one_per_learner` 按 `learner_id` 排序后 `[:quorum_max]`；`select_one_per_dynamic_member` 以 `(local_step_end, committed_at, update_id)` 排序后 `[:quorum_max]`；无任何跨轮次公平状态 |
| H-05 | `fs_diloco/protocol/merge.py:16-33` | `normalized_update_weights()` 只检查 `total <= 0`，未拒绝 NaN/Inf/负值 |
| H-06 | `fs_diloco/storage/atomic_io.py:77-81` | `safe_read_json()` 把 `OSError` 与 `JSONDecodeError` 一并折叠为 `None` |
| H-08 | `fs_diloco/runtime/syncer.py:3601-3602` | `total_seen_tokens += sum(row["tokens_this_update"] ...)`，与 learner 侧的 processed 语义混用 |
| H-09 | `fs_diloco/tools/phase2_matched_evidence.py:81-87` | `ratio = max(0.0, dynamic - static) / static`，负 overhead 被截断为 0 后必然通过 `< 0.05` |
| M-01 | `fs_diloco/protocol/liveness.py:225-227` | `no_progress_timed_out()` 使用 `time.time()` |
| M-02 | `fs_diloco/modeling/hf_data.py:91-102` | `text_rows_to_blocks()` 一次性 materialize 全量 token 序列 |
| M-04 | `fs_diloco/runtime/learner.py:1567, 1665` | `update_uuid = uuid.uuid4().hex[:12]`（48 bit） |

尚未被任何证据证实、必须在 Phase 0 复现或证伪的部分：

- H-01 的**端到端 livelock**（静态代码只证明了三个必要条件同时成立）；
- H-05 饥饿的**实际幅度**（当前正式实验普遍 `quorum_max == num_learners`，此时不发生饥饿）；
- H-06 瞬态错误**是否真的导致过 drop**；
- H-07 PBS 歧义窗口**是否可触发**。

上次审查自述未运行测试。本计划不允许在没有复现的情况下改写这四项对应的行为。

### 0A.3 已核实的仓库现状（影响可行性的硬事实）

| 项目 | 实际状态 | 对计划的影响 |
|---|---|---|
| 测试规模 | `pytest --collect-only` 收集 **495 个用例**、55 个文件，全部位于 `tests/` 顶层；`tests/coordination` 等子目录只剩 `__pycache__` | 与审查引用的 495 一致，可直接作为基线 |
| HA/dynamic 测试覆盖 | 仅 `test_plan02_phase1_ha.py`(2303)、`test_plan02_phase2_dynamic.py`(1727)、`test_plan02_phase2_review_remediation.py`(422) 三个文件覆盖 `fenced_store`/`syncer_ha`/dynamic | 统一 runtime 后所有模式都走这条路径，测试密度必须显著提高才能删除 classic |
| fragment 规模 | 11 个测试文件、9 个 config、4 个 PBS、`protocol/fragment_{codec,index,scheduler}.py`、`schema.sql` 中 4 张 `fragment_*` 表 | 归档工作量远大于“删 config” |
| classic 使用面 | **所有真实 GPT-2/WikiText-2 实验 config 都是 classic**；只有 4 个 tiny config 打开 `coordination.syncer_ha` | 删除 classic 前必须先迁移正式实验 config 与 PBS，见 §10.8 |
| 入口点 | `pyproject.toml` 只注册 `fs-diloco-syncer`、`fs-diloco-learner`、`fs-diloco-inspect`、`fs-diloco-export-run-metrics`、`fs-diloco-lm-eval`、`fs-diloco-validation-eval`、`fs-diloco-publish-quality-gate` | §10.5 中的 `fs-diloco-init-run` / `fs-diloco-launch-run` / `fs-diloco-syncer-candidate` **当前不存在**，需新增 |
| 初始化调用方式 | PBS 里是 `python -m fs_diloco.tools.init_run`；没有通用 `launch_run`，只有 `tools/launch_phase{1,2}_acceptance.py` 等专用 launcher | 通用 launcher 是新增工作，不是重命名 |
| 版本常量 | `PROTOCOL_VERSION = 3`、`HA_SCHEMA_VERSION = 2`、`DYNAMIC_SCHEMA_VERSION = 3`、`FORMAT_VERSION = 1`、`CONTROL_EPOCH_FORMAT_VERSION = 1` | §2.3 原写 `protocol_version = 2` 是**版本回退**，已更正 |
| fenced 写命令 | `_BOUND_MUTATORS` 当前 **42 个**方法（Plan 02 G0 记录的 31 个已增长） | capability 重构必须以 42 为冻结基数做前后集合比对 |
| Checker 位置 | 既有 checker 全在 `scripts/miyabi/check_plan0N_*.py` | §16.5 原写 `tools/check_plan03_*.py`，已更正 |
| 类型/属性工具 | 仓库**没有** `pyright`、`hypothesis`、`ruff format` 的任何使用记录，`pyproject.toml` 也无 `[tool.pytest.ini_options]` | §12.1、§12.3 依赖的工具是新增前置条件，见 §22 |
| `updates.status` 实际取值 | `pending / selected / applied / dropped`；`UPDATE_STATUS_FAILED` 常量定义了但从未写入 | schema v4 的 CHECK 列表必须与常量表同时收敛，见 §7.2 C |

---

## 1. 为什么不能先直接删除经典代码

最优顺序不是“先删 classic，再修 bug”，而是：

```text
冻结经典实现作为 oracle
    ↓
提取共享的可测试协议核心
    ↓
修复安全、活性和测量问题
    ↓
把所有正式入口切换到统一 HA runtime
    ↓
完成等价性、性能和故障门禁
    ↓
最后删除经典生产代码
```

原因如下：

1. 经典路径是无故障、单 Syncer 情况下的行为参考。过早删除会失去回归 oracle。
2. 上次审查中的 H-02、H-03、H-04、H-05、H-06 会改变 proposal、selection 和指标语义，应先形成明确的新协议版本。
3. 当前 `SQLiteStore` 被 HA wrapper 复用；若一边删除一边修复事务，很容易产生双重语义。
4. fragment 依赖经典路径。直接删除会迫使执行者临场决定 fragment 的命运。
5. 当前 `syncer.py`、`learner.py`、`fenced_store.py` 已经过大，必须先建立窄接口，避免删除条件分支时误删权威边界。

经典实现只在 Phase 0—Phase 4 作为受控 oracle 存在。Phase 5 后不得继续作为生产入口或配置选项。

---

## 2. 最终模式、保证和兼容性矩阵

### 2.1 最终支持矩阵

| 模式 | 最终状态 | 说明 |
|---|---|---|
| full + static membership + 1 syncer | 支持 | 统一 fenced runtime；不是 classic |
| full + static membership + 多 candidate | 支持 | single-active-leader failover |
| full + dynamic membership | 支持 | 必须使用统一 fenced runtime |
| full + automatic recovery submission | 可选 | 默认关闭；不影响 HA 协议基础 |
| classic full | 删除 | 仅由冻结 tag/旧 commit 提供参考 |
| fragment classic | 从主线删除 | 保存归档分支，不提供正式入口 |
| fragment HA | 本计划不实现 | 下一独立计划重新设计 |
| 旧 run 分析/导出 | 支持 | 只读 |
| 旧 classic run 原地 resume | 不支持 | 防止双重权威 |
| 旧 HA run 原地自动升级 | 不支持 | 使用显式离线迁移或新 run |

### 2.2 配置变化

删除：

```yaml
coordination:
  syncer_ha:
    enabled: true
```

改为：

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
```

删除或迁移：

- `init.resume`：正式 runtime 不再由配置决定。initializer 创建新 run；candidate 根据 authority state 自动 initialize-or-resume。
- `fragments.*`：从正式配置 schema 删除，迁移工具明确报告“已归档，不可在 Plan 03 runtime 中执行”。
- classic-only launcher/config/PBS 参数。
- `--num-learners` 对 dynamic learner 的兼容覆盖。
- 任何允许 learner 创建 authority 目录的兼容路径。

### 2.3 协议与 schema 版本

当前值为 `PROTOCOL_VERSION = 3`、`HA_SCHEMA_VERSION = 2`、`DYNAMIC_SCHEMA_VERSION = 3`、`FORMAT_VERSION = 1`、`CONTROL_EPOCH_FORMAT_VERSION = 1`（`fs_diloco/core/constants.py`）。所有新版本号只允许递增，冻结为：

```text
protocol_version          = 4    # 现值 3，不得写成 2
authority_schema_version  = 4    # 统一值，取代 HA=2 / DYNAMIC=3 的按模式分叉
proposal_format_version   = 2    # 现 FORMAT_VERSION = 1
control_format_version    = 2    # 现 CONTROL_EPOCH_FORMAT_VERSION = 1
token_ledger_version      = 1
artifact_manifest_version = 1
```

`authority_schema_version` 统一意味着 `schema_bootstrap.schema_version_for_mode()` 在 Phase 1 被删除：static 与 dynamic 共用同一张物理 schema，static run 在初始化时写入固定的 `learner_instances / placements / streams` 行（见 §10.4），membership fence 退化为固定 generation 但仍由 authority 校验。任何“static 没有这些表”的假设在 v4 之后都不成立。

原则：

- 新 runtime 只写新版本。
- analysis/export 工具可读取旧版本。
- 不在运行时隐式迁移旧 authority DB。
- 迁移必须使用显式 `--dry-run`、输出 invariant audit，并写入新目录。
- 完成的旧 run 不修改，只读保留。

---

## 3. 权威链与 writer 规则

### 3.1 唯一权威链

```text
learner immutable payload
    ↓
原子 proposal pointer
    ↓
typed ingest + identity/integrity validation
    ↓
SQLite fenced authority transaction
    ├── proposal state
    ├── membership state
    ├── selection/fairness state
    ├── token ledger
    ├── committed global version
    ├── terminal/controller state
    └── control publication ledger
    ↓
epoch-scoped canonical latest/terminal/control
    ↓
fixed latest/stop/summary convenience cache
```

SQLite transaction 是唯一业务提交点。fixed cache、CSV、W&B、普通 JSONL 都不能决定恢复结果。

### 3.2 writer 分类

| Writer | 允许写入 | 禁止写入 |
|---|---|---|
| initializer | staging root、authority schema、descriptor、bootstrap marker | 训练版本、proposal |
| active leader | fenced DB transaction、自己 epoch 的 payload/control | 其他 epoch、无 token mutation |
| learner | 自己 instance 的 heartbeat、payload、pointer、registration | authority DB、control authority |
| checker/analysis | 只读 DB、artifact | 任何修复或隐式写入 |
| cleanup | 仅按 artifact manifest 和 completion evidence 删除 | authority/audit history |

### 3.3 Fenced mutation by construction

最终不得继续依赖：

```python
LeaderBoundSQLiteStore.__getattr__()
_BOUND_MUTATORS = {...}
```

目标 API：

```python
with authority.begin(token) as tx:
    tx.proposals.ingest(...)
    tx.membership.revoke(...)
    tx.publication.commit(...)
```

要求：

- raw SQLite connection 不暴露给 application/runtime；
- write command 只能由 `FencedTransaction` capability 创建；
- token 在 `BEGIN IMMEDIATE` 后验证；
- transaction 内不执行文件 I/O、qstat/qsub、sleep、模型计算、大文件 hash；
- CI 自动枚举 public write commands，未分类 mutation 直接失败。

迁移基数（已核实）：`fs_diloco/storage/fenced_store.py:3036` 的 `_BOUND_MUTATORS` 当前恰好 **42** 个方法。重构必须是集合保持的：

- Phase 0 把这 42 个名字冻结成 `tests/architecture/fenced_mutator_inventory.json`；
- 每次迁移后比对 `capability 暴露的写命令集合 == 冻结集合 ± 显式登记的增删`；
- 差异必须在 review 报告中逐条说明，禁止“重构顺带删了一个 mutator”。

---

## 4. 核心不变量

以下 ID 应进入 requirement matrix、测试名和 Checker artifact，并与 `plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv` 的 `invariant_id` 列一一对应。

**命名空间纪律（必须遵守）**：本仓库已经存在三套互不相同的 ID：

| 前缀 | 归属 | 例 |
|---|---|---|
| `HA-01..HA-20` | Plan 02 Phase 1 不变量，`scripts/miyabi/check_plan02_phase1.py` 已发射 | 已占用 |
| `MEM-01..MEM-20` | Plan 02 Phase 2 不变量，`scripts/miyabi/check_plan02_phase2.py:604` 已发射 | **已占用** |
| `M-01..M-15` | 上次审查的 Medium finding 编号 | 已占用 |

因此本计划的动态成员/活性不变量使用 `DMB-` 前缀，不再使用 `MEM-`，避免 Plan 02 的 checker artifact 与 Plan 03 的 artifact 在同一 `requirements` 字典里语义冲突。所有新 ID 前缀（`AUTH/PROP/DMB/TOK/SEL/FS/SCHED/DATA/TERM/AUDIT/ENV`）在本计划开始前需确认未被既有 checker 使用。

### 4.1 Authority 与 leader

- **AUTH-01**：committed global version 单调、连续，每个版本恰有一个 predecessor。
- **AUTH-02**：同一时刻最多一个 epoch 可以成功提交业务 mutation。
- **AUTH-03**：stale token 在新 epoch commit 后不能成功 mutation。
- **AUTH-04**：固定 cache 缺失或被污染不能改变 authority。
- **AUTH-05**：authority 缺失时不能仅凭 cache 恢复。
- **AUTH-06**：checkpoint weight 与 outer-state theta 必须一致。
- **AUTH-07**：checkpoint/control publication 使用 epoch-unique path。
- **AUTH-08**：所有写命令均经过 fenced transaction capability。
- **AUTH-09**：一个 candidate 在只有自己存在时仍使用完整 lease/epoch 语义。
- **AUTH-10**：自动 recovery submission 关闭不影响手工 successor 接管。

### 4.2 Proposal

- **PROP-01**：proposal 必须通过版本化 typed schema。
- **PROP-02**：payload path 由 identity 推导，不信任任意 `file_path`。
- **PROP-03**：payload 必须是 canonical root 下的 regular non-symlink file。
- **PROP-04**：immutable object 使用完整 UUID 或内容 hash，并以 create-if-absent 发布。
- **PROP-05**：新 proposal 未成功 insert 前，不得 supersede 旧 pending。
- **PROP-06**：frontier 只能指向 accepted、exact replay 或 quarantined proposal。
- **PROP-07**：冲突不得由 `INSERT OR IGNORE` 静默吞掉。
- **PROP-08**：同一 contributor 的 active pending/selected proposal 数保持有界。
- **PROP-09**：文件瞬态不可见不能被一次观察永久化。
- **PROP-10**：永久 malformed/conflict 必须进入可审计 quarantine。

### 4.3 Membership 与活性

- **DMB-01**：dynamic eligible proposal 只来自 current incarnation。
- **DMB-02**：revoke 与该 incarnation 的 pending/selected 终结位于同一事务。
- **DMB-03**：最终 commit 再次校验 instance、placement、stream、generation、token。
- **DMB-04**：同一 stream/placement 每次 merge 至多贡献一次。
- **DMB-05**：logical launch request 至多 admission 一个 instance。
- **DMB-06**：存在 current quorum 且存储最终可用时，系统在有限循环内前进或进入明确 terminal。
- **DMB-07**：membership fence 失败只 drop 无效 row，不无条件回滚整个合法 batch。
- **DMB-08**：drained member 只允许其声明的 final proposal 被摄取。
- **DMB-09**：terminal close 后不得创建新 admission。
- **DMB-10**：replacement 复用 stream 时 `stream_epoch` 严格增加。
- **DMB-11**：static run 在统一 schema 下同样持有 instance/placement/stream 行，其 fence 恒为固定 generation 且由 authority 校验，不允许绕过。

### 4.4 Token 与训练 segment

- **TOK-01**：proposal 参数变化必须由 `effective_tokens_this_update` 对应的 segment 解释。
- **TOK-02**：mid-cycle replace 之前的计算计入 local discard，不进入 merge weight。
- **TOK-03**：rebase/predict 保留的 segment token 不得被重复计数。
- **TOK-04**：merge weight 只使用正、finite 的 effective token。
- **TOK-05**：terminal 时 segment ledger 精确守恒。
- **TOK-06**：`processed`、`produced`、`ingested`、`selected`、`effective_applied`、`discarded` 分开。
- **TOK-07**：任何 stop criterion 明确声明使用哪种 token。
- **TOK-08**：CSV/W&B 缺失不能改变 authoritative token summary。

### 4.5 Selection

- **SEL-01**：per-contributor proposal choice 与 contributor admission 分离。
- **SEL-02**：长期稳定 eligible 集合中不存在 ID/stream 饥饿。
- **SEL-03**：fairness state 与成功 commit 原子更新。
- **SEL-04**：crash 后不会因“已选未提交”错误消耗 selection credit。
- **SEL-05**：选择顺序完全由 authority state 和 stable tie-breaker 决定。
- **SEL-06**：相同 event tape 重放得到相同 selection lineage。

### 4.6 Filesystem 与 scheduler

- **FS-01**：runtime read 返回 typed result：OK / NOT_FOUND / TRANSIENT_IO / MALFORMED / IDENTITY_MISMATCH。
- **FS-02**：drop 前必须满足 visibility grace 和重复稳定观察。
- **FS-03**：ESTALE、EIO、短暂 ENOENT 恢复后不能产生永久 drop。
- **FS-04**：hash/size/tensor shape mismatch fail closed。
- **FS-05**：GC 不删除 current/committed/selected/pending reference。
- **SCHED-01**：qsub 成功但 receipt 丢失可通过 request identity reconcile。
- **SCHED-02**：`no_record` 不等于立即失败。
- **SCHED-03**：scheduler uncertainty 有持久 deadline。
- **SCHED-04**：deadline 后进入 terminal 或 `manual_review`，不得无限占用。
- **SCHED-05**：不确定状态不得自动重复 admission。
- **SCHED-06**：自动 `qdel` 仍不是默认正确性动作。

### 4.7 数据、终止和审计

- **DATA-01**：stream 对应确定性 sample/block sequence。
- **DATA-02**：replacement 从 durable cursor 恢复。
- **DATA-03**：无法避免的 replay 有明确上界并进入 ledger。
- **DATA-04**：unresumable streaming source 被配置层拒绝。
- **TERM-01**：dynamic terminal 必须先 close input，再发布正常最终 terminal。
- **TERM-02**：terminal drain 不放宽 future/staleness/membership fence。
- **TERM-03**：terminal 后 pending/selected 全部有最终命运。
- **AUDIT-01**：authority、audit、telemetry、cache、payload 有机器可读分类。
- **AUDIT-02**：archive batch 至少一次写入可按 batch/row ID 去重。
- **AUDIT-03**：cleanup 不删除不可重建 authority/audit。
- **ENV-01**：source、dependency、environment、dataset、tokenizer identity 冻结。

---

## 5. 目标目录和模块边界

不要求一次大爆炸重写，但最终应达到：

```text
fs_diloco/
├── core/
│   ├── config.py
│   ├── identity.py
│   └── versions.py
├── domain/
│   ├── proposal.py
│   ├── membership.py
│   ├── selection.py
│   ├── token_accounting.py
│   ├── publication.py
│   ├── terminal.py
│   └── errors.py
├── application/
│   ├── learner_cycle.py
│   ├── proposal_ingest.py
│   ├── membership_reconcile.py
│   ├── merge_cycle.py
│   ├── publication_service.py
│   ├── terminal_service.py
│   └── recovery_service.py
├── ports/
│   ├── authority.py
│   ├── object_store.py
│   ├── scheduler.py
│   ├── clock.py
│   ├── data_stream.py
│   ├── audit.py
│   └── telemetry.py
├── adapters/
│   ├── sqlite_authority/
│   │   ├── session.py
│   │   ├── proposals.py
│   │   ├── membership.py
│   │   ├── selection.py
│   │   ├── publication.py
│   │   ├── terminal.py
│   │   └── maintenance.py
│   ├── posix_object_store.py
│   ├── pbs_scheduler.py
│   ├── hf_data_stream.py
│   ├── jsonl_audit.py
│   └── wandb_telemetry.py
├── runtime/
│   ├── learner_main.py
│   ├── syncer_candidate_main.py
│   └── bootstrap_main.py
└── tools/
```

依赖方向：

```text
core/domain
    ↑
application
    ↑
ports
    ↑
adapters/runtime/tools
```

禁止：

- `core` import `runtime`；
- domain 直接 import SQLite/PBS/Path；
- application 直接调用 `subprocess.run`；
- runtime 直接拼 SQL；
- tool 以 read-write 模式打开 authority；
- learner 创建 authority 目录。

与现状的差异（已核实，必须在 Phase 5 显式处理，不能默认“重命名即可”）：

- 现有包为 `core / modeling / observability / protocol / runtime / storage / tools`；上面的目标树遗漏了 `observability/`，它应作为 `adapters/` 下的 telemetry adapter 或保持独立包，二选一并写进 `docs/05-code-structure.md`；
- `fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py` 是仍然存在的兼容 shim（`from .runtime.learner import ...`）。它们要么在 Phase 5 一并删除，要么明确保留为 deprecated 入口；不允许在删除 classic 后仍指向已消失的符号；
- `protocol/{fragment_codec,fragment_index,fragment_scheduler}.py` 随 fragment 一起归档，见 §11.2；
- `runtime/failure_sim.py` 是故障注入设施，属于测试支撑而非生产路径，Phase 5 需给出归属结论。

---

## 6. Phase 0：范围冻结、归档与行为 oracle

Phase ID：`P0-freeze-oracles`

### 6.1 目标

在修改语义前冻结 classic、fragment 和现有 HA 的可复现参考，避免删除后无法判断回归。

### 6.2 实现任务

1. 从 `codex/better_docs` 当前 tip 创建 Plan 03 branch（见 §0A.1；不要从 plan02 tip 创建）。
2. 创建只读 tag：
   - `archive/classic-full-v1-final`
   - `archive/fragment-v0-final`
3. 记录 branch point、source fingerprint、依赖锁 hash（`uv.lock`）。
4. 计划文件已在 `plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha.md`，无需新建。
5. 新增 requirement matrix CSV：`plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv`（列：`invariant_id, phase, review_finding, requirement, implementation_contract, test_contract, gate, artifact_contract, status, evidence_path`）。
6. 新增：
   - `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/progress.md`
   - `failures.md`
   - `code_review.md`
   - `artifacts/`
7. 建立 deterministic update tape：
   - 固定 v0 theta；
   - 固定 learner proposal tensors；
   - 固定 arrival order；
   - 固定 membership；
   - 固定 selection policy；
   - 固定 outer optimizer。
8. 分别由 classic full 和 static HA 执行同一 tape，保存：
   - selected update IDs；
   - effective weights；
   - theta v0…vN；
   - outer state；
   - drop decisions；
   - latest/terminal。
9. 运行当前完整测试组并保存基线结果（已核实基线：495 collected）。
10. 对 current classic、static HA、dynamic HA 做文件/DB/控制面 inventory。
11. **Finding triage（新增，硬性要求）**：对上次审查的 9 High + 15 Medium + 10 架构 + 6 文档共 40 条 finding 逐条给出 `reproduced` / `rejected-with-evidence` / `deferred-with-justification`，写入 `artifacts/<ts>_p0-finding-triage_review.json`。§0A.2 已核实的静态证据可直接复用，但下列四项**必须给出可执行的复现或反证**，否则该 finding 不得进入 Phase 2 的行为改动范围：

    | Finding | 需要证明的命题 | 最小复现形式 |
    |---|---|---|
    | H-01 | 旧 incarnation 的 pending proposal 能使 current quorum 存在时的 global version 停止前进 | 单进程 store-level 场景：admit A → A 发布 step100 proposal → revoke A → admit B 复用同 stream → B 发布 step1 proposal → 断言下一次 selection 选中 B 且 version 前进 |
    | H-05 | 在 `quorum_max < contributor 数` 时存在长期饥饿 | 纯函数 1000 轮 `select_one_per_learner` 模拟，记录每个 contributor 的 selected 次数与最大等待轮数 |
    | H-06 | 一次瞬态 `OSError` 会把仍然存在的 payload 判为永久缺失 | 在 payload 读取点注入一次 `EIO`/`ESTALE`，断言当前实现产生 drop |
    | H-07 | qsub 成功但 receipt 丢失后，`no_record` 会被当作立即失败 | `pbs_scheduler` 层用假 qstat 输出驱动状态机 |

    这四个复现测试必须是 **RED**（在当前代码上失败或断言到错误行为），并在 Phase 2 修复后转 GREEN。若某项无法复现，则该 finding 记为 `rejected-with-evidence`，Phase 2 对应改动降级为“防御性加固”，不得占用 High 优先级。
12. **测试底座（新增）**：建立 `fs_diloco/testing/`（或 `tests/support/`）最小可测基座，Phase 2 的所有 RED 测试都基于它，而不是每次现搭：
    - 虚拟时钟（可注入的 `Clock` 双实现）；
    - fault tape（按序注入 `ENOENT/EIO/ESTALE/malformed/crash-point`）；
    - in-memory / tmpdir authority fixture；
    - 假 PBS 调度器。
13. **门禁阈值标定（新增，决定 P6 是否可行）**：在 tiny 配置下测量 **classic vs 当前 static HA** 的完整训练时间，至少 5 次重复，记录 signed delta 与 bootstrap CI，写入 `artifacts/<ts>_p0-classic-vs-ha-overhead_review.json`。理由：仓库目前**没有任何** classic 与 HA 的性能对照证据，而 §12.10 直接规定了 5%/8% 门槛。标定结果的处置规则：
    - 若实测 median overhead ≤3%：§12.10 阈值保持 5%/8%；
    - 若实测在 3%–8% 之间：§12.10 阈值改为 `实测 median + 3pp`，并在计划中记录修订；
    - 若实测 >8%：先做一次 syncer duty-cycle 归因（lease renew、fenced transaction、epoch publication 各自占比），再决定是优化还是把门槛改成“记录并解释”，不允许直接用一个注定失败的门槛开工。
14. **迁移面 inventory（新增）**：产出 `artifacts/<ts>_p0-classic-surface_inventory.json`，逐项列出必须迁移或归档的对象——正式实验 config、PBS 脚本、fragment 表与模块、兼容 shim、测试文件。这是 §10.8 与 §11.2 的输入，缺它就无法判断“删除 classic”的真实成本。
15. **工具前置条件（新增）**：按 §21 落地 `hypothesis`、pytest 标记与静态检查配置。Phase 2 的生成式门禁依赖它们，不能等到 P6 才发现依赖缺失。

### 6.3 测试

建议新增：

```text
tests/reference/test_classic_ha_characterization.py
tests/fixtures/update_tapes/full_v1_100_merges.json
tests/fixtures/golden/classic_full_v1_trace.json
tests/fixtures/golden/static_ha_v1_trace.json
tests/regressions/test_h01_old_incarnation_livelock.py     # RED
tests/regressions/test_h05_selection_starvation.py         # RED
tests/regressions/test_h06_transient_read_drop.py          # RED
tests/regressions/test_h07_scheduler_no_record.py          # RED
```

等价性 oracle 的可行性已核实：classic 与 static HA 共用同一个 `full_update_proposal_source`（`fs_diloco/runtime/syncer.py:1599`）与同一个 `select_one_per_learner`，因此 bitwise 等价在原理上成立；差异只可能来自 store 层 `eligible_updates` 的返回顺序。若出现差异，先定位到具体 SQL `ORDER BY` 而不是直接判 BLOCKED。

### 6.4 Gate

P0 只有 `PASS` 或 `BLOCKED`。

必须满足：

- branch point 与 tag 可解析；
- 当前完整测试通过，且收集数与 §0A.3 基线（495）一致或差异有解释；
- classic 与 HA 在相同 deterministic tape 下：
  - selected IDs 完全一致；
  - effective weights 完全一致；
  - FP32 theta 和 outer state `torch.equal`；
  - version lineage 完全一致；
- 若当前两条路径不等价，差异必须先形成显式 disposition，不能继续删除；
- fragment 已有独立冻结 tag；
- 40 条 finding 全部有 triage 结论，四个必复现项各有一个 RED 测试或一份反证；
- classic-vs-HA overhead 已标定，§12.10 阈值据此确认或修订；
- classic 迁移面 inventory 完整（config / PBS / 测试 / schema 表逐项列举）；
- `hypothesis` 等工具前置条件就绪；
- 未修改生产语义。

### 6.5 产物

```text
artifacts/<timestamp>_p0-baseline-tests_pass.json
artifacts/<timestamp>_p0-classic-ha-equivalence_pass.json
artifacts/<timestamp>_p0-runtime-surface_inventory.json
artifacts/<timestamp>_p0-finding-triage_review.json
artifacts/<timestamp>_p0-classic-vs-ha-overhead_review.json
artifacts/<timestamp>_p0-classic-surface_inventory.json
tests/architecture/fenced_mutator_inventory.json          # 冻结的 42 个写命令
```

---

## 7. Phase 1：协议类型、transaction capability 与 schema v4

Phase ID：`P1-typed-foundation`

### 7.1 目标

建立后续修复的共同语言，先消除无类型 dict、隐式 mutation 和弱 schema。

### 7.2 实现任务

#### A. Typed domain objects

新增 frozen dataclass/tagged union：

```python
FullProposalV2
ProposalIdentity
MembershipFence
TrainingSegment
TokenAccounting
SelectionCandidate
SelectionBatch
PublicationIntent
ReadResult
LaunchState
ControllerState
TerminalState
```

解码边界：

```text
JSON/DB row
    ↓ decode + validate
typed domain object
    ↓
application service
```

DB row dict 不得进入 merge/adoption/domain。

#### B. Proposal V2 schema

强制验证：

- `effective_tokens_this_update > 0`
- `processed_tokens_this_cycle >= effective_tokens_this_update`
- `inner_steps > 0`
- step range 一致
- base/version/generation 非负
- timestamp finite、顺序合法
- metric finite
- canonical path ownership
- regular file、非 symlink
- exact expected size
- configurable digest
- safetensors key/shape/numel/dtype
- unknown-field policy
- format migration policy

#### C. Authority schema v4

新增或调整：

- `proposal_conflicts`
- `proposal_visibility`
- `token_segments`
- `selection_state`
- `stream_cursors`
- scheduler uncertainty fields
- `archive_batches`
- `artifact_manifest`
- CHECK constraints
-必要 FK/unique constraints

建议 CHECK：

```sql
CHECK(effective_tokens_this_update > 0)
CHECK(processed_tokens_this_cycle >= effective_tokens_this_update)
CHECK(discarded_tokens_this_cycle >= 0)
CHECK(processed_tokens_this_cycle
      = effective_tokens_this_update + discarded_tokens_this_cycle)
CHECK(inner_steps > 0)
CHECK(local_step_end > local_step_start)
CHECK(base_global_version >= 0)
CHECK(status IN (
  'pending','selected','applied','dropped','quarantined'
))
```

加 CHECK 前必须完成的三项核对（缺一不可，否则会在真实 run 上炸掉）：

1. **status 域必须与常量表同时收敛。** `fs_diloco/core/constants.py:20` 定义了 `UPDATE_STATUS_FAILED = "failed"`，但已核实它从未被写入 `updates.status`（实际只有 `pending/selected/applied/dropped`）。Phase 1 要么删除该常量，要么把 `'failed'` 加进 CHECK；不允许留下一个“合法常量但被 CHECK 拒绝”的陷阱。同理，新增的 `'quarantined'` 必须同时加入常量表。
2. **`local_step_end > local_step_start` 需先对历史 run 做数据审计。** drain 场景下的 “final proposal” 已核实是普通的 `last_update_id`（`fs_diloco/runtime/learner.py:2997`），不是零步合成 proposal，因此该 CHECK 预期成立；但迁移工具的 `--dry-run` 必须在真实历史 DB 上跑一遍并报告违例行数，而不是假定成立。
3. **`processed = effective + discarded` 的等式约束只在 v4 之后成立。** 旧行的 `tokens_this_update` 是 processed 语义（见 §8.3），迁移时不得直接改名，必须按 §8.3 的语义断点规则处理。

#### D. Fenced transaction capability

替换动态 proxy：

```python
session = authority.open_leader_session(token)
with session.transaction() as tx:
    ...
```

规则：

- `AuthorityStore` read/write 接口分离；
- public mutator inventory 由 CI 检查；
- raw connection 私有；
- transaction 后采样 token/lease；
- transaction metrics 统一。

#### E. Clock、object store 和 read ports

定义：

```python
Clock.wall_time()
Clock.monotonic()
ImmutableObjectStore.create(...)
ImmutableObjectStore.stat(...)
ImmutableObjectStore.read(...)
```

### 7.3 测试

新增：

```text
tests/domain/test_proposal_v2.py
tests/domain/test_token_accounting_types.py
tests/domain/test_state_unions.py
tests/authority/test_schema_v4_constraints.py
tests/authority/test_fenced_transaction_capability.py
tests/architecture/test_dependency_boundaries.py
tests/architecture/test_mutator_inventory.py
```

负例至少覆盖：

- zero/negative/NaN/Inf token；
- invalid step range；
- path escape；
- symlink；
- wrong tensor shape；
- unknown format；
- stale/negative epoch；
- direct raw mutation；
- unfenced write command；
- core→runtime import。

### 7.4 Gate

- schema/property 测试通过；
- 每个接受的协议 JSON 在进入 application 前转为 typed object；
- application 层不出现 `dict[str, Any]` proposal；
- `core` 不再 import runtime；
- 所有 public write commands 均由 capability inventory 覆盖；
- migration dry-run 能审计旧 DB，但 runtime 尚不切换；
- P0 golden trace仍保持原行为。

---

## 8. Phase 2：P0 正确性、活性、token 和测量闭合

Phase ID：`P2-correctness-measurement`

本 phase 修复所有阻止新实验的 High 问题：H-01、H-02、H-03、H-04、H-05、H-06、H-08、H-09，并处理 M-04、M-07、M-09、M-10、M-13。

### 8.1 H-01：旧 incarnation livelock

#### 实现

1. dynamic eligible query 只返回 current fence：

```sql
JOIN learner_instances li
JOIN placements p
JOIN streams s
...
AND p.current_instance_id = li.instance_id
AND p.current_placement_epoch = li.placement_epoch
AND s.current_instance_id = li.instance_id
AND s.current_stream_epoch = li.stream_epoch
```

2. `revoke_dead_instances()` 同一 transaction：

```sql
UPDATE updates
SET status='dropped', drop_reason='revoked_incarnation'
WHERE learner_instance_id=?
  AND status IN ('pending','selected');
```

3. final drained proposal 作为显式例外：

- instance status=`drained`
- `update_id == final_update_id`
- close generation 匹配
- fence 仍 current 或由 terminal rule 明确接受

例外与第 2 步不冲突，边界必须按 **instance status** 划分而不是在同一条 `UPDATE` 里特判：`revoke_dead_instances()` 只终结 `dead`/`revoked` 的 incarnation；`drained` 的 instance 走 `advance_dynamic_drain()` 路径，其 final proposal 保持可摄取直至 terminal 规则消费。任何把 drain 逻辑塞进 revoke SQL 的实现都视为不满足 DMB-08。

4. commit fence error 返回结构化 invalid IDs。
5. retry：
   - invalid selected → drop；
   - still-current selected → reset pending；
   - 不回滚已确定无效 proposal 为 pending；
   - 记录 fence diff。

实现落点（已核实）：需要改的是 `fs_diloco/storage/sqlite_store.py:947` 的 `eligible_updates()`、`fs_diloco/storage/fenced_store.py:2038` 的 `revoke_dead_instances()`、以及 `fs_diloco/runtime/syncer.py:3647-3684` 的整批 `reset_selected_to_pending`。三处必须在同一工作单元内改完；只改其中一处会把 livelock 换成另一种形态（例如只改查询而不终结旧行，会让旧 pending 永久滞留并撑破 §13.4 的 `pending + selected ≤ current contributors` 上界）。

统一 schema 之后 `eligible_updates()` 只有一个实现，static 与 dynamic 共用同一条 membership-scoped 查询（static 的 fence 是固定 generation，见 DMB-11）。不允许保留两条并行查询路径。

#### 测试

- A step100 revoke → B step1 同 stream → 下一 merge 必须使用 B；
- revoke before select；
- revoke after select before commit；
- 一批中一个旧 proposal + 多个合法 proposal；
- replacement 再失败；
- drain final proposal；
- randomized admit/revoke/publish/select/commit state machine。

#### 通过指标

在 current quorum 持续存在时：

- global version 在最多 2 个 selection cycle 内前进；
- `membership_fence_retry_same_invalid_id_count = 0`；
- stale incarnation 成功 commit 数 = 0；
- revoked proposal 最终状态唯一且为 dropped；
- 不因单个 stale proposal触发无限 whole-batch rollback。

### 8.2 H-03：安全 ingest 顺序

#### 新事务顺序

```text
decode
→ identity/integrity validate
→ ordinary INSERT
→ exact replay / conflict adjudication
→ supersede older accepted pending
→ update frontier with disposition
→ commit
```

禁止 `INSERT OR IGNORE`。

冲突规则：

- exact replay：同 identity、digest、协议字段，返回 replay；
- same logical key but different bytes/identity：quarantine；
- update ID collision：quarantine；
- 旧 pending 保持有效，直到新 insert 成功；
- frontier 记录 disposition。

#### 测试

- same unique key / same content；
- same unique key / different update ID；
- same update ID / different hash；
- insert crash；
- supersede crash；
- frontier crash；
- pointer old/new reorder；
- fragment 归档参考测试保留，但主 runtime 不执行 fragment。

#### 通过指标

任何 transaction 结束后：

```text
frontier target ∈ accepted ∪ exact_replay ∪ quarantine
```

不得出现：

```text
frontier 已推进
AND 新 proposal 未入库/未 quarantine
AND 旧 pending 已 dropped
```

### 8.3 H-02/H-08：segment-based token accounting

#### 设计

learner cycle 内使用 `TrainingSegmentAccumulator`。

每次 global replace：

1. 关闭当前 segment；
2. 记录：
   - processed tokens
   - discarded reason=`local_mid_cycle_replace`
3. 清空有效 loss/token/examples accumulator；
4. 新 segment 以新 global version 为 base。

rebase/predict：

- 不丢弃 segment；
- 按 strategy 明确 carry；
- token 不重复。

proposal V2：

```text
processed_tokens_this_cycle
effective_tokens_this_update
discarded_tokens_this_cycle
segment_count
effective_segment_start_step
effective_segment_end_step
base_global_version
```

merge weight：

```text
raw_i = effective_tokens_i / staleness_penalty_i
```

权威 ledger：

```text
token_segments
proposal fate
application fate
discard reason
```

terminal 守恒（原式漏掉了“已摄取但最终没进 merge”的两类去向，会在任何发生 supersede 或 staleness drop 的 run 上失衡；修正为）：

```text
processed_segment_tokens
=
  effective_applied_tokens        # 进入某个 committed merge 的
+ local_discarded_tokens          # mid-cycle replace 等本地丢弃，从未发布
+ ingested_not_applied_tokens     # 已入库但最终 status='dropped'
                                  #   （superseded / stale / revoked_incarnation / missing_payload）
+ quarantined_tokens              # status='quarantined'
+ unpublished_tokens              # 训练完但进程终止前未发布 pointer
+ pending_segment_tokens          # 仍在途
```

terminal 时 `pending_segment_tokens = 0`，其余五项各自有独立计数器，且每一项都必须能按 `drop_reason` 下钻。

`token_segments` 因此需要区分两个层级，不能只有一个 `discarded` 桶：

| 层级 | 记录者 | 去向字段 |
|---|---|---|
| local segment | learner | `local_discarded` / `published` |
| proposal fate | authority | `applied` / `dropped(reason)` / `quarantined` / `pending` |

`unpublished_tokens` 只能由 learner 的最终 heartbeat 或 authority 的 revoke 记账补齐；若 learner 硬崩溃且没有最终 heartbeat，该项**无法精确恢复**。这是本计划接受的已知不精确点：terminal 守恒断言必须写成

```text
processed = applied + local_discarded + ingested_not_applied
          + quarantined + pending + unaccounted
```

并对 `unaccounted` 单独设阈值（无 learner 硬崩溃的 run 中必须为 0；有硬崩溃时必须 ≤ 该 learner 一个 cycle 的 token 上界）。把 `unaccounted` 强行归零的实现是在掩盖问题，不是守恒。

#### 测试

对每个 inner step 注入：

- replace；
- rebase；
- predict；
- stop；
- upload skip；
- crash before pointer；
- pointer accepted/superseded/stale。

最小反例：

```text
step1 → replace → step2 → publish
```

断言：

- proposal 参数只受 step2；
- effective token 只包含 step2；
- step1 进入 discarded；
- selected token 与参数语义一致。

#### 通过指标

- terminal ledger 中 `unaccounted = 0`（无硬崩溃场景），有硬崩溃时 ≤ 一个 cycle 上界且有对应事件记录；
- NaN/negative token weight 接受数 = 0；
- CSV 删除后 authority summary 不变；
- `total_seen_tokens`（`fs_diloco/runtime/syncer.py:3601`）不再作为模糊公开指标；
- 旧名称只在 migration/历史分析中出现。

#### 语义断点声明

`updates.tokens_this_update` 在 v4 中被拆成 `processed_tokens_this_cycle` 与 `effective_tokens_this_update`。旧 run 的该列是 **processed** 语义，新 run 的 `effective_*` 是 **effective** 语义，两者在发生过 mid-cycle replace 的 run 上不可比。规则：

- 迁移工具**不得**把旧列直接改名为任一新列；
- 分析工具按 `schema_version` 分支解释，`< 4` 一律标注 `token_semantics = "processed(legacy)"`；
- 跨 v3/v4 的 token 曲线对比在报告中必须显式标注不可比，或只使用 `processed` 侧对齐。

这条断点会使 Plan 01/02 已发布的 token 相关结论无法与 Plan 03 直接并列，属于计划内的已知代价。

### 8.4 H-05：公平 contributor selection

#### 选择分层

第一层：

```text
每 contributor 选一个 proposal
```

第二层：

```text
从 contributor 中按 persistent service debt 选 quorum_max
```

推荐确定性策略：

```text
(last_selected_committed_version,
 first_eligible_at,
 stable_contributor_id)
```

优先最久未服务者。

`selection_state` 只在 global commit 成功的同一 transaction 更新。

dynamic contributor key 使用 `stream_id`；instance 只是当前承载者。

#### 指标

- per-stream selected count；
- max wait versions；
- Jain fairness；
- selection entropy；
- applied/produced effective token ratio；
- skipped due quorum cap。

#### 通过指标

门禁只在 **`quorum_max < N`** 时有判别力：当 `quorum_max >= N`（当前正式实验 config 的普遍情形，例如 8 learner + `quorum_max = 8`）每轮全选，任何策略都满足公平性，此时门禁只做回归保护。因此模拟必须显式覆盖两组配置：

| 组 | N | quorum_max | 作用 |
|---:|---:|---:|---|
| A | 8 | 8 | 回归保护：确认新策略在生产配置下不改变现有行为 |
| B | 8 | 3 | 判别组：H-05 的实际门禁 |

在固定 N 个持续 eligible contributor、无 churn 的 1000 merge 模拟中：

- Jain index ≥ 0.95（B 组）；
- max wait versions ≤ `ceil(N / quorum_max) + 1`（B 组）；
- 任一 contributor selected count 与均值偏差 ≤ 1（B 组）；
- A 组的 selected 集合与修复前逐轮完全一致（否则说明公平性改动越界影响了生产配置）；
- replay 相同 tape 得到相同 lineage（两组）。

### 8.5 H-06：structured FS read 与 visibility grace

#### 实现

runtime 不再使用模糊 `safe_read_json()`。

```python
Ok
NotFound
TransientIO(errno)
Malformed(fingerprint, reason)
IdentityMismatch(reason)
```

proposal visibility tracker 持久化：

- first_missing_at；
- consecutive_not_found；
- last_success_at；
- last_errno；
- stable_malformed_fingerprint；
- visibility deadline。

drop 条件：

- NOT_FOUND 跨越 grace；
- 至少 3 次独立 NOT_FOUND；
- 中间没有成功观察；
- payload 不属于正在 publication 的年轻对象。

TRANSIENT_IO：

- 不直接 drop；
- 计数和告警；
- 超过 operator threshold 可 terminal/manual review。

MALFORMED：

- 两次读取到相同 fingerprint 后 quarantine；
- 保留原始诊断，不 unlink。

registration request 同样适用。

#### 通过指标

故障注入：

- 1–2 次 ENOENT 后恢复：0 drop；
- ESTALE/EIO 后恢复：0 drop；
- 永久缺失：grace 后 exactly-once drop；
- stable malformed：exactly-once quarantine；
- transient fault 不导致 dynamic admission request 被删除。

### 8.6 H-09：matched comparison 重写

先运行 workload equivalence checker：

- source/env/model init；
- dataset/tokenizer；
- seed；
- stream/cursor plan；
- outer target；
- effective token；
- selected count；
- failure tape；
- timer anchor；
- GPU allocation。

输出：

```text
overall_status = PASS | BLOCKED
comparison_status = COMPARABLE | INCOMPARABLE
signed_delta
```

禁止：

```python
max(0, ratio)
```

异常差异：

- 绝对 signed 差异 >20%：自动 `INCOMPARABLE`；
- 工作量不等：`BLOCKED`；
- 缺失权威 denominator：`BLOCKED`。

### 8.7 Golden trace 重新基线（必须显式执行）

Phase 2 会**故意**改变可观察行为：merge weight 从 processed token 改为 effective token（§8.3），selection 顺序从“ID 排序 + 截断”改为 service-debt 排序（§8.4）。因此 Phase 0 冻结的 `classic_full_v1_trace.json` / `static_ha_v1_trace.json` 在 Phase 2 结束时**必然失配**，这不是回归，但必须被证明是受控的。

规则：

1. Phase 1 结束时 golden trace 必须仍然逐位相等（§7.4 已要求），任何偏差都是 Phase 1 越权。
2. Phase 2 结束时产出 `tests/fixtures/golden/unified_v2_trace.json`，并写一份 `artifacts/<ts>_p2-trace-rebaseline_review.json`，逐条列出：
   - 哪些 tape case 的 selected 集合变化、变化原因归属哪个 finding；
   - 哪些 case 的 effective weight 变化、变化量、是否只来自 mid-cycle replace 的 segment；
   - 哪些 case **不应该**变化却变化了 —— 这类必须当作缺陷处理，不得写进 rebaseline。
3. 构造 tape 时必须包含至少一组 **不含 mid-cycle replace、不触发 quorum 截断** 的 case，这组在 Phase 2 前后必须 `torch.equal`。它是区分“预期语义变更”与“意外数值漂移”的唯一锚点。
4. §10.8 中“deterministic tape 与 Phase 2 post-fix oracle `torch.equal`”指的是与 `unified_v2_trace.json` 比对，不是与 Phase 0 的 classic trace 比对。

### 8.8 Phase 2 state-machine gate

新增：

```text
tests/state_machines/test_proposal_membership_machine.py
tests/state_machines/test_token_segment_machine.py
tests/state_machines/test_selection_machine.py
```

正式 Gate：

- 至少 1000 randomized examples；
- 每个 example 最多 500 transitions；
- invariant violation = 0；
- liveness bounded-step assertion 通过；
- deterministic replay hash 一致；
- 受影响完整 pytest 组通过；
- 真实 tiny static/dynamic pipeline 通过。

---

## 9. Phase 3：scheduler、数据连续性、时间、初始化、审计和环境

Phase ID：`P3-operational-robustness`

修复 H-07 与 M-01—M-03、M-05—M-06、M-08、M-11—M-12、M-15，以及 authority/audit/telemetry 边界。

### 9.1 Scheduler uncertainty state machine

持久字段：

```text
first_scheduler_uncertain_at
last_positive_scheduler_evidence_at
uncertainty_deadline
terminal_evidence_source
manual_review_reason
```

状态：

```text
planned
→ submitting
→ submission_unknown
→ submitted
→ started
→ terminal_uncertain
→ admitted | failed | expired | manual_review
```

规则：

- live `no_record` 不立即释放；
- 查询 historical；
- 检查 registration receipt；
- zombie window 到期前保留 reservation；
- query_failed 超过 deadline → manual_review；
- uncertain 不自动重提；
- logical request at-most-one admission；
- operator action必须审计。

### 9.2 Monotonic clock 全面替换

- 所有进程内 timeout/deadline 使用 `Clock.monotonic()`；
- wall time 只用于持久化审计和跨进程比较；
- 获得 SQLite write lock 后重新采样 wall time；
- 记录 DB lock wait；
- lease safety tracker 使用一致采样点。

测试：

- wall clock +1h/-1h 跳变；
- monotonic 正常；
- DB lock 长等待；
- lease acquire/renew。

### 9.3 Idempotent staged initializer

```text
<run>.staging.<uuid>
    ↓ schema/config/source validation
    ↓ fsync
    ↓ bootstrap-complete
    ↓ atomic finalize or identity-bound marker
```

要求：

- 失败不留下可被 candidate 误认的完成 authority；
- 同 identity 重试可 resume/repair；
- 不同 identity fail closed；
- initializer 不覆盖已有完成 run。

### 9.4 Archive exactly-once interpretation

不要求物理 exactly-once append，但要求逻辑去重：

- archive batch ID；
- row primary key；
- batch manifest；
- fsync before prune；
- analysis 按 `(record_kind, primary_key)` 去重；
- cleanup 读取 artifact policy。

### 9.5 Artifact manifest

每个 artifact 分类：

```text
authority
audit
telemetry
cache
payload
temporary
```

字段：

- correctness_required；
- rebuildable；
- retention policy；
- owner；
- digest mode；
- cleanup eligibility。

cleanup 不再硬编码“看起来像 telemetry 的文件名”。

### 9.6 配置全量 validate

每个 section 实现纯函数 `validate()`：

- type；
- enum；
- positive durations/counts；
- cross-field；
- removed keys；
- protocol version；
- unsupported modes；
- source/environment requirements。

直接构造 `Config()` 后进入 runtime 也必须 validate。

### 9.7 Environment identity

descriptor 增加：

- `uv.lock` hash；
- Python version；
- torch/CUDA/driver；
- transformers/datasets/safetensors version；
- container/image digest（若有）；
- model revision；
- tokenizer hash；
- dataset revision/fingerprint；
- PBS queue/resource class；
- Lustre mount identity（非秘密字段）。

### 9.8 Deterministic stream cursor

目标不是承诺跨 crash 的严格 exactly-once 数据消费，而是：

```text
确定性 stream sequence
+ monotonic durable cursor
+ 有界、可计量 replay
```

设计：

- 每个 stream 映射 deterministic global block index；
- learner 定期发布 fenced stream progress；
- authority 只接受 current stream epoch 的单调 cursor；
- replacement 从 durable cursor 开始；
- 两次 cursor publication 之间的 replay 计入 `replayed_tokens`；
- progress interval 决定 replay 上界；
- proposal 记录 sample/block range；
- duplicate physical job不能同时推进 cursor。

### 9.9 真正 streaming 或 fail closed

`data.streaming=true` 不得继续 materialize 全量 token stream。

支持两类 adapter：

```text
indexed_resumable
iterable_resumable
```

无法提供 stable cursor 的 source：

```text
config validation error
```

iterable 实现：

- bounded shuffle buffer；
- online token packing；
- bounded memory；
- deterministic seed/cursor；
- no full list materialization。

### 9.10 Phase 3 Gate

Scheduler：

- qsub timeout/reconcile；
- receipt missing；
- live no-record；
- historical lag；
- qstat outage；
- same PBS ID rerun；
- uncertainty deadline；
- manual review；
- duplicate admission = 0。

Data：

- replacement cursor 单调；
- replay tokens ≤ 一个 progress interval 的配置上界；
- same seed/cursor 产生相同 block hash；
- concurrent writer被 fence；
- streaming 100k rows后 RSS 相对 warm-up增长 ≤10%。

Time/init/archive：

- wall jumps不改变 timeout结果；
- init crash 点全部可安全重试或 fail closed；
- archive crash不丢 authority；
- duplicate archive 分析结果不重复计数；
- cleanup 不选择 authority/audit。

---

## 10. Phase 4：统一 HA cutover

Phase ID：`P4-mandatory-fenced-runtime`

### 10.1 目标

所有正式 full runtime 入口切换到统一 fenced implementation。classic 暂时保留在不可达的 reference/compat 区域，供最终对照；正式入口不再调用。

### 10.2 Syncer cutover

`run_syncer()` 改为：

```text
load immutable descriptor
→ wait bootstrap complete
→ acquire candidate lease
→ open LeaderSession
→ initialize-or-resume authority
→ publish heartbeat
→ run common merge application
→ terminal
→ release
```

删除正式路径中的：

- `ha_mode` 分支；
- raw `SQLiteStore` runtime construction；
- `publish_stop()` 作为 authority；
- `latest.json` 恢复；
- `config.init.resume` 分派；
- learner/syncer 同一 launcher 下的 classic role mixing。

### 10.3 Learner cutover

learner 永远：

- 加载 descriptor；
- 校验 source/environment/config；
- 只创建自己 instance 目录；
- 使用 epoch control reader；
- 不直接信任 fixed latest/stop；
- static learner 也有稳定 process/stream identity；
- dynamic learner再增加 admission fence。

### 10.4 Static membership 在统一 authority 中的表示

不要为 static 创建另一套 store。

建议：

- static run 初始化固定 contributor rows；
- identity 可以保留 `learner_000` 等稳定 ID；
- stream ID固定；
- 不启用 registration/outbox；
- proposal、selection、token ledger、terminal 与 dynamic 共用；
- membership fence简化为固定 generation，但仍由 authority验证。

### 10.5 Launcher 和 CLI

正式启动顺序统一：

```text
python -m fs_diloco.tools.init_run          → 建议同时注册 console script fs-diloco-init-run
python -m fs_diloco.tools.launch_run        → 新增；当前只有 launch_phase{1,2}_acceptance 等专用 launcher
fs-diloco-syncer   (candidate 语义)          → 已存在，行为改为“永远是 candidate”
fs-diloco-learner                            → 已存在
```

已核实：`pyproject.toml` 的 `[project.scripts]` 当前只有 `fs-diloco-{syncer,learner,inspect,export-run-metrics,lm-eval,validation-eval,publish-quality-gate}`；`fs-diloco-init-run`、`fs-diloco-launch-run`、`fs-diloco-syncer-candidate` 都不存在，PBS 里用的是 `python -m fs_diloco.tools.init_run`。所以这里有两项真实工作，不是改名：

1. 在 `pyproject.toml` 注册新入口（并保留 `python -m` 形式，因为现有 PBS 脚本依赖它）；
2. 新写一个通用 `fs_diloco/tools/launch_run.py`，把 `launch_phase1_acceptance.py` / `launch_phase2_acceptance.py` / `launch_phase2_matched.py` 的公共编排收敛进去。

是否新增 `fs-diloco-syncer-candidate` 需要一次决策：统一 runtime 之后 syncer 永远是 candidate，保留旧名 `fs-diloco-syncer` 可以避免改动全部 PBS 脚本。**建议保留旧名**，只在文档中改称谓，不新增别名入口。

`launch_run`：

- initializer；
- qsub syncer candidate；
- qsub static array或dynamic bootstrap array；
- durable receipts；
- 不自动取消已接受的其他作业。

单机 local smoke 也必须走 initializer + candidate，不得保留 classic shortcut。

### 10.6 Config migration tool

新增：

```text
fs-diloco-migrate-config-v3-to-v4
```

行为：

- `--dry-run` 默认；
- 删除 `syncer_ha.enabled`；
- 移动 lease fields；
- 删除 `init.resume`；
- 报告 fragment 不支持；
- 输出 diff；
- 不覆盖原文件；
- 完整 round-trip validate。

### 10.7 Legacy run policy

- old classic completed run：inspect/export；
- old classic incomplete run：不 resume；
- old HA completed run：inspect/export；
- old HA incomplete run：只允许显式 offline migration 到新 root；
- migration 必须重新校验 checkpoint、identity、terminal；
- 不修改原 root。

### 10.8 正式实验 config 与 PBS 的迁移矩阵（Phase 4 必做，删除 classic 的前置条件）

这是本计划最容易被低估的部分。**已核实：仓库中所有真实 GPT-2/WikiText-2 实验 config 都是 classic**，只有 4 个 tiny config 打开了 `coordination.syncer_ha`：

```text
带 syncer_ha 的（4）：
  configs/fs_diloco_tiny_ha_static.yaml
  configs/fs_diloco_tiny_ha_static_acceptance.yaml
  configs/fs_diloco_tiny_ha_dynamic_2node.yaml
  configs/fs_diloco_tiny_ha_dynamic_acceptance.yaml

classic 的正式实验 config（必须迁移，否则 Plan 01/02 的结果不可复现）：
  configs/fs_diloco_gpt2_wikitext2_8l.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_predict.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_predict_bf16all_cuda.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_rebase_bf16all_cuda.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_wait2p5.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_wait2p5_predict.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_5000steps_terminal_capture.yaml
  configs/fs_diloco_gpt2_wikitext2_8l_acceptance.yaml
  configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml
  configs/fs_diloco_gpt2_wikitext2_1l_rebase_state_debug.yaml
  + 各 tiny 本地 config（adaptive_global_stop / midcycle_replace / predict / rebase /
    publish_ingest / wait / watchdog / scheduler_replace / syncer_bf16_gpu / ...）
```

以及依赖它们的 PBS 脚本（`scripts/miyabi/run_9node_gpt2_wikitext2*.pbs`、`run_8node_colocated_*.pbs`、`run_1node_debug.pbs`、`run_2node_debug.pbs`、`run_2node_resume_regression.pbs`、`run_plan01_regression.pbs` 等）。

因此 Phase 4 增加以下强制工作单元，且必须在 Phase 5 删除 classic **之前**完成：

1. **迁移矩阵**：为上表每个 config 产出 v4 版本，写入 `artifacts/<ts>_p4-config-migration_matrix.json`，逐行记录 `旧路径 / 新路径 / 语义差异 / 是否需重新基线`。
2. **迁移工具覆盖**：`fs-diloco-migrate-config-v3-to-v4`（§10.6）必须能处理上表全部 config 并 round-trip validate，而不是只处理 4 个 tiny HA config。
3. **PBS 脚本迁移**：每个受影响脚本改为 initializer + candidate 顺序，`bash -n` 通过，`#PBS -W group_list=` 保持字面组 ID 规则。
4. **回归锚点**：至少一条 Plan 01 回归路径（`run_plan01_regression.pbs`）在 v4 config 下重跑通过，作为“迁移没有改变训练语义”的证据。
5. **重新基线声明**：由于 §8.3 的 token 语义断点，迁移后的 config 跑出的 token 曲线与历史结果不可直接并列；矩阵中必须逐条标注 `需重新基线 = yes/no`。

未完成本节任一项时，Phase 5 的删除动作视为**未获授权**。

### 10.9 Phase 4 Gate

静态源码门禁：

```text
runtime 中无 "if ha_mode"
runtime 中无 "syncer_ha.enabled"
runtime 中无 raw SQLiteStore(...)
runtime learner 不调用 prepare_run_dirs(...)
```

允许这些字符串只存在于：

- migration；
- historical reader；
- frozen tests/docs。

行为：

- 1 syncer static run；
- 2 candidate takeover；
- dynamic replacement；
- error terminal + successor resume；
- canonical cache corruption；
- auto recovery submission disabled；
- manual candidate接管。

通过指标：

- committed RPO = 0；
- stale leader successful business mutations = 0；
- successor 下一版本严格为 N+1；
- fixed cache 删除后可修复；
- authority 删除后 fail closed；
- deterministic tape 与 Phase 2 post-fix oracle `torch.equal`；
- 单 candidate 正常完成。

---

## 11. Phase 5：删除 classic、归档 fragment 与架构收口

Phase ID：`P5-delete-classic-refactor`

只有 P4 完整通过后才允许执行删除。

### 11.1 删除范围

删除生产代码中的：

- classic syncer loop；
- classic learner latest/stop authority path；
- classic init/resume；
- classic `publish_stop`；
- legacy full configs；
- classic PBS scripts；
- classic local smoke shortcut；
- `syncer_ha.enabled`；
- `init.resume`；
- full classic tests；
- fragment正式入口和 configs；
- docs 中 classic/HA mode 对照。

保留：

- archived Git tag；
- 只读 legacy analysis/export；
- migration tests；
- deterministic pure reference trace，不保留可执行生产 classic runtime。

### 11.2 Fragment 处理

已核实的 fragment 表面（远大于“删 config”）：

```text
模块    fs_diloco/protocol/fragment_codec.py
        fs_diloco/protocol/fragment_index.py
        fs_diloco/protocol/fragment_scheduler.py
        fs_diloco/runtime/syncer.py 中的 fragment_update_proposal_source 及分支
        fs_diloco/tools/analysis.py、run_metrics_csv.py 中的 fragment 读取
schema  fs_diloco/storage/schema.sql: fragments / fragment_versions /
        fragment_updates / fragment_proposal_frontiers（4 张表）
        + idx_fragment_updates_status / idx_fragment_updates_target /
          idx_fragment_versions_event
配置    9 个 config（tiny_fragment_local / tiny_fragment_terminal_local /
        tiny_watchdog_fragment_local / tiny_scheduler_fragment_local /
        gpt2_1l_fragment_debug / gpt2_8l_fragment_5000steps /
        gpt2_8l_fragment_50x4 / gpt2_8l_fragment_50x10 /
        gpt2_8l_no_fragment_50x10）
PBS     run_1node_fragment_debug.pbs / run_2node_fragment_debug.pbs /
        run_9node_fragment_gpt2_wikitext2_5000steps.pbs /
        run_9node_fragment_gpt2_wikitext2_50x10.pbs /
        run_9node_fragment_gpt2_wikitext2_50x4.pbs
测试    11 个文件（test_fragment_{analysis,codec,final_wait,index,latest_retry,
        materialization,merge,pipeline_smoke,pointer_discovery,scheduler,store}.py）
```

执行前必须：

1. 创建 `archive/fragment-v0-final`；
2. 保存 current fragment tests、configs、实验结果索引；
3. README 写明：
   - mainline暂不支持 fragment；
   - 历史实现在哪个 tag；
   - 重新引入需要 version-vector authority、resume、HA；
4. 删除主 config 中 `fragments.enabled`；
5. runtime收到旧 fragment config时明确报迁移错误，不能静默转 full；
6. **解决“归档 vs 只读分析”的冲突**：§2.1 承诺旧 run 支持只读分析/导出，而旧 fragment run 的 DB 里就有 `fragment_*` 表。因此四张 fragment 表**不得**从 legacy reader 的可读集合中移除。落地方式二选一并在 `docs/migration-v3-v4.md` 写明：
   - (a) v4 schema 不再创建这些表，legacy reader 按 `schema_version < 4` 走独立只读路径；
   - (b) v4 schema 保留表定义但 runtime 永不写入，由 CI 断言“无写入路径”。
   建议 (a)：保留空表会让 §13.6 的“no dead code”门禁失去意义。
7. **测试删除的记账**：删除 11 个 fragment 测试文件会使收集数从 495 下降。删除前必须逐个确认它测的是 fragment 专有逻辑还是共享不变量（`test_fragment_store.py` 尤其可疑，很可能覆盖了通用 store 行为）。共享部分必须在删除前迁移到 full 路径的等价测试中，并在 `progress.md` 记录“删除 N 个用例 / 迁移 M 个断言 / 净变化”。不允许出现“测试数下降但没人解释”的情况。

### 11.3 God module 拆分

目标约束：

- runtime entrypoint ≤300 行；
- application service 单文件建议 ≤500 行；
-任一 authority concern 不与其他 concern 共用 3000 行 store；
- syncer main loop不直接 SQL/Path/qstat；
- learner main loop不直接负责 schema decode；
- no dynamic `__getattr__` mutation dispatch。

不是单纯按行数判定，但超过上限必须在审查报告解释。

### 11.4 Authority/audit/telemetry 文档

新增：

```text
docs/guarantees.md
docs/protocol-v2.md
docs/migration-v3-v4.md
docs/threat-and-failure-model.md
docs/artifact-classes.md
```

README 只描述最终模式：

```text
Full static
Full dynamic
```

不再写：

```text
classic mode
HA mode
```

而写：

```text
single-candidate deployment
multi-candidate failover deployment
```

### 11.5 Architecture Gate

- import boundary test通过；
- no domain→adapter dependency；
- no core→runtime dependency；
- external PBS/FS/SQLite/HF/W&B只通过 adapter；
- 100% write command有 fencing classification；
- legacy reader全部 query-only；
- main config无法表达 classic/fragment runtime；
- help/CLI/docs无死入口；
- `git grep` classic symbols结果只在 migration/history。

---

## 12. Phase 6：完整验收、性能与最终审查

Phase ID：`P6-acceptance-final-review`

### 12.0 G0：范围、成本与前置条件门禁

沿用 Plan 02 的 G0 惯例（Plan 02 §7.3）。在任何 phase 的计算节点验证开始前必须确认：

- 工作树干净、review-target commit 已冻结、base 是 target 的祖先；
- schema/source pin 与 descriptor 一致；
- 42 个 fenced 写命令 inventory 与冻结文件一致；
- §21 的工具前置条件已就绪；
- **本 phase 的 PBS 成本估算已写入 `progress.md`**（见 §12.14），包括 job 数、每个 job 的 walltime 估计与依据；
- `miyabi-development` skill 可用性确认（涉及 PBS/GPU 的 phase）；
- 故障矩阵与生成式测试的预计 wall time。

G0 未通过时不得提交任何 `qsub`。

### 12.1 G1：登录节点静态门禁

必须通过（已在仓库中使用过的部分）：

```bash
git diff --check
python -m compileall -q fs_diloco
ruff check fs_diloco tests
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
```

新增门禁（**均为本仓库首次引入，属于 Phase 0 前置工作，见 §21**）：

```bash
ruff format --check fs_diloco tests        # 需先做一次全仓格式化提交，否则必然全红
pyright fs_diloco/domain fs_diloco/ports fs_diloco/application   # 只对新包，不对存量
```

已核实：仓库**从未**使用过 `pyright`、`ruff format`、`hypothesis`，`pyproject.toml` 也没有 `[tool.pytest.ini_options]`。因此：

- `pyright` **不得**一次性指向整个 `fs_diloco`（25842 行、长期未做类型检查，必然产生大量报错并把 G1 变成永久 BLOCKED）。范围限定为 Phase 1 新建的 `domain/ports/application` 三个包，随重构逐步扩大，每次扩大范围都要在 `progress.md` 记录新纳入的模块；
- `ruff format --check` 引入前必须有一次独立的“纯格式化”提交，且该提交不得与任何行为改动混在一起（否则审查 diff 无法阅读）。

增加：

- config removed-key scan；
- classic symbol scan；
- raw SQLite writer scan；
- import dependency checker；
- docs link checker；
- PBS group ID literal checker。

### 12.2 G2：聚焦测试组

至少：

```text
proposal schema/conflict
membership revoke/replacement/liveness
token ledger/adoption
fair selection
filesystem visibility
scheduler ambiguity
stream cursor
fenced transaction
terminal
migration
cleanup/artifact classes
legacy read-only analysis
```

要求：

- 每个 accepted bug先有 RED；
- 正例、反例、rollback 同组通过；
- 0 个核心 invariant xfail。

### 12.3 G3：生成式状态机

规模按 §21.3 分层执行；下列是 **gate 层**的要求，开发内环与工作单元层使用较小 profile 但共用同一组不变量。

最低：

- 1000 examples；
- 每 example最高 500 transitions；
- 随机动作：
  - admit
  - publish
  - pointer reorder
  - transient read fault
  - select
  - revoke
  - replace
  - commit
  - crash/restart
  - drain
  - scheduler ambiguity
- invariant violation = 0；
- deterministic replay hash一致。

### 12.4 G4：publication crash matrix

故障点至少包括：

1. object temp write；
2. object fsync；
3. immutable create；
4. weight complete；
5. outer complete；
6. pre-commit fence；
7. transaction after version insert；
8. transaction after proposal transitions；
9. DB commit；
10. canonical pointer；
11. fixed cache；
12. maintenance/archive。

每个点至少重复 10 次。

检查：

- 只能恢复到 transaction 前或后；
- selected不重复应用；
- next commit=N+1；
- orphan可清理；
- DB integrity；
- cache repair；
- stale leader不能提交。

### 12.5 G5：真实 local/tiny pipeline

矩阵：

| learners | syncer candidates | membership | 故障 |
|---:|---:|---|---|
| 1 | 1 | static | none |
| 2 | 1 | static | none |
| 2 | 2 | static | active crash |
| 2 | 1 | dynamic | learner replacement |
| 2 | 2 | dynamic | syncer + learner failure |

检查：

- terminal；
- summary；
- heartbeat；
- DB；
- current checkpoint；
- token ledger；
- active rows；
- temp/orphan；
- no error event。

### 12.6 G6：10,000-cycle boundedness

基线：现有 `tests/test_bounded_1000_cycles.py` 已覆盖 1000 cycle。本门禁把规模提高 10 倍，因此必须先在 Phase 0 用现有测试测出单 cycle 的实际耗时，再决定承载形式：

- 若 10,000 cycle 的墙钟时间 ≤10 分钟：作为带 `@pytest.mark.slow` 的 pytest 用例，默认不在快速回归中运行；
- 否则：作为独立 PBS job（`scripts/miyabi/run_plan03_bounded_10k.pbs`），并在 G0 记录预计 walltime。

不允许把一个数小时的循环塞进默认 pytest 组。

在固定 M 下：

硬门禁：

- `pending + selected <= current contributor count`；
- current committed global row = 1；
- current weight/outer pair = 1；
- proposal pointer数 = contributor count；
- active payload数有界；
- epoch dirs ≤ configured retention；
- unresolved scheduler requests有界；
- quarantine由 retention policy有界。

物理门禁：

- warm-up 后 SQLite live page slope `< 0.01 page/cycle`，否则 BLOCKED 或必须实施显式 compaction；
- active文件数线性回归斜率约为 0；
- recovery scan time不随累计 cycle显著线性增长；
- discovery每轮 stat/open/read上界与 M 成正比、与历史无关。

### 12.7 G7：跨节点

至少 2 节点：

- shared SQLite争抢；
- active leader提交；
- successor reopen；
- old writer SIGSTOP/SIGCONT；
- cache delete/repair；
- transient FS visibility fault；
- qstat live/historical。

指标：

- SQLite integrity；
- busy retry有界；
- RPO 0；
- stale commit 0；
- no duplicate selected application。

### 12.8 G8：Miyabi 8+1 正式 static acceptance

配置：

- 8 learners；
- 1 syncer node；
- single candidate 正常路径；
- 至少 50 local steps/cycle；
- 至少 20 global versions；
- 后续 soak 建议 120 versions；
- full FP32 和 BF16至少各一组。

通过：

-每版本 selected contributor distinct；
- target version达到；
- token ledger zero balance；
- no fairness starvation；
- no transient drop；
- terminal input closed；
- current-only；
- authority/cache一致；
-所有 learner final heartbeat；
- Checker PASS。

### 12.9 G8：Miyabi 8+1 dynamic failure acceptance

固定 failure tape：

1. 8 bootstrap；
2. 一个 learner永久终止；
3. replacement queue/started/admitted；
4. 旧 instance proposal仍存在；
5. replacement复用 stream；
6. duplicate physical job；
7. active syncer crash；
8. successor takeover；
9. old syncer恢复；
10. terminal drain。

通过：

- current quorum存在时进展；
-旧 proposal不会livelock；
- logical request只 admission一次；
- stream epoch增加；
- replay token有界；
- stale syncer commit=0；
- terminal closed；
- 120 versions soak完成；
- authority状态有界。

### 12.10 性能门禁：classic reference vs unified single-candidate

classic 只从冻结 tag在独立 worktree运行。执行约束（避免双重权威）：

- 独立 `git worktree` + 独立虚拟环境（classic 侧的 schema 版本与 config schema 都与主线不同）；
- 独立 run root、独立 authority DB，两侧不得共享任何 run 目录；
- classic 侧不得写入主线的 `reports/DOING/<plan-id>/artifacts/` 以外的任何路径。

先 workload equivalence PASS，然后比较。

至少 5 次独立重复，报告 signed delta 和 bootstrap CI。

**阈值来源**：下面的 5%/8% 是**待标定的初值**，不是既有证据。仓库当前没有任何 classic 与 HA 的性能对照数据（唯一的 matched 证据是 Plan 02 的 static-vs-dynamic）。Phase 0 任务 13 会先测出实际值并按其规则确认或修订本节阈值；未完成标定就直接以 5% 开工属于计划缺陷。

通过条件：

- source/env/model/data/work量一致；
- unified median positive overhead ≤5%（或 Phase 0 标定后的修订值）；
- 95% CI upper bound ≤8%（或标定后的修订值）；
- `abs(signed median delta) ≤10%`；
- 任何方向绝对差异 >20% 自动 INCOMPARABLE；
-不能使用 clipped ratio；
- syncer duty cycle、lease/control开销单独报告。

### 12.11 性能门禁：static vs dynamic no-failure

至少 5 次 matched repeat。

工作量：

- same unique/effective tokens；
- same outer versions；
- same selected count distribution；
- no failure；
- same timer anchor。

通过：

- comparison_status=COMPARABLE；
- positive dynamic overhead median ≤5%；
- absolute signed median ≤10%；
- 95% CI和原始值全部保存；
- 巨大负 overhead不能自动 PASS。

### 12.12 质量回归

两层：

#### Deterministic synthetic

- exact update tape；
- theta/outer state bitwise equal；
- selection lineage equal。

#### Real training

- 至少 3 seeds；
- same unique-token budget；
- validation perplexity；
-相同 checkpoint anchor。

通过建议：

- 95% CI 上界显示相对 perplexity regression ≤1%；
- 若统计功效不足，状态为 BLOCKED/INCONCLUSIVE，不写 PASS；
- WikiText-2只作为 smoke，不作为最终论文质量结论。

### 12.13 实验成本预算与门禁降级规则

本计划的 P6 隐含了一个很大的 PBS 预算。执行前必须把它写明，否则会在中途因为排队时间被迫跳过门禁。粗略计数：

| 门禁 | job 数 | 规模 | 备注 |
|---|---:|---|---|
| G4 publication crash matrix | 12 故障点 × ≥10 次 | 1 节点 tiny | 可合并进少量 job，内部循环 |
| G5 local/tiny pipeline | 5 组合 | 1–2 节点 tiny | |
| G6 10,000-cycle | 1 | 纯 DB，无 GPU | 见 §12.6 |
| G7 跨节点 | ≥7 场景 | 2 节点 | |
| G8 static acceptance | 1（+FP32/BF16 各一组 = 2） | 9 节点 | ≥50 local × ≥20 global |
| G8 dynamic failure acceptance | 1 + soak(120 versions) | 9 节点 | |
| §12.10 classic vs unified | 5 重复 × 2 臂 = 10 | 9 节点或缩小规模 | **成本最高项** |
| §12.11 static vs dynamic | 5 重复 × 2 臂 = 10 | 9 节点 | |
| §12.12 质量回归 | ≥3 seeds × 2 臂 = 6 | 9 节点长跑 | |

合计约 30+ 个 9 节点作业。**这是必须在 G0 明确接受或缩减的量级**，不能默认可行。允许的缩减手段（按优先级）：

1. §12.10 与 §12.11 降到 3 次重复，并把 CI 宽度作为报告项而非门禁项 —— 需在计划中记录降级理由；
2. §12.10 用缩小规模（1 syncer + 2 learner、更少 global step）执行，因为它测的是控制面开销而非训练吞吐；
3. §12.12 的质量回归按 `plans/AGENTS.md` 记为 `PASS_WITH_FOLLOWUPS`，在 plan 完成后单独排期。

**不允许**降级的门禁：G4、G6、G7、G8 static、G8 dynamic 中与正确性/活性/有界性直接相关的部分。性能与质量门禁可以降级为 `PASS_WITH_FOLLOWUPS`；正确性门禁只有 `PASS` 或 `BLOCKED`。

长作业阶段性结果按 `plans/AGENTS.md` 只能记 `PASS_WITH_FOLLOWUPS`，且必须写明剩余部分与预计完成条件。

### 12.14 最终双模型审查

每个 phase 按 `plans/AGENTS.md`：

- freeze review-target commit；
- Codex与Claude独立审查；
- High/Critical必须修；
- Medium修复或有证据延期；
- accepted finding先 RED；
- phase-final commit。

P6 后：

- 对最新 target 中全部 `fs_diloco/` current state 做完整审查；
- 汇总 remediation；
- 修复后增量复审；
- 未关闭 High/Medium 不得完成 plan。

---

## 13. 全局通过指标

### 13.1 正确性

| 指标 | 门槛 |
|---|---:|
| stale leader成功业务提交 | 0 |
| committed version跳号/分叉 | 0 |
| weight/outer theta mismatch | 0 |
| stale incarnation成功commit | 0 |
| logical request多次admission | 0 |
| frontier orphan disposition | 0 |
| token ledger终态差值 | 0 |
| current quorum下无限livelock | 0 |
| current reference被GC | 0 |
| authority缺失后凭cache恢复 | 0 |

### 13.2 活性

| 指标 | 门槛 |
|---|---:|
| replacement后有current quorum的前进循环 | ≤2 selection cycles |
| scheduler uncertainty | deadline内terminal或manual_review |
| syncer RPO | 0 committed versions |
| takeover下一版本 | N+1 |
| permanent missing proposal | grace后exactly-once drop |
| transient read恢复后的永久drop | 0 |

RTO使用配置公式判定：

```text
takeover_to_next_commit
≤ lease expiry
+ max clock skew
+ candidate poll
+ candidate startup/reload
+ one normal merge interval
```

### 13.3 公平性

| 指标 | 门槛 |
|---|---:|
| 1000 merge Jain fairness | ≥0.95 |
| stable eligible max wait | ≤ceil(N/quorum_max)+1 |
| contributor count偏差 | ≤1 |
| deterministic replay lineage | 完全一致 |

### 13.4 有界性

| 指标 | 门槛 |
|---|---:|
| active pending+selected | ≤current contributors |
| current global version rows | 1 |
| current weight/outer pairs | 1 |
| proposal pointers | current contributors |
| epoch dirs | ≤configured max |
| active file slope | ≈0 |
| DB live-page slope | <0.01 page/cycle |
| recovery scan vs history | 不线性增长 |

### 13.5 性能与可比性

| 指标 | 门槛 |
|---|---:|
| unified vs classic positive median overhead | ≤5% |
| unified vs classic CI upper | ≤8% |
| static vs dynamic positive median overhead | ≤5% |
| comparison absolute signed median sanity | ≤10% |
| absolute single comparison异常界限 | 20%，超过则INCOMPARABLE |
| workload equivalence | 必须PASS |

### 13.6 代码与架构

| 指标 | 门槛 |
|---|---:|
| runtime `ha_mode` 分支 | 0 |
| runtime `syncer_ha.enabled` | 0 |
| runtime raw SQLiteStore writer | 0 |
| unfenced public mutation | 0 |
| core→runtime import | 0 |
| domain→adapter import | 0 |
| legacy DB read-write tool | 0 |
| classic/fragment正式config | 0 |
| requirement→test→artifact覆盖 | 100% |
| unresolved Critical/High | 0 |
| unresolved Medium without disposition | 0 |

---

## 14. 上次审查 finding 处置映射

| Finding | Phase | 处置 |
|---|---|---|
| H-01 old incarnation livelock | P2 | current-only eligibility + revoke drop + selective retry |
| H-02 mid-cycle replace token | P2 | segment accumulator + authoritative ledger |
| H-03 insert/supersede order | P2 | insert-first + replay/conflict quarantine |
| H-04 strict schema | P1/P2 | Proposal V2 + DB CHECK + tensor validation |
| H-05 fairness | P2 | persistent oldest-unserved selection |
| H-06 transient FS error | P2 | typed read + persisted visibility grace |
| H-07 PBS ambiguity | P3 | uncertainty state machine |
| H-08 token semantics | P2 | metric ontology/ledger |
| H-09 matched gate | P2/P6 | equivalence checker + signed repeat |
| M-01 wall timeout | P3 | Clock port/monotonic |
| M-02 fake streaming | P3 | resumable bounded streaming or reject |
| M-03 stream restart | P3 | durable cursor + bounded replay |
| M-04 short UUID/replace | P2 | full UUID/content ID + O_EXCL |
| M-05 half init root | P3 | staged idempotent initializer |
| M-06 archive duplicate | P3 | batch/row IDs |
| M-07 multiwriter CSV | P2/P5 | per-process telemetry + offline merge |
| M-08 weak config validation | P1/P3 | section validators |
| M-09 weak DB constraints | P1/P2 | schema v4 constraints |
| M-10 oldest semantics | P2 | explicit service age |
| M-11 publication idempotence | P3 | deterministic command ID |
| M-12 lease clock sample | P3 | post-lock resampling |
| M-13 orphan after fence fail | P2 | staging/intent + fast GC |
| M-14 fragment weaker guarantees | P0/P5 | archive/remove mainline |
| M-15 env identity | P3 | environment fingerprint |
| A-01 God modules | P5 | application/domain/adapter split |
| A-02 dependency inversion | P1/P5 | ports and import gates |
| A-03 proxy mutator risk | P1/P2 | capability transaction |
| A-04 full/fragment split | P0/P5 | full V2 only; fragment archived |
| A-05 dict/string states | P1 | typed unions |
| A-06 external system ports | P1/P5 | explicit ports/adapters |
| A-07 artifact boundary | P3/P5 | artifact manifest |
| A-08 scenario-only tests | P2/P6 | state-machine tests |
| A-09 guarantee matrix | P0/P5 | docs/guarantees.md |
| A-10 migration/version | P1/P3 | schema/command versions |
| D-01 immutable wording | P2/P5 | enforce O_EXCL then document |
| D-02 token naming | P2/P5 | precise fields |
| D-03 PASS domains | P5 | correctness/perf/quality separation |
| D-04 fragment boundary | P0/P5 | top-level unsupported |
| D-05 signed performance | P2/P5/P6 | no clipping |
| D-06 fault model | P3/P5 | threat/failure document |

补充规则：

- 表中的 `Phase` 是**修复落地**的 phase；每条 finding 的**判定**一律在 P0 完成（§6.2 任务 11）。P0 判为 `rejected-with-evidence` 的 finding，其对应行改记为“不修复 + 证据路径”，不得静默保留原处置。
- H-09 的处置跨 P0/P2/P6：P0 标定阈值、P2 实现 signed 比较、P6 执行门禁。表中原写 `P2/P6`，实际起点在 P0。
- 本表只覆盖上次审查的 40 条 finding。§10.8（config/PBS 迁移）、§11.2 第 6–7 项（fragment 归档的 schema 与测试记账）、§21（工具前置）不是 finding 的处置，而是“删除 classic”这一目标自带的成本，必须单独在 requirement matrix 中占行。

---

## 15. 推荐 commit/工作单元顺序

每个工作单元完成关联测试后再提交，不把所有改动压成一个 commit。

```text
C00 plan/bootstrap/report scaffolding + requirement matrix
C01 frozen tags + deterministic tapes + 42-mutator inventory
C01a finding triage + 四个 RED 复现测试 + 测试基座
C01b 工具前置（hypothesis/pytest markers）+ 独立的纯格式化提交
C01c classic-vs-HA overhead 标定 + classic/fragment 迁移面 inventory
C02 typed proposal/control/domain states
C03 schema v4 + migration dry-run
C04 fenced transaction capability
C05 safe proposal ingest/conflict/quarantine
C06 current-membership eligibility/revoke/retry
C07 segment token ledger
C08 fair selection
C09 typed FS read/visibility tracker
C10 matched-equivalence checker
C11 scheduler uncertainty
C12 clock/init/archive/artifact manifest
C13 stream cursor + resumable data adapter
C13a Phase 2 golden trace 重新基线 + 归因报告
C14 mandatory syncer cutover
C15 mandatory learner cutover
C16 launcher/config migration + 新增入口点
C16a 正式实验 config 与 PBS 全量迁移（§10.8）+ Plan 01 回归重跑
C17 delete classic production runtime
C18 archive/remove fragment mainline（含 fragment_* 表与测试记账）
C19 split God modules/import gates
C20 full tests/checkers/docs
C21 Miyabi acceptance evidence
C22 final review remediation
```

禁止把 C17/C18 提前到 C14—C16a 之前。特别地，**C16a 未完成时 C17 不得开始**：正式实验 config 与 PBS 仍指向 classic 时删除 classic，会同时失去主线运行能力和 Plan 01/02 的复现能力。

---

## 16. 预计修改与新增文件

### 16.1 核心

```text
fs_diloco/core/config.py
fs_diloco/core/run_descriptor.py
fs_diloco/core/constants.py
```

### 16.2 新 domain/application/ports

```text
fs_diloco/domain/*
fs_diloco/application/*
fs_diloco/ports/*
```

### 16.3 authority/adapters

```text
fs_diloco/adapters/sqlite_authority/*
fs_diloco/adapters/posix_object_store.py
fs_diloco/adapters/pbs_scheduler.py
fs_diloco/adapters/hf_data_stream.py
```

可在迁移期间从现有：

```text
storage/sqlite_store.py
storage/fenced_store.py
storage/leader_lease.py
storage/maintenance.py
runtime/pbs_scheduler.py
runtime/launch_outbox.py
modeling/hf_data.py
```

逐步提取，最终删除重复路径。

### 16.4 Runtime

```text
fs_diloco/runtime/syncer.py
fs_diloco/runtime/learner.py
fs_diloco/runtime/syncer_ha.py
fs_diloco/runtime/adoption.py
```

最终 entrypoint 应变薄。

### 16.5 Tools 与 Checker

应用工具放在 `fs_diloco/tools/`：

```text
fs_diloco/tools/migrate_config_v3_v4.py
fs_diloco/tools/migrate_run_v3_v4.py
fs_diloco/tools/check_workload_equivalence.py
fs_diloco/tools/launch_run.py            # 新增，见 §10.5
fs_diloco/tools/run_metrics_csv.py
fs_diloco/tools/analysis.py
fs_diloco/tools/clean_run.py
```

Checker 放在 `scripts/miyabi/`，与既有 `check_plan01_invariants.py` / `check_plan02_phase1.py` / `check_plan02_phase2.py` 同级（这是仓库已有惯例，不要放进 `fs_diloco/tools/`）：

```text
scripts/miyabi/check_plan03_phase0.py
scripts/miyabi/check_plan03_phase1.py
scripts/miyabi/check_plan03_phase2.py
scripts/miyabi/check_plan03_phase3.py
scripts/miyabi/check_plan03_phase4.py
scripts/miyabi/check_plan03_phase5.py
scripts/miyabi/check_plan03_phase6.py
```

Checker 与 phase ID 的绑定：

| Checker | phase-id | 模式 |
|---|---|---|
| `check_plan03_phase0.py` | `P0-freeze-oracles` | `PASS` / `BLOCKED` |
| `check_plan03_phase1.py` | `P1-typed-foundation` | `--mode staged\|completed` |
| `check_plan03_phase2.py` | `P2-correctness-measurement` | `--mode staged\|completed` |
| `check_plan03_phase3.py` | `P3-operational-robustness` | `--mode staged\|completed` |
| `check_plan03_phase4.py` | `P4-mandatory-fenced-runtime` | `completed` 需绑定 §10.8 迁移矩阵 artifact |
| `check_plan03_phase5.py` | `P5-delete-classic-refactor` | `completed` |
| `check_plan03_phase6.py` | `P6-acceptance-final-review` | `completed` 需绑定 matched-performance artifact |

### 16.6 Tests

```text
tests/domain/
tests/authority/
tests/application/
tests/adapters/
tests/state_machines/
tests/integration/
tests/architecture/
tests/reference/
```

---

## 17. Checker 与 evidence 规范

每个 checker 输出 JSON：

```json
{
  "plan_id": "fsb_decoupled_diloco_plan_03_unified_ha",
  "phase_id": "P2-correctness-measurement",
  "status": "PASS",
  "source_identity": {},
  "environment_identity": {},
  "requirements": {
    "DMB-06": {
      "status": "PASS",
      "evidence": []
    }
  },
  "metrics": {},
  "errors": []
}
```

stdout 只打印：

```text
PASS
PASS_WITH_FOLLOWUPS
BLOCKED
```

性能比较使用：

```json
{
  "comparison_status": "COMPARABLE",
  "signed_delta_ratio": -0.012,
  "positive_overhead_ratio": 0.0,
  "raw_repeats": [],
  "confidence_interval": [],
  "workload_equivalence": {}
}
```

`positive_overhead_ratio` 可以作为派生展示字段，但不得作为唯一 gate；原始 signed delta 必须存在。

---

## 18. 失败升级与清理纪律

执行时严格遵循 `plans/AGENTS.md`：

- 相关测试组通过后追加 `progress.md`；
- 每次失败先追加 `failures.md`；
- 同一实验连续三次失败后停止局部修补；
- 第四次运行前必须完成全面 code review；
- 不把登录节点 torch/pytest 结果当正式 runtime evidence；
- 不自动 qdel；
- cleanup 前必须有 matching completion evidence；
- authority/audit 不允许进入 cleanup candidate；
- 长作业阶段性通过只能是 `PASS_WITH_FOLLOWUPS`；
- phase/plan 必须完成冻结 target 双模型审查。

---

## 19. Rollback 和安全边界

本计划发生问题时不允许通过“重新启用 classic”作为运行时 fallback。

允许的 rollback：

1. Git 回到 Plan 02 tag/branch，运行独立旧实验；
2. 新 Plan 03 run 使用新 root；
3. 旧 run保持只读；
4. migration 失败删除新 staging root，不改旧 root。

禁止：

- 同一个 run root 在 classic 和 unified runtime 间切换；
- schema v4 DB 由旧 runtime写；
- 新 runtime自动降级信任 `latest.json`；
- fragment config静默转 full；
- fencing failure 后调用 unfenced legacy mutator。

---

## 20. Plan 完成定义

只有全部满足才标记完成：

1. 主线正式 runtime 只有统一 fenced full protocol。
2. classic full 不再有可调用入口、配置或 writer。
3. fragment V0 已冻结归档并从主运行 schema 删除。
4. 上次审查所有 High 已修复。
5. 所有 Medium 已修复或有明确证据、负责人和后续计划；本计划建议全部闭合。
6. typed proposal/control schema 生效。
7. old-incarnation liveness、ingest conflict、token ledger、fairness、FS transient、scheduler ambiguity均有 RED 和 state-machine test。
8. 1/2/9 节点验证通过。
9. 10,000-cycle logical/physical boundedness通过。
10. static/dynamic matched comparison可比且报告 signed statistics。
11. unified vs frozen classic reference性能门禁通过。
12. deterministic model/outer state等价性通过。
13. source/environment/data identity完整。
14. authority/audit/telemetry分类和 cleanup安全通过。
15. requirement matrix 100% 映射到测试和 artifact。
16. 最终 current-state 多模型审查无未处置 Critical/High/Medium。
17. README、architecture、runtime flow、data flow、configuration、operations、guarantees、migration、failure model全部同步。
18. plan-final commit、review报告、Checker artifact和清理 manifest完整。
19. 40 条 finding 全部有 P0 triage 结论，四个必复现项各自的 RED 测试已转 GREEN 或已记 `rejected-with-evidence`。
20. §10.8 的正式实验 config 与 PBS 迁移矩阵 100% 完成，至少一条 Plan 01 回归路径在 v4 config 下重跑通过。
21. fragment 归档的 schema 与测试记账（§11.2 第 6–7 项）已完成，测试收集数变化有逐项解释。
22. `unaccounted` token 项在无硬崩溃 run 中为 0，有崩溃时在声明上界内（§8.3）。
23. §12.10 / §12.11 的性能阈值来自 P0 标定或有明确的修订记录，不是未经测量的默认值。

---

## 21. 工具与依赖前置条件（Phase 0 完成，否则后续门禁不可执行）

已核实：仓库当前的 dev 依赖只有 `pytest` 和 `ruff`；没有 `hypothesis`、没有 `pyright`、没有 `[tool.pytest.ini_options]`、`ruff format` 从未运行过。本计划的多个门禁依赖这些工具，必须在 Phase 0 一次性落地。

### 21.1 依赖

```toml
[project.optional-dependencies]
dev = [
    "pytest",
    "ruff",
    "hypothesis",     # 新增：§8.8 / §12.3 生成式状态机
    "pytest-timeout", # 新增：防止状态机/有界性测试挂死
]
```

`pyright` 通过独立方式安装（node 或 `pyright` pip 包），不进主依赖树；若 Miyabi 登录节点无法安装，则 §12.1 中的 `pyright` 一项降级为 `deferred-with-justification` 并在 `progress.md` 记录，不阻断其余门禁。

### 21.2 pytest 配置

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "slow: 长时间运行（有界性、生成式、故障矩阵），默认回归不运行",
    "stateful: hypothesis RuleBasedStateMachine",
    "requires_gpu: 需要 GPU",
]
```

理由：§12.3 要求 1000 examples × 最多 500 transitions（最坏 50 万次状态转移）。这必须与日常回归分离，否则每次改动都要等数十分钟。默认回归跑 `-m "not slow"`，门禁跑全量。

### 21.3 生成式测试的规模分层

一次性要求 1000 examples 在开发期不可持续。分层执行：

| 场景 | examples | max transitions | 何时运行 |
|---|---:|---:|---|
| 开发内环 | 25 | 50 | 每次改动 |
| phase 关联测试组 | 200 | 200 | 工作单元完成时 |
| phase gate / G3 | 1000 | 500 | Checker 与完成门禁 |

三层必须使用同一个 state machine 定义与同一组不变量，只有 profile 不同；任何一层出现 violation 都按失败处理并按 `plans/AGENTS.md` 记录。deterministic replay hash 在三层都要求一致。

### 21.4 测试目录

`tests/coordination`、`tests/distributed_syncer`、`tests/learner_protocol`、`tests/lifecycle`、`tests/log`、`tests/performance_core`、`tests/protocol` 当前只剩 `__pycache__`，没有任何 `.py`。所有 55 个测试文件都在 `tests/` 顶层。Phase 0 需要二选一：

- (a) 清理这些空目录，按 §16.6 的新结构（`tests/domain`、`tests/authority`、…）重建；
- (b) 保留并复用其中语义匹配的目录。

建议 (a)，并在同一提交里删除全部 `__pycache__`，避免新旧两套目录结构并存造成导入歧义。

---

## 22. 冻结决策（review 给出多个选项时，本计划的选择）

上次审查在若干处只给出方向而未定选项。为避免执行期临场决策，冻结如下；改变任一条都需要新的计划修订记录。

| # | 议题 | 冻结选择 | 理由 |
|---:|---|---|---|
| 1 | mid-cycle `replace` 的处理 | **保留**并做 segment accounting（§8.3），不采用“禁止 mid-cycle replace”的简化方案 | adoption 策略是研究变量，禁用会砍掉一整类实验 |
| 2 | `tokens_this_update` 语义 | v4 拆为 `processed_*` + `effective_*`，旧列**不改名**，按 `schema_version` 分支解释 | 见 §8.3 语义断点声明 |
| 3 | 公平性默认策略 | `oldest_unserved_first`（`(last_selected_committed_version, first_eligible_at, stable_contributor_id)`） | 确定性、可重放；DRR/rotating hash 作为可选 |
| 4 | typed schema 实现方式 | 手写 frozen dataclass + 显式 validate，**不引入 Pydantic** | 避免为一个原型引入运行时依赖与性能不确定性 |
| 5 | quarantine 存储 | 保留原始字节到 `control/quarantine/`，不 unlink | 保证可诊断，D-01 的“immutable”表述才成立 |
| 6 | fragment | 本计划只归档，不实现 fragment HA | 见 §0 的三重风险论证 |
| 7 | outer optimizer 数学 | 本计划**不改** | 保持与 Plan 01/02 的可比性 |
| 8 | 指标改名 | 通过版本断点改，不做双写兼容 | 双写会把 H-08 的 ontology 问题延续下去 |
| 9 | Phase 5 重构 | 行为等价重构，不夹带语义修复 | 语义修复全部前置在 P2/P3 |
| 10 | `fs-diloco-syncer-candidate` | **不新增**，沿用 `fs-diloco-syncer` | 避免改动全部既有 PBS 脚本，见 §10.5 |

---

## 23. 与候选计划的关系

`plans/DOING/candidate_plans/fsb_decoupled_diloco_plan_03.md` 是本计划的前身，采取“只修 review 问题、不动 classic”的四阶段方案（Phase 0 triage → P0 修复 → P1 运维 → 架构）。本计划取代它，但吸收了三样东西：

1. **Phase 0 的 triage 门禁**：每条 finding 必须 `reproduced` / `rejected-with-evidence` / `deferred-with-justification`（已并入 §6.2 任务 11）；
2. **代码证据表**：file:line 级别的事实核对（已并入 §0A.2），避免按未验证的审查结论改代码；
3. **冻结决策表**（已并入 §22）。

候选计划保留为只读参考。若本计划在 Phase 4 因为 §10.8 的迁移成本被判定不可行，**回退路径是执行候选计划**（只修问题、保留 classic），而不是缩减本计划的删除范围。这个回退决策必须在 Phase 4 G0 之前做出，不能在删除动作进行到一半时做。

---

## 24. 下一计划的允许起点

Plan 03 完成后，下一研究计划才能选择以下之一：

1. **FragmentProtocolV1 on fenced authority**
   - version vector；
   - multiple in-flight fragments；
   - crash-consistent resume；
   - streaming overlap。

2. **Transport baseline**
   - same application/domain；
   - POSIX FS vs message/RPC；
   -只替换 adapter。

3. **Asynchronous optimizer study**
   - base-relative displacement；
   - delayed Nesterov；
   - RDA；
   -固定 failure/data tape。

在 Plan 03 完成前，不建议同时引入 fragment HA、micro-syncer、gossip、object store 或新的 second-order outer optimizer。
