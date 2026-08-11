# 删除 static learner mode，统一使用 stream-pool(当前为membership.mode: dynamic) membership

## 目标

删除 static learner mode，将 stream-pool membership(当前为membership.mode: dynamic) 作为唯一的成员管理方式。完成后，配置、run descriptor、协议、authority、CLI、PBS 脚本、测试和当前文档均不得再提供 membership mode 选择。不得保留旧字段、旧数据格式、兼容解析或迁移逻辑。

系统采用以下唯一模型：

- 固定的 stream pool 定义逻辑贡献者和数据分片。
- learner instance 必须通过 admission 才能占用一个 stream。
- admission 生成 `ContributorFence`。learner 提交 receipt、proposal 或 terminal ack 时，必须携带当前 fence。
- learner instance 被替换后，stream 及其进度继续保留，旧 instance 的 fence 失效。

`scaling.enabled` 只控制自动容量管理：

- `scaling.enabled=false`：容量固定，不自动执行 replacement 或 scale-out。
- `scaling.enabled=true`：允许 capacity service 根据容量观测发起 replacement 或 scale-out。

两种配置使用相同的 membership 和 admission 协议，不构成两种 learner mode。

本 plan 不修改 learner 训练流程、DiLoCo inner/outer optimizer、merge 数学、staleness weighting 或 token accounting。

变更后的实现不支持读取变更前创建的 run root。

## 核心术语

| 术语 | 含义 |
|---|---|
| stream | 稳定的逻辑贡献者和数据分片标识。learner 进程更换后，stream 及其进度继续存在。 |
| instance | 一次具体的 learner 进程实例，由 `instance_id` 标识。进程重启或替换后会产生新的 instance。 |
| placement | instance 的执行位置，由 `placement_id` 和 `placement_epoch` 标识。 |
| admission | authority 将一个可用 stream 授予符合条件的 instance，并返回恢复状态和当前 fence。 |
| fence | 当前 stream 的所有权凭证。唯一 `ContributorFence` 包含 `instance_id`、`placement_id`、`placement_epoch`、`stream_id`、`stream_epoch`、`admission_generation` 和 `admission_token_sha256`。 |
| membership | 固定 stream pool 与当前已获准 instance/placement 之间的所有权关系，不是 learner 进程的静态名单。 |

必须保持以下约束：

1. `stream_id` 是稳定 contributor key。
2. instance 只有在 admission 成功后才能代表 stream 提交数据。
3. replacement 必须产生新的 stream incarnation；旧 `stream_epoch` 对应的提交必须被拒绝。
4. `placement_epoch` 和 `admission_generation` 必须参与当前所有权校验。
5. 是否启用 scaling 不得改变 membership、fence 或 admission 的数据模型。

## 执行依据

1. 主要原则遵循 `AGENTS.md`。
2. 实施流程遵循 `plans/workflow.md`。
3. 测试和实验阶段使用 `$miyabi-development` skill。
4. 实施前和完整源码审查时，使用 CodeGraph 重新确认调用路径和影响范围。

## 实施流程

1. 按 `plans/workflow.md` 建立 plan05 的精简执行包和 requirement matrix。根据本指南和最新 CodeGraph 结果确定删除边界。
2. 按 P0 至 P3 依次修改版本、配置、协议、authority、runtime 和 launcher。每个阶段均直接删除旧接口及其测试，不增加兼容层。
3. 更新现有 test owner。在 inactive computation node 上依次运行 focused tests、完整测试和 harness。
4. 在昂贵实验前完成 current-state 全量审查。确认代码、DDL、配置、脚本、Checker、网页文档和生成物只描述唯一协议。
5. 更新 `do_experiments/experiment04` 的当前实验入口。在相同 workload 和 seed 下，重新运行 no-failure、failure/no-replacement、failure/authorized-replacement。先在 `execution.md` 中登记比较指标和阈值，再与变更前冻结的 dynamic baseline 比较。
6. 完成仓库级清理搜索。`plans/DONE/**`、`reports/**`、已有 artifacts 和已归档设计文档保持原样；使用 commit、source fingerprint、config 和 Checker 输出界定其历史协议。
7. 使用修改完成的 `gpt2_wikitext2_8l_200x10.yaml` 配置文件完成一次**正常执行**：同时提交 8 个独立的 learner job 和 1 个 syncer job，按 `local steps 200 × global steps 10` 正常训练至结束

## 修改指南

以下要求已根据当前源码和 CodeGraph 调用关系校正。实施开始时仍须重新查询 CodeGraph，不得将本文件中的行号或阶段划分视为新的 source of truth。

### P0–P2：版本、配置和协议边界

| 阶段 | 文件 | 现状 | 修改要求 |
|---|---|---|---|
| P0 | [`core/versions.py`](../../../fs_diloco/core/versions.py) | config、descriptor、authority 和嵌入 fence 的多种 wire object 各有独立版本 | 提升 `CONFIG_SCHEMA_VERSION`、`RUN_DESCRIPTOR_FORMAT_VERSION`、`AUTHORITY_SCHEMA_VERSION` 和 `PROTOCOL_VERSION`；由于唯一 fence 的序列化删除 `kind`，同时提升实际受影响的 proposal、proposal pointer、cycle receipt 和 control format。`storage/admission.py` 中 request/response/current/disposition 的局部 format version 也分别提升。不要无差别提升无关版本；删除确认未被 producer/consumer 使用的陈旧常量。 |
| P1 | [`core/config.py`](../../../fs_diloco/core/config.py#L148) | `membership.mode` 默认 `static`；`sync.num_learners` 与 `membership.stream_pool_size` 重复表达同一容量；存在 static 归一化和双模式校验 | 删除 `membership.mode` 和 `sync.num_learners`，以 `membership.stream_pool_size` 作为唯一 contributor/数据分片数量；保留 `bootstrap_instances`；删除 `_validate_source_shape` 中的 static 特例和 `config_to_dict` 的条件裁剪。quorum、bootstrap 和 scaling 约束直接针对唯一 stream pool 校验。旧字段应由严格 unknown-field 校验拒绝。 |
| P1 | [`core/run_descriptor.py`](../../../fs_diloco/core/run_descriptor.py#L63) | descriptor 读取并交叉校验 `mode`，依赖 nullable 的双模式字段 | descriptor 只接受当前 schema，删除 `mode` 和 `static_learner_ids`；直接严格校验 `stream_pool_size`、`bootstrap_slots` 及其与 resolved config 的关系。不要接受旧 descriptor。 |
| P1 | [`tools/init_run.py`](../../../fs_diloco/tools/init_run.py#L44) | 生成 `static_learner_ids` 或 `stream_pool_size`，并把 `mode` 写入 descriptor、`.identity` 和 bootstrap identity | 只生成 non-null 的 stream-pool descriptor，直接构造唯一 `MembershipScope`；从 descriptor、run identity 和初始化 identity 中删除 `mode`。 |
| P1 | [`storage/run_initializer.py`](../../../fs_diloco/storage/run_initializer.py#L245) | completed-run 校验根据 descriptor `mode` 重建两种 scope，并核对 identity mode | 只从 `stream_pool_size` 重建唯一 scope；删除 mode/nullable-field 分派和 mode mismatch 校验，保留 immutable identity、checksum、manifest 与 authority 校验。 |
| P2 | [`protocol/contributor.py`](../../../fs_diloco/protocol/contributor.py) | `StaticContributorFence`、`DynamicContributorFence` union，`kind` decoder，以及两种 membership scope | 删除 static fence/scope；将当前 dynamic 结构收敛为唯一 `ContributorFence` 和 `MembershipScope`。fence 只保留 `instance_id`、`placement_id`、`placement_epoch`、`stream_id`、`stream_epoch`、`admission_generation`、`admission_token_sha256`，删除冗余 `kind` 和按 kind 分派的 decoder；由唯一类型负责严格 `from_dict`。 |
| P2 | [`protocol/authority.py`](../../../fs_diloco/protocol/authority.py#L194) | `StaticBinding` 和 `DynamicAdmission` 暴露模式名称 | 删除 `StaticBinding`；将唯一 admission result 收敛为中性名称 `Admission`，不保留旧类名 alias。 |
| P2 | [`protocol/cycle_receipt.py`](../../../fs_diloco/protocol/cycle_receipt.py) | receipt 接受两种 fence，receipt namespace 以 `fence.kind` 为前缀 | 只接受唯一 fence；namespace 使用完整 canonical fence hash，不再编码 mode。同步提升 receipt/control 边界的版本。 |
| P2 | [`protocol/proposal.py`](../../../fs_diloco/protocol/proposal.py) | proposal 对两种 fence 做 union 类型判断 | 只接受唯一 fence，并提升 proposal 及 proposal-pointer format；`runtime/learner.py` 中 pointer 的硬编码版本改用其唯一版本 owner。 |
| P2 | [`storage/control.py`](../../../fs_diloco/storage/control.py) | receipt ack、terminal ack 和 admission/current controls 序列化并解码带 `kind` 的 fence | 更新所有嵌入 fence 的 control producer/consumer，确保不存在旧 decoder 或旧 namespace fallback。 |

### P2–P3：Authority、admission 和 runtime

| 阶段 | 文件 | 现状 | 修改要求 |
|---|---|---|---|
| P2 | [`storage/schema.sql`](../../../fs_diloco/storage/schema.sql) | 基础 DDL 包含 schema `mode`、`features_json`、三处 `fence_kind` 以及 static binding/history 表 | 合入当前 stream/instance/placement/launch DDL；删除 `static_contributor_bindings`、`static_binding_history`、schema `mode`/`features_json` 和冗余 `fence_kind` 列。fence 身份只由严格 `fence_json` 与关联 instance/stream 状态表达。 |
| P2 | [`storage/schema_dynamic.sql`](../../../fs_diloco/storage/schema_dynamic.sql) 与 [`pyproject.toml`](../../../pyproject.toml#L44) | stream membership 是附加 DDL，并作为第二个 package-data 文件发布 | 将 DDL 合并进 `schema.sql` 后删除 `schema_dynamic.sql`，并从 package data 删除该文件，形成唯一 schema bundle。 |
| P2 | [`storage/authority.py`](../../../fs_diloco/storage/authority.py) | `_schema_text(mode)`、`canonical_features(mode)`、两种 scope、static binding/history，以及多套 current-fence、terminal、fence-validation 事务 | 删除全部 static API、SQL 和 type branch；`AuthorityMetadata`、bootstrap marker 和 schema validation 不再保存 mode/features。`ddl_bundle_sha256()` 只计算唯一 DDL。通用事务直接走 instance/stream/placement；只为模式区分而存在的 `dynamic_` API 名称收敛为当前中性名称，例如 membership initialization、admission、streams、instances 和 launch requests，不保留 alias。 |
| P2 | [`storage/admission.py`](../../../fs_diloco/storage/admission.py) | static registration/generation/replacement authorization、request `mode`、两套字段和两种 discovery path | 删除全部 static request/replacement 函数和常量；唯一 request 严格使用 instance/stream/bootstrap-or-launch authorization 字段。删除 payload `mode` 和 `registration_requests/dynamic/` 目录层级，相应简化 discovery、actor identity、response repair 和 disposition 校验；提升所有嵌入新 request/fence shape 的局部 format version。 |
| P2 | [`storage/paths.py`](../../../fs_diloco/storage/paths.py#L72) | `static_replacement_requests` 目录和 path builder | 删除目录属性、path builder 和初始化逻辑。 |
| P2 | [`storage/artifact_policy.py`](../../../fs_diloco/storage/artifact_policy.py#L103) | policy 声明 static replacement artifacts | 删除对应 policy 条目；如 artifact policy 的 schema 形状没有变化，不因单个 pattern 删除而错误提升其 format version。 |
| P3 | [`runtime/learner_entrypoint.py`](../../../fs_diloco/runtime/learner_entrypoint.py) | 同时接受 static identity 和 stream admission 参数 | 删除 `--learner-id`、`--logical-launch-id`、`--attempt-id`；保留 `--bootstrap-slot`，或 `--launch-request-id` 配合 replacement 所需的 `--stream-id`/`--replace-instance-id`。始终在 Torch import 前完成唯一 admission 和即时重校验。 |
| P3 | [`runtime/learner.py`](../../../fs_diloco/runtime/learner.py#L450) | 根据 `fence.kind` 选择 learner index 和数据分片总数 | 直接使用 `fence.stream_id` 和 `membership.stream_pool_size`；其余训练和 token accounting 行为不变。 |
| P3 | [`runtime/syncer_entrypoint.py`](../../../fs_diloco/runtime/syncer_entrypoint.py#L84) | 构造两种 scope、两种 initial-admission 条件，并条件初始化 stream membership | 直接构造唯一 scope，始终初始化 stream pool；initial admission 只按 `bootstrap_slots` 判断。 |
| P3 | [`runtime/syncer.py`](../../../fs_diloco/runtime/syncer.py#L468) | `_admit_requests`、response repair、actor identity、current admission 和 capacity service 构造都含模式分支 | 删除 static admission/replay/replacement 分支；所有路径直接使用 instance/stream 语义。capacity service 只由 `scaling.enabled` 控制，不再检查 membership mode。 |
| P3 | [`runtime/services/dynamic_capacity.py`](../../../fs_diloco/runtime/services/dynamic_capacity.py) | 调用 `dynamic_streams`、`dynamic_instances`、`plan_dynamic_launch_request` 等 mode-disambiguated authority API | 改用收敛后的中性 authority API；模块与 service 的 dynamic-capacity 名称可以保留，因为它描述 `scaling.enabled` 的运行行为，而不是可选 membership mode。同步更新 `runtime/services/__init__.py` 和调用测试。 |
| P3 | [`runtime/services/terminal.py`](../../../fs_diloco/runtime/services/terminal.py) | terminal ack 根据 fence kind 解析 actor | 只验证 `instance_id` 和当前 stream incarnation；删除 static learner/attempt 分支。 |
| P3 | [`tools/analysis.py`](../../../fs_diloco/tools/analysis.py) | 根据 descriptor 创建两种 scope，并输出 `run.mode` 和嵌套 `dynamic` 区域 | 只使用唯一 scope；删除冗余 membership mode，把 stream/instance/launch/capacity 信息放入中性命名的 summary 区域。 |
| P3 | [`tools/summarize_runs.py`](../../../tools/summarize_runs.py) | Full Protocol parser 要求 descriptor `mode=dynamic`、fence `kind=dynamic`，并输出 `fs_diloco_dynamic_full` | 按唯一 descriptor/fence schema 解析，输出中性 `fs_diloco_full_protocol`；DDP/periodic-average 自身的 `mode` 字段仍有独立含义，不应误删。 |
| P3 | [`tools/launch_independent_run.py`](../../../fs_diloco/tools/launch_independent_run.py) | 分别生成 static learner 和 bootstrap qsub，结果含 `membership_mode`/`learner_index` | 只生成 bootstrap-slot scalar jobs；删除 `FS_DILOCO_STATIC_LAUNCH_PREFIX`、learner-index 分支和冗余 mode 输出。 |
| P3 | [`tools/request_static_replacement.py`](../../../fs_diloco/tools/request_static_replacement.py) | 完全服务于 static replacement | 整个文件删除；replacement 只能由当前 launch-request/scheduler authorization 协议触发。 |

### 配置、PBS、实验入口和正式验收

| 文件 | 修改要求 |
|---|---|
| [`configs/full_protocol_static.yaml`](../../../configs/full_protocol_static.yaml) 与 [`configs/full_protocol_dynamic.yaml`](../../../configs/full_protocol_dynamic.yaml) | 不保留两份模式配置。创建唯一 `configs/full_protocol.yaml`：保留正式 8-stream、8-bootstrap、50 local steps × 10 global steps workload，并使用 stream-pool membership；默认 `scaling.enabled=false`，作为 fixed-capacity/no-churn 正式 control。随后删除两份旧文件。不要直接把当前 4-stream dynamic smoke 配置原样重命名为正式配置。 |
| [`configs/full_protocol_functional.yaml`](../../../configs/full_protocol_functional.yaml) | 改为 4-stream 的唯一协议 functional 配置；为 authorized-replacement fault gate 提供短时且有效的 scaling/launch 参数。 |
| [`configs/dynamic_full/gpt2_wikitext2_8l_200x10.yaml`](../../../configs/dynamic_full/gpt2_wikitext2_8l_200x10.yaml) | 移到中性实验路径，例如 `configs/experiments/gpt2_wikitext2_8l_200x10.yaml`；删除 `membership.mode` 和 `sync.num_learners`，保持 Plan04 的 8-stream、quorum=4、200 × 10 workload。 |
| [`scripts/miyabi/agent/run_learner.pbs`](../../../scripts/miyabi/agent/run_learner.pbs) | 删除 static shell 分支和相关环境变量；只生成 bootstrap admission，或消费包含 stream/replaced-instance 身份的 authorized launch request。 |
| [`scripts/miyabi/agent/run_full_protocol_rank.sh`](../../../scripts/miyabi/agent/run_full_protocol_rank.sh) | 用 bootstrap slot 启动 co-allocated learner。co-allocated harness 只负责 no-failure 和 syncer-takeover；不要在同一 PBS allocation 内伪造需要独立 qsub receipt 的 replacement。learner loss/replacement 由 independent-actor supervisor 验证。 |
| [`scripts/miyabi/agent/run_full_protocol_allocation.sh`](../../../scripts/miyabi/agent/run_full_protocol_allocation.sh) | 删除 static descriptor 要求；读取 `stream_pool_size`/`bootstrap_slots`，并校验 allocation topology 与唯一 descriptor。 |
| [`scripts/miyabi/agent/check_full_protocol_run.py`](../../../scripts/miyabi/agent/check_full_protocol_run.py) | 删除 descriptor/schema mode、`sync.num_learners`、static binding/history 和 fence-kind oracle；改查 stream epoch、instance replacement、launch request/qsub receipt、duplicate rejection、cursor continuity、terminal fence 和 token ledger。co-allocated 与 independent topology 必须分别绑定真实 scheduler evidence。 |
| [`scripts/miyabi/agent/run_full_protocol.pbs`](../../../scripts/miyabi/agent/run_full_protocol.pbs) | 默认使用唯一 `full_protocol.yaml`；移除 co-allocated `learner_replacement` fault path，保留与该 topology 相符的 gate 参数。 |
| [`scripts/miyabi/agent/run_independent_launcher.pbs`](../../../scripts/miyabi/agent/run_independent_launcher.pbs) | 默认使用唯一 `full_protocol.yaml`。 |
| [`scripts/miyabi/agent/submit_independent_8l1s_50x10.sh`](../../../scripts/miyabi/agent/submit_independent_8l1s_50x10.sh) | 文件名已经是中性的，无需改名；更新配置引用、注释、run identity 和提交结果字段，明确其提交固定 stream pool 的 scalar bootstrap jobs。 |
| [`do_experiments/submit_independent_8l1s_50x10.sh`](../../../do_experiments/submit_independent_8l1s_50x10.sh) | 与 canonical Miyabi submitter 重复，删除该副本。 |
| [`do_experiments/experiment04`](../../../do_experiments/experiment04) | 更新 config path、descriptor/fence 解析、gate 名称和 durable oracle；继续用真实独立 PBS actor、capacity observation 和 launch outbox 验证 learner loss/replacement，禁止测试专用 admission bypass。 |
| [`scripts/miyabi/agent/check_plan_completion.py`](../../../scripts/miyabi/agent/check_plan_completion.py) | 这是硬编码 `plan03-1` 和旧 static gate contract 的历史 checker，当前只有其专用测试、validation-suite 清单和 module-coverage 映射依赖。与 `tests/harness/test_plan_completion.py` 一并删除，并更新这两处索引；plan05 使用自己的 requirement/evidence 审查，不把它改造成兼容两代协议，也不篡改历史 artifact。 |

### 测试修改

遵循现有 test owner；修改测试时补齐 `AGENTS.md` 要求的英文 module/function/class docstring 和行为原因注释。

| 测试区域 | 文件 | 修改要求 |
|---|---|---|
| 直接删除 | [`test_static_contributor_binding.py`](../../../tests/storage/test_static_contributor_binding.py)、[`test_request_static_replacement.py`](../../../tests/tools/test_request_static_replacement.py) | 仅验证已删除的 static binding/operator command。 |
| 配置 | [`test_config.py`](../../../tests/test_config.py) | 删除 mode/static shape 测试；验证 `membership.mode` 和 `sync.num_learners` 是 unknown field，并覆盖 stream pool、bootstrap、quorum、fixed-capacity 与 scaling-enabled 组合。 |
| 初始化与 descriptor | [`test_run_initializer.py`](../../../tests/storage/test_run_initializer.py) | 删除 identity-mode mismatch；验证唯一 descriptor、scope、identity、format version 和 completed-run recovery。 |
| 协议类型 | [`test_cycle_receipt.py`](../../../tests/protocol/test_cycle_receipt.py)、[`test_proposal.py`](../../../tests/protocol/test_proposal.py) | 全部改用唯一 fence；验证无 `kind` 的 strict shape、namespace 隔离和新版本拒绝旧 payload。 |
| learner admission | [`test_learner_entrypoint.py`](../../../tests/runtime/test_learner_entrypoint.py) | 删除 static admission；验证旧 static CLI 参数不存在，bootstrap/launch-request 互斥且 admission 在 Torch import 前完成。 |
| Syncer admission | [`test_syncer_composition.py`](../../../tests/runtime/test_syncer_composition.py)、[`test_syncer_startup_admission.py`](../../../tests/runtime/test_syncer_startup_admission.py) | 删除 static replacement 场景；保留 bootstrap/replacement authorization、replay、duplicate-instance、response repair 和 startup admission。 |
| Runtime service | [`test_syncer_fault_boundary.py`](../../../tests/runtime/test_syncer_fault_boundary.py)、[`test_terminal_service.py`](../../../tests/runtime/test_terminal_service.py)、[`test_dynamic_capacity_service.py`](../../../tests/runtime/test_dynamic_capacity_service.py) | fixture 改为唯一 incarnation；capacity service 名称仍可保留，因为它描述 `scaling.enabled` 的动态容量行为，而不是 membership mode。 |
| Authority schema | [`test_schema.py`](../../../tests/storage/test_schema.py) | 删除双 schema 参数化和 mode mismatch；验证唯一 DDL/hash/version，确认 static tables、schema mode/features 和 fence-kind 列不存在。 |
| Authority membership | [`test_authority_dynamic.py`](../../../tests/storage/test_authority_dynamic.py)、[`test_dynamic_launch_authorization.py`](../../../tests/storage/test_dynamic_launch_authorization.py) | 分别改名为中性的 membership/incarnation owner 和 launch-authorization owner；更新唯一 authority API，不保留 `dynamic_` mode-disambiguation 名称。 |
| Authority 通用事务 | [`test_authority_operational.py`](../../../tests/storage/test_authority_operational.py)、[`test_leader_authority_commands.py`](../../../tests/storage/test_leader_authority_commands.py) | 将 static helper/fence 全部替换为完成 admission 后得到的 incarnation fixture，保留事务、rollback、terminal、replay、cursor 和 replacement 断言。 |
| Proposal/状态机 | [`test_contributor_progress.py`](../../../tests/storage/test_contributor_progress.py)、[`test_proposal_adjudication.py`](../../../tests/storage/test_proposal_adjudication.py)、[`test_publication.py`](../../../tests/storage/test_publication.py)、[`test_state_machine.py`](../../../tests/storage/test_state_machine.py)、[`test_visibility.py`](../../../tests/storage/test_visibility.py) | 这些测试验证通用行为，只是借用了便宜的 static fixture；必须迁移，不能删除。 |
| Admission 边界 | [`test_dynamic_admission_request.py`](../../../tests/storage/test_dynamic_admission_request.py) | 改名为唯一 admission-request owner；删除跨模式拒绝，改测旧 `mode`/static 字段和未知字段被严格拒绝。同步更新 validation-suite 的 test path。 |
| 工具与实验 | [`test_analysis.py`](../../../tests/tools/test_analysis.py)、[`test_launch_independent_run.py`](../../../tests/tools/test_launch_independent_run.py)、[`test_resolve_scheduler_uncertainty.py`](../../../tests/tools/test_resolve_scheduler_uncertainty.py)、[`test_summarize_runs.py`](../../../tests/test_summarize_runs.py)、[`test_plan04_experiment.py`](../../../tests/harness/test_plan04_experiment.py) | 验证中性 summary/run kind、bootstrap scalar qsub、唯一 authority 方法名、唯一 descriptor/fence 和更新后的实验 config/oracle。 |
| Harness | [`test_full_protocol_harness.py`](../../../tests/harness/test_full_protocol_harness.py) | fixture 改为 stream/instance/launch authority；co-allocated fault 与 independent replacement oracle 分开，fault evidence 必须绑定 durable replacement 和 scheduler receipt。 |
| 公共 fixture | [`tests/support/protocol.py`](../../../tests/support/protocol.py) | 删除默认 `static_fence()`；提供标准唯一 fence，并由需要 authority 状态的测试通过 admission fixture 获得 current fence。 |
| 架构与覆盖索引 | [`test_authority_surface.py`](../../../tests/architecture/test_authority_surface.py)、[`test_plan_complete_dead_surfaces.py`](../../../tests/architecture/test_plan_complete_dead_surfaces.py)、[`module_coverage.json`](../../../tests/module_coverage.json)、[`run_validation_suite.py`](../../../scripts/miyabi/agent/run_validation_suite.py) | 删除 static API/schema allowlist，登记中性 authority/admission surface；删除已移除文件的映射和 validation path，并更新所有被改名 test owner。 |
| Artifact policy | [`test_artifact_policy.py`](../../../tests/storage/test_artifact_policy.py) | 删除 static replacement artifact 分类断言，保留 authority/audit/cleanup 安全边界。 |

### 当前文档、生成物和历史证据

| 项目 | 修改要求 |
|---|---|
| [`README.md`](../../../README.md) | 删除双模式说明；使用唯一 `full_protocol.yaml`；明确 fixed-capacity 与 automatic replacement/scale-out 都基于同一 stream-pool protocol。 |
| [`website/app`](../../../website/app) | 更新 Overview、Getting Started、Concepts、User Guide、Architecture、Reference 和首页中的模式、CLI、配置与 replacement 文案。 |
| [`website/scripts/generate_reference.py`](../../../website/scripts/generate_reference.py) 及生成的 API reference data | 删除 static module/symbol summary，按修改后的 Python surface 重新生成 `api-index.ts` 和 `api-manifest.json`，不得手工保留已删除 API。 |
| [`plans/00-RESEARCH_PLAN.md`](../../../plans/00-RESEARCH_PLAN.md) | 保留已有 static/dynamic matched-run 结论，但明确标记为冻结历史 baseline；后续实验使用 Full Protocol fixed-capacity/no-churn control。 |
| `plans/DONE/**`、`reports/**`、已有 artifacts | 不修改、不删除历史记录，也不让当前代码兼容其旧 schema；通过其 commit、source fingerprint、config 和 Checker 输出界定复现边界。 |
| 新实验矩阵 | 固定唯一协议，比较 no-failure、failure/no-replacement、failure/authorized-replacement。每组使用 fresh run root，并预注册 workload、seed、quorum/scaling 差异、durable oracle、统计量、阈值和 incomparable 条件。 |

## 不应改变的核心行为

以下部分只消费 current contributor fence/proposal，不应改变算法行为：

- learner 本地训练循环；
- DiLoCo inner/outer optimizer；
- model、dataset 和 checkpoint publication；
- merge 数学；
- staleness weighting；
- token accounting 的研究定义。

本次实施的核心是：

> **删除两套身份与 admission 语义，保留唯一的 stream/instance admission 协议和同一套训练算法；随后把 static-centered Full Protocol 验收迁移为 scheduler-authorized instance replacement。**

风险最高的修改区域是 `storage/authority.py`、唯一 DDL、`check_full_protocol_run.py` 和 independent replacement harness。

实施时，先确定 wire/schema 版本和唯一 config/descriptor，再修改 authority/runtime，最后迁移 harness、当前文档和正式实验。

## 完成条件

除 `plans/DONE/**`、`reports/**`、已有 artifacts，以及 `plans/00-RESEARCH_PLAN.md` 中明确标记为冻结 baseline 的历史叙述外，仓库级搜索不得再发现：

- `StaticContributorFence`、`StaticMembershipScope`、`StaticBinding` 或 static replacement API；
- membership/descriptor/authority/admission 中的 `mode` 选择；
- `static_learner_ids`、`sync.num_learners`、`schema_dynamic.sql` 或 `fence_kind`；
- 当前配置、launcher、Checker、README 或网页指向 `full_protocol_static.yaml` / `full_protocol_dynamic.yaml`；
- 只为旧模式存在的 alias、fallback、nullable field、test fixture、module-coverage entry 或生成的 API 文档。

最终候选必须通过以下检查：

1. 静态检查；
2. focused tests 和完整测试；
3. 唯一协议 harness；
4. PREFORMAL current-state 审查；
5. FINAL evidence 审查。

正式 experiment evidence 必须绑定同一个 clean target。
