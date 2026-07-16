# 配置参数参考 / Configuration Reference

[返回总览 / Back to index](00-README.zh-en.md)

中文：逐项说明 YAML 配置组和关键参数含义。

English: Detailed YAML config groups and parameter meanings.

---

# 中文

## 配置参数说明

### `run`

| 参数 | 含义 |
| --- | --- |
| `name` | run 名称，用于默认 `RUN_ID` 后缀。 |
| `run_id` | 显式 run ID；为 `null` 时自动生成 `<timestamp>_<name>`。 |
| `shared_root` | 共享 run 根目录；为 `null` 时使用 `$PROJECT_ROOT/runs/fs_diloco/$RUN_ID`。 |
| `log_level` | 预留日志级别；当前主要使用 JSONL event logging。 |

### `init`

| 参数 | 含义 |
| --- | --- |
| `resume` | 是否恢复已有 run。 |
| `resume_version` | `latest` 或整数版本号。 |
| `resume_db_dump` | 可选 SQLite dump 路径；为空时尝试选择最新兼容 dump。 |
| `allow_overwrite_existing_run` | `resume=false` 且 run 目录已有 `latest.json` 时，是否允许覆盖。默认 false。 |

### `model`

| 参数 | 含义 |
| --- | --- |
| `name_or_path` | Hugging Face model name/path；默认 `gpt2`。`synthetic-tiny` 用于 smoke。 |
| `trust_remote_code` | 是否允许 HF remote code。默认 false。 |
| `dtype` | 模型加载 dtype，例如 `bfloat16`。 |
| `compile` | 是否 `torch.compile` 模型。默认 false。 |
| `synthetic_vocab_size` | synthetic tiny model vocab size。 |
| `synthetic_hidden_size` | synthetic tiny model hidden size。 |

### `data`

| 参数 | 含义 |
| --- | --- |
| `dataset_name` | HF dataset 名称，默认 `wikitext`。也支持 `synthetic`。 |
| `dataset_config_name` | dataset config，WikiText-2 使用 `wikitext-2-raw-v1`。 |
| `train_split` | 训练 split。 |
| `validation_split` | 预留验证 split。 |
| `block_size` | packed causal LM block length，同时会同步到 `training.block_size`。 |
| `num_proc` | 预留 tokenization 并行参数。 |
| `cache_dir` | HF dataset cache 路径。 |
| `streaming` | 是否使用 streaming dataset。 |
| `synthetic_num_batches` | synthetic 数据配置预留。 |

### `sync`

| 参数 | 含义 |
| --- | --- |
| `num_learners` | learner 总数。PBS 9-node 配置为 8。 |
| `upload_mode` | 当前实现使用 `params`，即完整参数向量。 |
| `quorum_min` | 开始一个 outer step 所需最少 eligible learner 数。 |
| `quorum_max` | 一个 outer step 最多选多少 learner。 |
| `max_staleness_versions` | `current_version - base_global_version` 超过该值的 update 会被 drop。 |
| `staleness_lambda` | staleness 权重衰减系数。 |
| `selection_policy` | 默认 `most_recent_per_learner`；也支持 `oldest_pending` ablation。 |
| `scan_interval_seconds` | syncer 扫描 heartbeat/update 的 sleep 间隔。 |
| `grace_window.fixed_seconds` | 达到 quorum 后继续等待更多 update 的固定时间。 |
| `grace_window.max_seconds` | grace window 上限。 |
| `db_dump_every_versions` | 每多少个 global version 备份一次 SQLite。 |
| `stop_after_outer_steps` | 达到该 global version 后停止。 |
| `stop_after_global_tokens` | 按累计 selected token 数停止；`null` 表示禁用。 |
| `stop_file_poll_seconds` | 预留 stop polling 间隔。 |

### `liveness`

| 参数 | 含义 |
| --- | --- |
| `heartbeat_interval_seconds` | learner 周期 heartbeat 间隔。 |
| `stale_after_seconds` | heartbeat 超过该年龄后 learner 标为 stale。 |
| `dead_after_seconds` | heartbeat 超过该年龄后 learner 标为 dead。 |
| `no_progress_timeout_seconds` | 长时间无法达到 quorum 时 syncer 写 stop 并退出。 |
| `quorum_policy` | 当前实现为 fixed，不根据 dead learner 自动降低 quorum。 |

### `training`

| 参数 | 含义 |
| --- | --- |
| `inner_steps` | 每个 upload interval 的本地 optimizer step 数。 |
| `micro_batch_size` | 每次 forward 的 batch size。 |
| `gradient_accumulation_steps` | 梯度累积步数。 |
| `block_size` | causal LM 序列长度。 |
| `max_local_steps` | learner 本地最大 step；`null` 表示仅由 stop file 控制。 |
| `precision` | `bf16` 时 CUDA 上使用 bfloat16 autocast。 |
| `seed` | base random seed，learner 会加上 learner index。 |
| `log_every_steps` | learner loss summary 日志间隔。 |
| `grad_clip` | 可选 gradient norm clipping。 |

### `inner_optimizer`

| 参数 | 含义 |
| --- | --- |
| `name` | 当前 learner 仅支持 `adamw`。 |
| `lr` | inner learning rate。 |
| `betas` | AdamW beta 参数。 |
| `eps` | AdamW epsilon。 |
| `weight_decay` | inner weight decay。 |
| `scheduler` | `cosine` 或 `none`。 |
| `warmup_steps` | warmup step 数。 |
| `reset_on_global_update` | 设计语义：采用新全局版本时重置 optimizer；当前 learner 始终按此语义执行。 |

### `outer_optimizer`

| 参数 | 含义 |
| --- | --- |
| `name` | `sgd`、`momentum`、`nesterov` 或 `adamw`。 |
| `lr` | outer learning rate。 |
| `momentum` | momentum / Nesterov 系数。 |
| `weight_decay` | outer weight decay。 |
| `betas` | AdamW-style outer optimizer beta 参数。 |
| `eps` | AdamW-style epsilon。 |

### `io`

| 参数 | 含义 |
| --- | --- |
| `tensor_dtype` | learner update tensor dtype，默认 `float32`。 |
| `atomic_write` | 设计开关；当前关键写入始终使用 atomic helpers。 |
| `compute_sha256` | 是否计算 update tensor SHA256；大模型下默认 false。 |
| `keep_processed_updates` | 预留清理策略。 |
| `cleanup_applied_after_versions` | 预留：应用后保留多少版本再清理。 |
| `sqlite_local_dir` | syncer-local SQLite 目录；为空时用 `${TMPDIR:-/tmp}/fs_diloco/$RUN_ID`。 |

### `learner`

| 参数 | 含义 |
| --- | --- |
| `poll_latest_during_inner_steps` | 是否在 inner steps 中间也轮询新 global。默认 false。 |
| `adopt_global_after_upload` | upload 后是否采用新 global。默认 true。 |

### `failure_sim`

| 参数 | 含义 |
| --- | --- |
| `enabled` | 是否启用 failure simulation。 |
| `sleep_jitter_seconds` | interval 边界随机 sleep。 |
| `upload_skip_probability` | 训练 interval 但跳过 upload 的概率。 |
| `crash_probability` | interval 边界故意异常退出概率。 |

### `wandb`

| 参数 | 含义 |
| --- | --- |
| `enabled` | 是否启用 syncer 侧 W&B 记录；默认 true。 |
| `mode` | W&B mode；默认 `offline`，可用环境变量 `WANDB_MODE=online` 覆盖。 |
| `entity` | 可选 W&B entity。 |
| `group` | 可选 W&B group；为空时使用 `run.name`。 |
| `tags` | 附加 tags。Project name 固定为 `fs-diloco-miyabi-syncer`，run name 由时间戳、模型/数据集、learner 数、quorum、inner batch/accumulation 和 outer optimizer 超参数自动生成，不依赖 CLI 参数。 |

---

# English

## Configuration Reference

### `run`

| Parameter | Meaning |
| --- | --- |
| `name` | Human-readable run name, also used as the suffix of the default `RUN_ID`. |
| `run_id` | Explicit run ID; if `null`, the system creates `<timestamp>_<name>`. |
| `shared_root` | Shared run directory; if `null`, resolves to `$PROJECT_ROOT/runs/fs_diloco/$RUN_ID`. |
| `log_level` | Reserved log level. The current implementation mainly writes JSONL events. |

### `init`

| Parameter | Meaning |
| --- | --- |
| `resume` | Resume an existing run instead of creating a fresh run. |
| `resume_version` | `latest` or an integer global version. |
| `resume_db_dump` | Optional SQLite dump to restore; if empty, the syncer tries the latest compatible dump. |
| `allow_overwrite_existing_run` | When `resume=false`, allow replacing an existing run directory that already has `latest.json`. Default is false. |

### `model`

| Parameter | Meaning |
| --- | --- |
| `name_or_path` | Hugging Face model name/path. Default is `gpt2`; `synthetic-tiny` is used by smoke configs. |
| `trust_remote_code` | Whether to allow Hugging Face remote code. Default is false. |
| `dtype` | Model load dtype, for example `bfloat16`. |
| `compile` | Whether to use `torch.compile`. Default is false. |
| `synthetic_vocab_size` | Vocabulary size for the synthetic tiny model. |
| `synthetic_hidden_size` | Hidden size for the synthetic tiny model. |

### `data`

| Parameter | Meaning |
| --- | --- |
| `dataset_name` | Hugging Face dataset name. Default is `wikitext`; `synthetic` enables synthetic data. |
| `dataset_config_name` | Dataset config. WikiText-2 uses `wikitext-2-raw-v1`. |
| `train_split` | Training split name. |
| `validation_split` | Reserved validation split name. |
| `block_size` | Packed causal LM sequence length. This is also copied to `training.block_size`. |
| `num_proc` | Reserved tokenization parallelism setting. |
| `cache_dir` | Hugging Face dataset cache directory. |
| `streaming` | Whether to use streaming datasets. |
| `synthetic_num_batches` | Reserved synthetic-data batch count setting. |

### `sync`

| Parameter | Meaning |
| --- | --- |
| `num_learners` | Total learner count. The 9-node PBS configuration uses 8. |
| `upload_mode` | Currently `params`, meaning full parameter-vector uploads. |
| `quorum_min` | Minimum eligible learners required before starting an outer step. |
| `quorum_max` | Maximum number of learners selected for one outer step. |
| `max_staleness_versions` | Drop updates whose `current_version - base_global_version` exceeds this value. |
| `staleness_lambda` | Staleness weight decay coefficient. |
| `selection_policy` | Default is `most_recent_per_learner`; `oldest_pending` is available for ablations. |
| `scan_interval_seconds` | Sleep interval between syncer heartbeat/update scans. |
| `grace_window.fixed_seconds` | Extra fixed wait after quorum is reached to collect more updates. |
| `grace_window.max_seconds` | Grace-window upper bound. |
| `db_dump_every_versions` | SQLite backup cadence in global versions. |
| `stop_after_outer_steps` | Stop after this many committed global versions. |
| `stop_after_global_tokens` | Stop after this many selected tokens; `null` disables it. |
| `stop_file_poll_seconds` | Reserved stop-file polling interval. |

### `liveness`

| Parameter | Meaning |
| --- | --- |
| `heartbeat_interval_seconds` | Learner heartbeat write interval. |
| `stale_after_seconds` | Learner becomes stale after heartbeat age exceeds this threshold. |
| `dead_after_seconds` | Learner becomes dead after heartbeat age exceeds this threshold. |
| `no_progress_timeout_seconds` | If quorum cannot be reached for this long, the syncer writes `stop.json` and exits. |
| `quorum_policy` | Current implementation is fixed and does not automatically lower quorum for dead learners. |

### `training`

| Parameter | Meaning |
| --- | --- |
| `inner_steps` | Local optimizer steps per upload interval. |
| `micro_batch_size` | Batch size for each forward pass. |
| `gradient_accumulation_steps` | Number of micro-batches accumulated before one optimizer step. |
| `block_size` | Causal LM sequence length. |
| `max_local_steps` | Maximum local learner steps; `null` means learners stop through `stop.json`. |
| `precision` | `bf16` enables bfloat16 autocast on CUDA. |
| `seed` | Base random seed; learner index is added to it. |
| `log_every_steps` | Learner loss-summary logging interval. |
| `grad_clip` | Optional gradient norm clipping value. |

### `inner_optimizer`

| Parameter | Meaning |
| --- | --- |
| `name` | Learner optimizer. Currently only `adamw` is supported. |
| `lr` | Inner learning rate. |
| `betas` | AdamW beta coefficients. |
| `eps` | AdamW epsilon. |
| `weight_decay` | Inner weight decay. |
| `scheduler` | `cosine` or `none`. |
| `warmup_steps` | Warmup step count. |
| `reset_on_global_update` | Intended semantics for global adoption; the current learner always resets optimizer/scheduler when adopting a new global version. |

### `outer_optimizer`

| Parameter | Meaning |
| --- | --- |
| `name` | `sgd`, `momentum`, `nesterov`, or `adamw`. |
| `lr` | Outer learning rate. |
| `momentum` | Momentum/Nesterov coefficient. |
| `weight_decay` | Outer weight decay. |
| `betas` | AdamW-style beta coefficients. |
| `eps` | AdamW-style epsilon. |

### `io`

| Parameter | Meaning |
| --- | --- |
| `tensor_dtype` | Learner update tensor dtype. Default is `float32`. |
| `atomic_write` | Design switch; critical writes currently use atomic helpers. |
| `compute_sha256` | Whether to compute update tensor SHA256. Disabled by default for large models. |
| `keep_processed_updates` | Reserved cleanup policy setting. |
| `cleanup_applied_after_versions` | Reserved retention window for applied updates. |
| `sqlite_local_dir` | Syncer-local SQLite directory; if empty, uses `${TMPDIR:-/tmp}/fs_diloco/$RUN_ID`. |

### `learner`

| Parameter | Meaning |
| --- | --- |
| `poll_latest_during_inner_steps` | Whether to poll for newer global versions inside an upload interval. Default is false. |
| `adopt_global_after_upload` | Whether to adopt a newer global version after each upload. Default is true. |

### `failure_sim`

| Parameter | Meaning |
| --- | --- |
| `enabled` | Enable failure simulation. |
| `sleep_jitter_seconds` | Random sleep at interval boundaries. |
| `upload_skip_probability` | Probability of skipping upload after an interval. |
| `crash_probability` | Probability of intentional crash at interval boundaries. |

### `wandb`

| Parameter | Meaning |
| --- | --- |
| `enabled` | Enable syncer-side W&B logging. Default is true. |
| `mode` | W&B mode. Default is `offline`; override with `WANDB_MODE=online`. |
| `entity` | Optional W&B entity. |
| `group` | Optional W&B group; defaults to `run.name`. |
| `tags` | Extra tags. The project name is `fs-diloco-miyabi-syncer`, and the run name is generated from timestamp, model/dataset, learner count, quorum, inner batch/accumulation, and outer optimizer hyperparameters instead of CLI arguments. |
