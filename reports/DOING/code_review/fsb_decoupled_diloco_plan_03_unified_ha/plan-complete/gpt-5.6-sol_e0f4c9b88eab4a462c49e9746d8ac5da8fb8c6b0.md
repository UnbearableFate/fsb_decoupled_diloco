# Plan 03 current-state 全量代码审查（Codex / GPT-5.6-sol）

- Base commit：`58a48c5f87f5ff311196e4d69dc6eecc6b81f0ba`
- Target commit：`e0f4c9b88eab4a462c49e9746d8ac5da8fb8c6b0`
- 审查类型：plan-complete current-state；base 只记录审查连续性，本报告没有把范围限制在 diff。
- 审查目标：target 中 `fs_diloco/` 的全部 tracked 文件，以及用来确认公开边界/当前调用关系的对应 tests、docs、config 和已完成 P6 证据。
- 结论：`CHANGES_REQUIRED`（1 High、2 Medium、1 Low；未发现 Critical）。

## 覆盖清单

逐模块检查了 target 当前状态的全部 tracked `fs_diloco/` 文件：

- 顶层/API：`__init__.py`、`cli.py`、`learner.py`、`syncer.py`、`analysis.py`、`eval_lm_harness.py`。
- core：`__init__.py`、`adoption_rules.py`、`config.py`、`config_v4.py`、`constants.py`、`run_descriptor.py`、`versions.py`。
- protocol：`__init__.py`、`_validation.py`、`authority.py`、`contributor.py`、`cycle_receipt.py`、`data_cursor.py`、`merge.py`、`proposal.py`、`scheduler.py`、`selection.py`、`token_accounting.py`。
- runtime：`__init__.py`、`adoption.py`、`learner_control.py`、`learner_entrypoint.py`、`learner_v4.py`、`pbs_scheduler.py`、`syncer_entrypoint.py`、`syncer_v4.py`，以及 services 的 `__init__.py`、`dynamic_capacity.py`、`maintenance.py`、`merge.py`、`terminal.py`。
- storage：`__init__.py`、`admission.py`、`artifact_policy.py`、`atomic_io.py`、`audit_archive.py`、`authority.py`、`control.py`、`leader_lease.py`、`object_store.py`、`paths.py`、`run_initializer.py`、`schema_v4.sql`、`schema_v4_dynamic.sql`、`tensor_codec.py`、`tensor_identity.py`、`terminal_request.py`。
- modeling：`__init__.py`、`hf_data.py`、`hf_model.py`、`outer_optim.py`、`param_index.py`、`training.py`。
- legacy query-only：`__init__.py`、`config_v1_v3.py`、`fragment_v0.py`、`reader.py`。
- baselines：`__init__.py`、`artifacts.py`、`health.py`、`protocol.py`、`train.py`。
- observability：`__init__.py`、`logging_utils.py`、`resource_monitor.py`、`wandb_logging.py`。
- tools：`__init__.py`、`analysis.py`、`authorize_static_replacement.py`、`check_workload_equivalence.py`、`clean_run.py`、`compare_event_traces.py`、`eval_lm_harness.py`、`init_run.py`、`launch_independent_run.py`、`migrate_config_v3_to_v4.py`、`paired_performance.py`、`publish_quality_gate.py`、`request_terminal_close.py`、`resolve_scheduler_uncertainty.py`、`run_metrics_csv.py`、`validation_eval.py`。

同时核对了 authority 两份 DDL 与 `LeaderAuthority` open-time schema/identity fingerprint，lease-renewer 使用独立 connection 的线程边界，所有 business write 的 transaction 内 token 复检，publication prepare/object/commit 顺序，terminal fence/token 收口，dynamic scheduler uncertainty/tombstone，initializer no-replace/hard-link manifest，audit archive/command receipt/GC 引用闭包，legacy query-only 输出隔离，baseline 与 full runtime 的依赖方向，以及 G0–G10、751-test full suite、10,000-cycle boundedness、两节点锁边界和两个 9-node gate 的最终共同 source evidence。

## 架构与已确认的正确边界

- 业务写权威已经收敛到 `storage.authority.LeaderAuthority/LeaderSession`；runtime 只持 typed read model 和显式粗粒度 command，没有 raw SQL escape 或 classic writer 分支。
- static/dynamic 共用 candidate、merge、publication、terminal 和 maintenance service；dynamic-only DDL/service 没有被强塞进 static authority。
- lease 的 DB wall-clock fence 与本地 monotonic safety tracker、transaction 内 token 复检、独立 renewer connection 形成完整的双层 fence。G7 对 transaction 内/外 SIGSTOP 的差异验证与实现一致。
- immutable object 校验使用 canonical relative path、非 symlink ancestor、inode/size/hash/schema/theta 二次核验；publication intent 和 commit 均受 fenced transaction 约束。
- audit batch/partition 与 command receipt 保留 append-only replay，hot authority 做 dependency-closed prune；current receipt closure 在 terminal 前受保护。G6 的 hot set、SQLite pages、active/recovery files 均有界，audit 线性增长被明确分域。
- legacy 模块只提供 query-only inspect/export/eval，production runtime 没有导入 legacy writer/DDL；fragment V0 只剩纯 decoder。
- baseline 路径与 unified authority 隔离，full runtime 的 Torch import 仍在 descriptor/admission 之后。

## High

### H1 — descriptor 声称冻结的 Hugging Face revision 没有传给实际 loader

证据：

- `fs_diloco/modeling/hf_model.py:69-79` 调用 `AutoTokenizer.from_pretrained` 和 `AutoModelForCausalLM.from_pretrained` 时只传 `trust_remote_code`/`dtype`，完全忽略 `ModelSection.revision` 与 `tokenizer_revision`。
- `fs_diloco/modeling/hf_data.py:70-88` 的 primary 与 `Salesforce/wikitext` fallback 都没有把 `DataSection.revision` 传给 `load_dataset`。
- `fs_diloco/tools/init_run.py:202-215` 却把这些字段写进 immutable descriptor；`plans/...unified_ha.md:639` 的 `ENV-01` 和 `docs/modules/modeling.md:3` 明确宣称 runtime 按 frozen revision 加载。

影响：配置/descriptor/attestation 可以声明 revision A，但实际模型、tokenizer 和数据从默认 branch HEAD 解析为 B；不同 actor 或不同时间的 replacement 可能使用不同 bytes，而 authority 仍把它们视作同一 run identity。这破坏 reproducibility 和 ENV-01 的 identity contract，并会让 cursor/dataset identity 与实际 block stream 脱节。

修复建议：loader 必须显式传 `model.revision`、`tokenizer_revision or model.revision` 和 `data.revision`，primary/fallback 使用同一 revision；新增 mock-based RED，分别断言 model、tokenizer、primary dataset、WikiText fallback 的 exact revision 参数，并覆盖 tokenizer fallback-to-model revision。

缺失测试：当前只有 initializer descriptor identity 测试，没有 producer→loader 参数闭环测试。

## Medium

### M1 — non-synthetic Full v4 允许缺失或可移动 revision，`ENV-01` 仍不能真正成立

证据：`fs_diloco/core/config.py:127-142` 三个 revision 字段默认 `None`；`config_v4._validate_shared` 没有对 non-synthetic model/dataset 要求 immutable commit revision。当前 tracked GPT-2/WikiText full configs 也未显式填写 revision。即使修复 H1，传 `None` 仍会解析移动的默认 branch。

影响：fresh v4 run 可以合法产生 descriptor 中 revision 为 `null` 的“冻结 identity”，随后 replacement/retry 加载不同 Hub 内容。H1 修复不能单独闭合这一点。

修复建议：为 Hub-backed non-synthetic v4 输入要求明确 immutable commit SHA，并给 repository-owned GPT-2/WikiText configs 写入已验证的 exact model/tokenizer/dataset commit；local/synthetic 输入应有明确豁免而不是把任意 `None` 当作冻结。query-only legacy projection不应因此失去可读性。RED 必须证明 unpinned full v4 被拒绝、synthetic 仍通过、tracked full configs 都满足 pinning；baseline 是否同样强制应保持 BASE-01 兼容并在测试中明确。

### M2 — final tree 仍保留会重新引入旧行为的生产兼容/死代码

证据：

- `protocol/merge.py:66-109` 的 `select_one_per_learner`/`stale_update_ids` 没有 production caller；前者正是 architecture review 已指出会按低 ID 截断、产生 starvation 的旧选择逻辑，只由旧单测维持。
- `storage/authority.py:1603-1608` 的 public `record_proposal` 是早期 P1 alias，production 只用 `ingest_proposal`，但 architecture test 反而把重复 mutator spelling 冻结在公开 authority surface。
- `protocol/authority.py:284-300` 的扁平 `DynamicAdmission` compatibility accessor 只服务早期测试。
- `runtime/syncer_v4.py:860-875` 仍在 canonical fence-namespaced receipt 缺失时读取 early unnamespaced receipt layout；`storage/admission.py:1182-1188` 仍扫描 early P4 admission layout。计划的最终边界是不支持旧 incomplete run 原地 resume，这些 fallback 没有 current producer 或专门兼容承诺。

影响：公开保留 known-unfair helper 和重复 writer API 增加误调用/回归面；production silently 接受未声明的 early-development layout，模糊 fresh-v4/fail-closed 边界。它们不是旧完成 run 的 query-only 支持。

修复建议：删除这些 alias/helper/fallback，更新测试只覆盖 canonical typed API/layout；新增 architecture RED，断言 authority 只有 `ingest_proposal` spelling、runtime 不出现 unnamespaced receipt fallback、known-unfair helper 不再可导入。不要删除 `legacy/` 的 query-only decoder。

## Low

### L1 — `core/constants.py` 与 `core/versions.py` 并存一套无人使用且数值过时的 protocol 常量

证据：`core/constants.py:5-33` 仍定义 generic `FORMAT_VERSION`、`PROTOCOL_VERSION=3`、旧 schema/control/member status 和 fixed checkpoint template；当前 authority 使用 `core/versions.py` 的 `PROTOCOL_VERSION=4`/schema 9。除 `FORMAT_VERSION` 被 `modeling/param_index.py:11,36,50` 使用、`DEFAULT_RUNS_DIR` 和两个只服务 dead fixed-path method 的 template 外，其余旧符号没有 repo caller。`storage/paths.py:209-213` 的 fixed `global_weight_path`/`outer_optim_path` 也无 caller，current publication 只用 epoch-scoped paths。

影响：同包存在两个同名、不同值的 protocol constant，未来 import 选错会静默制造 v3 marker；generic param-index version 违背 `core/versions.py` 已声明的 independently-versioned boundary。

修复建议：param index 改用 `PARAM_INDEX_FORMAT_VERSION`；删除过时常量、dead fixed path methods/template 与其他确认无 caller 的旧初始化 helper，只保留真实共享常量。用 import/static test 防止再次出现第二套 protocol version。

## 验证要求

1. 每个接受的行为 finding 先增加能在 target 上失败的 RED；运行环境相关测试只在 PBS compute node执行。
2. H1/M1 会改变外部输入 identity/config，至少重跑 config/model/data focused tests、完整 suite、G0/G1 source/config gate和受影响的 runtime acceptance；若 canonical source fingerprint 改变，所有最终 P6 evidence 必须按 Checker 的同源规则重新生成，不能沿用 `dea559a...`。
3. M2/L1 删除 public/dead surface 后重跑 architecture、authority、protocol、runtime、legacy query-only、baseline regression，并确认 fragment/classic writer symbol scan仍为零。
4. 修缮后冻结新 target，以本报告 target 为 base 做增量 Codex review；Claude 若仍为可核验 session-limit 可按规则跳过，其他失败不能伪装成 skip。

## 最终结论

`CHANGES_REQUIRED`。HA/persistence 主链和 P6 证据整体强，但 Hugging Face revision 的 producer/consumer 断链直接违反 ENV-01；final tree 还应删除会重新引入旧选择/布局/API 的兼容死代码和过时常量后，才满足 plan-complete。
