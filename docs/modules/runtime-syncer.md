# 模块参考:fs_diloco/runtime/syncer.py

syncer 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 4、5 节;合并算法见 [02-architecture.md](../02-architecture.md) 第 4 节。

## CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--sqlite-local-dir`、`--num-learners`。
- **`sqlite_path(config) -> Path`** — 本地库路径:`{io.sqlite_local_dir | $TMPDIR/fs_diloco/<run_id>}/syncer_metadata.sqlite3`。
- **`main(argv)`** — `resolve_config` 后调 `run_syncer`。
- **`run_syncer(config)`** — 公共启动(目录、SQLite、日志、设备、W&B)后分派:fragment 模式 → `run_fragment_syncer`;否则按 `init.resume` 走 `resume_run`/`initialize_run`,再执行全量模式主循环(函数体内,详见下文)。

## 发布

- **`latest_payload(*, config, paths, version, weight_path, optim_path, total_seen_tokens) -> dict`** — 全量模式 `latest.json` 内容。
- **`publish_global(*, config, paths, store, version, theta, outer_state, param_index, num_updates, total_update_tokens, total_seen_tokens)`** — 一个版本的完整发布序列:存权重 → 存外层状态 → 写 `global_versions` 行 → **最后原子写 `latest.json`**(顺序保证 learner 看到指针时数据必已就绪)。
- **`fragment_latest_payload(*, ..., global_merge_event, fragment_versions, fragment_updated_events, materialized_weight_path)`** — fragment 布局的 latest 内容(`latest_kind: "fragment"`,每片版本/路径/最后更新事件)。
- **`should_materialize_fragment_full(config, global_merge_event) -> bool`** — 是否本事件重拼完整权重:事件 0、达到 `stop_after_outer_steps`、或按 `materialize_full_every_events` 取模;interval 未设/≤0 时每次都拼。
- **`publish_fragment_latest(*, ...) -> (materialized_weight_path, materialize_seconds)`** — 按需 materialize 完整权重存为 `weights/global_v{event:06d}.safetensors`,再原子写 fragment latest;不 materialize 时沿用上一次的路径。
- **`publish_stop(paths, *, config, reason, version, total_seen_tokens)`** — 原子写 `control/stop.json`。

## 初始化与恢复

- **`initialize_run(config, paths, store, logger, *, device) -> (version=0, theta, outer_state, param_index, total_seen_tokens=0)`** — 全新 run:防覆盖检查 → 加载模型 → 构建 param index → flatten θ → 初始化外层状态 → 发布 param_index/解析配置快照 → `publish_global(0)` → 配置存入 run_state。
- **`initialize_fragment_run(config, paths, store, logger, *, device) -> (event=0, fragment_thetas, outer_states, param_index, fragment_index, fragment_versions, fragment_updated_events, total_seen_tokens=0, materialized_weight_path)`** — fragment 版:另建并发布 fragment index;逐片抽取 θ_f、建状态、存 v0、写 `fragments`/`fragment_versions` 表;materialize 事件 0 并发布 fragment latest。
- **`_resume_latest_payload(config, paths)`**(私有)— `resume_version == "latest"` 时读 latest.json,否则按指定版本号构造等效 payload。
- **`_newest_db_dump(paths, version)`**(私有)— `db_dumps/` 中匹配 `metadata_*_v{version:06d}.db` 的最新一份。
- **`resume_run(config, paths, store, logger, *, device)`** — 恢复(仅全量模式):校验 param index 兼容 → 加载权重与外层状态(**优化器文件中的 theta 与权重同长时以前者为准**,保证 θ 与优化器状态出自同一原子快照)→ 本地库为空时从 dump 恢复 → 以 `notes="resumed"` upsert 版本行。

## 摄取(共享盘 → SQLite)

- **`validate_update_metadata(payload, *, config, paths) -> bool`** — 元数据准入:format_version、run_id、learner_id 合法;fragment 模式要求 `update_kind == "fragment"`、fragment_id 在界内、fragment 专属字段齐全(全量模式反之拒绝 fragment 更新);张量文件必须存在。
- **`ingest_update_metadata(store, paths, config, logger) -> int`** — 扫描 `updates/pending/learner_*/update_*.meta.json`,合法者入库(`INSERT OR IGNORE`,唯一约束幂等),返回新插入数。
- **`sync_liveness_and_metadata(store, paths, config, logger)`** — 每轮例行:摄取心跳 → 重分类 liveness → 摄取元数据。

## 选择

- **`drop_missing_update_files(store, updates, logger) -> list`** / **`drop_missing_fragment_update_files(...)`** — 过滤掉张量文件已消失的行(库中标 `dropped(missing_file)`),返回存活子集。
- **`collect_with_grace_window(store, paths, config, logger, *, current_version) -> list`** — 宽限窗口收集:循环【查合格 → 滤丢失 → 每 learner 选一】,凑满 `quorum_max` 或窗口(`min(fixed_seconds, max_seconds)`)耗尽为止;每轮间 sleep 并重新摄取。fragment 版 **`collect_fragment_with_grace_window(..., fragment_id, current_fragment_version)`** 逻辑相同、按片过滤。
- **`finite_local_training_complete(store, config) -> bool`** — 有限步训练判定:库中已知 learner 数 ≥ num_learners 且全部 `last_local_step ≥ max_local_steps`。
- **`select_terminal_drain_updates(store, paths, config, logger, *, current_version) -> list`** — 末端排空(仅全量模式):目标外层步数未达、全部 learner 已训完时,对**全部 pending**(不限 staleness)按 `oldest_pending` 每 learner 选一;否则返回空。

## 观测与辅助

- **`dump_db(store, paths, version, logger)`** — backup 到 `db_dumps/metadata_{ts}_v{version:06d}.db`。
- **`init_wandb_run(*, config, paths, logger, device, hostname) -> run | None`** — W&B 初始化(项目/名称/标签/config 由 `observability/wandb_logging.py` 生成;`syncer/version` 定义为 step 轴);禁用、import 失败、init 失败都返回 None 并记日志,不影响训练。
- **`_fragment_staleness_stats(selected, current_fragment_version)`**(私有)— 选中集合的 staleness min/mean/max。

## 主循环

### 全量模式(`run_syncer` 内)

每次迭代尝试 `v → v+1`,详细伪代码见 [03-runtime-flow.md](../03-runtime-flow.md#4-syncer-主循环全量模式)。关键实现细节:

- 选中后、读取前**再次**检查文件存在性;`load_update_vector` 期间竞态出现 `FileNotFoundError` 时同样处理:丢失者 `dropped`,其余 `reset_selected_to_pending` 回滚,放弃本次合并;
- `run_selection_id = f"{run_id}_v{v+1:06d}"` 写入 selected_by_run,便于审计"哪次合并选了它";
- terminal drain 轮次跳过 `drop_obsolete_updates`(排空时旧更新是主角);
- 每次成功合并后刷新 `last_progress_time`;quorum 等待期间超过 `no_progress_timeout_seconds` 即停机;
- finally 序列:`publish_stop` → 最终 `dump_db` → W&B summary/finish → `store.close()`(嵌套 try/finally 保证逐层执行)。

### fragment 模式(`run_fragment_syncer`)

结构同上,差异:

- 开头 `raise NotImplementedError` 拦截 resume;
- 每轮以 `select_fragment(global_merge_event, K)` 确定目标片;资格、quorum、宽限窗口、staleness、superseded/obsolete 丢弃全部**按片**进行;
- 合并成功后:片版本 +1、事件 +1,存片权重/优化器状态、写 `fragment_versions` 行,`publish_fragment_latest`(内含按需 materialize);
- quorum 不足只能等待或 `no_progress_timeout`(无 terminal drain);
- finally:非 error 时先做一次**最终 materialize + 发布**,再走 stop/dump/W&B/close 序列。
