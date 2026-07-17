# 模块参考:fs_diloco/runtime/syncer.py

syncer 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 4、5 节;合并算法见 [02-architecture.md](../02-architecture.md) 第 4 节。

## CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--num-learners`。
- **`sqlite_path(config) -> Path`** — 唯一持久库路径:`<shared_root>/control/syncer_metadata.sqlite3`。
- **`run_identity(config)`** — 形成写入 DB 的 run/format/protocol/mode/model identity,恢复时逐项严格比对。
- **`resolve_syncer_device(config) -> torch.device`** — 解析 `syncer.device=auto/cpu/cuda`;显式 CUDA 不可用时 fail fast。
- **`syncer_compute_dtype(config)`** / **`syncer_publish_dtype(config)`** — 把 syncer 的计算/发布 dtype 配置解析为 torch dtype。
- **`align_state_to_publication_dtype(config, theta, state)`** — 每次发布前按发布 dtype 量化浮点权重/状态并转回计算 dtype,让持续运行、learner 可见 checkpoint 与 resume 共享同一数值边界。
- **`publication_failpoint(name)`** — 由环境变量控制的确定性崩溃注入点,用于 publication crash matrix。
- **`main(argv)`** — `resolve_config` 后调 `run_syncer`。
- **`run_syncer(config)`** — 公共启动(目录、SQLite、日志、设备、W&B)后分派:fragment 模式 → `run_fragment_syncer`;否则按 `init.resume` 走 `resume_run`/`initialize_run`,再执行全量模式主循环(函数体内,详见下文)。

## 发布

- **`latest_payload(*, config, paths, version, weight_path, optim_path, total_seen_tokens) -> dict`** — 全量模式 `latest.json` 内容。
- **`publish_global(..., selected_updates, effective_weights, predecessor_version)`** — 一个全量版本的完整发布序列:权重 → outer → `initialize_full_run` 或 `commit_full_merge` 单事务 → **最后原子写 `latest.json`**。事务成功是正确性边界;latest 只是缓存。支持 weight temp、weight 后、outer 后、事务内、DB commit 后、latest 后六个 failpoint。
- **`fragment_latest_payload(*, ..., global_merge_event, fragment_versions, fragment_updated_events, materialized_weight_path)`** — fragment 布局的 latest 内容(`latest_kind: "fragment"`,每片版本/路径/最后更新事件)。
- **`should_materialize_fragment_full(config, global_merge_event) -> bool`** — 是否本事件重拼完整权重:事件 0、达到 `stop_after_outer_steps`、或按 `materialize_full_every_events` 取模;interval 未设/≤0 时每次都拼。
- **`publish_fragment_latest(*, ...) -> (materialized_weight_path, materialize_seconds)`** — 按需 materialize 完整权重存为 `weights/global_v{event:06d}.safetensors`,再原子写 fragment latest;不 materialize 时沿用上一次的路径。
- **`publish_stop(paths, *, config, reason, version, total_seen_tokens)`** — 原子写 `control/stop.json`。

## 初始化与恢复

- **`initialize_run(config, paths, store, logger, *, device) -> (version=0, theta, outer_state, param_index, total_seen_tokens=0)`** — 全新 run:DB committed 防覆盖 → 按 `syncer.compute_dtype` 加载模型/index/θ/outer → 按 `syncer.publish_dtype` 发布 index/config/checkpoint → `publish_global(0)` 在一个事务写 v0+identity+config → maintenance。
- **`initialize_fragment_run(config, paths, store, logger, *, device) -> (event=0, fragment_thetas, outer_states, param_index, fragment_index, fragment_versions, fragment_updated_events, total_seen_tokens=0, materialized_weight_path)`** — fragment 版:另建并发布 fragment index;逐片抽取 θ_f、建状态、存 v0、写 `fragments`/`fragment_versions` 表;materialize 事件 0 并发布 fragment latest。
- **`resume_run(config, paths, store, logger, *, device)`** — DB-first 恢复(仅全量模式):integrity → identity/protocol → 最大 committed row → model/index → 引用文件存在 → 浮点状态转换到 `syncer.compute_dtype` → weight θ 与 outer θ 精确相等 → 全部 selected 回滚 → 重建 latest → maintenance。任何权威状态缺失/冲突都 fail closed;不读取 latest 决定版本,不回退 DB dump。

## 摄取(共享盘 → SQLite)

- **`validate_update_metadata(payload, *, config, paths) -> bool`** — 元数据准入:format_version、run_id、learner_id 合法;fragment 模式要求 `update_kind == "fragment"`、fragment_id 在界内、fragment 专属字段齐全(全量模式反之拒绝 fragment 更新);张量文件必须存在。
- **`ingest_update_metadata(store, paths, config, logger) -> int`** — 全量模式每轮读取恰好 `num_learners` 个 `updates/latest/learner_XXX.json` 并 latest-wins 摄取;fragment 模式扫描 payload 目录中的 fragment meta。返回新插入数。
- **`sync_liveness_and_metadata(store, paths, config, logger)`** — 每轮例行:摄取心跳 → 重分类 liveness → 摄取元数据。
- update 元数据中的 learner 资源字段随 `updates`/`fragment_updates` 行持久化;已有 SQLite 文件在连接时用兼容迁移补齐新列。

## 选择

- **`drop_missing_update_files(store, updates, logger) -> list`** / **`drop_missing_fragment_update_files(...)`** — 过滤掉张量文件已消失的行(库中标 `dropped(missing_file)`),返回存活子集。
- **`configured_grace_seconds(config)`** — fixed 模式返回 `min(fixed_seconds,max_seconds)`,adaptive 模式返回 `min(initial_seconds,max_seconds)`。
- **`fastest_next_upload_eta_seconds(updates, *, inner_steps, now)`** — 用各已选 update 的 `committed_at + local_cycle_step_time_seconds_mean × inner_steps` 估计下一上传时间,返回最快剩余秒数;序列化与版本采纳开销不计入,自然形成保守余量。
- **`collect_with_grace_window(store, paths, config, logger, *, current_version) -> list`** — 宽限窗口收集:循环【查合格 → 滤丢失 → 每 learner 选一】。adaptive 模式每轮用最快上传 ETA 向前收紧 deadline,不允许后续估计把窗口延长;凑满 `quorum_max` 或 deadline 耗尽即结束,并记录 started/shortened/completed 事件。fragment 版逻辑相同、按片过滤。
- **`all_expected_learners_stopped(store, config) -> bool`** — 只有全部预期 learner 都存在且最终状态明确为 `stopped` 才证明输入闭合;dead/step 达标不够。
- **`select_terminal_drain_updates(...)`** — 输入闭合后的全量末端排空:仍执行严格 future/staleness 准入与 missing-file 检查,按 `oldest_pending` 每 learner 选一,允许低于 quorum;无合法 proposal 时由主循环以 `input_exhausted` 停止。

## 观测与辅助

- **`init_wandb_run(*, config, paths, logger, device, hostname) -> run | None`** — W&B 初始化(项目/名称/标签/config 由 `observability/wandb_logging.py` 生成;`syncer/version` 定义为 step 轴);禁用、import 失败、init 失败都返回 None 并记日志,不影响训练。
- **`_fragment_staleness_stats(selected, current_fragment_version)`**(私有)— 选中集合的 staleness min/mean/max。
- **`wait_for_learner_shutdown(...)`** — 发布 stop 后在有界窗口内持续摄取 heartbeat 与 update 元数据;只有 SQLite `learners` 表中的全部预期 learner 都成为 stopped 才返回成功,从而保证磁盘 heartbeat、持久 DB 与 summary 的终态一致。
- **`learner_resource_summary(...)`** — 合并 SQLite 历史 update 与最终 stopped 心跳,生成逐 learner 训练期 CPU/GPU 峰值及 max/mean 聚合。
- **`write_training_summary(...)`** — 原子写 `control/summary.json`,包含 syncer 启动至 learner 全部停止的完整训练时间、最终版本/token 数/停止原因及 learner 资源峰值;同样更新 W&B run summary。

## 主循环

### 全量模式(`run_syncer` 内)

每次迭代尝试 `v → v+1`,详细伪代码见 [03-runtime-flow.md](../03-runtime-flow.md#4-syncer-主循环全量模式)。关键实现细节:

- 选中后、读取前**再次**检查文件存在性;`load_update_vector` 期间竞态出现 `FileNotFoundError` 时同样处理:丢失者 `dropped`,其余 `reset_selected_to_pending` 回滚,放弃本次合并;
- `run_selection_id = f"{run_id}_v{v+1:06d}"` 写入 selected_by_run,便于审计"哪次合并选了它";
- 每次合并的 applied/superseded/stale/future 状态都由 `commit_full_merge` 与 global row 同事务提交;
- 全部 stopped 后先等待一个 grace/reingest 周期,再 terminal drain;若严格准入后无 proposal 则 `input_exhausted`,不会等待 no-progress timeout;
- 每次成功合并后刷新 `last_progress_time`;quorum 等待期间超过 `no_progress_timeout_seconds` 即停机;
- 每次成功提交后执行 archive/GC,因此 active DB/checkpoint/proposal 面有界;
- finally 序列:`publish_stop` → 等待 learner stopped 心跳/继续摄取 → 正常闭合时终态化未消费 proposal → summary → archive/GC → W&B/close。

### fragment 模式(`run_fragment_syncer`)

结构同上,差异:

- 开头 `raise NotImplementedError` 拦截 resume;
- 每轮以 `select_fragment(global_merge_event, K)` 确定目标片;资格、quorum、宽限窗口、staleness、superseded/obsolete 丢弃全部**按片**进行;
- 合并成功后:片版本 +1、事件 +1,存片权重/优化器状态、写 `fragment_versions` 行,`publish_fragment_latest`(内含按需 materialize);
- quorum 不足只能等待或 `no_progress_timeout`(无 terminal merge);
- finally:非 error 时先做一次**最终 materialize + 发布**,再走 stop/末次摄取/终态化/archive/GC/W&B/close 序列。
