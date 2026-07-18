# 04 数据流:目录布局、文件格式与状态机

## 1. 共享目录布局

一次 run 的 `shared_root`(默认 `runs/fs_diloco/<RUN_ID>/`)由 `storage/paths.py: RunPaths` 定义、`prepare_run_dirs()` 创建:

```
<shared_root>/
├── run_config.resolved.yaml         # 解析后的完整配置快照,放在 run 根便于直接检查
├── control/
│   ├── latest.json                  # 唯一全局指针(learner 轮询)
│   ├── stop.json                    # 停机标记(syncer 发布)
│   ├── summary.json                 # 完整训练时间与 learner 全训练资源峰值
│   ├── param_index.json             # 参数 ↔ 扁平向量映射契约
│   ├── run_config.resolved.yaml     # 同一配置快照的 control 副本,兼容恢复与工具路径
│   └── syncer_metadata.sqlite3      # 权威持久 DB(rollback journal,FULL sync)
├── weights/
│   └── global_v{VVVVVV}.safetensors # 全局权重(全量模式每版一份;fragment 模式为 materialize 产物)
├── optim/
│   └── outer_v{VVVVVV}.safetensors  # 外层优化器状态(theta + momentum/exp_avg 等)
├── updates/
│   ├── latest/
│   │   ├── learner_{III}.json       # 全量模式固定 proposal pointer,每 learner 恰好一份
│   │   └── learner_{III}_f{FFF}.json # fragment 模式固定 per-(learner,fragment) pointer
│   └── payloads/learner_{III}/      # 仅不可变 proposal tensor；metadata 在固定 pointer
│       ├── <update_id>.params.safetensors
│       └── <update_id>_f{FFF}.params.safetensors
├── fragments/                       # fragment 模式专用
│   ├── fragment_index.json
│   ├── weights/fragment_{FFF}/v{VVVVVV}.safetensors
│   └── optim/fragment_{FFF}/v{VVVVVV}.safetensors
├── heartbeats/
│   └── learner_{III}.json           # 每 learner 一份,原子覆盖
├── eval_checkpoints/                 # 默认不存在；Q5 terminal-partial 离线证据，非权威
│   ├── terminal_predecessor_v{V}.safetensors
│   └── terminal_predecessor_v{V}.manifest.json
├── logs/
│   ├── syncer.jsonl                 # JSONL 事件日志(每进程一份)
│   ├── learner_{III}.jsonl
│   └── wandb/                       # W&B 本地目录
└── metrics/
    ├── syncer_metrics.csv
    ├── learner_metrics.csv
    ├── update_manifest.csv
    ├── update_history.jsonl         # applied/dropped 历史(append+fsync 后 DB 剪枝)
    ├── global_version_history.jsonl # 旧 global/fragment version 历史
    ├── validation_eval.json         # terminal latest 的 validation 主结果
    └── validation_terminal_predecessor_v{V}.json # Q5 前驱结果，不覆盖主 attachment
```

SQLite 与 run 同生命周期、固定在 `control/syncer_metadata.sqlite3`。它不使用 WAL 或节点本地 shadow copy;计算节点切换后直接重开同一个文件。

`eval_checkpoints/` 只在显式研究开关开启且 input-closed terminal selection 低于
`quorum_min` 时创建。manifest 的 source version/checksum/selected/quorum 是评估溯源；
目录不进入 DB、latest、resume 或 runtime GC 引用集合。

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

fragment 模式(`fragment_latest_payload`,`latest_kind` 用于区分):

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

### 2.2 proposal pointer（提交标记）

全量 learner 的 `write_update()` 先创建不可变 payload,再把下面的 metadata 原子写到固定路径 `updates/latest/learner_XXX.json`。fragment learner 同样先写不可变 tensor，再原子替换每 `(learner, fragment)` 的 `updates/latest/learner_XXX_fNNN.json`，并追加 `update_kind/fragment_id/base_fragment_version/base_global_merge_event/tokens_since_fragment_load/fragment_norm` 字段；payload 目录不再保存 metadata。新 pointer 覆盖同 pair 旧 pointer，因此 discovery 只枚举全量模式 `N` 个或 fragment 模式 `N×K` 个 JSON；SQLite frontier 防止重放，进程内文件 signature 让未变化 pointer 不重复解析。

| 字段 | 含义 |
|---|---|
| `format_version` / `run_id` | JSON 格式版本与所属 run(syncer 校验不匹配即忽略;DB identity 另含 `protocol_version`) |
| `update_id` | `learner_XXX_{local_step:08d}_{uuid12}`(fragment:中间再加 `fXXX`) |
| `learner_id` / `hostname` / `pid` | 来源标识 |
| `base_global_version` | 通常是本区间出发时加载的版本；replace + inner poll 时为最近一次中途采纳后的版本(staleness 依据) |
| `local_step_start` / `local_step_end` / `inner_steps` | 区间步数信息 |
| `tokens_this_update` / `tokens_since_global_load` / `num_examples_this_update` | token/样本计量(合并权重依据) |
| `mid_cycle_adoption_count` / `base_switched_at_step` | full replace 路径在本区间 inner poll 成功采纳的次数，以及最近一次切换前已完成的区间内 step 数(1-based)；无切换恒为 `0` / `null` |
| `train_loss` / `grad_norm` / `param_norm` / `delta_norm` | 训练侧统计(`delta_norm` 当前恒为 null) |
| `tensor_dtype` | learner update 的实际落盘 dtype(例如 `bfloat16`) |
| `training_{cpu,gpu}_utilization_peak_percent` | 从 learner 启动至本 update 为止的 CPU/GPU 利用率最高值 |
| `local_cycle_{cpu,gpu}_utilization_peak_percent` | 本 update 对应的上一 local 训练周期 CPU/GPU 利用率最高值 |
| `local_cycle_step_time_seconds_mean` | 上一 local 训练周期内逐训练 step 耗时的算术平均值 |
| `local_cycle_step_count` / `local_cycle_resource_sample_count` | 上一周期的计时 step 数与资源采样数 |
| `file_path` / `file_size_bytes` / `sha256` | 张量文件指针(sha256 仅在 `io.compute_sha256` 开启时计算) |
| `created_at` / `committed_at` | learner 节点 wall clock 的张量写完时间 / 元数据提交时间；跨节点仅作研究证据与排序，不参与秒级 deadline 减法 |
| `ingested_at`（SQLite） | syncer 节点 wall clock 的首次入库时间；适合离线证据，不参与进程内 adaptive deadline |

adaptive grace 另维护有界的进程内 `update_id → first_seen_monotonic/first_seen_wall` registry。deadline 只使用 monotonic 值；wall 值供诊断，不落入协议元数据。syncer resume 后 registry 为空，旧 update 保守地不提供 ETA。

staleness 加权仍把整份 full proposal 近似为基于单一 `base_global_version` 训练。replace + inner poll 发生中途切换时，以上两个字段量化该近似，但不改变 token 统计或 merge 数学；fragment、rebase 与 predict 不使用这组字段表达其 reference 语义。

### 2.3 张量文件(safetensors)

| 文件 | 键 | 内容 |
|---|---|---|
| `global_v*.safetensors` | 每参数一键(参数名) | 按 param index 还原的命名权重；浮点 dtype 由 `syncer.publish_dtype` 决定 |
| `outer_v*.safetensors` | `theta` + 状态键(`step`,`momentum` 或 `exp_avg`/`exp_avg_sq`) | 外层优化器完整状态；浮点张量按 `syncer.publish_dtype` 发布，整数 step 保持 int64 |
| `update_*.params.safetensors` | `local_params` | learner 参数扁平向量(dtype 由 `io.tensor_dtype` 决定;50x10 配置为 bfloat16) |
| `*_fragment_*.params.safetensors` | `fragment_params` | 直接从模型目标参数切片构造的单个 fragment(dtype 由 `io.tensor_dtype` 决定) |
| `fragments/weights/**` | `fragment_params` | syncer 发布的单个 fragment 全局权重(dtype 由 `syncer.publish_dtype` 决定) |

update 文件可用 BF16 降低共享文件系统 payload;syncer 读取 `local_params` / learner fragment 后转换到 `syncer.compute_dtype`,并在 `syncer.device` 指定的 CPU/GPU 上做加权聚合和外层优化。

### 2.4 心跳 `heartbeats/learner_XXX.json`

`write_heartbeat()` 原子覆盖:`format_version, run_id, learner_id, hostname, pid, timestamp, status(active/stopped), phase(inner_steps/update_written/...), last_loaded_global_version, last_local_step, last_update_id, tokens_per_sec`;fragment 模式追加 `last_loaded_global_merge_event, last_loaded_fragment_versions, last_adopted_fragments`;update_written 阶段追加 local cycle 资源指标,stopped 阶段追加全训练 CPU/GPU 峰值、采样数与读取错误数。

### 2.5 `control/stop.json`

`{format_version, run_id, reason, version, total_seen_tokens, timestamp}`;常见 `reason ∈ {stop_after_outer_steps, stop_after_global_tokens, input_exhausted, no_progress_timeout, completed, error}`。

### 2.6 CSV 指标(字段清单见 `observability/metrics.py`)

- `syncer_metrics.csv`:每次合并一行——版本/事件号、selected_count、token 数、read/aggregation/outer_step/publish/SQLite commit/maintenance/materialize 耗时、是否物化及字节数、`maintenance_scanned_rows/gc_pending_rows` 有界性指标、staleness min/mean/max、按有效 merge 权重计算的 `effective_staleness_mean`、fresh effective-weight mass 与 staleness count JSON、丢弃数、两次合并间隔,以及本次 selected learners 的资源指标均值。interval 使用 monotonic clock 进一步分成 discovery/idle/grace/read/merge/publish/maintenance/residual，并记录 quorum trigger；full publish 还记录 I/O future 等待期间摄取 metadata/heartbeat 的次数与耗时。
- `learner_metrics.csv`:每次上传一行——loss、tokens、tokens/s、写盘耗时、显式 `local_cycle_elapsed_seconds`、param/fragment norm、已加载片版本,全训练/当前 local cycle 资源峰值和 cycle 平均 step 时间等。
- `update_manifest.csv`:每份 update 一行的清单(id、base 版本、步区间、`tensor_dtype`、文件指针与大小)。

### 2.7 `control/summary.json`

syncer 停止后等待 learner 收尾,然后写入 `run_id, final_version, stop_reason, total_seen_tokens, training_started_at, training_completed_at, complete_training_time_seconds, all_learners_stopped`。`learner_resources` 包含逐 learner 的全训练 CPU/GPU 峰值、跨 learner 的 max/mean,并显式注明 CPU 是整节点利用率、GPU 是 learner CUDA 可见设备利用率。相同聚合也写入 W&B summary。

### 2.8 JSONL 日志 `logs/*.jsonl`

`JsonlLogger` 逐行追加 `{timestamp, actor, event_type, hostname, ...payload}` 并镜像到 stdout,fsync 落盘。关键事件类型:learner 侧 `process_start / loaded_global / update_written / global_adopted / fragments_adopted / heartbeat_written / error / process_exit`;其中 adoption 事件携带 load/apply、optimizer reset 与总 pause 三段计时，未来 latest 的纯等待另记。syncer 侧 `run_initialized / metadata_ingested / quorum_wait / updates_selected / outer_step_applied / global_published / terminal_predecessor_captured / updates_dropped / state_maintenance_completed / stop_published / no_progress_timeout / error`。

## 3. update 生命周期状态机(SQLite 中跟踪)

```
                    learner 替换固定 pointer
                            │  syncer latest-wins 摄取
                            ▼
                        ┌─────────┐
       宽限窗口选中     │ pending │──────────────┐
     ┌──────────────────└─────────┘              │ 新 pointer 到达 → dropped(superseded)
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

- 新 pointer 若替换同 learner 的 pending 行,旧行先终态化;已 selected 行不会被新 pointer 覆盖。摄取后即使旧 DB 行已归档,相同 pointer 也会被 frontier 拒绝重放。
- `pending → selected`、`selected → pending`(崩溃恢复/读取失败回滚)、`* → dropped` 都是带状态前置条件的转换。
- 常量定义在 `core/constants.py`(另有 `failed` 状态常量,当前未使用)。
- 终态行先追加并 fsync 到 `metrics/update_history.jsonl`;随后在同一个 SQLite 事务中把 payload 路径写入 `gc_pending` 并从活跃 DB 删除。对应 payload 删除/确认不存在后清除 pending 行。archive 允许崩溃重试形成重复行,分析时按 update identity 去重；运行时 GC 不回读随历史增长的 JSONL。

## 4. SQLite schema(`storage/schema.sql`)

业务写入由 syncer 单进程串行完成。连接强制 `journal_mode=DELETE`、`synchronous=FULL`、60 秒 busy timeout;打开/恢复时可执行 `integrity_check`。全量 global commit 使用显式事务,不是每语句提交。

| 表 | 作用 | 关键列/约束 |
|---|---|---|
| `run_state` | 键值杂项(如解析后配置) | `key` 主键,值为 JSON 文本 |
| `global_versions` | 每个全局版本一行 | `version` 主键;weight/optim 路径、num_updates、token 计数、status、notes |
| `learners` | 每 learner 最新快照 | `learner_id` 主键;last_seen、last_local_step、tokens_per_sec、status(+reason) |
| `proposal_frontiers` | 全量固定 pointer 摄取水位 | `learner_id` 主键;最后观察到的 update_id/pointer_path |
| `fragment_proposal_frontiers` | fragment 固定 pointer 摄取水位 | `(learner_id, fragment_id)` 主键;最后观察到的 update_id/pointer_path |
| `updates` | 全量模式 update 生命周期 | `update_id` 主键;`UNIQUE(learner_id, local_step_end, base_global_version)` 幂等去重;status 索引;learner 资源指标列与 mid-cycle adoption 元数据列 |
| `fragments` | fragment 定义 | `fragment_id` 主键;strategy、numel、slices_json |
| `fragment_versions` | 每片每版本一行 | `(fragment_id, version)` 主键;global_merge_event 索引 |
| `fragment_updates` | fragment 模式 update 生命周期 | `UNIQUE(learner_id, fragment_id, local_step_end, base_fragment_version)`;`(fragment_id, status, base_fragment_version)` 索引;learner 资源指标列 |

全量 `v → v+1` 的事务同时校验唯一 committed 前驱、目标版本连续性、selected ID/learner 唯一性、future/stale 准入和归一化权重;随后插入唯一 committed `global_versions(v+1)`,记录 selected 的 applied version/staleness/effective weight,并终态化 superseded/stale/future pending 行。任一校验或 failpoint 异常都会 rollback 整个事务。

旧 run 的 `updates` 表缺少 mid-cycle 两列时，connect-time 幂等迁移补上 `mid_cycle_adoption_count INTEGER NOT NULL DEFAULT 0` 与 nullable `base_switched_at_step`；`fragment_updates` 不迁移这两列。

## 5. 端到端数据流小结

```
 数据集(HF WikiText-2,按 learner 连续分片)
   │ tokenize + 切块
   ▼
 learner GPU:inner_steps × AdamW
   │ 全量:按 param_index flatten;fragment:只抽取目标参数切片
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
 learner 整体加载 θ′、重置内层优化器 → 下一个区间
```
