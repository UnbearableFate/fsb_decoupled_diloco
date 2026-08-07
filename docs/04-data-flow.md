# 04 数据流:目录布局、文件格式与状态机

> 术语约定见 [00-glossary.md](00-glossary.md)。本文描述共享文件系统上的全部持久化数据。

## 1. 共享目录布局

一次 run 的 `shared_root` 由 `storage/paths.py: RunPaths` 定义。legacy runtime 用 `prepare_run_dirs()` 创建;HA initializer 用 `prepare_authority_dirs()`,learner 只用 `prepare_instance_dirs(instance_id)`,因此 learner 不会创建权威目录。dataclass 默认 null 会回退到项目根/cwd 下的 `runs/fs_diloco/<run_id>`;仓库正式配置显式使用主工作树绝对模板:

```
<shared_root>/
├── run_config.resolved.yaml         # 解析后的完整配置快照,放在 run 根便于直接检查
├── control/
│   ├── latest.json                  # 唯一全局指针(learner 轮询)
│   ├── stop.json                    # 停机标记(syncer 发布)
│   ├── summary.json                 # 完整训练时间与 learner 全训练资源峰值
│   ├── param_index.json             # 参数 ↔ 扁平向量映射契约
│   ├── run_config.resolved.yaml     # 同一配置快照的 control 副本,兼容恢复与工具路径
│   ├── source_identity.json          # 正式 launcher 写;git/fingerprint 结构化证据,非 runtime 必需
│   ├── source_identity.env           # 同一 identity 的 shell export,供 MPI ranks source
│   └── syncer_metadata.sqlite3      # 权威持久 DB(回滚日志,FULL sync)
├── weights/
│   └── global_v{VVVVVV}.safetensors # 全局权重(全量模式每版一份;分片模式为物化产物)
├── optim/
│   └── outer_v{VVVVVV}.safetensors  # 外层优化器状态(theta + momentum/exp_avg 等)
├── updates/
│   ├── latest/
│   │   ├── learner_{III}.json       # 全量模式固定提议指针,每 learner 恰好一份
│   │   └── learner_{III}_f{FFF}.json # 分片模式固定 per-(learner,fragment) 指针
│   └── payloads/learner_{III}/      # 仅不可变提议 tensor;元数据在固定指针
│       ├── <update_id>.params.safetensors                     # 全量模式
│       └── update_{uuid12}_fragment_{FFF}.params.safetensors  # 分片模式
├── fragments/                       # 分片模式专用
│   ├── fragment_index.json
│   ├── weights/fragment_{FFF}/v{VVVVVV}.safetensors
│   └── optim/fragment_{FFF}/v{VVVVVV}.safetensors
├── heartbeats/
│   └── learner_{III}.json           # 每 learner 一份,原子覆盖
├── eval_checkpoints/                 # 默认不存在;评审项 Q5(末端低 quorum 合并)的离线评估证据,非权威
│   ├── terminal_predecessor_v{V}.safetensors
│   └── terminal_predecessor_v{V}.manifest.json
├── logs/
│   ├── syncer.jsonl                 # JSONL 事件日志(每进程一份)
│   ├── learner_{III}.jsonl
│   ├── resume_*                     # resume 时归档的可恢复 stop/summary(不可读、reason 空或 error)
│   └── wandb/                       # W&B 本地目录
└── metrics/
    ├── syncer_metrics.csv
    ├── learner_metrics.csv
    ├── update_manifest.csv
    ├── update_history.jsonl         # applied/dropped 历史(append+fsync 后 DB 剪枝)
    ├── global_version_history.jsonl # 旧 global/fragment version 历史
    ├── validation_eval.json         # terminal latest 的 validation 主结果
    └── validation_terminal_predecessor_v{V}.json # 评审项 Q5 前驱评估结果,不覆盖主 attachment
```

SQLite 与 run 同生命周期,固定在 `control/syncer_metadata.sqlite3`。它不使用 WAL 或节点本地影子副本;计算节点切换后直接重开同一个文件。

HA full 在上述兼容布局之外增加以下权威面:

```text
<shared_root>/
├── control/
│   ├── run_descriptor.json / run_source_manifest.json / bootstrap_complete.json
│   ├── syncer_epochs/eNNNNNN/
│   │   ├── latest/vVVVVVV.json       # 不可变权威头部(每版本一个)
│   │   ├── latest/head.json          # 当前 epoch 权威头部
│   │   └── terminal/{stop,summary}_gNNNNNN.json
│   └── launch_claims/<observation>/attempt_NNNNNN.lock/
├── weights/epochs/eNNNNNN/<publication>/global_vVVVVVV.safetensors
├── optim/epochs/eNNNNNN/<publication>/outer_vVVVVVV.safetensors
├── heartbeats/syncer_epochs/eNNNNNN/<owner>.json
├── logs/syncer_candidates/<owner>.jsonl
└── logs/syncer_epochs/eNNNNNN/<owner>.jsonl
```

大文件路径在发布前即唯一确定且不复用;数据库只保存相对 run-root 路径、size、发布 ID 和可选摘要。`syncer_leader` 是 current token,`syncer_epochs` 保留/归档 epoch 历史,`control_publications` 把权威产物路径及 JSON SHA 绑定到 epoch 和 logical generation。后继者/Checker 用这些 DB 行;learner 不打开 SQLite,而是验证最高合法文件系统 epoch 的心跳/头部及头部内指针 SHA。fixed `control/latest.json`、`stop.json`、`summary.json` 仍会尽力更新,但 HA 读取器和 completed Checker 不以其内容作为权威。

dynamic HA 在同一布局再增加成员控制面:

```text
<shared_root>/
├── control/
│   ├── registration_requests/learner_li_<uuid4>.json
│   ├── bootstrap_scheduler_jobs.json
│   ├── dynamic_close_request.json
│   └── syncer_epochs/eNNNNNN_<owner>/
│       ├── membership/bootstrap_ready_g000001.json
│       ├── membership/admissions/learner_li_<uuid4>.json
│       └── terminal/drain_gNNNNNN.json
├── updates/latest/learner_li_<uuid4>.json
├── updates/payloads/learner_li_<uuid4>/...
├── heartbeats/learner_li_<uuid4>.json
└── metrics/{membership_event,learner_instance,registration,launch_request,capacity_observation}_history.jsonl
```

注册请求是 learner 单写者的准入申请;携带调度器 job ID 的请求在 launch 行尚无精确回执绑定时保持 DB pending 且保留源文件,不能提前 admit。epoch 准入与排空是 leader 发布并登记 SHA 的权威产物。active DB 行保持有界,已终态 instance/请求/观测先归档到上述 JSONL 再删除。dynamic 发现通过 `RunPaths` 的 mode-aware 迭代器扫描 UUID 路径,既不调用 static learner 白名单,也不递归扫描历史载荷。

`eval_checkpoints/` 只在显式研究开关开启且 input-closed terminal selection 低于 `quorum_min` 时创建。清单的 source version/checksum/selected/quorum 是评估溯源;它是证据包提交点。清单前的 checkpoint 是可校验、可原子覆盖的未提交中间态;清单存在后,缺失/损坏 checkpoint 或 identity/source checksum 冲突均 fail closed。目录不进入 DB、latest、resume 或 runtime GC 引用集合。

## 2. 文件格式详解

### 2.1 `control/latest.json`

全量模式(`runtime/syncer.py: latest_payload`):

```json
{
  "format_version": 1,
  "run_id": "...",
  "version": 12,
  "weight_path": ".../weights/global_v000012.safetensors",
  "optim_path": ".../optim/outer_v000012.safetensors",
  "param_index_path": ".../control/param_index.json",
  "created_at": 1752650000.0,
  "total_seen_tokens": 123456789
}
```

全量载荷还包含 `"total_update_tokens"`:**本次 merge** 中 selected updates 的 `tokens_this_update` 之和;`total_seen_tokens` 才是所有已提交 merge 的累计 token 数。prediction adoption 把前一 global 的 `total_update_tokens` 作为本轮全局进展 token 的初始估计,为 0 时用本地进展引导;resume 从 DB 恢复累计的 `total_seen_tokens`。

分片模式(`fragment_latest_payload`,`latest_kind` 用于区分):

```json
{
  "format_version": 1,
  "latest_kind": "fragment",
  "latest_layout_version": 2,
  "run_id": "...",
  "version": 37,
  "global_merge_event": 37,
  "param_index_path": ".../control/param_index.json",
  "fragment_index_path": ".../fragments/fragment_index.json",
  "materialized_weight_path": ".../weights/global_v000030.safetensors",
  "created_at": ...,
  "total_seen_tokens": ...,
  "fragments": {
    "0": {"version": 10, "weight_path": "...", "optim_path": "...", "updated_at_global_merge_event": 37},
    "1": {"version": 9,  ...},
    ...
  }
}
```

### 2.2 提议指针(提交标记)

全量 learner 的 `write_update()` 先创建不可变载荷,再把下面的元数据原子写到固定路径 `updates/latest/learner_XXX.json`。分片 learner 同样先写不可变 tensor,再原子替换每 `(learner, fragment)` 的 `updates/latest/learner_XXX_fNNN.json`,并追加 `update_kind/fragment_id/base_fragment_version/base_global_merge_event/tokens_since_fragment_load/fragment_norm` 字段;载荷目录不再保存元数据。新指针覆盖同 pair 旧指针,因此发现只需枚举全量模式 `N` 个或分片模式 `N×K` 个 JSON;SQLite 摄取水位在两种模式都防止重放,只有分片摄取额外用进程内 stat 签名跳过未变化指针的 JSON 解析,full 每轮仍读取固定 JSON 后由摄取水位拒绝同 ID。

| 字段 | 含义 |
|---|---|
| `format_version` / `run_id` | JSON 格式版本与所属 run(syncer 校验不匹配即忽略;DB identity 另含 `protocol_version`) |
| `update_id` | `learner_XXX_{local_step:08d}_{uuid12}`(分片:中间再加 `fXXX`) |
| `learner_id` / `hostname` / `pid` | 来源标识 |
| `base_global_version` | 通常是本区间出发时加载的版本;replace + 中途轮询时为最近一次中途采纳后的版本(陈旧度依据) |
| `local_step_start` / `local_step_end` / `inner_steps` | 区间步数信息 |
| `tokens_this_update` / `tokens_since_global_load` / `num_examples_this_update` | token/样本计量(合并权重依据) |
| `mid_cycle_adoption_count` / `base_switched_at_step` | full replace 路径在本区间中途轮询成功采纳的次数,以及最近一次切换前已完成的区间内步数(1 起);无切换恒为 `0` / `null` |
| `train_loss` / `grad_norm` / `param_norm` / `delta_norm` | 训练侧统计(`delta_norm` 当前恒为 null) |
| `tensor_dtype` | 原样记录 `io.tensor_dtype` 配置字符串(例如 `bfloat16` 或其别名);编解码器据此选择实际落盘 dtype,字段本身不做规范化 |
| `training_{cpu,gpu}_utilization_peak_percent` | 从 learner 启动至本 update 为止的 CPU/GPU 利用率最高值 |
| `local_cycle_{cpu,gpu}_utilization_peak_percent` | 本 update 对应的上一 local 训练周期 CPU/GPU 利用率最高值 |
| `local_cycle_step_time_seconds_mean` | 上一 local 训练周期内逐训练步耗时的算术平均值 |
| `local_cycle_step_count` / `local_cycle_resource_sample_count` | 上一周期的计时步数与资源采样数 |
| `file_path` / `file_size_bytes` / `sha256` | learner 提议张量指针(提议 SHA 仍由 `io.compute_sha256` 控制);HA global checkpoint 另由 `io.checkpoint_digest_mode` 控制摘要,默认不计算 |
| `created_at` / `committed_at` | learner 节点墙钟的张量写完时间 / 元数据提交时间;跨节点仅作研究证据与排序,不参与秒级截止时刻减法 |
| `ingested_at`(SQLite) | syncer 节点墙钟的首次入库时间;适合离线证据,不参与进程内自适应截止时刻 |

自适应宽限另维护有界的进程内 `update_id → first_seen_monotonic/first_seen_wall` 登记表。截止时刻只使用单调时钟值;墙钟值供诊断,不落入协议元数据。syncer resume 后登记表为空,旧 update 保守地不提供预计时间。

陈旧度加权仍把整份 full proposal 近似为基于单一 `base_global_version` 训练。replace + 中途轮询发生中途切换时,以上两个字段量化该近似,但不改变 token 统计或 merge 数学;fragment、rebase 与 predict 不使用这组字段表达其 reference 语义。

### 2.3 张量文件(safetensors)

| 文件 | 键 | 内容 |
|---|---|---|
| `global_v*.safetensors` | 每参数一键(参数名) | 按参数索引还原的命名权重;浮点 dtype 由 `syncer.publish_dtype` 决定 |
| `outer_v*.safetensors` | `theta` + 状态键(`step`,`momentum` 或 `exp_avg`/`exp_avg_sq`) | 外层优化器完整状态;浮点张量按 `syncer.publish_dtype` 发布,整数 step 保持 int64 |
| `update_*.params.safetensors` | `local_params` | learner 参数扁平向量(dtype 由 `io.tensor_dtype` 决定;50×10 对照配置即 50 内层步 × 10 外层步,使用 bfloat16) |
| `*_fragment_*.params.safetensors` | `fragment_params` | 直接从模型目标参数切片构造的单个分片(dtype 由 `io.tensor_dtype` 决定) |
| `fragments/weights/**` | `fragment_params` | syncer 发布的单个分片全局权重(dtype 由 `syncer.publish_dtype` 决定) |

update 文件可用 BF16 降低共享文件系统载荷;syncer 读取 `local_params` / learner fragment 后转换到 `syncer.compute_dtype`,并在 `syncer.device` 指定的 CPU/GPU 上做加权聚合和外层优化。

### 2.4 心跳 `heartbeats/learner_XXX.json`

`write_heartbeat()` 原子覆盖:`format_version, run_id, learner_id, hostname, pid, timestamp, status(active/stopped), phase(inner_steps/update_written/...), last_loaded_global_version, last_local_step, last_update_id, tokens_per_sec, learning_rate, scheduler_total_steps`;分片模式追加 `last_loaded_global_merge_event, last_loaded_fragment_versions, last_adopted_fragments`;update_written 阶段追加 local cycle 资源指标,stopped 阶段追加全训练 CPU/GPU 峰值、采样数与读取错误数,看门狗自保退出时附 `status_reason=syncer_unresponsive`。

full resume 会把切代时通过 run/learner/JSON 校验的指针内容 SHA256 记录在 `run_state.resume_generation.heartbeat_fences`。该隔离栅栏是恢复辅助状态,不是训练权威:只有字节内容完全相同的旧指针被忽略,任意合法的新原子替换都按本代心跳摄取。fragment final wait 使用 `active, phase=final_fragment_wait` 周期心跳,最后仍以 `stopped, phase=process_exit` 收束。

### 2.5 `control/stop.json`

`{format_version, run_id, reason, version, total_seen_tokens, timestamp}`;常见 `reason ∈ {stop_after_outer_steps, stop_after_global_tokens, input_exhausted, no_progress_timeout, completed, error}`。

### 2.6 CSV 指标(字段清单见 `observability/metrics.py`)

- `syncer_metrics.csv`:每次合并一行——版本/事件号、selected_count、token 数、read/aggregation/outer_step/publish/SQLite commit/maintenance/materialize 耗时、是否物化及字节数、`maintenance_scanned_rows/gc_pending_rows` 有界性指标、staleness min/mean/max、按有效 merge 权重计算的 `effective_staleness_mean`、fresh effective-weight mass 与 staleness count JSON、丢弃数、两次合并间隔,以及本次 selected learners 的资源指标均值。interval 使用单调时钟进一步分成 discovery/idle/grace/read/merge/publish/maintenance/residual,并记录 quorum trigger。full publish 的 `publish_ingest_passes` 只统计 checkpoint futures 未完成时真实调用回调的轮数;updates/heartbeats 是各轮插入数之和,seconds 只含回调墙钟时间。`sync.ingest_during_publish=false` 时四字段严格为 `0/0/0/0.0`。
- `learner_metrics.csv`:每次上传一行——loss、tokens、tokens/s、写盘耗时、显式 `local_cycle_elapsed_seconds`、param/fragment norm、已加载片版本,全训练/当前 local cycle 资源峰值和 cycle 平均步时间等。
- `update_manifest.csv`:每份 update 一行的清单(id、base 版本、步区间、`tensor_dtype`、文件指针与大小)。

`syncer_metrics.csv` 只有 syncer 写;后两张 learner CSV 由所有 learner 进程调用普通 append 无锁共享写入,没有显式 fsync 或跨进程 header/row 原子保证。它们是分析便利面,不参与提议提交、恢复或 exactly-once 判定;严格生命周期以 SQLite 和归档 JSONL 为准。

### 2.7 `control/summary.json`

syncer 停止后等待 learner 收尾,然后写入 `run_id, final_version, stop_reason, total_seen_tokens, training_started_at, training_completed_at, complete_training_time_seconds, all_learners_stopped`。`learner_resources` 包含逐 learner 的全训练 CPU/GPU 峰值、跨 learner 的 max/mean,并显式注明 CPU 是整节点利用率、GPU 是 learner CUDA 可见设备利用率。相同聚合也写入 W&B summary。

### 2.8 JSONL 日志 `logs/*.jsonl`

`JsonlLogger` 逐行追加 `{timestamp, actor, event_type, hostname, ...payload}` 并镜像到 stdout,fsync 落盘。关键事件类型:learner 侧 `process_start / loaded_global / update_written / global_adopted / fragments_adopted / heartbeat_written / error / process_exit`;其中采纳事件携带 load/apply、optimizer reset 与总 pause 三段计时,未来 latest 的纯等待另记。syncer 侧 `run_initialized / metadata_ingested / quorum_wait / updates_selected / outer_step_applied / global_published / terminal_predecessor_captured / updates_dropped / state_maintenance_completed / stop_published / no_progress_timeout / error`。

## 3. 更新生命周期状态机(SQLite 中跟踪)

```
                    learner 替换固定指针
                            │  syncer latest-wins 摄取
                            ▼
                        ┌─────────┐
       宽限窗口选中     │ pending │──────────────┐
     ┌──────────────────└─────────┘              │ 新指针到达 → dropped(superseded)
     ▼                       ▲                   │ 同 learner 更新被选 → dropped(superseded)
┌──────────┐  文件丢失回滚   │                   │ 张量文件消失      → dropped(missing_file)
│ selected │─────────────────┘                   ▼
└──────────┘                                ┌─────────┐
     │ 合并成功,记录 applied_version、      │ dropped │(记录 drop_reason)
     ▼ staleness、effective_weight          └─────────┘
┌─────────┐
│ applied │
└─────────┘
```

- 新指针若替换同 learner 的 pending 行,旧行先终态化;已 selected 行不会被新指针覆盖。摄取后即使旧 DB 行已归档,相同指针也会被摄取水位拒绝重放。
- 若指针在 syncer 首次读取前已再次被 learner 替换,被遮蔽的中间提议从未进入 SQLite,不发生状态转移或丢弃原因;其载荷在孤儿宽限期到期后由 maintenance 回收。固定指针协议提供 latest-wins 摄取,不提供每次 learner 发布的持久队列语义。
- 运行时字面丢弃原因是 `missing_file / superseded / too_stale / future_base`,正常闭合时剩余项还会使用停止原因(例如 `input_exhausted`)终态化。图中的「文件丢失回滚」表示缺失的 selected 项被 dropped、同批其余 selected 回到 pending,并非缺失项本身回到 pending。
- `pending → selected`、`selected → pending`(崩溃恢复/读取失败回滚)、`pending|selected → dropped` 都是带状态前置条件的转换;`applied/dropped` 不再回到活跃状态。
- 常量定义在 `core/constants.py`(另有 `failed` 状态常量,当前未使用)。
- 终态行先追加并 fsync 到 `metrics/update_history.jsonl`;随后在同一个 SQLite 事务中把载荷路径写入 `gc_pending` 并从活跃 DB 删除。对应载荷删除/确认不存在后清除 pending 行。归档允许崩溃重试形成重复行,分析时按 update identity 去重;运行时 GC 不回读随历史增长的 JSONL。
- runtime maintenance 只扫描不可变提议 tensor 与临时文件;元数据的唯一运行时发现面是 `updates/latest/` 固定指针,不扫描 `updates/payloads/**/.meta.json`。旧布局如需迁移必须由离线工具显式完成。

## 4. SQLite 表结构(`storage/schema.sql`)

业务写入由 syncer 单进程串行完成。连接强制 `journal_mode=DELETE`、`synchronous=FULL`、60 秒 busy timeout;打开/恢复时可执行 `integrity_check`。全量 global 提交使用显式事务,不是每语句提交。

| 表 | 作用 | 关键列/约束 |
|---|---|---|
| `run_state` | 键值杂项(如解析后配置) | `key` 主键,值为 JSON 文本 |
| `global_versions` | 每个全局版本一行 | `version` 主键;weight/optim 路径、num_updates、token 计数、status、notes |
| `learners` | 每 learner 最新快照 | `learner_id` 主键;last_seen、last_local_step、tokens_per_sec、status(+reason) |
| `proposal_frontiers` | 全量固定指针摄取水位 | `learner_id` 主键;最后观察到的 update_id/pointer_path |
| `fragment_proposal_frontiers` | 分片固定指针摄取水位 | `(learner_id, fragment_id)` 主键;最后观察到的 update_id/pointer_path |
| `updates` | 全量模式更新生命周期 | `update_id` 主键;`UNIQUE(learner_id, local_step_end, base_global_version)` 幂等去重;status 索引;learner 资源指标列与 mid-cycle adoption 元数据列;dynamic v3 追加 instance/placement/stream/admission/selection-generation 隔离栅栏 |
| `fragments` | 分片定义 | `fragment_id` 主键;strategy、numel、slices_json |
| `fragment_versions` | 每片每版本一行 | `(fragment_id, version)` 主键;global_merge_event 索引 |
| `fragment_updates` | 分片模式更新生命周期 | `UNIQUE(learner_id, fragment_id, local_step_end, base_fragment_version)`;`(fragment_id, status, base_fragment_version)` 索引;learner 资源指标列 |
| `gc_pending` | 已从活跃 update 表归档、但载荷物理删除尚未确认的持久队列 | `file_path` 主键;`archived_at`;归档后/删除前崩溃可继续回收 |
| `learner_instances`(v3) | dynamic 化身与 current 准入状态 | UUID 主键;placement/stream epoch、token hash、launch request、PBS job、last proposal、drain/final/expired 状态;current stream/placement 部分唯一索引 |
| `placements` / `streams`(v3) | 物理位置与固定数据流的 current owner | placement 主键保存 current epoch/instance/reusable stream;stream ID 主键保存 current epoch/instance/state |
| `registration_requests`(v3) | 注册 TTL、内容 hash、pending/幂等结果/tombstone | instance 主键;request hash、launch request、state、expiry、处理 epoch、rejection/result;scheduler receipt 未绑定时可持久 pending |
| `launch_requests`(v3) | bootstrap/scale logical request 和 PBS 发件箱 | request 主键;bootstrap slot 唯一;授权 placement epoch、scheduler 状态、job ID、reservation/admitted 映射 |
| `capacity_observations`(v3) | 幂等容量窗口与 scale 决策依据 | observation key 主键、sequence 唯一;eligible/selected/productive/reserved、low flag、close generation、writer epoch |

全量 `v → v+1` 的事务同时校验唯一已提交前驱、目标版本连续性、selected ID/learner 唯一性、future/stale 准入和归一化权重;dynamic 还逐项 join 并重验 current instance、placement epoch、stream epoch、admission generation/token 以及每 stream/placement 唯一性,并要求把唯一 `merge:<v+1>` capacity observation 与 global 行原子提交。随后插入唯一已提交 `global_versions(v+1)`,记录 selected 的 applied version/staleness/effective weight,并终态化 `superseded/too_stale/future_base` pending 行。无 merge 时,新的饥饿世代也与对应 observation 在一个事务中分配。任一校验或 failpoint 异常都会 rollback 整个事务,不留下 version 或 observation sequence 缺口。分片的 version、latest 和 applied 状态不是同一个事务,不能据此推导同等级恢复保证。

旧 run 的 `updates` 表缺少 mid-cycle 两列时,连接时幂等迁移补上 `mid_cycle_adoption_count INTEGER NOT NULL DEFAULT 0` 与 nullable `base_switched_at_step`;`fragment_updates` 不迁移这两列。

## 5. 端到端数据流小结

```
 数据集(HF WikiText-2,按 learner 连续分片)
   │ tokenize + 切块
   ▼
 learner GPU:inner_steps × AdamW
   │ 全量:按 param_index 扁平化;分片:只抽取目标参数切片
   │ 转为 io.tensor_dtype
   ▼
 updates/payloads/…/<update_id>.params.safetensors ──(先写,不可变)
 updates/latest/learner_XXX.json                   ──(后写/原子替换 = 提交)
   │ syncer 每轮读 N 个固定路径 → SQLite(pending)
   ▼
 资格筛选 → 每 learner 选一 → quorum/宽限窗口 → selected
   │ 读向量到 GPU
   ▼
 p̄ = Σ wᵢ pᵢ  →  g = θ − p̄  →  外层步进 θ′
   │
   ├─ weights/global_v{v+1}.safetensors + optim/outer_v{v+1}.safetensors
   ├─ SQLite 单事务:global_versions 行、updates → applied/dropped
   ├─ metrics 历史、logs、W&B
   ▼
 control/latest.json(原子覆盖,新版本全局可见)
   │ archive/GC:只保留 current checkpoint 与仍被引用的 payload
   │ learner 轮询
   ▼
 learner 按配置的 full 采纳策略采纳 θ′(或只采纳变化分片)→ 下一个区间
```
