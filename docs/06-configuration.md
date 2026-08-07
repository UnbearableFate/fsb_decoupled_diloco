# 06 配置参考

配置为单个 YAML 文件,由 `core/config.py` 解析为嵌套 dataclass。**所有字段都有默认值**;出现未知键会直接报错(防拼写错误)。CLI 的 `--run-id / --shared-root / --num-learners` 及一组实验覆盖参数(`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`)会覆盖对应字段,syncer 与 learner 两侧接受同一组参数。新 run 初始化时,解析后的完整配置会同时原子写入 run 根的 `run_config.resolved.yaml` 和 `control/run_config.resolved.yaml`;二者内容一致,后者保留给恢复与既有工具使用。

标注 ⚠ 的字段:在配置中声明但**当前运行时代码未消费**(预留)。

## run — run 标识

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `fs_diloco_gpt2_wikitext2_8l` | run 名,用于默认 run_id 后缀与 W&B group |
| `run_id` | `null` | 缺省时取 `$RUN_ID` 或 `时间戳_name` |
| `shared_root` | `null` | null 时解析为 `<project_root 或 cwd>/runs/fs_diloco/<run_id>`;非空值中的 `{run_id}` 替换为最终 ID。仓库正式 YAML 显式写主工作树绝对模板,CLI 仍可覆盖 |
| `log_level` | `INFO` | ⚠ 未消费(日志始终全量写 JSONL) |
| `git_commit` / `git_dirty` / `source_fingerprint` | `null` / `null` / `null` | launcher/source-identity 证据;分别可由 `$FS_DILOCO_GIT_COMMIT`、`$FS_DILOCO_GIT_DIRTY`、`$FS_DILOCO_SOURCE_FINGERPRINT` 补入。`$FS_DILOCO_REQUIRE_SOURCE_IDENTITY=true` 时 commit 与 fingerprint 必须存在 |

## init — 初始化与恢复(syncer)

| 字段 | 默认 | 说明 |
|---|---|---|
| `resume` | `false` | true 时从 `<shared_root>/control/syncer_metadata.sqlite3` 的最大已提交行恢复(分片模式不支持),并原子重置本代 learner 存活状态、持久化旧心跳内容隔离栅栏;DB 缺失/校验失败即退出。stop reason 或 summary stop_reason 任一为非 error 也拒绝恢复,不要求两文件成对一致 |

## model

| 字段 | 默认 | 说明 |
|---|---|---|
| `name_or_path` | `gpt2` | HF 模型名;`synthetic-tiny` 走内置冒烟模型 |
| `trust_remote_code` | `false` | 传给 HF from_pretrained |
| `dtype` | `bfloat16` | 模型参数 dtype;BF16/FP16 别名显式映射,其他任意字符串当前静默回退 FP32(不是严格枚举,需避免拼写错误) |
| `compile` | `false` | `torch.compile` |
| `synthetic_vocab_size` / `synthetic_hidden_size` | 128 / 32 | 冒烟模型尺寸 |

## data

| 字段 | 默认 | 说明 |
|---|---|---|
| `dataset_name` | `wikitext` | `synthetic` 走随机 token 流;wikitext 失败时自动回退 `Salesforce/wikitext`,可用 `$FS_DILOCO_HF_WIKITEXT_REPO` 重定向 |
| `dataset_config_name` | `wikitext-2-raw-v1` | |
| `train_split` / `validation_split` | `train` / `validation` | learner 使用 train;专用 validation evaluator 使用解析后配置的 validation split,并按训练 tokenizer/EOS/block 管线计算 loss/ppl |
| `block_size` | 1024 | 序列长度(会同步覆盖 `training.block_size`) |
| `num_proc` | 4 | ⚠ 未消费 |
| `cache_dir` / `streaming` | `null` / `false` | 传给 `load_dataset` |
| `shuffle_blocks` | `true` | WikiText 块在每个 epoch 以 learner 隔离的稳定 seed 重排;false 时按块原序无限循环 |
| `synthetic_num_batches` | 128 | ⚠ 未消费(合成流无限生成) |

## sync — 合并协议(syncer 核心)

| 字段 | 默认 | 说明 |
|---|---|---|
| `num_learners` | 8 | static learner 数、合法 ID 集与数据分片数;dynamic 保留为 merge/config 兼容值,成员权威来自 DB,数据分片分母来自 `membership.stream_pool_size`,并拒绝 CLI 覆盖 |
| `quorum_min` / `quorum_max` | 4 / 8 | 每次合并的 update 数下限/上限(每 learner 至多 1 份) |
| `max_staleness_versions` | 2 | 陈旧度窗口;超过即丢弃 |
| `staleness_lambda` | 0.25 | 加权公式中的 λ |
| `selection_policy` | `most_recent_per_learner` | 或 `oldest_pending` |
| `scan_interval_seconds` | 2.0 | 元数据/心跳重扫描间隔 |
| `ingest_during_publish` | false | full syncer 等待并行 checkpoint I/O 时,允许主线程继续串行摄取 heartbeat/pointer 元数据;不改变 selection、maintenance、DB version commit 或 latest 顺序 |
| `capture_terminal_predecessor_for_eval` | false | 研究证据开关;仅在 full input-closed 且末端排空低于 `quorum_min` 时,把 merge 前权威 weight 以硬链接(失败则原子复制)保留到 `eval_checkpoints/` 并写校验和清单。清单是证据提交点:清单前残留 checkpoint 可校验恢复,清单后任一 identity/checksum/缺文件冲突 fail closed。该目录不参与 latest、DB、resume 或 GC 权威判定 |
| `grace_window.mode` | `fixed` | `fixed` 或 `adaptive_fastest_upload_eta` |
| `grace_window.fixed_seconds` | 20.0 | fixed 模式的宽限窗口时长 |
| `grace_window.initial_seconds` | 10.0 | adaptive 模式的初始窗口;用 syncer 首见 update 的单调时钟估算最快下一上传,运行中只会缩短 |
| `grace_window.max_seconds` | 60.0 | 初始/固定窗口的硬上限 |
| `stop_after_outer_steps` | 20 | 外层步数(分片:merge event 数)停止条件;null 不限 |
| `stop_after_global_tokens` | `null` | 累计合并 token 停止条件;只在每轮开头检查,因此最后一次 merge 按整批提交,累计值可超过阈值。dynamic 命中后以 current version 为冻结上限进入持久 close/drain,不借用 outer-step target |
| `stop_file_poll_seconds` | 5.0 | learner 侧轮询 stop/latest 的间隔 |

## syncer — syncer 计算与发布

| 字段 | 默认 | 说明 |
|---|---|---|
| `device` | `auto` | `auto` 自动选择可用 CUDA、否则 CPU;也可显式设为 `cpu` 或 `cuda`。显式 `cuda` 但 CUDA 不可用时启动失败 |
| `compute_dtype` | `float32` | syncer 的全局参数、update 聚合和外层优化状态 dtype;也控制 full learner 的 prediction/reconcile 算术与 reference dtype。支持 `float32`/`fp32` 与 `bfloat16`/`bf16` |
| `publish_dtype` | `float32` | syncer 发布的 global/fragment 权重和外层优化器浮点状态落盘 dtype;支持同上。`step` 等整数状态保持整数 |
| `parallel_checkpoint_writes` | `true` | full publication 并发原子写 weight 与 outer checkpoint;两者都成功后主线程才提交 DB/latest。`false` 仅用于同 fingerprint 串行消融,不改变提交顺序 |

以下仅展示如何显式构造 BF16 compute/publish 实验配置,不代表推荐默认:

```yaml
syncer:
  device: cuda
  compute_dtype: bfloat16
  publish_dtype: bfloat16
```

`compute_dtype` 与 `publish_dtype` 可以独立配置。每次发布前,syncer 先按 `publish_dtype` 量化权重和浮点优化器状态,再转换回 `compute_dtype` 作为下一轮内存状态,因此 learner 可见 checkpoint、持续运行和 resume 使用同一个权威数值边界。full learner 的 prediction/reconcile 默认在 learner CUDA 设备上按 `compute_dtype` 执行;若按参数量和临时向量数估算会侵占 CUDA 安全余量,或实际触发 CUDA OOM,则保持相同 dtype 回退 CPU。恢复 full run 时,checkpoint 中的浮点状态会转换到当前配置的 `compute_dtype` 后继续计算;分片模式仍不支持恢复。

同 fingerprint、仅改变 `publish_dtype` 的三 seed 5000-step 门禁显示 BF16 的 validation
loss 没有劣化(paired 均值相对 FP32 为 -0.00359 nats,冻结 ε=0.01),round-trip
relative-L2 也未累积增长;但 checkpoint 字节减半的同时,测得平均 publish walltime 反而高
62.47%,完整时间均值高 2.01%。因此 `publish_dtype` 继续默认 `float32`,BF16 只保留为
显式实验/容量选项;若要改默认必须另行评审,而不能只凭质量门禁通过。

## coordination — full 模式 Syncer HA

HA 默认关闭,只允许 `fragments.enabled=false`;成员可为 static 或 dynamic。`recovery_submission.enabled=true` 还要求 HA 已开启;它创建候选 job,不授予领导权。

| 字段 | 默认 | 说明 |
|---|---|---|
| `syncer_ha.enabled` | `false` | 启用 initializer + 单调 leader epoch + 隔离业务事务 + epoch 权威控制 |
| `syncer_ha.lease_duration_seconds` | 90.0 | 租约有效期;必须覆盖续约间隔和允许的最大时钟偏差 |
| `syncer_ha.renew_interval_seconds` | 10.0 | active leader 续约周期 |
| `syncer_ha.max_clock_skew_seconds` | 2.0 | 租约安全计算接受的跨节点时钟偏差上限 |
| `syncer_ha.heartbeat_interval_seconds` / `heartbeat_stale_after_seconds` | 5.0 / 30.0 | syncer epoch 心跳发布周期与 learner 判陈旧门槛 |
| `syncer_ha.lease_busy_timeout_ms` | 5000 | acquire/renew 短连接单次等待 SQLite writer lock 的上限;busy 时 candidate 在总预算内轮询,renewer 在本地租约安全边界内重试 |
| `syncer_ha.business_busy_timeout_ms` | 60000 | 隔离业务事务等待 SQLite writer lock 的上限;取得锁后仍重新校验 DB token 与本地单调安全边界 |
| `syncer_ha.candidate_acquire_poll_seconds` / `candidate_wait_seconds` | 5.0 / 180.0 | loser candidate 轮询周期与总等待预算 |
| `syncer_ha.learner_recovery_wait_seconds` | 1800.0 | learner 看到 syncer 失去进展后允许 recovery claim/job 完成的总预算 |
| `syncer_ha.canonical_repair_wait_seconds` | 120.0 | DB commit 已存在而权威控制尚待后继者修复的等待预算 |
| `syncer_ha.max_retained_epoch_dirs` | 32 | maintenance 保留的 recent epoch 控制/目录上限;更旧历史先归档 |
| `recovery_submission.enabled` | `false` | learner-assisted candidate qsub 总开关;默认人工 restart |
| `recovery_submission.claim_timeout_seconds` / `reconciliation_interval_seconds` / `uncertainty_timeout_seconds` | 120 / 60 / 300 | mkdir claim、qstat 对账和 unknown 状态保留预算 |
| `recovery_submission.backoff_initial_seconds` / `backoff_max_seconds` | 60 / 900 | 同 observation 失败后的指数退避范围 |
| `recovery_submission.max_attempts_per_observation` / `max_outstanding_candidates` | 3 / 1 | observation 尝试预算和 scheduler 中未完成候选上限 |
| `recovery_submission.claim_retention_seconds` | 3600 | 已终态 claim 的保留/归档窗口 |
| `recovery_submission.candidate_pbs_script` | `scripts/miyabi/run_syncer_candidate.pbs` | qsub 候选脚本;scheduler 提交前仍执行 descriptor/source gate |
| `recovery_submission.candidate_walltime` | `null` | 自动 recovery 启用时必填的 workload 估算 `HH:MM:SS`;应结合相邻实测选取尽可能短、但足以覆盖排队后启动波动、预期运行和完整收尾的值,每次 qsub 显式传 `-l walltime=...`,避免继承通用脚本的过长默认。可靠跑完优先于进一步压缩请求 |

## membership — full HA 成员身份

`mode=dynamic` 要求 full + Syncer HA,并拒绝 fragment、`--learner-id` 和 `--num-learners`。static 保持原有固定 learner ID 路径。

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `static` | `static` 或 `dynamic`;run 初始化后冻结 |
| `stream_pool_size` | 8 | dynamic 数据流池固定大小,也是 data shard/RNG 分母;必须≥bootstrap 且≥quorum_max,不支持在线扩容 |
| `bootstrap_instances` | 8 | initializer 创建的确定性 bootstrap launch request/slot 数量 |
| `initial_membership_deadline_seconds` | 1800 | bootstrap 成员形成的总预算,必须覆盖 registration TTL |
| `registration_scan_interval_seconds` | 2 | leader 扫描注册请求的周期 |
| `registration_request_ttl_seconds` | 120 | 未处理注册的有效期;处理结果/内容 hash 支持幂等重放与冲突拒绝 |
| `heartbeat_stale_after_seconds` / `heartbeat_dead_after_seconds` | 120 / 300 | dynamic current instance 的 stale/dead 阈值;dead 必须严格大于 stale |
| `revocation_grace_seconds` | 60 | dead/replacement 转入隔离撤销前的宽限 |
| `expired_retention_seconds` | 600 | 已终态 instance/request 活跃保留时间,之后先归档再剪枝;必须覆盖撤销宽限 |
| `max_active_instance_records` | 16 | current + grace 状态的逻辑上限,必须≥stream pool;不是允许同时贡献的人数 |
| `allow_unsolicited_registration` | `false` | false 时 registration 必须绑定 bootstrap 或 scale logical request |
| `allow_healthy_placement_replacement` | `false` | 是否允许新 instance 驱逐仍健康的同 placement current owner;正式路径保持 false |
| `reuse_stream_for_same_placement` | `true` | replacement 优先复用 placement 记录的 stream,同时严格提升 stream epoch 并标记 restart |

## scaling — dynamic 容量/发件箱

`enabled=true` 只允许 dynamic。扩容由持久且去重的容量观测驱动;merge 观测与 global version 同事务提交,饥饿世代与其观测也原子分配。qsub 回执和 qstat 状态只是发件箱证据,最终准入仍由 leader 事务决定;携带 scheduler job ID 的 registration 在精确回执绑定出现前保持 pending。

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | dynamic 自动扩容总开关 |
| `desired_contributors` | 8 | 目标 current productive contributor;必须在 quorum_min 和 quorum_max 之间 |
| `low_contributor_threshold` | 6 | 低容量判定阈值,必须小于 desired |
| `consecutive_low_windows` | 2 | 创建请求所需不同 low observation 数,至少 2;重放同 key 不增加计数 |
| `productive_window_count` | 2 | 判定近期有生产性的 observation 窗口数 |
| `startup_grace_seconds` | 180 | 初始化后不触发扩容的保护期 |
| `productive_upload_grace_factor` / `productive_upload_grace_min_seconds` / `productive_upload_grace_max_seconds` | 2 / 60 / 600 | 根据近期 cycle 时间判定 productive 的乘数与上下界 |
| `cooldown_seconds` | 300 | 创建扩容请求后的冷却期 |
| `max_pending_launch_requests` / `max_total_launch_requests` | 2 / 16 | 同时 pending 和整个 run 扩容请求预算;pending 不得超过 total |
| `launch_request_ttl_seconds` | 900 | 未提交/未知请求的时效;已由 scheduler 确认 queued/running 的 job 即使超过 TTL 仍保留预留 |
| `capacity_observation_retention_count` | 64 | 活跃观测有界窗口;更旧记录先归档 |
| `scheduler_reconcile_interval_seconds` | 30 | qstat 对账与发件箱 maintenance 周期 |
| `starvation_observation_seconds` | 120 | 无 merge 时形成唯一饥饿观测的最小间隔 |
| `learner_pbs_script` | `scripts/miyabi/run_dynamic_learner.pbs` | replacement job 脚本;提交前仍经过 descriptor/source gate |
| `learner_walltime` | `null` | scaling 启用时必填,按已测 workload 显式传给 qsub 的最短实用 walltime |
| `learner_queue` | `null` | 可选 qsub `-q` 覆盖;null 继承脚本/站点默认,正式配置应按目标队列显式审查 |

## terminal — dynamic close/drain

| 字段 | 默认 | 说明 |
|---|---|---|
| `admission_close_policy` | `global_target_or_launch_budget` | dynamic 准入关闭策略;支持 global target,并可由认证 manual request、截止时间或有限启动预算触发 |
| `deadline_seconds` | `null` | 相对训练开始的可选 dynamic close 截止时间 |
| `drain_ack_timeout_seconds` | 300 | 排空发布后健康 instance 确认等待预算;超时后以成员隔离撤销 |
| `registration_visibility_grace_seconds` | 10 | input-closed 前最后注册摄取/判定的可见性宽限 |
| `proposal_visibility_grace_seconds` | 20 | 最终指针进入摄取水位后的可见性宽限 |
| `max_terminal_merges` | 1 | manual/budget/deadline/no-progress close 从 current version 起允许的最多额外 merge;仍受 global outer target 约束,close transaction 冻结最终上限;token close 不使用该余量 |
| `allow_preclose_admission_during_drain` | `false` | 是否允许 close 前已授权但尚未 admit 的进程在排空中进入;正式路径保持 false |

## liveness

| 字段 | 默认 | 说明 |
|---|---|---|
| `heartbeat_interval_seconds` | 30.0 | learner 写心跳间隔 |
| `stale_after_seconds` | 120.0 | 心跳超龄 → stale |
| `dead_after_seconds` | 300.0 | 心跳超龄 → dead |
| `no_progress_timeout_seconds` | 600.0 | syncer 无合并进展的超时;legacy/fragment 按既有停机语义处理,dynamic 以 `no_progress_timeout` 原因进入持久 close/drain,冻结的上限至多允许 `max_terminal_merges` 次额外 merge(仍受 global outer target 约束),并在 controller closed 且 `dynamic_input_closed` 前拒绝普通 terminal。fragment learner 在有全局外层目标时的 final-progress wait 也用它;full learner 没有同类 final wait,syncer 等 learner 则使用下面的 shutdown timeout |
| `syncer_unresponsive_timeout_seconds` | `null` | learner 观察不到新版 latest 时的自保超时;`null` 沿用 `no_progress_timeout_seconds`,显式值必须 > 0 |
| `learner_shutdown_timeout_seconds` | `null` | syncer 发布 stop 后等待 stopped 心跳的上限;`null` 为 `max(120, 2 × heartbeat_interval_seconds)`,大模型收尾更慢时可显式调大 |

## training — learner 内层训练

| 字段 | 默认 | 说明 |
|---|---|---|
| `inner_steps` | 100 | 每个更新区间的本地步数(DiLoCo 的 H) |
| `micro_batch_size` | 2 | 单次前向 batch |
| `gradient_accumulation_steps` | 8 | 每个本地步累积次数 |
| `block_size` | 1024 | 由 `data.block_size` 覆盖 |
| `max_local_steps` | `null` | 在默认 completion mode 下作为 learner 本地停止上限;不参与 LR 调度 |
| `completion_mode` | `local_or_global` | `local_or_global`:本地上限或 stop 任一满足即停;`global_only`:忽略本地上限,一直训练到 syncer 发布 stop(要求配置全局停止目标) |
| `precision` | `bf16` | 只控制训练 autocast:设备是 CUDA 且值(忽略大小写)恰为 `bf16` 时启用 BF16 autocast,其他情况返回禁用的上下文;它不把模型强制转 FP32,参数 dtype 仍由 `model.dtype` 决定 |
| `seed` | 1337 | 实际种子 = seed + learner_index |
| `log_every_steps` | 10 | inner_step_summary 日志频率 |
| `grad_clip` | `null` | 梯度范数裁剪阈值 |

## inner_optimizer(learner,仅支持 adamw)

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `adamw` | 其他值报错 |
| `lr` / `betas` / `eps` / `weight_decay` | 5e-5 / (0.9,0.95) / 1e-8 / 0.1 | AdamW 超参 |
| `scheduler` | `none` | `none` / warmup+`cosine`;cosine 必须显式配置独立上限 |
| `warmup_steps` | 100 | 线性 warmup 步数 |
| `scheduler_total_steps` | `null` | cosine 的累计本地 optimizer-step 上限;cosine 时必填且必须大于 warmup,不从 `max_local_steps` 推导 |
| `min_lr_ratio` | 0.1 | warmup 后的 LR/base-LR 下限,范围 `(0, 1]`;超过上限后保持该非零下限 |

LR 进度使用 learner 启动以来已完成的累计 `local_step`。第 k 个 optimizer step 使用
`schedule(k-1)`;replace 或 fragment adoption 即使重建 AdamW 与 scheduler 对象,也会恢复到
当前累计步,因此 warmup 只发生一次。`completion_mode`、`max_local_steps` 与采纳次数均不改变
同一 scheduler 配置的逐步 LR。WSD 尚未实现,配置为 `wsd` 会 fail-closed。

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
| `tensor_dtype` | `float32` | learner update 的构造/落盘 dtype(`float32`/`bfloat16`/`float16`;syncer 读取时转换为 `syncer.compute_dtype`)。本仓库的 GPT-2 1L debug 与两种 50×10(50 内层步 × 10 外层步)对照配置使用 `bfloat16` |
| `atomic_write` | `true` | ⚠ 未消费(始终原子写) |
| `compute_sha256` | `false` | 上传时计算张量文件摘要(分析工具可校验完整性) |
| `checkpoint_digest_mode` | `off` | HA global weight/outer 摘要策略:`off` 只校验唯一 path、size 和 loadability;`checker` 仅 completed Checker 离线计算;`always` 在 publisher 关键路径计算并持久化。YAML 1.1 把未加引号的 `off` 解析为布尔 false 时也会规范化为字符串 `off` |

checkpoint/update 保留数量不再是配置项。syncer 的 maintenance 按权威引用集合自动保留一个 current global weight/outer、每个 fragment 一个 current weight/outer、latest 引用的一个物化完整权重、active DB proposal 载荷与固定提议指针;历史先归档后回收。归档 JSONL 只追加、运行时不回读;归档后尚待删除的载荷由 SQLite `gc_pending` 有界集合跨崩溃记忆。未发布孤儿的宽限为 `max(2 × heartbeat_interval_seconds, 2 × scan_interval_seconds)`。

## learner

| 字段 | 默认 | 说明 |
|---|---|---|
| `poll_latest_during_inner_steps` | `false` | 区间中途也轮询/采纳新版本 |
| `adopt_global_after_upload` | `true` | 每次上传后轮询/采纳(fragment 模式会等待一小段时间) |
| `global_adoption_strategy` | `replace` | full learner 的采纳方式:`replace` 直接替换本地权重;`rebase_post_publish_delta` 仅在发布后的检查没有新版时保留按 `syncer.compute_dtype` 构造的发布点,随后将尚未发布的本地差值合成到首个新 global;`predict_post_publish_global` 则用上一 merge token 规模、外层 momentum 与本地差值预测下一 global,在真实新版到达时对齐预测后的本地进展。rebase/prediction 算术优先用 learner GPU,OOM 风险时回退 CPU;fragment 只允许 replace |
| `post_publish_latest_wait_seconds` | 0.0 | 三种 full 策略共用的发布后等待时长;0 表示只做即时检查 |
| `post_publish_latest_poll_seconds` | 0.2 | 发布后等待及 latest 引用文件 GC 竞态重试的轮询间隔,必须大于 0 |
| `prediction.reconcile_timeout_seconds` | 60.0 | prediction reconcile 的等待上限;也作为所有 learner latest 权重加载遭遇 current-only GC 竞态时的总重试预算,必须大于 0 |

普通 `load_config/resolve_config` 对未知键 fail-closed。`sync.upload_mode`、`liveness.quorum_policy`、`inner_optimizer.reset_on_global_update` 已删除;再次出现会明确报「字段已移除」。旧的平铺键 `learner.prediction_reconcile_timeout_seconds` 也会提示新路径。只有 `load_resolved_config_snapshot()` 为读取历史 run 快照提供迁移:删除三个无替代旧键,并在新嵌套键缺失时把旧 prediction timeout 移入 `learner.prediction.reconcile_timeout_seconds`;这不是新配置文件的兼容别名。

5000-step full 配置启用 `poll_latest_during_inner_steps=true` 和
`global_adoption_strategy=rebase_post_publish_delta`:发布后仍只无阻塞检查一次 latest;若没有新版,
才保留发布点并在每个后续 `optimizer.step()` 后再检查;若第一次检查已有新版则直接采纳且不保留发布点。
延迟发现新版并完成变基后也立即释放发布点。直接采用新版仍重置 AdamW moments;scheduler
对象若重建会恢复累计 local-step 相位,不会再次 warmup。rebase/prediction 对齐保留整个
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
| `materialize_full_every_events` | `null` | fragment 开启时必须显式为正整数;每 N 个 merge event 重拼完整权重,所有正常终止路径还会强制最终物化。性能配置使用 10,逐事件调试配置使用 1;缺失、null、0 或负数 fail closed |

`materialize_full_every_events` 的 dataclass 缺省仍为 `null`,目的是让开启 fragment
却没有明确选择新鲜度/写放大的配置直接报错,而不是静默采用最昂贵的逐事件物化。三 seed
50×10 消融中,10 相对 1 把完整物化字节减少 90%、物化时间均值减少 81.45%,完整训练时间
均值减少 2.21%;单 seed 完整时间有波动,因此该字段仍要求按评估/恢复新鲜度显式选择。

## failure_sim(learner 故障注入)

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关 |
| `sleep_jitter_seconds` | 0.0 | 上传前随机睡 0~N 秒 |
| `upload_skip_probability` | 0.0 | 跳过本次上传的概率 |
| `crash_probability` | 0.0 | 上传后/skip 后调用 `sys.exit(97)` 的概率。`SystemExit` 仍执行 runner `finally`,因此当前实现会停资源监控并尝试写最终 stopped 心跳;fragment 下它不设置 `had_error`,还可能先跑有界 final wait。它不是 SIGKILL/节点失联模拟 |

## wandb(syncer 侧)

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | `$WANDB_DISABLED` 亦可关闭 |
| `mode` | `offline` | `$WANDB_MODE` 优先 |
| `entity` / `group` / `tags` | null / null / [] | group 缺省用 `run.name` |

W&B run 命名规则见 `observability/wandb_logging.py: syncer_wandb_run_name()`(时间戳 + run 名 + 模型 + 数据集 + learner 数 + quorum + 内层超参 + 外层优化器/lr 的 slug 拼接);project 固定 `fs-diloco-miyabi-syncer`。import/init 失败会被捕获并降级为不上报;初始化成功后的 `run.log/summary/finish` 调用并非全部有局部异常隔离,因此不能笼统承诺任意 W&B 运行期故障都不影响训练。

## 校验边界

`resolve_config()` 当前显式校验:scan interval 正数、staleness λ/最大陈旧度非负、syncer device/dtype 枚举、宽限模式与非负秒数、completion 模式及 `global_only` 的全局停止目标、可选 timeout 正数、scheduler/warmup/horizon/min-ratio 组合、HA/fragment/dynamic 模式矩阵、stream/bootstrap/capacity 关系、membership TTL/retention、scale 迟滞/预算/对账/walltime 以及 terminal policy/grace/merge bound、fragment 策略/片数/调度/materialize/adoption 组合、采纳策略组合、发布后 wait/poll,以及 `training.block_size=data.block_size`。它**没有**为每个数值字段做统一范围校验,例如 static quorum 的正数与顺序、训练 batch/step 数、legacy liveness 阈值顺序、故障概率、`model.dtype`、`io.tensor_dtype`、selection policy 和外层优化器名会在各自消费点才报错;其中未知 `model.dtype` 甚至由 `model_dtype()` 静默当作 FP32。文档中的「未知键 fail-closed」不应被理解为所有语义都在解析期验证。
