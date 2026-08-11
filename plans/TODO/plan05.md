# 删除static learner mode, 让dynamic learner成为唯一模式

## 参照

1. 测试和实验阶段使用 `$miyabi-development` skill。
2. 主要原则遵循 `AGENTS.md`。
3. 实施流程遵循 `plans/workflow.md`。
4. 使用codegraph来掌握代码库知识

## 目的

删除static learner mode, 让dynamic learner成为唯一模式, 完成后, 将不会有选择让dynamic的分支和配置.

## 流程

1. 参考以下 `## 修改指南`删除static learner mode, 让dynamic learner成为唯一模式.
2. 修改涉及到的tests, 进入一个inactive computation node进行单元测试
3. 修改do_experiments中的实验代码, 重新实验, 并比较结果和修改前有没有显著的区别.

## 修改指南

### 1. 核心代码

| 阶段 | 文件 | 当前 static 依赖 | 建议修改 |
|---|---|---|---|
| P0 | [`core/versions.py`](fsb_decoupled_diloco/fs_diloco/core/versions.py) | 当前协议/schema 同时描述双模式 | schema 结构变化后提升 `AUTHORITY_SCHEMA_VERSION`；如果删除 fence 的 `kind` 字段或改变序列化格式，同时提升 `PROTOCOL_VERSION` |
| P1 | [`core/config.py`](fsb_decoupled_diloco/fs_diloco/core/config.py:149) | `membership.mode` 默认 static；存在 static/dynamic 分支校验 | 删除 `membership.mode`；`stream_pool_size`、`bootstrap_instances` 成为唯一 membership 配置；保留 `scaling.enabled` 控制是否自动扩缩 |
| P1 | [`core/run_descriptor.py`](fsb_decoupled_diloco/fs_diloco/core/run_descriptor.py:84) | 校验 descriptor 的 `mode` | 删除 `mode` 和双模式分派；始终验证 `stream_pool_size`、`bootstrap_slots` |
| P1 | [`tools/init_run.py`](fsb_decoupled_diloco/fs_diloco/tools/init_run.py:174) | 生成 `static_learner_ids` 或 `stream_pool_size` | 只生成 dynamic descriptor；删除 `static_learner_ids`、nullable 双模式字段和 `StaticMembershipScope` 分支 |
| P1 | [`storage/run_initializer.py`](fsb_decoupled_diloco/fs_diloco/storage/run_initializer.py:550) | 根据 descriptor mode 重建两种 scope | 只接受当前 dynamic descriptor；直接构造唯一 membership scope |
| P2 | [`protocol/contributor.py`](fsb_decoupled_diloco/fs_diloco/protocol/contributor.py:19) | `StaticContributorFence`、`DynamicContributorFence` union、两种 scope | 删除 `StaticContributorFence`、`StaticMembershipScope` 和 decoder 分支；建议将唯一 fence/scope 收敛为 `ContributorFence`、`MembershipScope` |
| P2 | [`protocol/authority.py`](fsb_decoupled_diloco/fs_diloco/protocol/authority.py:194) | `StaticBinding` 数据类型 | 删除 `StaticBinding` |
| P2 | [`protocol/cycle_receipt.py`](fsb_decoupled_diloco/fs_diloco/protocol/cycle_receipt.py:25) | receipt 接受两种 fence | 只接受唯一 dynamic contributor fence |
| P2 | [`protocol/proposal.py`](fsb_decoupled_diloco/fs_diloco/protocol/proposal.py:26) | proposal 接受两种 fence | 删除 static 类型判断和 union 校验 |
| P2 | [`storage/schema.sql`](fsb_decoupled_diloco/fs_diloco/storage/schema.sql:1) | 基础 schema 包含 mode、两种 fence kind、static binding 表 | 删除 `static_contributor_bindings`、`static_binding_history`；把 mode/fence-kind 约束收敛到唯一协议 |
| P2 | [`storage/schema_dynamic.sql`](fsb_decoupled_diloco/fs_diloco/storage/schema_dynamic.sql:1) | dynamic 目前是附加 schema | 合并进 `schema.sql`，随后删除该文件，形成唯一 DDL |
| P2 | [`storage/authority.py`](fsb_decoupled_diloco/fs_diloco/storage/authority.py:150) | schema mode 分派、static binding/history、两套 current-fence/terminal/fence-validation 事务 | 删除 static API 和 SQL；所有通用事务直接走 stream/instance/placement 路径；删除 `canonical_features(mode)`、`_schema_text(mode)` 等双模式抽象 |
| P2 | [`storage/admission.py`](fsb_decoupled_diloco/fs_diloco/storage/admission.py:74) | static registration、generation 恢复、replacement authorization、请求解析分支 | 删除全部 static request/replacement 函数；admission request 只保留 instance/stream/launch authorization 格式；`mode` 字段也可从 request 中删除 |
| P2 | [`storage/paths.py`](fsb_decoupled_diloco/fs_diloco/storage/paths.py:73) | `static_replacement_requests` 路径 | 删除目录属性、path builder 和初始化逻辑 |
| P2 | [`storage/artifact_policy.py`](fsb_decoupled_diloco/fs_diloco/storage/artifact_policy.py:124) | 声明 static replacement artifacts | 删除对应 policy 条目 |
| P3 | [`runtime/learner_entrypoint.py`](fsb_decoupled_diloco/fs_diloco/runtime/learner_entrypoint.py:25) | 同时接受 static identity 和 dynamic launch 参数 | 删除 `--learner-id`、`--logical-launch-id`、`--attempt-id`；只保留 bootstrap/launch-request admission |
| P3 | [`runtime/syncer_entrypoint.py`](fsb_decoupled_diloco/fs_diloco/runtime/syncer_entrypoint.py:83) | 构造两种 scope、两种 initial admission 条件 | 直接构造唯一 stream-pool scope；启动时始终初始化 dynamic membership |
| P3 | [`runtime/syncer.py`](fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:468) | `_admit_requests` 中约百行 static binding/replay/replacement 分支 | 删除 static admission 分支；response repair、actor identity、current admission 都直接使用 instance/stream 语义 |
| P3 | [`runtime/services/terminal.py`](fsb_decoupled_diloco/fs_diloco/runtime/services/terminal.py:245) | terminal ack 根据 fence kind 解析 actor | 只验证 `instance_id`；删除 static learner/attempt 分支 |
| P3 | [`tools/analysis.py`](fsb_decoupled_diloco/fs_diloco/tools/analysis.py:16) | 根据 descriptor 创建两种 scope并输出 mode | 只使用 stream-pool scope；删除 summary 中冗余的 membership mode |
| P3 | [`tools/launch_independent_run.py`](fsb_decoupled_diloco/fs_diloco/tools/launch_independent_run.py:168) | 分别生成 static learner 和 dynamic bootstrap qsub | 只生成 bootstrap-slot scalar jobs；删除 `FS_DILOCO_STATIC_LAUNCH_PREFIX` 和 learner index 路径 |
| P3 | [`tools/request_static_replacement.py`](fsb_decoupled_diloco/fs_diloco/tools/request_static_replacement.py:1) | 完全服务于 static replacement | 整个文件删除 |

### 2. 配置、PBS 和正式验收

| 文件 | 建议处理 |
|---|---|
| [`configs/full_protocol_static.yaml`](fsb_decoupled_diloco/configs/full_protocol_static.yaml) | 从主分支删除；通过 Git tag/commit 保留历史可复现性 |
| [`configs/full_protocol_dynamic.yaml`](fsb_decoupled_diloco/configs/full_protocol_dynamic.yaml) | 建议重命名为 `full_protocol.yaml`；删除 `membership.mode` |
| [`configs/full_protocol_functional.yaml`](fsb_decoupled_diloco/configs/full_protocol_functional.yaml:26) | 改为小规模 dynamic-only functional 配置 |
| [`scripts/miyabi/agent/run_learner.pbs`](fsb_decoupled_diloco/scripts/miyabi/agent/run_learner.pbs:22) | 删除 static shell 分支；只支持 bootstrap 或 authorized replacement |
| [`scripts/miyabi/agent/run_full_protocol_rank.sh`](fsb_decoupled_diloco/scripts/miyabi/agent/run_full_protocol_rank.sh:15) | 当前 co-allocated harness 完全按 static learner ID 启动；需要按 stream/bootstrap admission 重写，fault scenario 改成 dynamic incarnation loss/replacement |
| [`scripts/miyabi/agent/run_full_protocol_allocation.sh`](fsb_decoupled_diloco/scripts/miyabi/agent/run_full_protocol_allocation.sh:64) | 删除 static descriptor 要求，读取 `stream_pool_size/bootstrap_slots` |
| [`scripts/miyabi/agent/check_full_protocol_run.py`](fsb_decoupled_diloco/scripts/miyabi/agent/check_full_protocol_run.py:426) | 这是最大验证改动：删除 static binding/history 检查，改查 stream epoch、instance replacement、launch receipt、duplicate rejection、cursor continuity |
| [`scripts/miyabi/agent/run_full_protocol.pbs`](fsb_decoupled_diloco/scripts/miyabi/agent/run_full_protocol.pbs:20) | 默认配置改为唯一 `full_protocol.yaml`，调整 dynamic fault scenario/checker 参数 |
| [`scripts/miyabi/agent/run_independent_launcher.pbs`](fsb_decoupled_diloco/scripts/miyabi/agent/run_independent_launcher.pbs:18) | 默认配置改为 dynamic-only |
| [`scripts/miyabi/agent/submit_independent_8l1s_50x10.sh`](fsb_decoupled_diloco/scripts/miyabi/agent/submit_independent_8l1s_50x10.sh:30) | 改名和配置引用；说明其提交的是固定 stream pool，而不是 static learners |
| [`README.md`](fsb_decoupled_diloco/README.md:5) | 删除双模式说明；将 `scaling.enabled=false` 描述为固定容量运行，将 `true` 描述为自动 replacement/scale-out |

### 3. 测试修改

| 测试区域 | 文件 | 处理方式 |
|---|---|---|
| 直接删除 | [`test_static_contributor_binding.py`](fsb_decoupled_diloco/tests/storage/test_static_contributor_binding.py) | 仅验证已删除的 static binding |
| 直接删除 | [`test_request_static_replacement.py`](fsb_decoupled_diloco/tests/tools/test_request_static_replacement.py) | 仅验证已删除的 operator command |
| 配置 | [`test_config.py`](fsb_decoupled_diloco/tests/test_config.py:34) | 删除 mode/static shape 测试；增加 fixed-pool、bootstrap 和 scaling 组合验证 |
| 协议类型 | [`test_cycle_receipt.py`](fsb_decoupled_diloco/tests/protocol/test_cycle_receipt.py:24) | 全部改用唯一 dynamic fence |
| Learner admission | [`test_learner_entrypoint.py`](fsb_decoupled_diloco/tests/runtime/test_learner_entrypoint.py:12) | 删除 static admission 测试；验证旧 static CLI 参数不再存在、dynamic admission 在 Torch import 前完成 |
| Syncer admission | [`test_syncer_composition.py`](fsb_decoupled_diloco/tests/runtime/test_syncer_composition.py:53)、[`test_syncer_startup_admission.py`](fsb_decoupled_diloco/tests/runtime/test_syncer_startup_admission.py:54) | 删除 static replacement 场景；迁移为 bootstrap/replacement authorization、replay 和 duplicate-instance 场景 |
| Runtime service | [`test_syncer_fault_boundary.py`](fsb_decoupled_diloco/tests/runtime/test_syncer_fault_boundary.py)、[`test_terminal_service.py`](fsb_decoupled_diloco/tests/runtime/test_terminal_service.py) | 将 static scope/fence fixture 改为 dynamic incarnation |
| Authority schema | [`test_schema.py`](fsb_decoupled_diloco/tests/storage/test_schema.py:34) | 删除双 schema 参数化和 mode mismatch；验证唯一 schema、DDL hash、版本及 dynamic 表 |
| Authority 通用事务 | [`test_authority_operational.py`](fsb_decoupled_diloco/tests/storage/test_authority_operational.py)、[`test_leader_authority_commands.py`](fsb_decoupled_diloco/tests/storage/test_leader_authority_commands.py) | 这是测试迁移主体；把 static helper 和 fence 全部替换为 dynamic admission fixture，保留原事务、rollback、terminal、replay 断言 |
| Proposal/状态机 | `test_contributor_progress.py`、`test_proposal_adjudication.py`、`test_publication.py`、`test_state_machine.py`、`test_visibility.py` | 这些测试不是 static 专属，只是借用了便宜的 static fixture；应迁移，不能删除 |
| Admission 边界 | [`test_dynamic_admission_request.py`](fsb_decoupled_diloco/tests/storage/test_dynamic_admission_request.py) | 删除用于跨模式拒绝的 static payload，改测旧字段/未知字段被严格拒绝 |
| 工具 | [`test_analysis.py`](fsb_decoupled_diloco/tests/tools/test_analysis.py)、[`test_launch_independent_run.py`](fsb_decoupled_diloco/tests/tools/test_launch_independent_run.py) | 只验证 dynamic summary 和 bootstrap scalar qsub |
| Harness | [`test_full_protocol_harness.py`](fsb_decoupled_diloco/tests/harness/test_full_protocol_harness.py:1) | 将整个有效 checker fixture 改为 dynamic authority/instance/stream；fault evidence 改为 dynamic replacement |
| 验收索引 | [`module_coverage.json`](fsb_decoupled_diloco/tests/module_coverage.json)、[`test_plan_completion.py`](fsb_decoupled_diloco/tests/harness/test_plan_completion.py) | 删除 static requirement 映射，登记新的 dynamic-only owner |
| 公共 fixture | [`tests/support/protocol.py`](fsb_decoupled_diloco/tests/support/protocol.py:36) | 删除默认 `static_fence()`；提供完成 admission 后产生的标准 dynamic fence fixture |
| 架构约束 | [`test_authority_surface.py`](fsb_decoupled_diloco/tests/architecture/test_authority_surface.py:23) | 从公开 authority API allowlist 删除 static binding 方法 |
| Artifact policy | [`test_artifact_policy.py`](fsb_decoupled_diloco/tests/storage/test_artifact_policy.py) | 删除 static replacement artifact 分类断言 |

### 4. 研究文档与证据

| 项目 | 建议 |
|---|---|
| [`plans/00-RESEARCH_PLAN.md`](fsb_decoupled_diloco/plans/00-RESEARCH_PLAN.md:70) | 保留已有 static/dynamic matched-run 结果，但明确标记为冻结历史 baseline；后续实验使用 dynamic fixed/no-churn control |
| [`research roadmap`](fsb_decoupled_diloco/plans/DONE/plan03/fsb_decoupled_diloco_research_roadmap.md:292) | 将 `full/static/HA vs full/dynamic/HA` 改为 dynamic-only guarantee matrix |
| 同一 roadmap 的 baseline 表 | 将 `FS-DiLoCo full/static/HA` 替换为 `FS-DiLoCo dynamic/fixed-capacity/no-churn` |
| 已有 reports/artifacts | 不修改、不删除；通过 commit、source fingerprint、config 和 Checker 输出保留历史复现边界 |
| 新实验矩阵 | 固定 dynamic 协议，比较 no-failure、failure/no-replacement、failure/authorized-replacement 三组 |

### 5. 不应被波及的核心

以下部分理论上只消费通用 contributor fence/proposal，不需要改变算法行为：

- Learner 本地训练循环；
- DiLoCo inner/outer optimizer；
- model、dataset 和 checkpoint publication；
- merge 数学；
- staleness weighting；
- token accounting 的研究定义。

真正的工作重点可以概括为：

> **删除两套身份与 admission 语义，保留一套训练算法；然后把现有 static-centered Full Protocol 验收链迁移到 dynamic replacement。**

其中风险最高的是 `storage/authority.py` 和 `check_full_protocol_run.py`；最适合第一步处理的是 config、descriptor、protocol types 和 schema。