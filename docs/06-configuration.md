# 06 配置参考

配置为单个 YAML 文件,由 `core/config.py` 解析为嵌套 dataclass。**所有字段都有默认值**;出现未知键会直接报错(防拼写错误)。CLI 的 `--run-id / --shared-root / --num-learners` 会覆盖对应字段。新 run 初始化时,解析后的完整配置会同时原子写入 run 根的 `run_config.resolved.yaml` 和 `control/run_config.resolved.yaml`;二者内容一致,后者保留给恢复与既有工具使用。

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
| `resume` | `false` | true 时从 `<shared_root>/control/syncer_metadata.sqlite3` 的最大 committed 行恢复(fragment 模式不支持);DB 缺失或校验失败即退出 |

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
| `train_split` / `validation_split` | `train` / `validation` | learner 使用 train；专用 validation evaluator 使用 resolved config 的 validation split，并按训练 tokenizer/EOS/block 管线计算 loss/ppl |
| `block_size` | 1024 | 序列长度(会同步覆盖 `training.block_size`) |
| `num_proc` | 4 | ⚠ 未消费 |
| `cache_dir` / `streaming` | `null` / `false` | 传给 `load_dataset` |
| `synthetic_num_batches` | 128 | ⚠ 未消费(合成流无限生成) |

## sync — 合并协议(syncer 核心)

| 字段 | 默认 | 说明 |
|---|---|---|
| `num_learners` | 8 | learner 数;决定合法 learner_id 集与数据分片数 |
| `quorum_min` / `quorum_max` | 4 / 8 | 每次合并的 update 数下限/上限(每 learner 至多 1 份) |
| `max_staleness_versions` | 2 | staleness 窗口;超过即丢弃 |
| `staleness_lambda` | 0.25 | 加权公式中的 λ |
| `selection_policy` | `most_recent_per_learner` | 或 `oldest_pending` |
| `scan_interval_seconds` | 2.0 | 元数据/心跳重扫描间隔 |
| `ingest_during_publish` | false | full syncer 等待并行 checkpoint I/O 时，允许主线程继续串行摄取 heartbeat/pointer 元数据；不改变 selection、maintenance、DB version commit 或 latest 顺序 |
| `capture_terminal_predecessor_for_eval` | false | 研究证据开关；仅在 full input-closed 且 terminal drain 低于 `quorum_min` 时，把 merge 前权威 weight 以 hardlink（失败则原子 copy）保留到 `eval_checkpoints/` 并写 checksum manifest。该目录不参与 latest、DB、resume 或 GC 权威判定 |
| `grace_window.mode` | `fixed` | `fixed` 或 `adaptive_fastest_upload_eta` |
| `grace_window.fixed_seconds` | 20.0 | fixed 模式的宽限窗口时长 |
| `grace_window.initial_seconds` | 10.0 | adaptive 模式的初始窗口;用 syncer 首见 update 的 monotonic 时钟估算最快下一上传，运行中只会缩短 |
| `grace_window.max_seconds` | 60.0 | 初始/固定窗口的硬上限 |
| `stop_after_outer_steps` | 20 | 外层步数(fragment:merge event 数)停止条件;null 不限 |
| `stop_after_global_tokens` | `null` | 累计合并 token 停止条件 |
| `stop_file_poll_seconds` | 5.0 | learner 侧轮询 stop/latest 的间隔 |

## syncer — syncer 计算与发布

| 字段 | 默认 | 说明 |
|---|---|---|
| `device` | `auto` | `auto` 自动选择可用 CUDA、否则 CPU；也可显式设为 `cpu` 或 `cuda`。显式 `cuda` 但 CUDA 不可用时启动失败 |
| `compute_dtype` | `float32` | syncer 的全局参数、update 聚合和外层优化状态 dtype；也控制 full learner 的 prediction/reconcile 算术与 reference dtype。支持 `float32`/`fp32` 与 `bfloat16`/`bf16` |
| `publish_dtype` | `float32` | syncer 发布的 global/fragment 权重和外层优化器浮点状态落盘 dtype；支持同上。`step` 等整数状态保持整数 |
| `parallel_checkpoint_writes` | `true` | full publication 并发原子写 weight 与 outer checkpoint；两者都成功后主线程才提交 DB/latest。`false` 仅用于同 fingerprint 串行消融，不改变提交顺序 |

以下仅展示如何显式构造 BF16 compute/publish 实验配置，不代表推荐默认：

```yaml
syncer:
  device: cuda
  compute_dtype: bfloat16
  publish_dtype: bfloat16
```

`compute_dtype` 与 `publish_dtype` 可以独立配置。每次发布前，syncer 先按 `publish_dtype` 量化权重和浮点优化器状态，再转换回 `compute_dtype` 作为下一轮内存状态，因此 learner 可见 checkpoint、持续运行和 resume 使用同一个权威数值边界。full learner 的 prediction/reconcile 默认在 learner CUDA 设备上按 `compute_dtype` 执行；若按参数量和临时向量数估算会侵占 CUDA 安全余量，或实际触发 CUDA OOM，则保持相同 dtype 回退 CPU。恢复 full run 时，checkpoint 中的浮点状态会转换到当前配置的 `compute_dtype` 后继续计算；fragment 模式仍不支持恢复。

同 fingerprint、仅改变 `publish_dtype` 的三 seed 5000-step 门禁显示 BF16 的 validation
loss 没有劣化（paired 均值相对 FP32 为 -0.00359 nats，冻结 ε=0.01），round-trip
relative-L2 也未累积增长；但 checkpoint 字节减半的同时，测得平均 publish walltime 反而高
62.47%，完整时间均值高 2.01%。因此 `publish_dtype` 继续默认 `float32`，BF16 只保留为
显式实验/容量选项；若要改默认必须另行评审，而不能只凭质量门禁通过。

## liveness

| 字段 | 默认 | 说明 |
|---|---|---|
| `heartbeat_interval_seconds` | 30.0 | learner 写心跳间隔 |
| `stale_after_seconds` | 120.0 | 心跳超龄 → stale |
| `dead_after_seconds` | 300.0 | 心跳超龄 → dead |
| `no_progress_timeout_seconds` | 600.0 | syncer 无合并进展的停机超时;learner 收尾等待也用它 |
| `syncer_unresponsive_timeout_seconds` | `null` | learner 观察不到新版 latest 时的自保超时；`null` 沿用 `no_progress_timeout_seconds`，显式值必须 > 0 |
| `learner_shutdown_timeout_seconds` | `null` | syncer 发布 stop 后等待 stopped 心跳的上限；`null` 为 `max(120, 2 × heartbeat_interval_seconds)`，大模型收尾更慢时可显式调大 |

## training — learner 内层训练

| 字段 | 默认 | 说明 |
|---|---|---|
| `inner_steps` | 100 | 每个更新区间的本地步数(DiLoCo 的 H) |
| `micro_batch_size` | 2 | 单次前向 batch |
| `gradient_accumulation_steps` | 8 | 每个本地步累积次数 |
| `block_size` | 1024 | 由 `data.block_size` 覆盖 |
| `max_local_steps` | `null` | 在默认 completion mode 下作为 learner 本地停止上限；不参与 LR 调度 |
| `completion_mode` | `local_or_global` | `local_or_global`:本地上限或 stop 任一满足即停;`global_only`:忽略本地上限,一直训练到 syncer 发布 stop(要求配置全局停止目标) |
| `precision` | `bf16` | CUDA 上 bf16 autocast;其余 fp32 |
| `seed` | 1337 | 实际种子 = seed + learner_index |
| `log_every_steps` | 10 | inner_step_summary 日志频率 |
| `grad_clip` | `null` | 梯度范数裁剪阈值 |

## inner_optimizer(learner,仅支持 adamw)

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `adamw` | 其他值报错 |
| `lr` / `betas` / `eps` / `weight_decay` | 5e-5 / (0.9,0.95) / 1e-8 / 0.1 | AdamW 超参 |
| `scheduler` | `none` | `none` / warmup+`cosine`；cosine 必须显式配置独立 horizon |
| `warmup_steps` | 100 | 线性 warmup 步数 |
| `scheduler_total_steps` | `null` | cosine 的累计本地 optimizer-step horizon；cosine 时必填且必须大于 warmup，不从 `max_local_steps` 推导 |
| `min_lr_ratio` | 0.1 | warmup 后的 LR/base-LR 下限，范围 `(0, 1]`；超过 horizon 后保持该非零下限 |

LR 进度使用 learner 启动以来已完成的累计 `local_step`。第 k 个 optimizer step 使用
`schedule(k-1)`；replace 或 fragment adoption 即使重建 AdamW 与 scheduler 对象，也会恢复到
当前累计步，因此 warmup 只发生一次。`completion_mode`、`max_local_steps` 与 adoption 次数均不改变
同一 scheduler 配置的逐步 LR。WSD 尚未实现，配置为 `wsd` 会 fail-closed。

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
| `tensor_dtype` | `float32` | learner update 的构造/落盘 dtype(`float32`/`bfloat16`/`float16`；syncer 读取时转换为 `syncer.compute_dtype`)。本仓库的 GPT-2 1L debug 与两种 50x10 对照配置使用 `bfloat16` |
| `atomic_write` | `true` | ⚠ 未消费(始终原子写) |
| `compute_sha256` | `false` | 上传时计算张量文件摘要(分析工具可校验完整性) |

checkpoint/update 保留数量不再是配置项。syncer 的 maintenance 按权威引用集合自动保留一个 current global weight/outer、每个 fragment 一个 current weight/outer、latest 引用的一个 materialized full、active DB proposal payload 与固定 proposal pointers;历史先归档后回收。归档 JSONL 只追加、运行时不回读；归档后尚待删除的 payload 由 SQLite `gc_pending` 有界集合跨崩溃记忆。未发布孤儿的 grace 为 `max(2 × heartbeat_interval_seconds, 2 × scan_interval_seconds)`。

## learner

| 字段 | 默认 | 说明 |
|---|---|---|
| `poll_latest_during_inner_steps` | `false` | 区间中途也轮询/采纳新版本 |
| `adopt_global_after_upload` | `true` | 每次上传后轮询/采纳(fragment 模式会等待一小段时间) |
| `global_adoption_strategy` | `replace` | full learner 的采纳方式:`replace` 直接替换本地权重;`rebase_post_publish_delta` 仅在发布后的第一次检查没有新版时保留按 `syncer.compute_dtype` 构造的发布点,随后将尚未发布的本地差值合成到首个新 global 并立即释放 reference。prediction/reconcile 优先使用 learner GPU，OOM 风险时回退 CPU。后者不支持 fragment |
| `post_publish_latest_wait_seconds` | 0.0 | 三种 full 策略共用的发布后等待时长；0 表示只做即时检查 |
| `post_publish_latest_poll_seconds` | 0.2 | 发布后等待及 latest 引用文件 GC 竞态重试的轮询间隔，必须大于 0 |
| `prediction.reconcile_timeout_seconds` | 60.0 | prediction reconcile 的等待上限；也作为所有 learner latest 权重加载遭遇 current-only GC 竞态时的总重试预算，必须大于 0 |

配置解析对未知键 fail-closed。`sync.upload_mode`、`liveness.quorum_policy`、`inner_optimizer.reset_on_global_update` 已作为未消费字段删除；再次出现会明确报“字段已移除”。旧的平铺键 `learner.prediction_reconcile_timeout_seconds` 同样拒绝，错误会指向 `learner.prediction.reconcile_timeout_seconds`，不提供静默别名或自动迁移。

5000-step full 配置启用 `poll_latest_during_inner_steps=true` 和
`global_adoption_strategy=rebase_post_publish_delta`:发布后仍只无阻塞检查一次 latest;若没有新版,
才保留发布点并在每个后续 `optimizer.step()` 后再检查;若第一次检查已有新版则直接采纳且不保留发布点。
延迟发现新版并完成 rebase 后也立即释放发布点。直接采用新版仍重置 AdamW moments；scheduler
对象若重建会恢复累计 local-step 相位，不会再次 warmup。rebase/prediction reconcile 保留整个
optimizer/scheduler 状态。

## fragments

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 开启分片模式 |
| `strategy` | `full` | `full`(单片)/ `balanced_tensor`(按张量均衡装箱) |
| `num_fragments` | 1 | 片数(full 必须为 1;balanced_tensor ≤ 张量数) |
| `schedule` | `round_robin_global` | 仅此一种 |
| `fragments_per_update` | 1 | 仅支持 1 |
| `reset_inner_optimizer_on_fragment_adopt` | `true` | 采纳片更新后是否重置内层优化器 |
| `materialize_full_every_events` | `null` | fragment 开启时必须显式为正整数；每 N 个 merge event 重拼完整权重，所有正常终止路径还会强制最终物化。性能配置使用 10，逐事件调试配置使用 1；缺失、null、0 或负数 fail closed |

`materialize_full_every_events` 的 dataclass 缺省仍为 `null`，目的是让开启 fragment
却没有明确选择新鲜度/写放大的配置直接报错，而不是静默采用最昂贵的逐事件物化。三 seed
50×10 消融中，10 相对 1 把完整物化字节减少 90%、物化时间均值减少 81.45%，完整训练时间
均值减少 2.21%；单 seed 完整时间有波动，因此该字段仍要求按评估/恢复新鲜度显式选择。

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
