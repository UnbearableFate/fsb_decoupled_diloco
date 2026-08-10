# Plan 04 设计：Full Protocol 当前态收敛

计划 ID：`fsb_decoupled_diloco_plan_04_current_protocol_cleanup`

状态：设计冻结候选；待实施启动提交固定 branch point 与 workflow commit

配套文件：

- `plans/DOING/plans/fsb_decoupled_diloco_plan_04_current_protocol_cleanup.md`
- `plans/DOING/plans/fsb_decoupled_diloco_plan_04_current_protocol_cleanup-requirement-matrix.csv`

## 1. 设计目标

把仓库收敛成只有一种可运行、可配置、可分析、可测试的 filesystem Decoupled DiLoCo 协议：`Full Protocol`。它就是当前已经实现的 Full Protocol v4 语义，但产品名、模块名、类型名、配置入口、schema 文件名和运行目录不再携带 `v4` 世代后缀。

本设计不提供任何历史兼容能力。v1/v2/v3、classic、Fragment V0、旧 HA/config 包装层、旧 run 查询器、配置迁移器、fallback、shim 和历史实验 harness 全部从 active tree 删除。旧配置、旧数据库、旧 checkpoint、旧 metadata 和旧 run root 不可读取、不可查询、不可迁移、不可 resume；操作者必须从当前配置和空 run root 启动。

这是对 `plans/plans_create_guide.md` §5.3 默认删除模板的有意收紧：根目录 `AGENTS.md` 和本任务明确禁止 legacy query-only，因此本计划的 disposition 只有 `retain-current`、`rename-current`、`delete-obsolete`，不允许 `retain-query-only`。

最终仓库应呈现为从一开始就按当前协议实现，而不是在旧协议上叠加 v4 包装。

## 2. 构建基线与事实边界

### 2.1 已核实事实

本设计基于 2026-08-10 的只读 inventory：

| 项目 | 已核实事实 |
|---|---|
| construction baseline | `c8b7aa9b33138fa7751317b0bfe50a94c9b672aa`，分支 `plan03` |
| 工作树 | `AGENTS.md`、`plans/AGENTS.md` 已修改；新 workflow、guide、复盘与 review PBS 尚未提交；它们属于用户现有工作，不由本计划构建步骤改写 |
| 构建规则身份 | `AGENTS.md` SHA-256 `aae547be4eb51ff86c7c685d342d36b3d02c025e6d7c7de0684e2ddbf0450675`；`plans/AGENTS.md` SHA-256 `5807c13f5bef091adce64f1c25096f57b5abe0f50594a44584e58bd4b8c4e56c` |
| 指南身份 | `plans/plans_create_guide.md` SHA-256 `bef25a5fc8403b5b607f5586719da848b8c367c76a55536a2234010552125508` |
| workflow 身份 | version `3`；当前内容 SHA-256 `c5017e9114666b0eda13fc6b6f11ac4f5a806662dbe6ba334423f7863c44c08f`，但尚无包含该文件的 commit |
| 配置 | 30 个 tracked YAML：26 个 `fs_diloco*.yaml`，4 个 torch baseline YAML；当前均声明 `config_schema_version: 1` |
| 代码/测试/脚本表面 | 89 个 tracked `fs_diloco/**` 文件、80 个 tracked `tests/**` 文件、51 个 tracked `scripts/**` 文件，其中 48 个在 `scripts/miyabi/**`、3 个在 `scripts/local/**` |
| 明确 legacy | `fs_diloco/legacy/**` 4 个源码文件、`tests/legacy/test_legacy_v1_v3_reader.py`、`tests/test_fragment_analysis.py` |
| 当前 v4 包装 | `core/config_v4.py` 包住 `core/config.py`；`ConfigProfile.FULL_V4` 与 `TORCH_BASELINE` 在同一 loader 中分支 |
| schema packaging 缺陷 | `pyproject.toml` 声明 `schema.sql`、`schema_v4.sql`、`schema_v4_dynamic.sql`，但当前只有后两者；package-data 已包含不存在的陈旧条目 |
| descriptor 模式 | `full_ha_static`、`full_ha_dynamic`，同时配置又用 `membership.mode=static|dynamic`，同一身份有两种拼写 |
| 其他 topology 投影 | `tools/init_run.py` 的 run identity/bootstrap 另写 `full`、`full_dynamic`；同一 current topology 实际有三套拼写 |
| 环境入口 | descriptor loader 接受 `FS_DILOCO_SHARED_ROOT` 或 `SHARED_ROOT`，形成重复外部接口 |
| 配置覆盖 | `core.config.resolve_config` 除 launch identity 外还接受 learner 数、seed、polling、dtype、staleness、adoption、completion 与 profile 等语义 override，形成第二配置接口 |
| config dead/duplicate fields | `run.log_level`、`data.num_proc`、`data.synthetic_num_batches`、`io.atomic_write`、`io.compute_sha256`、`io.checkpoint_digest_mode` 没有 production behavior consumer；所有 tracked config 的 `model.compile=false`、Hub config 的 `trust_remote_code=false`、`data.streaming=false`、`data.cache_dir=null`；`data.block_size` 与 `training.block_size` 重复表达 sequence length，且两个 obsolete tiny config 还会因后者缺省而投影出不同值 |
| 配置可移植性 | 30 个 repository YAML 均把 `run.shared_root` 固定为当前 Miyabi 用户的绝对工作目录 |
| baseline 外部输入 | 3 个 retained 候选中的两个 GPT-2/WikiText baseline YAML 没有 model/tokenizer/dataset revision，实际 loader 可落到 movable upstream default |
| 当前 writer | learner 发布 immutable proposal/receipt；fenced SQLite leader 是唯一业务 mutation authority；static/dynamic 共用 Full Protocol 语义 |

上述数字只描述 construction baseline。实施 P0 必须在 plan-init 冻结 commit 上重算，任何差异作为 drift 记录，不得静默复制数字。

### 2.2 已知 obsolete 表面

必须删除而不是改名保留：

- `fs_diloco/legacy/**` 与 `tests/legacy/**`；
- Fragment reader/decoder/query/assertion、fragment fixture 与只为 Fragment 存在的测试；
- `fs_diloco/tools/migrate_config_v3_to_v4.py` 及其 console script；
- `REMOVED_CONFIG_KEYS`、`_REMOVED_V4_PATHS`、v1-v3 migration 函数及针对旧字段给 replacement 建议的分支；
- `fs_diloco/analysis.py`、`fs_diloco/eval_lm_harness.py` 两个 compatibility shim；
- 只转发到 `fs_diloco.baselines.health` 的 `scripts/miyabi/check_torch_baseline_health.py` compatibility wrapper；baseline PBS 直接调用 current module；
- analysis、metrics、eval、validation、quality gate、cleanup 中的 legacy/classic/fragment fallback；
- 只为 Plan03 classic/unified 比较存在且没有 current runtime caller 的 `tools/compare_event_traces.py`、`tools/paired_performance.py`、`tools/check_workload_equivalence.py` 及其专用 tests/support；本计划不保留“以后也许有用”的离线工具；
- `--allow-legacy-run-without-policy` 和无 artifact policy 的清理路径；
- `logs/syncer.jsonl` 等历史 filesystem fallback；
- 无 authority/policy 证明便按扩展名或目录名递归删除 run 的 `scripts/local/clean_run.sh`、`scripts/local/prune_runs_without_5000.sh`；
- 无独立用途的 `main.py` + `fs_diloco/cli.py` convenience dispatcher；manual close 改为直接 current console entrypoint；
- 只转发到已存在 inspect console 的 `scripts/miyabi/inspect_run.sh` 二次 wrapper；
- classic/fragment golden fixture、Plan 01/03 专用 checker、marker、PBS 和只验证已删除历史面的测试；
- `docs/08-compatibility-and-migration.md` 及 active 文档中的 migration/legacy/classic/fragment/v4 世代说明。
- 仍位于 active plans 区且以 Fragment/旧 Full reference 为目标的 `plans/00-RESEARCH_PLAN.md`、`plans/01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md`、`plans/followups/B4-fragment-terminal-drain.md`、`plans/ref/queue-native.md`。

### 2.3 待 P0 证伪的假设

| 假设 | 最早证伪方式 | 失败后的动作 |
|---|---|---|
| 当前 Full Protocol 的 static/dynamic 行为可在去掉 generation 名称后保持不变 | 冻结现有 current-only focused/full 测试清单并在重构前运行 | 若测试实际依赖 legacy 行为，按 disposition ledger 判断删除或改写，不给生产代码补兼容分支 |
| Plan 03 harness 中只有一部分仍表达当前 fault oracle | 对每个脚本建立调用者、输入、oracle、Plan03 字段依赖表 | 只保留能以更小的通用 harness 表达的当前 oracle；其余删除 |
| 26 个 Full YAML 可映射到冻结的 7 个正式用途配置 | 生成 config→script/test/doc caller graph | 无 current owner 的配置删除；若 7 项 manifest 不能承载 current caller，先修订 design/matrix 并重审，不能临时增加模板 |
| 冻结的 3 个 torch baseline 配置覆盖全部当前 baseline 用途 | 对 4 个 baseline YAML 建 caller graph | 删除无独立用途的 1-rank debug；若假设失败，先修订 design/matrix 并重审，不因历史文件存在而保留 |

## 3. 最终架构

```text
current YAML
  └── fs_diloco.core.config.Config + load_config/resolve_config
        ├── immutable resolved config + source identity
        └── immutable run descriptor (mode = static | dynamic)
              └── fresh authority schema (schema.sql | schema_dynamic.sql)
                    └── fenced leader/session commands
                          ├── current learner proposal/receipt ingest
                          ├── selection/merge/publication
                          ├── membership/recovery/terminal
                          └── current-only inspect/eval/cleanup

torch baseline YAML
  └── fs_diloco.baselines.config.BaselineConfig
        └── fs_diloco.baselines.train
```

没有从旧配置、旧 descriptor、旧 schema、旧路径或旧 run reader 指向上述图的边。

### 3.1 单一配置边界

`fs_diloco/core/config.py` 成为 Full Protocol 唯一配置 authority：

- `Config` 直接对应 YAML，不再有 `ConfigV4.shared` 外壳；
- `Config.coordination: CoordinationSection` 直接拥有 `leader`，`Config.maintenance` 直接拥有 maintenance，`SyncSection.stop_after_direct_weight_tokens_applied` 直接拥有 current direct-token stop；不再由 envelope 抽取/拼回字段；
- 删除没有 current behavior owner 的 `run.log_level`、`model.compile`、`model.trust_remote_code`、`data.num_proc`、`data.cache_dir`、`data.streaming`、`data.synthetic_num_batches`、`io.atomic_write`、`io.compute_sha256`、`io.checkpoint_digest_mode` 及相应分支；Hub loader 固定 `trust_remote_code=False`，cache/offline 只由运行环境管理，indexed/materialized data 与 atomic publication 是不可关闭的 current contract；
- 删除 `IOSection`：唯一有效的 `io.tensor_dtype` 改为职责明确的 `learner.proposal_dtype`；sequence length 只保留 `data.block_size`，terminal/accounting/W&B/training 全部消费该值，删除 `training.block_size`；
- 公共 API 只保留 `Config`、`load_config`、`resolve_config`、`config_to_dict`、`resolved_config_bytes`、`write_resolved_config`；
- unknown key 一律由通用 strict decoder 拒绝，不识别历史名字，也不提示旧→新替换；
- Full loader 无 `profile` 参数、无 baseline 分支、无旧字段 normalization；
- dtype 只接受当前 canonical spelling `float32`、`bfloat16`；YAML 中字符串 `"off"` 必须显式引用，不为 PyYAML 历史布尔解析做兼容；
- 外部 shared-root 环境变量只有 `FS_DILOCO_SHARED_ROOT`；删除 `SHARED_ROOT` fallback。

Torch baseline 不是协议 mode。它移到 `fs_diloco/baselines/config.py` 的独立 `BaselineConfig`/`load_baseline_config` 边界，使用字段 `baseline_config_schema_version` 和 exact `BASELINE_CONFIG_SCHEMA_VERSION=1`，不复用 Full 的 `config_schema_version`。最终 section 只有 `run`、`model`、`data`、`training`、`inner_optimizer`、`distributed`、`wandb`；`distributed` 直接保存 `world_size`、`backend`、`require_distinct_hosts` 和 periodic-average interval，不再借用 `sync.num_learners` 或 `torch_baseline.enabled`。Full-only `syncer/membership/scaling/terminal/liveness/outer_optimizer/learner/coordination/maintenance` 不出现在 baseline YAML。

Baseline production CLI 只保留 `--config`、独立比较算法 selector `--mode=ddp|periodic_average` 和显式 launch identity `--run-id/--shared-root`。Baseline operator/PBS launcher 另须显式接收 source project root/identity；训练进程只验证并消费该身份。删除 `--max-steps`、`--average-interval`、`--backend` 等与 YAML 重复的语义 override；测试变体必须先生成完整 strict baseline YAML，不能形成第二套 baseline 配置接口。

只有语义完全相同且确实减少复杂度的 leaf dataclass/helper 才可复用；shared modeling/data/optimizer helper 应接收其实际消费的 section 或窄 typed view，不能继续以整个 Full `Config` 作为 baseline 接口。不得重新引入 `profile=`、`torch_baseline.enabled` mode switch 或让 baseline YAML 通过 Full loader。

配置入口本身也只保留一种表达。Full `resolve_config` 除显式 `run_id`、`shared_root` 和 source capture 所需启动身份外，不再接受 `num_learners`、seed、scan interval、dtype、staleness、adoption strategy、completion mode 等语义 override；测试和 harness 必须先生成一份完整、strict、可 hash 的 current resolved YAML。Repository YAML 的 `run.run_id` 与 `run.shared_root` 均为 `null`，production init/launch CLI 要求显式 `--run-id`、`--shared-root` 与 `--project-root`，不能把某个用户/机器的绝对路径写进模板，也不能从 `RUN_ID`、actor cwd、`PBS_O_WORKDIR`、`project_root/runs` 或其他环境 fallback 猜测运行身份、运行路径或 source root。

CLI/PBS/env 只能携带 launch identity、source identity、scheduler resource 和 cache/offline 等不改变输入内容的运行参数；不能再覆盖 YAML 中的 learner 数、训练参数、算法策略、dtype、W&B mode 或外部数据源。`FS_DILOCO_HF_WIKITEXT_REPO` 这类会改变实际输入仓库的环境 override 删除，`NUM_LEARNERS` 等重复值由 resolved descriptor 派生而不是成为第二个配置入口。确需跨进程传递的 expected identity 只能用于 exact-match assertion，不能改写 resolved config。

Model 与 data source 各只有一个显式 discriminant：`source_kind: hub|synthetic`。`hub` 必须提供 repo identity 及 model、tokenizer、dataset 各自实际消费的 40-hex commit；`synthetic` 必须提供所需生成参数且不得携带 Hub repo/revision。删除 `synthetic-tiny|tiny-synthetic|tiny-local` 等 magic-name alias、local-path 猜测和 content-changing environment override；当前 manifest 没有 owner 的第三种 source kind 不保留。WikiText 配置直接写唯一 canonical Hub repo identity；`hf_data` 中 `wikitext → Salesforce/wikitext` 的 try/except 与环境 fallback 删除，配置的 exact repo 失败就直接失败。

所有 retained config 的 config validator、canonical resolved bytes、descriptor/attestation 和实际 `from_pretrained`/dataset loader 必须消费同一 source identity；baseline 不能因为独立 schema 而退回 movable default revision。Synthetic identity 在 resolved identity projection 中明确记录 `source_kind=synthetic` 与参数 digest，不伪造 Hub revision。

`replace`、`rebase_post_publish_delta`、`predict_post_publish_global` 是当前 learner 的 full-model adoption strategy，不是旧协议 mode。它们在 current config 中仍是一个 strict enum，并继续由行为级 accounting/adoption tests 覆盖；本设计只删除为每个 strategy 复制的 repository YAML，测试通过 typed config builder 生成变体。任何仍以 `fragment` 命名或依赖 fragment payload/table 的 adoption 分析字段、函数和测试必须删除或按真实 full-model 语义重命名，不能据此删除这三个当前 strategy。

### 3.2 单一协议与 topology identity

产品名恒为 `Full Protocol`。不再持久化或分支判断 `protocol_version=4`，也不再定义 `PROTOCOL_VERSION`。当前 run 的可判定身份由以下正交字段组成：

- `mode: static|dynamic`：来自 config `membership.mode`；descriptor、authority schema、run identity、bootstrap marker、actor attestation、result artifact 和 harness 使用完全相同的值，不再出现 `full|full_dynamic|full_ha_*` 的第二套拼写；
- exact `config_schema_version` 与 resolved-config digest；
- exact `RUN_DESCRIPTOR_FORMAT_VERSION` 与 descriptor digest；
- exact `AUTHORITY_SCHEMA_VERSION`、DDL digest/features；
- source manifest/fingerprint 与 immutable external-input identity；
- 每种 artifact 自己的 exact `format_version`。

`static` 和 `dynamic` 是同一协议的两种当前部署 topology，不是兼容模式。两者保留现有 fenced authority、proposal、token、publication 和 terminal 不变量；dynamic 额外启用当前 membership/scheduler 表。

### 3.3 v4 名称去除表

这是替换，不是 alias。旧名在同一提交中消失。

| 当前名 | 最终唯一名称 |
|---|---|
| `fs_diloco/core/config_v4.py` | 删除；当前实现并入 `fs_diloco/core/config.py` |
| `ConfigV4` | `Config` |
| `ConfigProfile.FULL_V4` / `full_v4_shared` | 删除，无替代 selector |
| `load_config_v4` / `resolve_config_v4` | `load_config` / `resolve_config` |
| `config_v4_to_dict` / `resolved_config_v4_bytes` / `write_resolved_config_v4` | 对应 suffix-free API |
| `fs_diloco/runtime/learner_v4.py` | `fs_diloco/runtime/learner.py` |
| `fs_diloco/runtime/syncer_v4.py` | `fs_diloco/runtime/syncer.py` |
| `schema_v4.sql` / `schema_v4_dynamic.sql` | `schema.sql` / `schema_dynamic.sql` |
| SQLite application ID `0x46534434` (`FSD4`) | `0x4653444C` (`FSDL`，filesystem DiLoCo)；只接受新值，避免与 PyTorch FSDP 混淆 |
| `initialize_authority_v4` | `initialize_authority` |
| `V4ControlPublisher` | `ControlPublisher` |
| `V4_BOOTSTRAP_MARKER_NAME` | `AUTHORITY_BOOTSTRAP_MARKER_NAME` |
| `authority_v4_bootstrap_complete.json` / `control/bootstrap_complete.json` | 单一 `control/authority_bootstrap_complete.json` |
| `control/syncer_metadata.sqlite3` / `RunPaths.sqlite_db` | `control/authority.sqlite3` / `RunPaths.authority_db` |
| root 与 `control/` 各一份 `run_config.resolved.yaml` | 只保留 `control/run_config.resolved.yaml` |
| `dynamic_close_request_json` / `control/dynamic_close_request.json` | `manual_terminal_request_json` / `control/manual_terminal_request.json` |
| `registration_history_v4` | `registration_history` |
| `registration_dispositions_v4` | `registration_dispositions` |
| `admissions_v4` | `admissions` |
| `scripts/miyabi/run_v4_allocation.sh` | `scripts/miyabi/run_allocation.sh` |
| `tests/support/v4_protocol.py` | `tests/support/protocol.py` |
| `unified_v4_trace.json` | `protocol_trace.json` |
| `Full Protocol v4` / `Full-v4` / `full_v4` | `Full Protocol`；不保留 generation selector |
| descriptor `schema_version` | descriptor `authority_schema_version`；避免与 config/descriptor format 混装 |

`learner_entrypoint.py` 与 `syncer_entrypoint.py` 保留：它们是 Torch-free admission/lease 边界，保护 compute actor 在通过 descriptor/source/admission 校验前不加载 Torch，并非兼容 shim。顶层 console entry 模块 `fs_diloco/learner.py`、`fs_diloco/syncer.py` 若与 runtime 新文件同名造成含混，执行者应保持“public CLI module + runtime package module”的清楚导入边界，不得用动态 import/fallback 解决冲突。

`main.py`/`fs_diloco.cli` 的多命令 convenience dispatcher 和 `scripts/miyabi/inspect_run.sh` 二次 wrapper 删除。`syncer`、`learner`、`inspect`、`close` 都使用各自唯一的 `fs-diloco-*` console entry；为 manual close 增加直接的 `fs-diloco-close = fs_diloco.tools.request_terminal_close:main`。这不会删除 Torch-free public actor modules，只删除没有独立入口身份的第二层 dispatcher。

`fs-diloco-init-run` 是唯一的 fresh-root initializer，`fs-diloco-launch-run` 是唯一的 init+PBS submit operator command；suffix-free `run_allocation.sh` 只是已分配 static PBS job 内部 runner，不作为第二个对外 launcher 写入文档。若 P0 发现两个 operator command 对同一操作形成重叠入口，应把共享逻辑收敛到 initializer/service，而不是加入 dispatcher 或 fallback。

### 3.4 独立 artifact version 不并入本次 generation 清理

`PROPOSAL_FORMAT_VERSION=2`、`CYCLE_RECEIPT_FORMAT_VERSION=1`、`SOURCE_MANIFEST_FORMAT_VERSION=2` 等是当前持久化格式的 fail-closed identity，不是 Full Protocol v1/v2/v4 mode。只要 serialized schema 未改变且 producer/consumer 仍只接受一个 exact current value，它们继续保留并独立演进；本次 shape 确实改变的 descriptor/config/schema/control 则按下表推进。

因此本计划禁止两种机械处理：

1. 不把所有 artifact version 合并成一个新的全局 protocol version；
2. 不因名字含 `V1`/`V2` 就删除当前 wire type 或兼容多个格式。

若某个 artifact decoder 实际接受旧格式，旧分支必须删除；若类型后缀只是当前 schema 的强类型名称，可保留。任何例外必须在 P0 ledger 中说明 producer、consumer 和 exact current version。

本次确实改变 serialized schema 的边界必须推进自己的 exact current version，而不是继续使用旧数字或接受两种值：

| Identity owner | 当前值 → 目标值 | 原因 |
|---|---|---|
| `CONFIG_SCHEMA_VERSION` | `1 → 2` | Full schema 直接化、baseline 分离、canonical spelling 收紧 |
| `BASELINE_CONFIG_SCHEMA_VERSION` | 新建 exact `1` | baseline 成为独立 config domain，不继承 Full schema identity |
| `AUTHORITY_SCHEMA_VERSION` | `9 → 10` | DDL 删除 protocol-generation column 并建立 current schema identity |
| `RUN_DESCRIPTOR_FORMAT_VERSION` | `2 → 3` | mode canonicalization 且删除 `protocol_version` |
| `CONTROL_FORMAT_VERSION` | `2 → 3` | summary/control 删除 `full_protocol_v4` generation marker |

其他 artifact version 只有在其实际字段/schema 改变时才推进；仅文件路径重命名且路径由 descriptor/policy digest 绑定时不机械 bump。所有 parser 只接受目标 exact value，没有旧值分支。`authority: full_protocol_v4` 这种冗余 summary 字段直接删除；authority 已由 descriptor、schema 和 fenced SQLite 状态确定，不换成另一个产品 generation string。

### 3.5 current-only 工具边界

| 工具 | 最终输入 | 明确拒绝/删除 |
|---|---|---|
| inspect / metrics export | 当前 descriptor + 当前 authority DB + 当前 artifacts | protocol 猜测、fragment 表、旧 run-state fallback |
| LM/validation eval | 当前 descriptor-bound config/checkpoint/source identity | hard-coded historical run ID、旧 reader、缺失 descriptor fallback |
| quality gate | 当前 resolved config 与 current result artifact | legacy query config loader |
| cleanup | current artifact policy + terminal evidence + authority live refs | `--allow-legacy-run-without-policy`、policy 缺失路径 |
| config CLI | current Full loader；baseline CLI 使用独立 baseline loader | v3→v4 migration CLI |

旧输入的期望结果不是“友好迁移”，而是在当前入口最早的通用身份/schema/unknown-key 检查处失败。错误信息不得继续列举旧协议名称或给迁移建议。

## 4. Authority table

| 状态/动作 | 唯一 authority | transaction/fence/identity | 可读 cache | recovery owner | 禁止路径 |
|---|---|---|---|---|---|
| current config | `core.config.Config` strict decoder | canonical YAML bytes + config schema + SHA-256 | resolved config snapshot | fresh initializer 重新创建 run | profile selector、old-key mapper、migration |
| baseline config | `baselines.config.BaselineConfig` strict decoder | baseline schema + canonical bytes | 无协议 config 投影 | baseline launcher | Full loader 猜测 baseline |
| run initialization | `tools.init_run` + `storage.run_initializer` | fresh root、descriptor/source/config digests、create-no-replace | 单一 authority bootstrap marker | 同一 current identity 的 initializer 幂等 replay/identity-reservation repair；身份无效时操作者改用 fresh root | 旧 schema/layout repair/migrate、双 schema writer、第二份 resolved config |
| run descriptor | immutable descriptor | descriptor format/digest + `static|dynamic` + config/source identity | actor-local loaded object | 无 mutation；损坏即 fail closed | `protocol_version` selector、`full_ha_*` 翻译 |
| membership/admission/replacement | current SQLite leader command | leader epoch + contributor/incarnation/stream fence | immutable response/control | current leader replay | actor 直写 DB、legacy filesystem admission |
| scheduler reconciliation | current launch outbox command + PBS live/historical receipt | launch request CAS + canonical full job ID + observation time | scheduler observation cache | current leader/operator request replay | PID/hostname 推断、伪造 FINISH、删除 reservation row |
| receipt/proposal ingest | current SQLite leader/session | exact pointer identity + fence + sequence + digest | discovery frontier | current leader idempotent replay | fragment decoder/table、unfenced writer |
| selection/commit/publication | current SQLite leader/session | `BEGIN IMMEDIATE` 后重验 leader/contributor fence | epoch control/latest convenience cache | successor reconcile durable intent | 第二 writer、v4/old path fallback |
| token/terminal/drain | current SQLite command ledger | committed receipt/proposal identity + terminal cutoff | summary/heartbeat | current leader | 以日志、version 或旧 metadata 猜测 |
| inspect/eval | read-only current descriptor/DB/artifacts | exact current identity | 可生成 report，不写 run | 无 | legacy query-only、隐式 DDL/repair |
| cleanup | current artifact policy + completion evidence | resolved inode/path + policy digest + authority live refs | dry-run manifest | 操作者显式 apply | policy override、旧 run 猜测 |

### 4.1 异步交接保持条件

删名/删码不能把 current handoff 退化成旧轮询猜测：

- learner publication commit point 仍是 immutable payload/receipt 加 canonical pointer 的完整发布；SQLite ingest transaction 才是 durable ingest commit point；
- receipt ack 继续绑定 run、descriptor、stable contributor、cycle sequence、contributor fence、receipt ID 和 content digest；stale ack 不能推进 producer；
- producer 能否进入下一 cycle、drain 或 terminal 等待由 exact ack/current control 决定，积压上界继续由每 contributor current pointer、selection 和 receipt frontier 约束；
- successor 只按 command journal/publication intent/ack identity 幂等 replay，terminal close 使用 authority 已 ingest frontier 和 cutoff，不从日志、mtime 或 global version 猜测；
- 本计划只改名称和删除旧分支，不改变这些 commit point、ack 字段或 backlog 公式；若实现者发现必须改变，先修订 design 和 artifact version owner，不能把它混入 mechanical rename。

## 5. Identity table

| Identity | canonical form | authority owner | 持久化位置 | 允许比较对象 |
|---|---|---|---|---|
| product protocol | 常量概念 `Full Protocol`，不持久化 generation number | current source tree | 文档/包 metadata | 不参与运行时 mode 比较 |
| deployment topology | `static` 或 `dynamic` | current config | resolved config、descriptor、schema meta、run identity/bootstrap、actor attestation、result artifact | 同一 enum 值；不与 baseline 的 `ddp|periodic_average` 比较 |
| config | exact schema version + canonical bytes SHA-256 | strict Full/baseline loader 各自所有 | resolved YAML + descriptor | 相同 loader/schema 的 digest |
| authority schema | exact schema version + DDL digest/features | initializer | SQLite schema/meta + marker | 相同 topology/current DDL |
| run descriptor | exact descriptor format + self digest | initializer | immutable descriptor JSON | descriptor digest |
| artifact wire | 每个 artifact 的 exact `format_version` + content digest | 对应 producer | immutable artifact/pointer | 同一 artifact type/version |
| source | peeled Git commit + dirty bit + scoped fingerprint | source manifest builder | descriptor/source manifest/actor attestation | canonical commit/fingerprint |
| model/tokenizer/dataset | immutable Hub commit 或 current manifest digest | config + actual loader | config/descriptor/attestation | 同类型 immutable identity |
| PBS actor | canonical PBS job ID + actor attempt/incarnation | launcher/scheduler receipt | launch/registration records | 同类型 canonical ID |
| static contributor | stable key=`learner_id`；incarnation=`logical_launch_id + attempt_id + binding_generation` | static binding command | binding/history、receipt/proposal fence | stable key 只用于 service/accounting；commit 必须比较完整 fence |
| dynamic contributor | stable key=`stream_id`；incarnation=`instance_id + placement_id/epoch + stream_id/epoch + admission_generation/token digest` | admission/replacement command | instance/stream rows、admission、receipt/proposal fence | replacement 可复用 stable stream；commit 必须比较完整 current fence，不能以 instance 数代替 stable contributor 数 |

## 6. 测试、fixture、harness 与配置的最终形态

### 6.1 测试原则

测试按当前领域命名，不按历史 plan/phase/generation 命名。至少完成：

- `test_schema_v4.py` → `test_schema.py`，其他 `*_v4.py` 同理；
- `test_p3_unified_v4_golden.py` → `test_protocol_golden.py`，只断言 `protocol_trace.json` 的当前语义，不再与 classic/static 历史 trace 比较；
- `test_authority_p2_dynamic.py`、`test_authority_p3_operational.py` 等按 `dynamic_authority`、`operational_authority` 领域重命名；
- 将超大的 `test_p4_mandatory_runtime.py` 删除 migration/legacy case 后按 admission、startup、publication、terminal/recovery 等当前职责拆分；
- 删除 `test_plan03_checker.py`、fragment/legacy tests 和 classic/fragment fixture；
- 用 `test_current_protocol_surface.py` 取代历史 tombstone blacklist，正向验证唯一 config loader、entrypoint、schema、writer 与 package manifest，并对 active scope 的禁用 token/path 做精确扫描；
- production/test 中的 `PLAN03_REQUIREMENTS` 全部删除；requirement 映射只存在本计划 matrix 与 evidence 中。

`test_proposal_v2.py`、`test_cycle_receipt_v1.py` 等若验证当前 exact wire format，可以保留原名；它们不得包含旧-format decode 分支。

Fail-closed 反例在临时目录中从 current schema 合成 unknown key、wrong exact version、wrong digest/path/mode；不得保留旧 YAML、旧 DDL、旧 descriptor、旧 run fixture 或旧错误文案来做兼容测试。测试的断言是“只接受 current contract”，不是“识别某一历史 generation”。

### 6.2 harness disposition

每个 `plan03_*`、`run_plan03_*`、`run_plan01_*` 文件必须进入 P0 ledger：

- 仅用于旧 matrix/checker/classic 对比/历史 artifact 的：`delete-obsolete`；
- 表达当前 fault oracle、但名称/metadata 绑定 Plan03 的：以更小通用 harness `rename-current`；
- oracle 与 product 混在一起的：提取当前最小 oracle 后删除原文件。

最终 active source 不出现 Plan01/Plan03 requirement ID、phase marker、archive tag 依赖或 pytest marker `plan03_red`/`p6_formal`。仍需要的通用 marker 使用行为名称，例如 `expected_red`、`formal_acceptance`。

### 6.3 最小配置 manifest

最终 manifest 精确为 7 个 Full Protocol 配置和 3 个 baseline 配置：

```text
configs/fs_diloco_tiny_static.yaml
configs/fs_diloco_tiny_dynamic.yaml
configs/fs_diloco_tiny_static_acceptance.yaml
configs/fs_diloco_tiny_dynamic_acceptance.yaml
configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml
configs/fs_diloco_gpt2_wikitext2_8l.yaml
configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml
configs/torch_baseline_tiny_2rank.yaml
configs/torch_baseline_gpt2_wikitext2_2rank_debug.yaml
configs/torch_baseline_gpt2_wikitext2_8n_5000steps.yaml
```

`tiny_local` 与 `tiny_ha_static` 合并为 `tiny_static`；dynamic/static acceptance 名称去掉 `ha` 世代噪声。其余 synthetic fault 配置由测试在临时目录生成，不作为产品模板。每个 retained YAML 必须恰有可解释的 current script/test/doc owner，并由对应 strict loader 解析；无 owner 即删除。P0 若证明这个 manifest 与真实 current caller 无法同时成立，必须先修订 design/matrix 并重新做 test-design review；执行者不能静默增减文件。

## 7. 文档与历史边界

Active 文档只描述 `Full Protocol` 当前态：

- 删除 compatibility/migration 文档而不是改写为历史指南；
- README、`docs/00..07`、module docs、示例和注释删除 v4、legacy、classic、fragment、migration 语言；
- 删除仍处于 active plans/ref/followups 区、且设计目标依赖 Fragment 或旧 Full reference 的四份文档；Git history 已保留其历史，不搬入新的 compatibility archive；
- static/dynamic 明确为 topology；baseline 明确为独立比较系统；
- 所有命令、路径、schema、配置名与当前代码一致。

`plans/DONE/**`、`reports/checked/**` 和 Git 历史是冻结审计证据，不是 active API，也不作为运行时或测试依赖。本计划不改写历史 commit、不删除 tag 对象、不篡改已归档报告。repository-wide scanner 必须显式区分 active source 与这些 immutable archive；任何 active code/test/config/script/doc 都不得读取 archive tag 或把 archive 文件当 oracle。

## 8. 被拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 保留 `config_v4.py` 并增加 suffix-free re-export | 形成永久 shim 和两个入口，违反单一接口 |
| 让旧 run 保留 query-only reader | 用户和根规则明确禁止；会把旧 schema/fragment 语义继续带入工具 |
| 只做机械文件重命名 | 无法删除 profile、migration、fallback、重复 mode identity 和历史 harness |
| 为旧字段保留更友好的专用错误 | 专用识别本身就是历史知识；strict unknown-key/schema failure 足够 |
| 把 baseline 继续作为 Full config profile | 维持同一 loader 中两种系统分支，概念不唯一 |
| 删除所有数字版本常量 | 会失去当前持久化格式的 exact identity，且把正交 artifact 生命周期错误合并 |
| 保留所有 YAML 以防将来使用 | 无 current owner 的模板是重复配置和漂移源 |
| 为减小 diff 保留 Plan03 测试名/marker | 历史 phase 身份会继续污染当前测试语义和 Checker |

## 9. 非目标

- 不重新设计 Full Protocol 的 fenced SQLite authority、aggregation、token accounting、publication、terminal 或 scheduler 语义；发现现有 correctness defect 时单独登记，不借清理计划静默改协议。
- 不统一各 artifact 的 format version，也不支持旧 artifact decoder。
- 不删除当前 static/dynamic topology 或当前 torch distributed baseline。
- 不开展 classic 性能/质量对比，不运行旧 tag，不把旧代码复制到新目录。
- 不迁移任何旧 run/config/checkpoint/database；不提供一次性 migration script。
- 不改写 `plans/DONE/**`、`reports/checked/**`、Git history 或 archive tag。
- 本设计文件不授权提交 PBS job；正式测试按计划和 workflow 在实施期执行。

## 10. 设计完成判据

当且仅当以下条件同时满足，代码实现才符合本设计：

1. active tree 只有一个 Full config/descriptor/schema/runtime/tool path；
2. Full Protocol generation `v4` 不再出现在 active path、symbol、CLI、config、runtime metadata 或文档；
3. legacy/classic/fragment/migration/fallback/shim 不再存在于 active source；
4. static/dynamic 使用同一 canonical identity spelling，旧 run 无任何读写入口；
5. baseline 有独立、最小、严格配置边界；
6. 当前 wire/schema version 仍 exact-match、fail closed，未被误当兼容模式删除；
7. 当前测试、config、scripts、package data 与 formal runtime evidence 指向同一冻结 runtime fingerprint；实验后的文档/报告 closure 只有在 executable scope byte-exact 等价时承接该证据。
