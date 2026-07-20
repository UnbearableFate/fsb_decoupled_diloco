# 模块参考:fs_diloco/runtime/syncer.py

syncer 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 4、5 节;合并算法见 [02-architecture.md](../02-architecture.md) 第 4 节。

## CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--num-learners`,以及实验覆盖参数:`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`(与 learner 的 parse_args 对称)。
- **`sqlite_path(config) -> Path`** — 唯一持久库路径:`<shared_root>/control/syncer_metadata.sqlite3`。
- **`run_identity(config)`** — 形成写入 DB 的 run/format/protocol/mode/model identity,恢复时逐项严格比对。
- **`resolve_syncer_device(config) -> torch.device`** — 解析 `syncer.device=auto/cpu/cuda`;显式 CUDA 不可用时 fail fast。
- **`syncer_compute_dtype(config)`** / **`syncer_publish_dtype(config)`** — 把 syncer 的计算/发布 dtype 配置解析为 torch dtype。
- **`maybe_capture_terminal_predecessor_for_eval(...)`** — 默认关闭的 full terminal-partial 研究捕获；从当前权威 weight 建 hardlink（失败则原子 copy），写入 checksum/版本/selected/quorum manifest。返回值只供事件日志，路径不进入 DB/latest/resume。
- **`align_state_to_publication_dtype(config, theta, state, *, roundtrip_metrics_out=None)`** — 每次发布前按发布 dtype 量化浮点权重/状态并转回计算 dtype,让持续运行、learner 可见 checkpoint 与 resume 共享同一数值边界；可在覆盖原值前记录 chunked L2/L∞/relative-L2 量化误差。
- **`publication_failpoint(name)`** — 由环境变量控制的确定性崩溃注入点,用于 publication crash matrix。
- **`main(argv)`** — `resolve_config` 后调 `run_syncer`。
- **`run_syncer(config)`** — 公共启动(目录、SQLite、日志、设备、W&B)后分派:fragment 模式 → `run_fragment_syncer`;否则按 `init.resume` 走 `resume_run`/`initialize_run`,再执行全量模式主循环(函数体内,详见下文)。

## 发布

- **`latest_payload(*, config, paths, version, weight_path, optim_path, total_seen_tokens) -> dict`** — 全量模式 `latest.json` 内容。
- **`publish_global(..., selected_updates, effective_weights, predecessor_version)`** — 一个全量版本的完整发布序列:默认两个 worker 并发原子写权重/outer → 主线程等待双成功 → `initialize_full_run` 或 `commit_full_merge` 单事务 → **最后原子写 `latest.json`**。worker 不接触 DB/latest，单侧失败不会提交。`syncer.parallel_checkpoint_writes=false` 只为串行消融恢复 weight→outer 写序；事务边界不变。`sync.ingest_during_publish=true` 时，等待 future 的主线程可以串行摄取 heartbeat/pointer 元数据，但不能 selection、maintenance 或提交版本；两个文件都成功后才跨越事务边界。返回两个 worker 时长、checkpoint walltime、dtype/bytes、等待期摄取计数与 round-trip 误差。事务成功是正确性边界;latest 只是缓存。支持 weight temp、weight 后、outer 后、事务内、DB commit 后、latest 后六个 failpoint。
- **`fragment_latest_payload(*, ..., global_merge_event, fragment_versions, fragment_updated_events, materialized_weight_path)`** — fragment 布局的 latest 内容(`latest_kind: "fragment"`,每片版本/路径/最后更新事件)。
- **`should_materialize_fragment_full(config, global_merge_event) -> bool`** — 是否本事件重拼完整权重:事件 0、达到 `stop_after_outer_steps`、或按显式正整数 `materialize_full_every_events` 取模；fragment 配置缺失/null/≤0 会在启动时 fail closed。
- **`publish_fragment_latest(*, ..., force_materialize=False) -> FragmentLatestPublication`** — 按需 materialize 完整权重存为 `weights/global_v{event:06d}.safetensors`,再原子写 fragment latest；返回路径、耗时、是否发生和字节数。不 materialize 时沿用上一次路径；正常终止以 `force_materialize=true` 保证最终权重对应最终 fragment state。
- **`publish_stop(paths, *, config, reason, version, total_seen_tokens)`** — 原子写 `control/stop.json`。

## 初始化与恢复

- **`initialize_run(config, paths, store, logger, *, device) -> (version=0, theta, outer_state, param_index, total_seen_tokens=0)`** — 全新 run:DB committed 防覆盖 → 按 `syncer.compute_dtype` 加载模型/index/θ/outer → 按 `syncer.publish_dtype` 发布 index/config/checkpoint → `publish_global(0)` 在一个事务写 v0+identity+config → maintenance。
- **`initialize_fragment_run(config, paths, store, logger, *, device) -> (event=0, fragment_thetas, outer_states, param_index, fragment_index, fragment_versions, fragment_updated_events, total_seen_tokens=0, materialized_weight_path)`** — fragment 版:另建并发布 fragment index;逐片抽取 θ_f、建状态、存 v0、写 `fragments`/`fragment_versions` 表;materialize 事件 0 并发布 fragment latest。
- **`resume_run(config, paths, store, logger, *, device)`** — DB-first 恢复(仅全量模式):integrity → identity/protocol → 最大 committed row → model/index → 引用文件存在 → 浮点状态转换到 `syncer.compute_dtype` → weight θ 与 outer θ 精确相等 → 全部 selected 回滚 → 重建 latest → maintenance。任何权威状态缺失/冲突都 fail closed;不读取 latest 决定版本,不回退 DB dump。

## 摄取(共享盘 → SQLite)

- **`validate_update_metadata(payload, *, config, paths) -> bool`** — 元数据准入:format_version、run_id、learner_id 合法;fragment 模式要求 `update_kind == "fragment"`、fragment_id 在界内、fragment 专属字段齐全(全量模式反之拒绝 fragment 更新);张量文件必须存在。
- **`ingest_update_metadata(store, paths, config, logger) -> int`** — 全量模式每轮读取恰好 `num_learners` 个 `updates/latest/learner_XXX.json`；fragment 模式枚举配置决定的 `num_learners × num_fragments` 个 `updates/latest/learner_XXX_fNNN.json`。两者都以固定 pointer latest-wins 摄取；fragment 的持久 frontier 与进程内文件 signature 会短路重放/重复 JSON 解析，不扫描历史 payload meta。返回新插入数。
- **`sync_liveness_and_metadata(store, paths, config, logger)`** — 每轮例行:摄取心跳 → 重分类 liveness → 摄取元数据。
- update 元数据中的 learner 资源字段随 `updates`/`fragment_updates` 行持久化;已有 SQLite 文件在连接时用兼容迁移补齐新列。

## 选择

- **`UpdateProposalSource` / `full_update_proposal_source(...)` / `fragment_update_proposal_source(...)`** — 参数化 full/fragment 的候选枚举、staleness 键、缺文件降级动作、事件名与上下文字段；两种磁盘协议保持各自原有事件 payload。
- **`drop_missing_update_files(store, updates, logger, *, source) -> list`** — 共享过滤骨架；张量文件消失时经 source 在对应表中标 `dropped(missing_file)`，发 full 或 fragment 原事件并返回存活子集。
- **`configured_grace_seconds(config)`** — fixed 模式返回 `min(fixed_seconds,max_seconds)`,adaptive 模式返回 `min(initial_seconds,max_seconds)`。
- **`UpdateFirstSeenRegistry` / `update_first_seen_capacity(config)`** — syncer 进程内、有界、插入有序的 update 首见时钟域；首次成功 DB insert 记录 monotonic+wall，重复 ingest 不刷新，应用/明确 missing 主动删除，FIFO 淘汰只会让 ETA 保守缺失。容量为 `max(64, 4 × learners × max(1, fragments))`。
- **`interval_breakdown(...)`** — 用单调时钟校验一次 merge interval 的 discovery、idle polling、grace、read、merge、publish、maintenance 分量互不重叠并显式给出 residual；full/fragment 主循环跨多次 quorum wait 累计这些计数，只在成功 merge 后重置。
- **`fastest_next_upload_eta_seconds(updates, *, first_seen, inner_steps, now_monotonic)`** — 用各已选 update 的 `first_seen_monotonic + local_cycle_step_time_seconds_mean × inner_steps` 估计下一上传时间,返回最快剩余秒数；不读取 learner `committed_at`，因此不受跨节点 wall-clock 偏差影响。
- **`collect_with_grace_window(store, paths, config, logger, *, source, first_seen) -> list`** — full/fragment 共用的宽限窗口骨架:循环【source 查合格 → 共享缺文件过滤 → 每 learner 选一】。adaptive 模式每轮用最快上传 ETA 向前收紧 deadline,不允许后续估计把窗口延长;凑满 `quorum_max` 或 deadline 耗尽即结束,并按 source 保留原 started/shortened/completed 上下文字段。resume 中无 registry 项的旧 update 跳过估计。
- **`all_expected_learners_stopped(store, config) -> bool`** — 只有全部预期 learner 都存在且最终状态明确为 `stopped` 才证明输入闭合;dead/step 达标不够。
- **`select_terminal_drain_updates(...)`** — 输入闭合后的全量末端排空:仍执行严格 future/staleness 准入与 missing-file 检查,按配置策略每 learner 选一,允许低于 quorum;无合法 proposal 时由主循环以 `input_exhausted` 停止。
- **`select_terminal_drain_fragment_updates(...)`** — fragment 对应入口：对当前调度目标片执行严格 future/staleness/missing-file 准入，复用共享 selector 并允许低于 quorum；目标片耗尽即结束，不跨过 round-robin 顺序消费其他片。
- **`merge_staleness_evidence(...)`** — 按实际 normalized merge weights 计算 effective staleness 均值、fresh 权重质量和 staleness count JSON；full 使用 global base，fragment 使用目标片 base。

## 观测与辅助

- **`init_wandb_run(*, config, paths, logger, device, hostname) -> run | None`** — W&B 初始化(项目/名称/标签/config 由 `observability/wandb_logging.py` 生成;`syncer/version` 定义为 step 轴);禁用、import 失败、init 失败都返回 None 并记日志,不影响训练。
- **`_fragment_staleness_stats(selected, current_fragment_version)`**(私有)— 选中集合的 staleness min/mean/max。
- **`learner_shutdown_timeout_seconds(config)`** — 显式 `liveness.learner_shutdown_timeout_seconds` 优先；null 时返回 `max(120, 2 × heartbeat_interval_seconds)`，不再用旧的 120 秒上限。
- **`wait_for_learner_shutdown(...)`** — 发布 stop 后在该有界窗口内持续摄取 heartbeat 与 update 元数据;只有 SQLite `learners` 表中的全部预期 learner 都成为 stopped 才返回成功。超时事件逐 learner 记录当前 status/status_reason/last_seen；安全地跳过未确认场景的强制 proposal 终态化。
- **`learner_resource_summary(...)`** — 合并 SQLite 历史 update 与最终 stopped 心跳,生成逐 learner 训练期 CPU/GPU 峰值及 max/mean 聚合。
- **`write_training_summary(...)`** — 原子写 `control/summary.json`,包含 syncer 启动至 learner 全部停止的完整训练时间、最终版本/token 数/停止原因及 learner 资源峰值;同样更新 W&B run summary。

## 主循环

### 全量模式(`run_syncer` 内)

每次迭代尝试 `v → v+1`,详细伪代码见 [03-runtime-flow.md](../03-runtime-flow.md#4-syncer-主循环全量模式)。关键实现细节:

- 选中后、读取前**再次**检查文件存在性;`load_update_vector` 期间竞态出现 `FileNotFoundError` 时同样处理:丢失者 `dropped`,其余 `reset_selected_to_pending` 回滚,放弃本次合并;
- `run_selection_id = f"{run_id}_v{v+1:06d}"` 写入 selected_by_run,便于审计"哪次合并选了它";
- 每次合并的 applied/superseded/stale/future 状态都由 `commit_full_merge` 与 global row 同事务提交;
- 全部 stopped 后先等待一个 grace/reingest 周期,再 terminal drain;若严格准入后无 proposal 则 `input_exhausted`,不会等待 no-progress timeout;
- fragment 主循环使用同一 input-closed 判定与 grace/reingest 生命周期；每次 terminal merge 推进 global event 后重新计算目标片，最终 pending/selected fragment proposal 由统一 shutdown 终态化。
- 每次成功合并后刷新 `last_progress_time`;quorum 等待期间超过 `no_progress_timeout_seconds` 即停机;
- 每次成功提交后执行 archive/GC,因此 active DB/checkpoint/proposal 面有界;
- finally 序列:`publish_stop` → 等待 learner stopped 心跳/继续摄取 → 正常闭合时终态化未消费 proposal → summary → archive/GC → W&B/close。

### fragment 模式(`run_fragment_syncer`)

结构同上,差异:

- 开头 `raise NotImplementedError` 拦截 resume;
- 每轮以 `select_fragment(global_merge_event, K)` 确定目标片;资格、quorum、宽限窗口、staleness、superseded/obsolete 丢弃全部**按片**进行;
- 合并成功后:片版本 +1、事件 +1,存片权重/优化器状态、写 `fragment_versions` 行,`publish_fragment_latest`(内含按需 materialize);
- 输入闭合(全部预期 learner stopped)后与 full 一样走 terminal grace/drain:`select_terminal_drain_fragment_updates` 对当前目标片按严格准入放宽 quorum 合并,目标片无合法 pending 即 `input_exhausted`;输入未闭合时 quorum 不足只能等待或 `no_progress_timeout`;
- finally:非 error 时先做一次**最终 materialize + 发布**,再走 stop/末次摄取/终态化/archive/GC/W&B/close 序列。
