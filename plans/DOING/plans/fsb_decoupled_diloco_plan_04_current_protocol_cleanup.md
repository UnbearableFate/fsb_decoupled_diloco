# Plan 04：清除历史协议并把 Full Protocol 收敛为唯一当前实现

计划 ID：`fsb_decoupled_diloco_plan_04_current_protocol_cleanup`

状态：待实施；禁止在完成 §0 启动冻结前修改产品代码

Workflow：version `3`

设计：

```text
plans/DOING/design/fsb_decoupled_diloco_plan_04_current_protocol_cleanup-design.md
```

Requirement matrix：

```text
plans/DOING/plans/fsb_decoupled_diloco_plan_04_current_protocol_cleanup-requirement-matrix.csv
```

实施与审查记录：

```text
reports/DOING/fsb_decoupled_diloco_plan_04_current_protocol_cleanup/
reports/DOING/code_review/fsb_decoupled_diloco_plan_04_current_protocol_cleanup/<review-id>/
```

构建依据：

- 根目录 `AGENTS.md` 的“最简设计&实现原则”；
- `plans/AGENTS.md`、`plans/plans_create_guide.md`、`plans/workflow.md`；
- `plans/DONE/plan03/fsb_decoupled_diloco_plan_03_unified_ha.md` 及 requirement matrix；
- `reports/checked/fsb_decoupled_diloco_plan_03_unified_ha/implementation-retrospective.md`；
- Plan03 同一 final source target 的 static/dynamic 证据 `reports/checked/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260810-075600_p6-g8-plan-final-pass.json`、`20260810-075600_p6-g9-plan-final-pass.json`，只用于现状/资源估算，不作为本计划 PASS；
- construction baseline 的 tracked code、tests、configs、scripts、DDL、docs、package data 和调用图。

## 0. 启动冻结：当前唯一阻塞前置条件

本计划构建时，`plans/workflow.md` 是尚未提交的新文件。当前内容身份为：

```text
workflow_version: 3
workflow_sha256: c5017e9114666b0eda13fc6b6f11ac4f5a806662dbe6ba334423f7863c44c08f
construction_baseline: c8b7aa9b33138fa7751317b0bfe50a94c9b672aa
workflow_commit: UNFROZEN
branch_point: UNFROZEN
```

执行者不得把 construction baseline 冒充 branch point，也不得声称未提交的 workflow 已被 commit 固定。第一项实施动作必须是：

1. 在不丢失用户现有改动的前提下，把本 plan/design/matrix、`plans/workflow.md`、`plans/plans_create_guide.md`、适用 `AGENTS.md`、`scripts/miyabi/run_multi_agent_review.pbs` 和本计划引用的 Plan03 retrospective 固定到一个完整 plan-init commit；
2. 从该 plan-init snapshot 创建本计划独立 branch；
3. 把完整 branch point、包含 workflow 的完整 commit、workflow SHA-256 和 source inventory 写入 `workflow_state.yaml` 与 `progress.md`；
4. 验证冻结文件的 SHA-256 与本计划构建身份一致；若不同，先记录 drift 并重新审查规则，不能继续沿用本计划中的旧 pin；
5. 运行 plan/matrix/schema 的静态自检后，才进入 P0。

这一步只建立可审计起点，不授权整理、覆盖或提交与本计划无关的用户改动。

## 1. 目标与完成定义

### 1.1 最终结果

完成后：

1. 仓库只提供 `Full Protocol`，其行为是当前 Full Protocol v4 的 fenced static/dynamic 语义，但 active 产品表面没有 v4 generation 名称或 selector；
2. v1/v2/v3、classic、Fragment V0、旧 HA/config/runtime、legacy reader、migration、fallback、shim、旧测试、旧配置和历史 plan harness 从 active tree 删除；
3. 旧 run/config/checkpoint/database/metadata 不可查询、迁移或 resume；系统只从 current config + fresh root 启动；
4. Full Protocol 只有一个 strict config API、一种 topology spelling、一组 fresh schema、一条 runtime/writer 链和一套 current-only 工具；
5. torch distributed baseline 保留为独立比较系统，使用独立 strict config loader，不再作为 Full config profile；
6. 当前 tests、fixtures、configs、scripts、package data 和 docs 全部使用 suffix-free 当前名称；
7. static 与 dynamic 的 9-node current-only acceptance 在同一冻结 runtime fingerprint 上通过；实验后的 docs/report closure 仅在 executable scope byte-exact 等价时承接证据。

权威链保持：

```text
current strict config + immutable external-input identity
  → current descriptor (mode = static | dynamic)
  → fresh current SQLite schema
  → leader-epoch/contributor-fenced command transaction
  → immutable proposal/receipt + durable ingest/selection/commit/publication
  → epoch-scoped control + rebuildable convenience cache
```

### 1.2 删除规则

本计划明确覆盖 `plans/plans_create_guide.md` 对 legacy query-only 的普通保留模板：

- 允许 disposition：`retain-current`、`rename-current`、`delete-obsolete`；
- 禁止 disposition：`retain-query-only`、`deprecated`、`compatibility-wrapper`、`migration-only`；
- 一个旧测试若包含仍有效的 current invariant，只把该 invariant 重写进当前测试；原旧测试/fixture 仍删除；
- 删除的证明不是单纯“文件不存在”，而是 writer、entrypoint、config、DDL、fallback、调用者和 current tests 一起闭合。

### 1.3 非目标

- 不重写 fenced authority、merge、token、terminal、scheduler 或训练算法；发现独立 correctness defect 时记录 follow-up，除非它阻断本计划 current acceptance。
- 不支持任何旧输入，不创建 migration 工具，不保留 query-only reader。
- 不删除 current static/dynamic topology，不改变 torch baseline 算法。
- 不合并 proposal/receipt/descriptor 等独立 artifact format version；current decoder 继续 exact-match、fail closed。
- 不运行 classic/fragment/旧 tag 对比，不重写 Git history、archive tag、`plans/DONE/**` 或 `reports/checked/**`。
- 不以性能研究或多 seed 质量研究阻塞结构清理；只验证当前 workload 完成、状态不变量与无明显资源失控。

## 2. 已核实基线与必须重算的 inventory

构建时已核实：

| Surface | construction baseline 事实 | 计划处置 |
|---|---|---|
| configs | 30 tracked YAML：26 Full、4 baseline | 收敛到 design 冻结的精确 7 Full + 3 baseline；每个有 current owner |
| production files | 89 tracked `fs_diloco/**` | 删除 legacy/fragment/migration/shim；rename current v4 |
| tests | 80 tracked `tests/**` | 删除旧行为；按当前领域重命名/拆分 |
| scripts | 51 tracked：48 个 Miyabi、3 个 local | 删除 Plan01/03-only 和两份无 current authority 的 local cleanup；rename/提取 current harness |
| legacy package | `fs_diloco/legacy/{config_v1_v3,fragment_v0,reader}.py` | 整包删除 |
| duplicated config | `core/config.py` + `core/config_v4.py`，profile 分支 | Full 单一 `core.config`; baseline 独立 loader |
| descriptor identity | `full_ha_static|full_ha_dynamic` + `protocol_version=4` | `static|dynamic`; 删除 protocol generation field |
| other topology identity | run identity/bootstrap 另写 `full|full_dynamic` | 所有 Full runtime projection 只写 `static|dynamic` |
| config overrides/paths | resolver 提供 semantic override；repository YAML 固定当前用户绝对 shared root | 只保留 launch identity override；模板 root 为 null 并由 launcher 显式绑定 |
| config dead/duplicate fields | 多个 run/model/data/io 字段没有 current behavior consumer或在所有 tracked config 固定；`data.block_size` 与 `training.block_size` 重复，两个 obsolete tiny config 的缺省还造成错误投影 | 删除无 owner flag/field 与对应分支；sequence length 和 proposal dtype 各只保留一个 owner |
| baseline external input | real-data baseline YAML 未 pin Hub revisions | 独立 loader 与实际 I/O 强制同一 immutable identity |
| schema | `schema_v4.sql`、`schema_v4_dynamic.sql`；package-data 另列不存在的 `schema.sql` | rename 为 `schema.sql`、`schema_dynamic.sql`，package-data 精确两项 |
| current v4 paths | `registration_history_v4`、`registration_dispositions_v4`、`admissions_v4`、bootstrap marker | suffix-free fresh layout；无 fallback |
| compatibility tools | config migration、legacy analysis/eval/metrics/cleanup | 删除或重写为 current-only |
| docs | active docs 仍描述 v4、legacy query-only 和 migration | compatibility 文档删除，其余只写当前态 |

P0 必须保存完整 machine-readable inventory，至少覆盖 production entrypoint、public API、imports、typed artifact、DDL/table、filesystem layout、config caller、PBS caller、tests/fixtures/markers、package data、docs 和 archive dependencies。旧 plan 中的数量和列表都不能代替新 inventory。

## 3. 全局实现约束

### 3.1 唯一 API 与名称

最终唯一名称以 design §3.3 rename table 为准。关键约束：

- 直接移动实现并更新调用者；不留下 re-export、alias、symlink、deprecated stub 或双文件过渡；
- Full loader 只暴露 `Config`、`load_config`、`resolve_config`、`config_to_dict`、`resolved_config_bytes`、`write_resolved_config`；
- `resolve_config` 只接收显式 `run_id`、`shared_root` 与 source capture 所需启动身份；production init/launch 要求 `--run-id/--shared-root/--project-root`，删除 `RUN_ID`、cwd、`PBS_O_WORKDIR`、default-runs 推断以及对 learner 数、seed、polling、dtype、staleness、adoption、completion 等协议语义的第二套 override 参数，harness 先生成完整 current YAML；
- CLI/PBS/env 只传 launch/source identity、scheduler resource 和不改变输入内容的 cache/offline 参数；删除 `FS_DILOCO_HF_WIKITEXT_REPO`、外部 W&B mode 和 `NUM_LEARNERS` 等 YAML 语义 override，重复的 expected identity 由 resolved descriptor 派生并只做 exact-match assertion；
- runtime 当前文件是 `runtime/learner.py` 与 `runtime/syncer.py`；Torch-free entrypoint boundary 保留；
- schema 当前文件是 `storage/schema.sql` 与 `storage/schema_dynamic.sql`；
- descriptor/config/schema/run identity/bootstrap/attestation/result mode 统一为 `static|dynamic`；baseline 的 `ddp|periodic_average` 属于独立比较系统，不进入 protocol topology；
- 删除 `PROTOCOL_VERSION` 及 descriptor/schema/meta 中 `protocol_version`；
- shared-root 外部变量只接受 `FS_DILOCO_SHARED_ROOT`；
- current persistent path 只使用 suffix-free 名称，旧 root 不探测、不 fallback。
- repository config 不携带开发者绝对路径；`run_id/shared_root` 保持 `null`，由 launcher 显式绑定 fresh identity/root。

### 3.2 保持的 correctness 边界

本计划虽以删码和重命名为主，以下不变量不得下降：

- SQLite fenced command 仍是唯一业务 mutation authority；
- actor 在 descriptor/source/admission 校验前不加载 Torch；
- publication 与 durable ingest/commit 使用 exact identity/fence/digest，不用日志或时间猜测；
- static/dynamic 均保持 leader fencing，dynamic 保持 incarnation/placement/stream fencing；
- resolved config、descriptor、actual model/data loader 消费相同 immutable source identity；
- non-synthetic Full/baseline config 都 pin immutable model/tokenizer/dataset identity；synthetic config 使用明确的 synthetic identity；
- cleanup 必须由 current artifact policy、terminal evidence 和 authority live refs 联合授权；
- artifact format version 仍由各自 producer/consumer 独立拥有。
- fresh current initializer 在同一 identity 下的 create-no-replace、crash-prefix replay 和 identity-reservation repair 仍保留；只删除旧 schema/layout repair 与 migration。

Serialized shape 发生变化的 current identities 使用 design 已冻结的 exact 新值：Full config schema `2`、独立 baseline config schema `1`、authority schema `10`、run descriptor format `3`、control format `3`。Parser 只接受本 domain 的 current 值，不保留旧值分支；其他独立 artifact version 若字段未变则保持当前值。

### 3.3 Active-scope 定义

repository cleanup 的 active product scopes 为：

```text
fs_diloco/**
tests/**
configs/**
scripts/**
main.py
pyproject.toml
.python-version
README.md
docs/**
plans/00-RESEARCH_PLAN.md
plans/01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md
plans/followups/B4-fragment-terminal-drain.md
plans/ref/queue-native.md
```

以下范围不参加 product-generation/legacy token zero scan，也不能被产品 runtime 当作 compatibility source 或行为 oracle；workflow/evidence 工具仍可按其合同读取当前报告 artifact：

```text
plans/DONE/**
reports/checked/**
.git/**
reports/DOING/**/artifacts/**
```

`plans/DOING` 中本计划会提到待删除名字，不参加 product-token zero scan。外部模型身份 `deepseek-v4-flash`、IPv4 查询命令 `getent ahostsv4`、Git `--porcelain=v1`、workflow 的短 prompt transport fallback 和 current artifact/checkpoint format version 都不是产品 generation/compatibility；scanner 必须做路径+symbol+语义限定，不能粗暴禁止任意 `v1`/`v2`/`v4`/`fallback` 子串。其他 product fallback 仍须在 P0 用 current caller 与不变量证明其唯一性，否则删除。`plans/00-RESEARCH_PLAN.md`、`plans/01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md`、`plans/followups/B4-fragment-terminal-drain.md` 与 `plans/ref/queue-native.md` 不是冻结 archive，因其目标依赖被删除协议而纳入 P5 删除范围。

### 3.4 Runtime 测试路由

按 workflow v3，control/login plane 只运行 `rg`、Git、plan/matrix/schema 校验、compile/lint 和 PBS syntax 等纯静态检查。每个进入非静态测试的执行阶段在首个 runtime test 前，由 Codex/GPT 主实例显式加载 `$miyabi-development`，在 login node 申请并通过持久 PTY/session 复用同一个 1-node、`interact-g`、`walltime=01:00:00` 的交互式 allocation；进入 compute node 后先核验 hostname/PBS/module/project identity，再由主实例直接编辑共享工作树并执行该阶段所有 focused、unit、full-suite 与单节点 tiny/smoke。此路径明确不得创建 subagent。

单节点测试和 evidence 保存完毕后，主实例停止后台进程、正常退出 allocation、确认 job terminal 并返回 login node，再进入 2-node/9-node batch gate。不得在 login node 重复跑任何非静态 gate。若 `qstat` 显示剩余时间小于 10 分钟，可先保存已完成 gate 的有效 evidence、退出当前 allocation，再申请新的 `interact-g` allocation 继续尚未完成的 gate；不得重复已有有效结果，也不能让 allocation 无任务驻留。

## 4. Requirement 总表

| ID | Owner phase | 可证伪结果 |
|---|---|---|
| `SURFACE-01` | P0/P5 | active product surface 的 current/rename/delete ledger 100% 闭合，无未知项 |
| `LEGACY-DEL-01` | P3 | legacy/classic/fragment/migration/shim 的 writer、reader、entrypoint、fixture、fallback 均不存在 |
| `CONFIG-01` | P1 | Full YAML 只有一个 strict loader/schema/API，无 profile/old-key/alias 分支 |
| `BASELINE-01` | P1/P4/P6 | baseline 使用独立最小 loader，Full loader 不能加载 baseline YAML，retained tiny baseline 可运行 |
| `IDENTITY-01` | P1 | product 无 protocol generation selector；topology 在 config/descriptor/schema 精确同名 |
| `STORAGE-01` | P2 | schema、bootstrap、current paths 和 public symbol 全部 suffix-free，fresh-only |
| `RUNTIME-01` | P3 | learner/syncer/current launcher 只指向 suffix-free runtime，保持 pre-Torch gate |
| `TOOLS-01` | P3 | inspect/export/eval/quality/cleanup 只消费 current descriptor/authority/artifacts |
| `TEST-01` | P4 | 无旧行为测试/fixture；所有共享 current invariant 已迁入 current-domain tests |
| `HARNESS-01` | P4 | 无 Plan01/03-specific active harness/marker/requirement constant；current oracle 使用通用 harness |
| `CONFIGSET-01` | P4 | retained YAML 精确等于 design manifest，且每个有 current owner 和 strict parse test |
| `DOCS-01` | P5/P6 | active docs 只描述当前名称/命令/路径，并记录最终 9-node current result |
| `PACKAGE-01` | P5 | wheel/import/console/package-data 只有当前文件和入口，无 stale/missing schema |
| `ACCEPT-01` | P5/P6 | static/focused/full/tiny/2-node current ladder 在同一 target 通过 |
| `ACCEPT-02` | P6 | 8 learners + 1 syncer 的 static 与 dynamic 正式实验超过 50×10 基线并通过 durable oracle |
| `EVIDENCE-01` | P0-P6 | matrix 100% 绑定 tracked、source-bound、独立 artifact；Checker 不自证 |

机器映射以 requirement matrix 为准；正文和 matrix ID 不一致即 plan-init 自检失败。

## 5. Phase 计划

Phase 只能按下列 commit dependency 顺序推进；每个 `phase-final` 都必须满足 §11 的测试、review、finding disposition 和 matrix 更新门禁：

| Phase | 唯一输入 | 冻结输出 |
|---|---|---|
| P0 | §0 建立的 plan branch point | `P0-phase-final`：inventory、ledger、RED 与 test-design 已冻结 |
| P1 | `P0-phase-final` | `P1-phase-final`：current config/baseline/topology identity 已冻结 |
| P2 | `P1-phase-final` | `P2-phase-final`：fresh suffix-free storage contract 已冻结 |
| P3 | `P2-phase-final` | `P3-phase-final`：runtime/tool cutover 与 legacy deletion 已冻结 |
| P4 | `P3-phase-final` | `P4-phase-final`：current tests/configs/harness 已冻结 |
| P5 | `P4-phase-final` | preformal remediation 后的 `P5-phase-final` candidate |
| P6 | `P5-phase-final` | 唯一 final common target、evidence/docs closure 与 plan-final |

后续 phase 不得读取未冻结 worktree 作为输入，也不得用后续高层 gate 倒替前一 phase 的 focused PASS。

### P0 — `P0-freeze-current-surface`

目标：冻结真实 current surface、删除/rename ledger、RED oracle 和成本预算；不先修改生产行为。

#### P0-W1：启动状态与规则冻结

- 完成 §0；初始化 workflow 规定的报告树与 `workflow_state.yaml`；
- 记录 plan branch point、workflow commit/version/hash、Python/package provenance、current Git status；
- 冻结 formal source scopes、phase IDs、matrix schema、artifact schemas 和测试阶梯；
- 验证 plan ID 在 `plans/DOING/**`/`plans/DONE/**` 全局唯一。

#### P0-W2：机器 inventory 与 disposition ledger

生成：

```text
reports/DOING/<plan-id>/artifacts/<timestamp>_p0-current-surface_review.json
reports/DOING/<plan-id>/artifacts/<timestamp>_p0-disposition-ledger_review.csv
reports/DOING/<plan-id>/artifacts/<timestamp>_p0-config-callers_review.json
reports/DOING/<plan-id>/artifacts/<timestamp>_p0-external-input-identities_review.json
```

Ledger 每行至少有：`path`、`kind`、`current_callers`、`current_invariant`、`disposition`、`replacement`、`owner_phase`、`test_owner`、`reason`。必须逐项覆盖：

- `fs_diloco/legacy/**`、fragment paths/tables/tools/tests；
- 所有 `v4` product path/symbol/string；
- 两个 top-level compatibility shim；
- config migration、removed-key map、env/path fallback；
- 所有 CLI/PBS/env→config/data/model 的 semantic override 与重复 identity projection；
- 每个 config field 的 current producer、consumer、retained-config variation 和 validation owner；零 owner 或只由 obsolete harness 消费的字段必须删除；
- 30 个 YAML 的 caller/owner；
- 所有 retained non-synthetic Full/baseline model、tokenizer、dataset 的 exact repo identity 与 40-hex Hub commit，以及 config→validator→actual I/O caller 的逐项投影；
- 所有 `plan01_*`/`plan03_*`/phase-numbered tests/scripts/markers；
- active docs、package data、console scripts、archive-tag 依赖；
- `main.py`/`fs_diloco.cli` dispatcher、3 个 `scripts/local/**` 文件和仍处于 active plans/ref/followups 区的旧协议文档；
- 每个 current artifact version 的 producer、consumer 与 exact accepted version。

任何 `unknown` disposition 阻断 P1。

#### P0-W3：先建立 RED 与 generic Checker

先写当前目标的失败测试/静态 checker：

- `tests/architecture/test_current_protocol_surface.py`：唯一 Full config API、唯一 schema/writer/entrypoint、active import graph、禁用 product-generation path；
- generic `scripts/check_plan_evidence.py`：只校验 matrix schema、唯一 ID、phase/status、evidence path/source identity，不内嵌 Plan04 requirement；
- checker 自身的 focused tests，覆盖 duplicate ID、missing evidence、wrong source、self-evidence、unknown status；
- pre-cleanup 运行必须因实际 legacy/v4/duplicate surface RED，而不是 invocation/harness 错误；预期 RED 以 strict xfail 或独立 RED artifact 保存，修复 phase 中移除 xfail。

禁用扫描必须区分：

- product generation：`Full Protocol v4`、`Full-v4`、`full_v4`、product path/symbol `_v4`；
- obsolete concepts：legacy/classic/fragment/migration/shim/fallback 的 active implementation/import/config/doc；
- 允许项：外部 reviewer model 名、独立 artifact format version、archive evidence、本计划文字。

不允许用巨大历史文件名 blacklist 代替正向 current allowlist、AST import/caller 检查和 package manifest 检查。

#### P0-W4：test-design review 与资源估计

- Codex/GPT mandatory review 先冻结报告；外部 reviewers 按 workflow 通过 Miyabi PBS best effort；
- Claude Code 若因账户/session quota、认证或 runner 不可用，标记 unavailable 并继续；任何已成功产出的有效 finding 仍逐条 disposition；
- review 必须判断 retained invariant、删除范围、negative oracle、9-node workload 和 artifact schema 是否能证伪 requirements；
- 使用 Plan03 已归档 runtime 证据与已归档 cheap-smoke 实测值估计 1-node/2-node/9-node walltime；P0 不运行项目 runtime，也不 qsub 正式实验。

P0 PASS：ledger 无未知项；RED 原因正确；matrix/schema 自检通过；test-design blocking finding=0；资源预算已记录。

---

### P1 — `P1-current-config-and-identity`

目标：先消除 config/profile/generation identity 的双重表达，为后续 schema/runtime rename 建唯一 typed foundation。

#### P1-W1：Full config 合并

- 在 `core/config.py` 中建立 `Config.coordination.leader`、`Config.maintenance` 和 `Config.sync.stop_after_direct_weight_tokens_applied`；YAML 与 dataclass 一一对应，不做 envelope projection；
- 使 `Config` 直接对应 Full YAML；删除 `ConfigV4` wrapper、`ConfigProfile.FULL_V4`、`full_v4_shared` 与 wrapper projection；
- strict decoder 对 unknown key 只报告 current schema unknown，不识别旧 key；
- 删除 `REMOVED_CONFIG_KEYS`、`_REMOVED_V4_PATHS`、v3→v4 bytes/payload migration、old spelling normalization；
- `CONFIG_SCHEMA_VERSION` 设为 exact `2`，所有 retained Full YAML 更新；Full version `1` 不进入任何 parser branch；
- canonical dtype 只允许 `float32|bfloat16`，配置中的 `"off"` 显式 quote；
- 删除 design §3.1 已核实的无 owner/fixed-off config 字段与实现分支；删除 `IOSection`，把唯一有效值改为 `learner.proposal_dtype`；删除 `training.block_size`，所有 sequence-length consumer 只读 `data.block_size`；
- model/data source 使用 exact `source_kind=hub|synthetic`；Hub 输入要求 repo + 40-hex model/tokenizer/dataset revision，synthetic 输入要求生成参数并记录参数 digest；删除 synthetic magic-name alias、local-path 猜测和跨 kind 字段；WikiText YAML 直接使用冻结的 canonical Hub repo，删除 loader 中 shorthand→`Salesforce/wikitext` 的 retry/fallback；
- repository YAML 的 `run.run_id/run.shared_root` 设为 `null`；launcher 必须显式提供 fresh identity/root，删除机器专属绝对路径和 cwd/env 推断；
- init/launch 还必须显式提供 source `project_root`，并由 source capture 生成身份；不得从 cwd 或 `PBS_O_WORKDIR` 推断，PBS/actor 只传 descriptor 派生的 expected identity，不传可改写 YAML 语义的第二套参数；
- `resolve_config` 只允许 launch identity override；删除 `num_learners`、`training_seed`、`scan_interval_seconds`、`ingest_during_publish`、syncer dtype/device、staleness、adoption strategy、completion mode 和 `profile` 等语义 override，测试/harness 改为先写完整 strict resolved YAML；
- 删除 `FS_DILOCO_HF_WIKITEXT_REPO`、外部 W&B mode、`NUM_LEARNERS` 等 content/algorithm config override；cache/offline、scheduler resource 与 exact expected digest 可保留，但不能改变 resolved semantics；
- 暂时更新所有 P1/P2/P3 所需 current YAML 使其只经唯一 Full loader 解析，P4 再做最终 prune。
- 对全 config schema 建 field→runtime consumer graph；删除零 current owner、只服务旧 harness 或只为未来扩展存在的 field/feature flag。若字段表达 retained current correctness/resource choice则保留并由 strict test 拥有，不能仅因当前 manifest 值相同就无证据删除。

RED/counterexample：同一 YAML 被两套 loader 接受、Full loader 接受 baseline、unknown old key 得到迁移分支、alias dtype 被接受、sequence length 有两个 authority、fixed-off/dead field 仍可配置，任一均 FAIL。

#### P1-W2：baseline 配置隔离

- 新建 `fs_diloco/baselines/config.py` 的最小 `BaselineConfig`/`load_baseline_config`，YAML 字段 `baseline_config_schema_version` 绑定 exact `BASELINE_CONFIG_SCHEMA_VERSION=1`，不复用 Full `config_schema_version`；
- baseline YAML 只保留 `run/model/data/training/inner_optimizer/distributed/wandb`；`distributed` 直接拥有 `world_size/backend/require_distinct_hosts/periodic_average_interval`，不复用 Full `sync` 或 enable flag；
- baseline train/launcher/tests 改用该入口；删除 Full `Config` 中 `torch_baseline.enabled` 和 profile switch；
- baseline CLI 只保留 `--config`、`--mode=ddp|periodic_average`、`--run-id`、`--shared-root`；baseline operator/PBS launcher 显式接收 project root/source identity；删除 `--max-steps`、`--average-interval`、`--backend` 语义 override，测试变体先写完整 strict baseline YAML；
- shared modeling/data/optimizer helper 改为接收实际 section 或窄 typed view，不再要求 baseline 伪装成整个 Full `Config`；
- baseline loader 只包含实际消费字段并 strict reject Full-only sections；Full loader strict reject baseline-only sections。
- retained GPT-2/WikiText baseline YAML 补齐 immutable model、tokenizer、dataset revision，baseline validator、actual Hugging Face loader、training manifest 和 artifact 证明这些值逐项一致；synthetic baseline 记录明确 synthetic identity。

RED/counterexample：两种 loader 对同一 repository YAML 都成功，或 baseline runtime 间接调用 Full resolver，均 FAIL。

#### P1-W3：descriptor/topology identity 收敛

- descriptor `mode` 直接保存 `static|dynamic`，与 config/schema 相同；删除 `full_ha_*` 翻译；
- run identity、bootstrap marker、actor attestation、metrics/formal artifact 和 harness 同步删除 `full|full_dynamic|full_ha_*` 拼写，只投影同一 topology enum；
- 删除 `PROTOCOL_VERSION`、descriptor/schema initialization 中 `protocol_version` 字段和检查；
- `RUN_DESCRIPTOR_FORMAT_VERSION` 设为 exact `3`；descriptor reader 只接受新 shape/value；
- descriptor 中泛化的 `schema_version` 改为 `authority_schema_version`，不得与 `config_schema_version` 或 descriptor `format_version` 混用；
- 保留 exact config/schema/descriptor/artifact format version 与 digest；
- `load_run_descriptor_from_environment` 只读取 `FS_DILOCO_SHARED_ROOT`；
- current invalid descriptor/schema 使用通用 mismatch fail closed，不实现旧 root 特判。

P1 测试：config pure/unit、canonical serialization round-trip、unknown-key/property tests、Full↔baseline cross-rejection、semantic override 拒绝、无机器绝对路径、zero-owner field scan、descriptor identity/relative path/symlink/env counterexamples、Full 与 baseline 的 actual modeling/data loader immutable-identity tests、Hub↔synthetic cross-kind/alias/local-path counterexamples、synthetic parameter-digest tests、baseline regression。

P1 PASS：`CONFIG-01`、`BASELINE-01`、`IDENTITY-01` focused evidence PASS；P1 phase review blocking finding=0；所有相关 tests 在冻结 target 通过。

---

### P2 — `P2-current-storage-layout`

目标：建立 suffix-free fresh schema、bootstrap marker 和 filesystem layout；不保留旧路径探测。

#### P2-W1：schema 与 authority public API

- `schema_v4.sql` → `schema.sql`，`schema_v4_dynamic.sql` → `schema_dynamic.sql`；
- 删除 `protocol_version` 列/insert/check；保留 exact `AUTHORITY_SCHEMA_VERSION`、mode、features 和 DDL identity；
- `AUTHORITY_SCHEMA_VERSION` 设为 exact `10`；不提供 9→10 migration 或旧 DDL reopen；
- SQLite `application_id` 从带 generation 的 `0x46534434` (`FSD4`) 改为 suffix-free exact `0x4653444C` (`FSDL`，filesystem DiLoCo)；open/validation 只接受新值，不保留旧 ID，也不使用易与 PyTorch FSDP 混淆的标识；
- `initialize_authority_v4` → `initialize_authority`，所有 production/test caller 原子切换；
- `V4_BOOTSTRAP_MARKER_NAME` → `AUTHORITY_BOOTSTRAP_MARKER_NAME`，marker 文件 suffix-free；
- fresh run 只使用 `control/authority.sqlite3`（`RunPaths.authority_db`）与 `control/authority_bootstrap_complete.json`；删除 `syncer_metadata.sqlite3`、`control/bootstrap_complete.json` 及相应 path aliases；
- resolved config 只写 `control/run_config.resolved.yaml`，删除 run root 的重复副本和 artifact-policy 条目；
- package-data 暂改为精确 `schema.sql`、`schema_dynamic.sql`，不存在/多余文件即测试失败。

#### P2-W2：current path layout

- `registration_history_v4`、`registration_dispositions_v4`、`admissions_v4` 的 property、目录、glob、artifact policy 和 tests 全部改为 suffix-free；
- `dynamic_close_request_json`/`control/dynamic_close_request.json` 改为 `manual_terminal_request_json`/`control/manual_terminal_request.json`，因为同一 current manual close contract 不属于 dynamic 专用路径；
- 删除 `logs/syncer.jsonl` 或其他历史 fallback；
- fresh initializer 只创建 current layout；不存在双写、copy、rename-on-open、old-path scan；
- 同一 current descriptor/config/source identity 的 initializer crash-prefix replay 与 identity-reservation repair 保持幂等；identity/schema/layout 不匹配时 fail closed，不把 current recovery 扩展成旧 root repair；
- 任何非 current descriptor/schema/layout 只在通用 identity validation 处 fail closed。

#### P2-W3：control/publication 名称

- `V4ControlPublisher` → `ControlPublisher`；服务层、entrypoint 和 tests 同步；
- `CONTROL_FORMAT_VERSION` 设为 exact `3`，summary/control 删除冗余 `authority: full_protocol_v4` 字段，不换成新的 generation marker；
- current publication/lease/contributor fence 行为不变；
- schema/paths/control tests 按当前领域重命名，不留下 `_v4` alias。

P2 测试：fresh static/dynamic DB 初始化、schema hash/features、duplicate init、initializer crash-prefix/identity-reservation replay、publication intent/replay、path uniqueness、artifact policy、current invalid identity rejection、wheel package-data focused test。

P2 PASS：`STORAGE-01` evidence PASS；fresh static/dynamic authority tests 和 phase review PASS；旧 schema/path 不在 package/import/caller graph 中。

---

### P3 — `P3-runtime-and-tools-current-only`

目标：切换唯一 runtime 并删除所有 legacy/fragment/migration/query/fallback 生产面。

#### P3-W1：runtime cutover

- `runtime/learner_v4.py` → `runtime/learner.py`，`runtime/syncer_v4.py` → `runtime/syncer.py`；
- 更新 Torch-free `learner_entrypoint.py`/`syncer_entrypoint.py` 和 console modules；
- `run_v4_allocation.sh` → `run_allocation.sh`，所有 PBS/caller 只引用新名；
- 删除无独立 authority 的 `main.py`/`fs_diloco.cli` dispatcher；增加直接 `fs-diloco-close` entrypoint，actor/inspect/close 各自只有一个推荐 console interface；
- 删除只转发到 inspect console 的 `scripts/miyabi/inspect_run.sh`；PBS/文档直接调用 `fs-diloco-inspect`；
- 保持 descriptor/source/admission 完成前不 import Torch 的 subprocess oracle；
- 不保留旧 module import alias、`sys.modules` hack 或 fallback import。

#### P3-W2：删除 obsolete production packages

删除：

```text
fs_diloco/legacy/**
fs_diloco/tools/migrate_config_v3_to_v4.py
fs_diloco/analysis.py
fs_diloco/eval_lm_harness.py
```

同时删除 fragment decoder/table/query/assertion、migration console entrypoint、legacy config/read imports 和只为这些路径存在的依赖/helper。若 `tools/analysis.py` 的非-fragment current summary 仍需要，直接留下 current-only implementation；不得保留 `assert_fragment_run` 空壳。

#### P3-W3：current-only operational tools

- `tools/analysis.py`、`run_metrics_csv.py` 只读取 current descriptor/authority tables；
- `tools/eval_lm_harness.py`、`validation_eval.py` 只接受 descriptor-bound current checkpoint/config/source，不使用 hard-coded run ID 或 fallback config；
- `publish_quality_gate.py` 使用唯一 current loader；
- `clean_run.py` 删除 `--allow-legacy-run-without-policy`、`legacy_policy_override` 和 policy=None 分支；
- 删除 `scripts/local/clean_run.sh` 和 `scripts/local/prune_runs_without_5000.sh`；它们按扩展名/名称删除且绕过 current policy/terminal/live-reference authority；
- 删除没有 current runtime/formal caller、只服务 Plan03 classic/unified 对比的 `tools/compare_event_traces.py`、`tools/paired_performance.py`、`tools/check_workload_equivalence.py`；同步删除专用 tests/support/docs，current 9-node 不做旧协议 performance comparison；
- 更新 `scripts/local/run_tiny_2proc_smoke.sh`，只使用 retained `tiny_static` config 和 current inspect console entry；
- ambiguous output field `protocol_version` 若表示结果 artifact schema，改为具体 `artifact_version` 名称，避免重新建立 product generation identity。

P3 测试：runtime import/admission/startup、current analysis/export/eval/quality/cleanup positive tests；missing current descriptor/policy、wrong digest/schema、unknown path negative tests；no legacy/fragment import/caller test。

P3 PASS：`LEGACY-DEL-01`、`RUNTIME-01`、`TOOLS-01` evidence PASS；P3 phase review blocking finding=0。

---

### P4 — `P4-tests-configs-and-harnesses`

目标：清除历史测试与实验脚手架，把 retained invariant、YAML、PBS 和 markers 全部改成 current-domain 表达。

#### P4-W1：tests/fixtures 当前化

- 删除 `tests/legacy/**`、`tests/test_fragment_analysis.py`、classic/static historical golden 和所有 migration compatibility cases；
- `unified_v4_trace.json` → `protocol_trace.json`，golden test 直接验证当前 trace，不比较旧协议；
- 按 design §6.1 重命名 v4/phase tests；拆分 `test_p4_mandatory_runtime.py`，删除其 migration/old-path cases；
- 删除 `PLAN03_REQUIREMENTS` 常量和 Plan03 checker/tombstone tests；
- 将仍有效的 fence、publication、recovery、accounting 不变量放入以行为命名的 current tests；
- 保留 `replace|rebase_post_publish_delta|predict_post_publish_global` 三个 current full-model adoption strategy 的行为/accounting tests；删除其重复 repository YAML，测试用 typed current config builder 生成变体；删除或重命名仍称 `fragment adoption` 的 current 分析字段和断言；
- property/state-machine/crash tests 只生成 current schema/input，不包含旧 decoder。
- fail-closed negative tests 从 current typed object 临时生成 unknown key、target±1 version、wrong digest/path/mode；不得保留旧 YAML/DDL/descriptor/run fixture 或专用旧错误消息。

#### P4-W2：historical harness/PBS disposition

- 删除 `build_plan03_p5_test_accounting.py`、`check_plan03.py`、`run_plan01_regression.pbs` 和只生成 Plan03 artifact/matrix/classic comparison 的 phase4/5/6 harness/PBS；
- 删除只转发到 `fs_diloco.baselines.health` 的 `scripts/miyabi/check_torch_baseline_health.py` compatibility wrapper；retained baseline PBS 直接调用 current module，不新增替代 wrapper；
- 删除零 current caller 的一次性 `benchmark_syncer_device.py`、`measure_pointer_polling.py`；`sqlite_shared_fs_probe.py` 作为本计划 2-node current capability preflight 保留，并由 `test_sqlite_probe.py` 继续拥有；
- harness 采用下列冻结 disposition；`replace-current` 表示只迁移列出的 durable oracle，不复制 Plan03 matrix/metadata/aggregate 逻辑：

| 现有 harness 类别 | disposition | current 落点/保留 oracle |
|---|---|---|
| `plan03_fs_capability.py` | `rename-current` | `fs_capability.py`；共享 FS/SQLite capability 与 source/PBS identity |
| `plan03_p6_crash_matrix.py` | `rename-current` | `protocol_crash_matrix.py`；publication transaction 边界 durable oracle |
| `plan03_p6_state_machine_gate.py` | `rename-current` | `protocol_state_machine_gate.py`；current generated state-machine result |
| `plan03_p6_tiny_scenarios.py` | `replace-current` | `protocol_tiny_scenarios.py`；只保留 current static/dynamic scenarios |
| `plan03_p6_two_node_sqlite.py` | `replace-current` | `protocol_two_node.py`；current shared-FS/recovery oracle |
| `plan03_p6_validate_run.py` | `replace-current` | `protocol_validate_run.py`；current descriptor/DB/terminal/accounting |
| `plan03_p6_dynamic_supervisor.py` | `replace-current` | `protocol_dynamic_supervisor.py`；exact PBS loss/FINISH/replacement oracle |
| `plan03_p6_test_gate.py`、`plan03_p6_acceptance.py` | `replace-current` | 最小 `protocol_test_gate.py` + generic `check_plan_evidence.py`；无 Plan ID hardcode |
| `plan03_p6_boundedness.py` | `delete-obsolete` | 把仍有效的 hot-set invariant 放入 current unit/state-machine tests；不保留 formal soak harness |
| `plan03_p6_performance.py`、`plan03_p6_quality_manifest.py` | `delete-obsolete` | 本计划无 performance/quality claim |

上述历史测试 harness 对应的 PBS 只保留并改名为 `run_protocol_tests.pbs`、`run_protocol_generated_and_crash.pbs`、`run_protocol_tiny_scenarios.pbs`、`run_protocol_two_node.pbs`、`run_protocol_static_9node.pbs`、`run_protocol_dynamic_9node.pbs`；phase4/5、boundedness、performance PBS 全部删除。这里的“只保留”仅限定 Plan01/03 测试 harness，不授权删除 current production actor、baseline、eval 或 multi-agent-review runner；这些脚本仍须在 P0 ledger 中逐个以 current caller/owner 判定，重复的 config-specific wrapper 则合并到最小 generic launcher 或删除。
- `run_2node_resume_regression.pbs` 若只测试 current crash recovery，改为 `run_2node_recovery_regression.pbs`；不得表达旧 run resume；
- pytest markers `plan03_red`、`p6_formal` 与 hypothesis profile/env 改为行为级名称或删除；
- 所有 retained PBS 在提交前必须满足 root `AGENTS.md` 的 syntax、literal group、walltime 条件。

#### P4-W3：配置 prune

- 按 design §6.3 收敛到精确 7 Full + 3 baseline YAML；
- fault-specific config 改由 test/harness 从当前 typed config 生成临时 resolved YAML；
- 由独立 Git/caller inventory producer 生成 `schema:current_config_manifest@1` evidence artifact，写入 `reports/DOING/<plan-id>/artifacts/<timestamp>_p4-config-manifest_pass.json`，逐项记录 `path`、`loader`、`owner_callers`、`purpose`、canonical digest 与 `source_identity_policy`；不在 active product tree 新增供 runtime 读取的第二份 config manifest；
- 任何零 owner、多 loader、旧字段、alias dtype、机器绝对路径、未 pin external input 的 retained non-synthetic YAML 均 FAIL；synthetic YAML 必须显式声明 synthetic identity；manifest 增减必须先修订 design/matrix 并重走 test-design review；
- 更新所有 PBS、docs、tests、CLI caller；禁止保留旧文件名 redirect。

P4 PASS：`TEST-01`、`HARNESS-01`、`CONFIGSET-01` evidence PASS；完整 pytest collect 无旧 marker/skip/xfail；current harness tests 与 phase review PASS。

---

### P5 — `P5-repository-closure`

目标：完成 package/import/docs/static closure，并在昂贵实验前审查最终 current state。

#### P5-W1：package、依赖与 active surface closure

- `pyproject.toml` console scripts/package data/pytest markers 只列 current entry；
- 删除因 legacy/fragment/migration 唯一需要的 dependency/import/helper；
- build sdist/wheel，在 clean 临时环境安装后验证 console imports、两个 schema 均存在且无旧文件；
- `test_current_protocol_surface.py` 对 active scopes 做 path、AST import、entrypoint、package-data、config manifest 与限定 token 扫描；
- `rg` 人工复核用于补充，不作为唯一 oracle。

机器 PASS 公式至少包括：

```text
obsolete_active_paths == []
obsolete_production_import_edges == []
full_config_loaders == [fs_diloco.core.config.load_config]
baseline_config_loaders == [fs_diloco.baselines.config.load_baseline_config]
authority_schema_files == [schema.sql, schema_dynamic.sql]
protocol_runtime_writers == 1 fenced composition path
stale_console_scripts == []
retained_configs == current_config_manifest
```

#### P5-W2：文档同步

- 删除 `docs/08-compatibility-and-migration.md` 并更新 docs index；
- 删除 `plans/00-RESEARCH_PLAN.md`、`plans/01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md`、`plans/followups/B4-fragment-terminal-drain.md`、`plans/ref/queue-native.md`；不另建 legacy archive，Git history 已提供恢复路径；
- README、`docs/00..07`、module docs、comments/examples 只使用 `Full Protocol`、`static|dynamic` 和 suffix-free paths/API；
- 删除 archive/migration 操作指南与旧 config/run 示例；
- baseline 文档只描述独立 current baseline，不称为 protocol profile；
- 暂不写未经正式实验验证的 9-node 成功结论，预留 P6 evidence-bound 更新。

#### P5-W3：candidate ladder 与 preformal review

在一个冻结 phase target 上依次通过：

1. `git diff --check`、compile/import、ruff、plan/matrix/checker tests；
2. `bash -n scripts/miyabi/*.pbs`；
3. focused config/schema/runtime/tools/architecture tests；
4. full pytest；
5. current generated state machine + publication crash matrix；
6. tiny current static + dynamic，以及 retained 2-rank synthetic baseline；
7. 2-node shared-FS/SQLite current recovery。

第 1–2 项纯静态部分在 control plane；首次非静态 gate 前按 §3.4 由主实例进入单节点交互式 allocation，并在第 6 项完成后退出。第 7 项另用预注册 2-node batch gate，不能继续占用或复用单节点 allocation。

随后按 workflow 执行 `PREFORMAL_PLAN_CURRENT_STATE_REVIEW`。必须审查完整 current state、deletion ledger、test design、package contents、formal source scopes、PBS invocation 和 structured artifact schema。先完成 Codex/GPT mandatory report，再读取外部 reviewer 结论。Critical/High finding 全部修复或以可复现反证拒绝；修复后重跑受影响 candidate ladder。

P5 PASS：`SURFACE-01`、`PACKAGE-01`、`ACCEPT-01` candidate evidence PASS；preformal blocking finding=0；允许冻结 final common target。

---

### P6 — `P6-formal-current-acceptance`

目标：在唯一 clean source target 上完成正式 current-only acceptance、文档实证与 requirement closure。

#### P6-W1：final common target

- commit 所有 formal source scope；工作树在这些 scope 内必须 clean；
- 记录 full commit、scopes、逐文件 hash/fingerprint、environment lock、resolved configs、PBS scripts 与 artifact schemas；
- final formal scopes 至少包含：

```text
fs_diloco/**
tests/**
configs/**
scripts/**
pyproject.toml
.python-version
uv.lock  # present 时即使 ignored 也必须按内容 digest 捕获
README.md
docs/**
plans/DOING/design/<plan-id>-design.md
plans/DOING/plans/<plan-id>.md
plans/DOING/plans/<plan-id>-requirement-matrix.csv
```

- formal source commit 后，上述 scope 任一 byte 改动都使依赖旧 fingerprint 的 formal evidence 失效；唯一例外是 workflow 已定义的 report/docs-only closure：必须由 source-equivalence Checker 证明所有 executable scopes 与实验 target 逐 byte 相同，且 aggregate 同时记录 runtime target 与 closure target。
- source capture 必须拒绝上述 scopes 内任何 untracked、non-ignored executable/config/input；不能因 `main.py` 已删除而继续在 fingerprint 中引用不存在的 path。

#### P6-W2：正式测试阶梯

只在 preformal review 关闭后执行，所有层绑定 P6-W1 target：

1. 重跑 P5 全部 cheap/full/current tiny/baseline tiny/2-node gate；其中单节点非静态部分由主实例为本 final target 按 §3.4 新建并持有交互式 allocation，保存证据并退出后再提交 2-node/9-node gate；
2. static 9-node：1 syncer + 8 learners，8 个 stable contributor 全部 admitted，`local_steps_per_outer >= 60` 且 committed outer steps `>= 12`；
3. dynamic 9-node：1 个 candidate allocation + 最多 8 个独立 learner allocations，同样 `>=60 × >=12`；8 个 stable stream 均被服务，对一个 exact learner PBS job 执行预注册的受控终止，等待 scheduler live+historical 证据确认该 allocation FINISH，再由 current launch outbox/replacement path 补回并产生后续 durable commit；因此 dynamic 至少有 9 个不同 instance admission，但 terminal stable-stream cardinality 仍为 8；不得把 instance count 与 stable contributor count 混为 `admitted=8`，不得直接写 SQLite、伪造 FINISH 或只杀 shared allocation 内 PID；
4. 两个 run 均完成 terminal/drain、SQLite integrity、source/config/descriptor identity、token/selection/publication accounting 和 stale-fence-commit=0 检查；
5. run tree 不得生成旧 schema/path/fragment artifact。

60×12 严格超过 50-local-step ×10-global-step 基线。若当前 replacement oracle 必须更长 workload，P0/P5 test-design review 可在正式提交前提高步数，但不得观察结果后降低阈值。无需重跑 classic performance；只记录当前 wallclock、峰值资源和 workload completion，用于发现明显失控。

#### P6-W3：PBS 与资源规则

正式资源预算在看到结果前按下表冻结；P0/P5 可以依据实测缩短 walltime，但不能降低 topology/oracle：

| Gate | queue/topology | compute/dtype | allocation 上限 | 初始 walltime 预算 |
|---|---|---|---|---|
| reviewer | `regular-g`，1 node | CPU-only reviewer processes；不加载项目 Torch | 1 parent，4 reviewer process | 10–30 min，按 prior review latency 收紧 |
| focused/full/tiny | `interact-g`，1-node persistent interactive allocation | workflow v3 Codex/GPT 主实例持有；tiny synthetic；只在测试明确要求时使用 GPU；canonical dtype 写入 artifact | 1 | exact `01:00:00`；阶段内复用，完成后立即退出 |
| 2-node recovery | `regular-g`，2 nodes | synthetic，SQLite/shared-FS；device/dtype 与 frozen config 一致 | 1 | 至少 10 min，覆盖 crash/reopen/terminal |
| static formal | `regular-g`，`select=9:mpiprocs=1` | 8 learner + 1 syncer；current acceptance config 的 device/dtype | 1 allocation/9 nodes | 以既有 20-min evidence 与 60×12 实测重估 |
| dynamic formal | `regular-g`，1 candidate + 最多 8 live learner allocations | synthetic；learner/syncer device/dtype 按 frozen config | 最多 9 simultaneous allocations；replacement 前先确认 lost job terminal | 以既有 30-min supervisor/20-min child evidence 与新 workload 重估 |

- 在任何 qsub 前运行 `bash -n scripts/miyabi/*.pbs`；
- 所有 `#PBS -W group_list=` 必须是已核验 literal group ID；
- 根据 P0 cheap smoke、Plan03 归档 evidence 和本 target 的启动/teardown 变化估算最短可行 walltime，至少 10 分钟并保留合理 margin；默认 walltime 过长时用 `qsub -l walltime=...` 明确收紧；
- login node 只做 control-plane；Miyabi runtime/pytest/GPU/distributed 工作在 compute allocation；
- 每个 job 保存 qsub receipt、canonical PBS ID、requested/actual resources、source fingerprint、resolved config digest、stdout/stderr 和 structured result；
- cleanup 只处理本计划创建且由 artifact policy 明确拥有的 run；不自动 qdel 不明 job，不触碰历史 run。
- 首次 qsub 前，所有 retained runner/harness 的纯单元测试必须覆盖 CLI 互斥参数、Git ref peeling/package origin、full/normalized PBS ID、artifact schema/projection、terminal reason、timeout/finally cleanup、failure classification、output path 和 source identity。

#### P6-W4：文档实证与 final evidence review

- 因两个 9-node workload 超过 50×10，按根 `AGENTS.md` 把 exact source commit、topology、workload、结果和 evidence path 写入相关 active docs；不得夸大为未测试的性能/质量结论；
- 文档改动属于 formal scope，因此先在 candidate target 写稳定结构和 evidence index，再在实验后建立 docs/report-only closure target；只有 source-equivalence Checker 证明 `fs_diloco/**`、`tests/**`、`configs/**`、`scripts/**`、`pyproject.toml`、`.python-version` 和 captured `uv.lock` 与实验 target 逐 byte 相同，才可承接 9-node evidence 而不重跑 compute。若任何 executable byte 改变，冻结新 common target 并重跑全部失效 formal gate；
- 更新 matrix `status/evidence_schema_or_path`，运行 generic evidence checker；
- 执行 `FINAL_EVIDENCE_REVIEW`，验证每条 requirement 的独立证据、source identity、raw logs、run/PBS identity、cleanup 与 docs；
- reviewer 发现 source defect 时修复、冻结新 target，并重跑所有失效 formal gate；只修 report/matrix mapping 且不改变 formal scope 时，按 workflow 做必要的 critical-boundary incremental re-review。

P6 PASS：`ACCEPT-02`、`DOCS-01`、`EVIDENCE-01` PASS；matrix 100% complete；final evidence blocking finding=0；workflow 允许 `PLAN_FINAL`。

## 6. 测试设计与反例矩阵

| 领域 | 正例 | 必须失败的反例 | durable oracle |
|---|---|---|---|
| Full config | 每个 retained Full YAML 经唯一 loader canonical round-trip | baseline-only key、unknown/old key、dtype alias、unquoted bool、movable external revision | resolved bytes/hash + loader identity |
| baseline config | 每个 retained baseline YAML 经 baseline loader，真实 model/data I/O 使用所 pin identity | Full coordination/membership section、Full loader 读取 baseline、movable Hub revision、loader 忽略 pinned revision | typed config + loader call graph + training manifest/attestation |
| descriptor | `static|dynamic` 与 config/schema 一致 | 其他 mode、wrong digest/schema/source/env alias | immutable descriptor/source/config digest |
| schema | fresh static/dynamic init 与 reopen | duplicate/missing/tampered DDL identity、旧路径探测 | SQLite meta/integrity + packaged DDL hash |
| runtime entry | admitted actor 进入 suffix-free runtime | descriptor/admission 前 Torch import、旧 module import | subprocess import trace + actor attestation |
| tools | current completed run inspect/eval/cleanup | missing descriptor/policy、fragment/legacy-shaped input | read-only query/result artifact；run DB unchanged |
| publication/recovery | current intent/replay/fence tests | stale leader/contributor commit | SQLite audit/journal + canonical control digest |
| packaging | clean wheel imports CLI/schema | missing schema、old console script、legacy module included | wheel file manifest + subprocess smoke |
| repository surface | exact current allowlist | obsolete active path/import/entry/config | structured surface inventory independent of Checker output |
| 9-node static | 8 contributors reach current terminal | incomplete workload、identity mismatch、old path output | terminal DB + accounting + source-bound result |
| 9-node dynamic | loss/replacement 后继续 durable commit 并终结 | stale incarnation commit、no replacement progress | membership/audit/commit rows + scheduler identity |

不得通过以下方式制造假 PASS：只断言 import 失败、只搜索文件名、只看 exit code/log、把 Checker 输出作为自身证据、用旧 archive artifact 绑定新 target、对 formal config 临时 override 未记录字段。

### 6.1 Fault layer 与 replay

- dynamic permanent-loss fault 注入在一个由本 harness 创建并记录 receipt 的独立 learner PBS allocation；终止请求绑定 canonical job ID，replacement 前同时查询 scheduler live 与 historical FINISH，并与 DB 中 exact instance/job identity 对账；
- stale writer/proposal 的 unit/state-machine fault 分别放在 SQLite transaction 前、事务持锁期间和 commit 后。事务内暂停不能靠 lease 绕过 SQLite lock；测试必须显式结束旧 writer 后再判断 successor；
- filesystem visibility fault 放在 immutable create-no-replace、pointer publication 和 manifest marker 边界，不通过直接篡改 authority row 模拟；
- 每个 replay 使用 current request/command/fence/digest 幂等 key；successor 的 PASS 来自 journal/audit/committed state，不来自 fixed sleep 或进程退出；
- fault harness 自己创建的 PBS job/run root 是唯一 cleanup 集合，异常路径在 `finally` 中保存 receipt、scheduler state 和 durable projection 后才尝试清理。

### 6.2 Performance、boundedness 与质量 disposition

- Performance：本计划没有旧协议 baseline，也不做 non-inferiority claim；`NOT_RUN` 对性能研究可接受。9-node 只记录 actual processed workload、wallclock、峰值资源和 timer anchor 作为诊断，不能据此声称提速；
- Boundedness：schema 业务语义不改，只删除 generation 字段并重命名路径。把 Plan03 harness 中仍有效的 hot-set invariant 提取到 current unit/state-machine tests 后删除原 harness；不安排长 soak。hot recovery set 与 append-only audit 分开判定，不用总文件数不增长作错误门槛；
- Quality：训练算法不变，多 seed/held-out quality 为非目标，`NOT_RUN` 不阻断且不得写成通过；
- 任一正式 run 若出现 processed workload 不等价、hot-set 无界增长或 non-finite loss，属于 current acceptance failure，而不是另开性能/质量研究来绕过。

## 7. Artifact 合同

所有结构化 artifact 遵循 workflow 通用字段，并补充下列 domain 字段。

### 7.1 `current_surface_inventory`

```text
artifact_version
status
source_identity
scopes / exclusions
tracked_counts
production_entrypoints
config_loaders
runtime_writers
schema_files
obsolete_paths / obsolete_import_edges / forbidden_product_tokens
config_callers
harness_dispositions
artifact_version_owners
errors
```

PASS：所有 obsolete/unknown 集合为空；current allowlist 与 Git/package/import graph 完全一致。

### 7.2 `current_protocol_test_gate`

```text
artifact_version
status
requirements_covered
source_identity
commands
collection
passed / failed / skipped / xfailed
config_manifest_sha256
wheel_manifest_sha256
errors / evidence_paths
```

PASS：required commands exit 0；unexpected skip/xfail=0；所有证据路径存在且 source identity 一致。

### 7.3 `current_protocol_9node`

```text
artifact_version
status
topology
source_identity
config_schema_identity / authority_schema_identity
pbs_job_identity / nodes / actors
workload_identity
admitted_stable_contributors / admitted_instances
committed_outer_steps / local_steps_per_outer
replacement_events / post_replacement_commits
stale_fence_commits
terminal_state / sqlite_integrity
token_selection_publication_checks
old_layout_artifacts
wallclock / peak_resources
run_root / raw_logs / errors
```

Static PASS：`admitted_stable_contributors=8`、`admitted_instances=8`、`local_steps_per_outer>=60`、`committed_outer_steps>=12`、terminal、integrity/accounting PASS、stale=0、old-layout=[]。

Dynamic PASS：`admitted_stable_contributors=8`、`admitted_instances>=9`，满足其余共同条件，且 `replacement_events>=1`、`post_replacement_commits>=1`、旧 incarnation 在 replacement boundary 后成功 commit=0。

## 8. Checker 与 evidence ownership

- `scripts/check_plan_evidence.py` 只验证通用 matrix/evidence 合同；它不能产生被自己验证的 product evidence；
- `test_current_protocol_surface.py` 是当前架构 contract 的 consumer，inventory producer 必须独立读取 Git/package/AST/caller graph；
- 9-node producer 从 run DB、immutable controls、scheduler receipt 和 raw logs 生成 artifact；aggregate checker 不得从摘要反推缺失字段；
- 每条 matrix complete 行至少有一个 tracked evidence path，artifact source commit/fingerprint 必须等于 final common target；
- matrix 中 `planned` 行的 `<timestamp>` 路径只是 schema/path 合同，不是预先存在的 PASS。P6 必须把每行替换为 final common target 上实际生成的 immutable artifact；早期 phase artifact 保留为历史，但不能单独关闭 final requirement。若某项只继承早期 pure/static evidence，必须用 source-equivalence artifact证明对应 executable/input scope 与 final target byte-exact；
- code review verdict 不能代替测试/实验 evidence，测试 exit code 也不能代替 durable oracle。

## 9. 失败分类、重试与 cleanup

沿用 workflow 的 `product|harness|source-freeze|infra` 分类和失败计数，不另建 Plan04 状态机。

- product/harness 有效失败：记录 exact command/config/source/run，做最小针对性修复，重新冻结 review target；同一 blocking condition 达 workflow 阈值时进入 failure review/logic rewrite；
- invocation 错误：不计为 product RED/PASS，修正调用并保留失败记录；
- source-freeze mismatch：立即作废相关 evidence，重新冻结；
- scheduler/filesystem/network/account quota：标记 infra/unavailable；外部 reviewer unavailable 不阻断，正式 product experiment 的 infra failure 不得伪装 PASS；
- 正式 job 是否重试由 evidence-bound failure classification 决定，不观察指标后修改 PASS 阈值；
- 清理只针对本计划新建 run，先 dry-run manifest，再按 current policy 显式 apply；所有 material deletion 记录 target 与可恢复性。

## 10. 风险、回滚与 follow-up

| 风险 | 最早检测 | 计划内处理 |
|---|---|---|
| 全局 rename 漏掉动态 import、resource path 或 PBS caller | P0 caller graph；P2/P3 import/package tests | 修正 caller；禁止 alias/fallback |
| config 合并改变 Full 或 baseline 的当前语义 | characterization + Full↔baseline cross-rejection + baseline regression | 回到 P1 typed boundary，不让一方通过另一方 profile |
| 删除 `protocol_version` 后身份检查变弱 | descriptor/schema/digest counterexamples | 用 exact schema/descriptor/artifact identity 补足，不恢复 generation field |
| 历史测试夹带 current invariant | P0 test accounting ledger + test-design review | 把 invariant 重写进行为命名测试，再删除原测试 |
| config prune 破坏隐蔽 launcher | Git/AST/shell caller graph + every-config owner manifest | 更新到 retained config 或删除 dead caller，不恢复 duplicate YAML |
| dynamic formal fault 只证明 PID 退出 | harness unit + scheduler receipt/history review | 在独立 PBS allocation 注入并以 DB+scheduler durable oracle判定 |
| docs closure 使 runtime evidence source 不一致 | executable-scope source-equivalence Checker | executable 有变化则重跑；仅 docs/report 变化才承接 evidence |

Rollback 只用于尚未闭合的实施 branch，不是产品兼容功能：

- work unit 以小型冻结 commit 实施；若其设计无法通过 review，用非破坏性的 Git revert 回到上一 phase-final，再重做该 current-only 设计；
- 不把旧 shim/reader/migration 恢复到最终树，也不尝试把失败实验的 run root 迁回旧格式；
- 本计划开始前的 run 不被修改。计划创建的失败 run 在 evidence 投影后按 current cleanup policy 处置；
- 已冻结 archive 只用于审计和根因调查，不能复制回 active source 或成为 formal runtime baseline。

允许的 follow-up 只有不阻断当前定义的独立问题，例如实施中发现但不由 rename/delete 引起的算法研究或性能优化；必须记录触发证据、owner 和为什么不影响唯一 current protocol correctness。任何旧协议恢复、兼容、migration 或 Fragment 重建都不是 follow-up，而是与本计划目标冲突。

## 11. Phase/plan 审查门禁

每个 phase 依次满足：

1. implementation + draft tests；
2. test-design frozen review target；
3. Codex/GPT mandatory review 先保存；外部 reviewers best effort；
4. staged tests 与 structured evidence；
5. phase code/evidence review；
6. finding disposition、remediation、受影响测试重跑；
7. phase-final commit，matrix 对应行状态和 evidence 完整。

不得以 reviewer 投票替代 finding 处置。任一有可复现依据的 Critical/High finding 未闭合时不能进入下一 phase。Claude Code session quota 不可用只影响该 reviewer availability，不降低其他门禁。

## 12. 最终完成判据

只有全部条件成立才能宣布 plan 完成并移动到 `plans/DONE/`：

- 16 条 requirement 全为 `complete`，无 open blocking finding；
- active product tree 无 legacy/classic/fragment/migration/shim/fallback 和 Full Protocol generation-v4 表面；
- Full、baseline 各自只有一个 strict loader，交叉输入 fail closed；
- descriptor/schema/runtime/path/CLI/config/docs 全部使用 current canonical 名称；
- old run 没有 reader、migration 或 query-only 入口；
- full pytest、package smoke、tiny、2-node（适用时）、static 9-node、dynamic 9-node 在同一 runtime fingerprint PASS；实验后的 docs/report closure 仅可用 byte-exact executable-scope equivalence 承接该证据；
- 9-node evidence 满足 workload、identity、terminal、accounting、replacement/stale-fence 公式；
- active docs 已与 verified behavior/result 同步；
- final evidence review APPROVE，matrix/evidence Checker PASS，所有临时资源有 cleanup disposition；
- plan/report 移动只改变位置和索引，不改写冻结 artifact。
