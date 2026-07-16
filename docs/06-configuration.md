# 06 配置参考

配置为单个 YAML 文件,由 `core/config.py` 解析为嵌套 dataclass。**所有字段都有默认值**;出现未知键会直接报错(防拼写错误)。CLI 的 `--run-id / --shared-root / --num-learners / --sqlite-local-dir` 会覆盖对应字段。解析后的完整配置会以 `control/run_config.resolved.yaml` 快照进 run 目录。

标注 ⚠ 的字段:在配置中声明但**当前运行时代码未消费**(预留)。

## run — run 标识

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `fs_diloco_gpt2_wikitext2_8l` | run 名,用于默认 run_id 后缀与 W&B group |
| `run_id` | `null` | 缺省时取 `$RUN_ID` 或 `时间戳_name` |
| `shared_root` | `null` | 共享目录;缺省 `<cwd>/runs/fs_diloco/<run_id>` |
| `log_level` | `INFO` | ⚠ 未消费(日志始终全量写 JSONL) |

## init — 初始化与恢复(syncer)

| 字段 | 默认 | 说明 |
|---|---|---|
| `resume` | `false` | true 时走 `resume_run()`(fragment 模式不支持) |
| `resume_version` | `latest` | `latest` 或具体版本号 |
| `resume_db_dump` | `null` | 指定 DB dump 路径;缺省自动找匹配版本的最新 dump |
| `allow_overwrite_existing_run` | `false` | `latest.json` 已存在时是否允许重新初始化 |

## model

| 字段 | 默认 | 说明 |
|---|---|---|
| `name_or_path` | `gpt2` | HF 模型名;`synthetic-tiny` 走内置冒烟模型 |
| `trust_remote_code` | `false` | 传给 HF from_pretrained |
| `dtype` | `bfloat16` | 模型参数 dtype(bf16/fp16/fp32) |
| `compile` | `false` | `torch.compile` |
| `synthetic_vocab_size` / `synthetic_hidden_size` | 128 / 32 | 冒烟模型尺寸 |

## data

| 字段 | 默认 | 说明 |
|---|---|---|
| `dataset_name` | `wikitext` | `synthetic` 走随机 token 流;wikitext 失败时自动回退 `Salesforce/wikitext`,可用 `$FS_DILOCO_HF_WIKITEXT_REPO` 重定向 |
| `dataset_config_name` | `wikitext-2-raw-v1` | |
| `train_split` / `validation_split` | `train` / `validation` | validation ⚠ 未消费 |
| `block_size` | 1024 | 序列长度(会同步覆盖 `training.block_size`) |
| `num_proc` | 4 | ⚠ 未消费 |
| `cache_dir` / `streaming` | `null` / `false` | 传给 `load_dataset` |
| `synthetic_num_batches` | 128 | ⚠ 未消费(合成流无限生成) |

## sync — 合并协议(syncer 核心)

| 字段 | 默认 | 说明 |
|---|---|---|
| `num_learners` | 8 | learner 数;决定合法 learner_id 集与数据分片数 |
| `upload_mode` | `params` | ⚠ 仅此一种,未消费 |
| `quorum_min` / `quorum_max` | 4 / 8 | 每次合并的 update 数下限/上限(每 learner 至多 1 份) |
| `max_staleness_versions` | 2 | staleness 窗口;超过即丢弃 |
| `staleness_lambda` | 0.25 | 加权公式中的 λ |
| `selection_policy` | `most_recent_per_learner` | 或 `oldest_pending` |
| `scan_interval_seconds` | 2.0 | 元数据/心跳重扫描间隔 |
| `grace_window.mode` | `fixed` | ⚠ 仅 fixed,未消费 |
| `grace_window.fixed_seconds` | 20.0 | 宽限窗口时长 |
| `grace_window.max_seconds` | 60.0 | 窗口上限(与 fixed 取 min) |
| `db_dump_every_versions` | 1 | 每 N 个版本 dump 一次 SQLite;0/null 关闭周期 dump(停机仍 dump) |
| `stop_after_outer_steps` | 20 | 外层步数(fragment:merge event 数)停止条件;null 不限 |
| `stop_after_global_tokens` | `null` | 累计合并 token 停止条件 |
| `stop_file_poll_seconds` | 5.0 | learner 侧轮询 stop/latest 的间隔 |

## liveness

| 字段 | 默认 | 说明 |
|---|---|---|
| `heartbeat_interval_seconds` | 30.0 | learner 写心跳间隔 |
| `stale_after_seconds` | 120.0 | 心跳超龄 → stale |
| `dead_after_seconds` | 300.0 | 心跳超龄 → dead |
| `no_progress_timeout_seconds` | 600.0 | syncer 无合并进展的停机超时;learner 收尾等待也用它 |
| `quorum_policy` | `fixed` | ⚠ 未消费 |

## training — learner 内层训练

| 字段 | 默认 | 说明 |
|---|---|---|
| `inner_steps` | 100 | 每个更新区间的本地步数(DiLoCo 的 H) |
| `micro_batch_size` | 2 | 单次前向 batch |
| `gradient_accumulation_steps` | 8 | 每个本地步累积次数 |
| `block_size` | 1024 | 由 `data.block_size` 覆盖 |
| `max_local_steps` | `null` | 有限步训练总步数;也是 cosine 调度的周期与 learner 停止条件 |
| `precision` | `bf16` | CUDA 上 bf16 autocast;其余 fp32 |
| `seed` | 1337 | 实际种子 = seed + learner_index |
| `log_every_steps` | 10 | inner_step_summary 日志频率 |
| `grad_clip` | `null` | 梯度范数裁剪阈值 |

## inner_optimizer(learner,仅支持 adamw)

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `adamw` | 其他值报错 |
| `lr` / `betas` / `eps` / `weight_decay` | 5e-5 / (0.9,0.95) / 1e-8 / 0.1 | AdamW 超参 |
| `scheduler` | `cosine` | `none` / warmup+`cosine`(cosine 需 `max_local_steps`) |
| `warmup_steps` | 100 | 线性 warmup 步数 |
| `reset_on_global_update` | `true` | ⚠ 未消费——当前实现**总是**在采纳新版本时重置内层优化器 |

## outer_optimizer(syncer)

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `nesterov` | `sgd` / `momentum` / `nesterov` / `adamw` |
| `lr` | 0.7 | 外层学习率 |
| `momentum` | 0.9 | sgd 系用 |
| `weight_decay` | 0.0 | |
| `betas` / `eps` | (0.9,0.999) / 1e-8 | adamw 用 |

## io

| 字段 | 默认 | 说明 |
|---|---|---|
| `tensor_dtype` | `float32` | learner update 的构造/落盘 dtype(`float32`/`bfloat16`/`float16`;合并时统一转回 fp32)。本仓库的 GPT-2 1L debug 与两种 50x10 对照配置使用 `bfloat16` |
| `atomic_write` | `true` | ⚠ 未消费(始终原子写) |
| `compute_sha256` | `false` | 上传时计算张量文件摘要(分析工具可校验完整性) |
| `keep_processed_updates` | `true` | ⚠ 未消费 |
| `cleanup_applied_after_versions` | `null` | ⚠ 未消费 |
| `keep_last_global_versions` | 3 | syncer 保留最近 N 版全局权重/优化器文件(其余删除) |
| `keep_last_learner_update_versions` | 3 | learner 保留自己 pending 目录最近 N 份 update(**fragment 模式不清理**;注意需 ≥ staleness 窗口需求,否则 syncer 会遇到 missing_file) |
| `sqlite_local_dir` | `null` | syncer SQLite 所在目录;缺省 `$TMPDIR/fs_diloco/<run_id>`。**务必指向节点本地盘,不要指向共享文件系统** |

## learner

| 字段 | 默认 | 说明 |
|---|---|---|
| `poll_latest_during_inner_steps` | `false` | 区间中途也轮询/采纳新版本 |
| `adopt_global_after_upload` | `true` | 每次上传后轮询/采纳(fragment 模式会等待一小段时间) |

## fragments

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 开启分片模式 |
| `strategy` | `full` | `full`(单片)/ `balanced_tensor`(按张量均衡装箱) |
| `num_fragments` | 1 | 片数(full 必须为 1;balanced_tensor ≤ 张量数) |
| `schedule` | `round_robin_global` | 仅此一种 |
| `fragments_per_update` | 1 | 仅支持 1 |
| `reset_inner_optimizer_on_fragment_adopt` | `true` | 采纳片更新后是否重置内层优化器 |
| `materialize_full_every_events` | `null` | 每 N 个 merge event 重拼完整权重;null/≤0 表示每次都拼 |

## failure_sim(learner 故障注入)

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关 |
| `sleep_jitter_seconds` | 0.0 | 上传前随机睡 0~N 秒 |
| `upload_skip_probability` | 0.0 | 跳过本次上传的概率 |
| `crash_probability` | 0.0 | 以 exit 97 崩溃的概率(上传后/跳过后检查) |

## wandb(syncer 侧)

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | `$WANDB_DISABLED` 亦可关闭 |
| `mode` | `offline` | `$WANDB_MODE` 优先 |
| `entity` / `group` / `tags` | null / null / [] | group 缺省用 `run.name` |

W&B run 命名规则见 `observability/wandb_logging.py: syncer_wandb_run_name()`(时间戳 + run 名 + 模型 + 数据集 + learner 数 + quorum + 内层超参 + 外层优化器/lr 的 slug 拼接);project 固定 `fs-diloco-miyabi-syncer`;W&B 初始化失败只降级不中断训练。
