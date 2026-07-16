# 模块参考:fs_diloco/storage

文件系统 I/O 与元数据持久化:原子写、safetensors 编解码、路径布局、持久 SQLite 状态机、归档与引用驱动 GC。

---

## storage/atomic_io.py — 原子文件系统助手

所有共享状态发布的唯一原语:**写临时文件 → fsync → `os.replace()` 原子改名**。同目录 rename 在 POSIX/Lustre 上原子,读者永远不会看到半截文件。

- **`ensure_dir(path) -> Path`** — `mkdir -p`。
- **`atomic_write_bytes(path, data, mode=0o644) -> Path`** — 在目标目录内建 `mkstemp` 临时文件(`.{name}.*.tmp`),写入 + flush + fsync,chmod 后 `os.replace` 到目标名;失败时清理临时文件并重抛。
- **`atomic_write_text(path, text)`** — UTF-8 编码后走 `atomic_write_bytes`。
- **`atomic_write_json(path, payload)`** — `json.dumps(sort_keys=True, indent=2)` 后原子写;所有心跳/元数据/latest/stop 都经此发布。
- **`atomic_write_with_writer(path, writer)`** — 先建临时文件,把路径交给回调 `writer(tmp_path)` 写内容(供 safetensors 的 `save_file` 使用),再 fsync + replace。
- **`read_json(path) -> dict`** — 直接读,失败抛异常。
- **`safe_read_json(path) -> dict | None`** — 读失败(不存在/损坏 JSON)返回 None;轮询共享文件的标准姿势。
- **`sha256_file(path, chunk_size=1MiB) -> str`** — 分块计算摘要。
- **`file_size(path) -> int`** — `stat().st_size`。
- **`wait_for_file(path, timeout_seconds, poll_seconds=1.0)`** — 轮询等待文件出现;超时抛 `TimeoutError`(当前 runtime 使用的是 learner 侧更严格的 `wait_for_json`)。

## storage/paths.py — 目录布局

- **`RunPaths(shared_root)`**(frozen dataclass)— 共享目录布局的**唯一权威定义**。属性:`control / weights / optim / updates_latest / updates_payloads / fragments / fragment_weights / fragment_optim / heartbeats / logs / metrics`,文件:`latest_json / stop_json / summary_json / param_index_json / fragment_index_json / resolved_config_yaml / sqlite_db / update_history_jsonl / global_version_history_jsonl`。方法:
  - **`update_pointer_path(learner_id)`** / **`update_payload_dir(learner_id)`** — 全量固定 proposal pointer 与不可变 payload 目录;
  - **`global_weight_path(version)`** / **`outer_optim_path(version)`** — 按模板拼版本化文件名;
  - **`fragment_weight_path(fragment_id, version)`** / **`fragment_outer_optim_path(...)`** — `fragments/{weights|optim}/fragment_{id:03d}/v{version:06d}.safetensors`。
- **`prepare_run_dirs(paths, num_learners)`** — 建齐固定目录 + 每 learner 的 payload 子目录(幂等;learner 和 syncer 启动时都会调用)。

## storage/tensor_codec.py — safetensors 编解码

- **`dtype_from_name(name) -> torch.dtype`** — `"float32"/"bf16"/"fp16"` 等别名 → torch dtype;未知报错。
- **`save_safetensors_atomic(path, tensors)`** — 张量搬到 CPU、contiguous 后用 `atomic_write_with_writer` + `safetensors.save_file` 原子保存。
- **`load_safetensors(path, device)`** — `safetensors.load_file` 薄封装。
- **`save_update_vector(path, flat, dtype)`** / **`load_update_vector(path, device)`** — update 扁平向量,单键 `local_params`;可用 BF16 落盘,加载时统一转 float32。
- **`save_global_weights(path, theta, param_index)`** — 扁平 θ 先经 `flat_to_named_tensors` 还原为命名张量再存(每参数一键,可独立加载/导出)。
- **`load_global_weights_flat(path, param_index, device) -> Tensor`** — 逆向:命名张量 → 扁平向量。
- **`save_outer_state(path, theta, state)`** / **`load_outer_state(path, device) -> (theta, state)`** — 外层优化器状态,`theta` + 各状态张量平铺为键;加载时 theta 转 float32、缺 `step` 时补 0。

## storage/sqlite_store.py — 权威持久元数据库

数据库固定在共享 run 的 `control/syncer_metadata.sqlite3`;schema 见 `schema.sql` 与 [04-data-flow.md](../04-data-flow.md) 第 4 节。业务写入由 syncer 串行执行,节点切换后可直接重开同一文件。

### 模块级函数

- **`connect(path) -> sqlite3.Connection`** — 建父目录、连接(`timeout=60s`)、`row_factory=Row`,强制 `journal_mode=DELETE`、`synchronous=FULL`、busy timeout,执行幂等 schema/资源列迁移。
- **`row_to_dict(row) -> dict | None`** — Row → dict 便捷转换。

### class SQLiteStore

通用:

- **`__init__(path)` / `close()` / `execute(sql, params)`** — 连接管理与单语句直通执行。
- **`integrity_check()` / `pragma_settings()`** — 验证 DB 完整性并报告 journal/synchronous/busy timeout。
- **`committed_global_count()` / `latest_global_version()`** — 查询活跃 DB 中唯一 current committed global。
- **`set_run_state(key, value)` / `get_run_state(key)`** — JSON 值的 kv upsert/读取。

全局版本 / learner:

- **`initialize_full_run(...)`** — 一个事务写入 committed v0、run identity 与配置快照;拒绝覆盖已有 committed run。
- **`commit_full_merge(...)`** — 全量 `N→N+1` 的唯一事务边界:校验前驱/目标、selected 行与 learner 唯一性、future/stale 准入、归一化权重,插入 global row,写 applied 字段并终态化 superseded/stale/future 行;任何异常整笔 rollback。
- **`upsert_global_version(...)`** — fragment/兼容路径的版本行写入。
- **`get_global_version(version)`**。
- **`upsert_learner(learner_id, *, hostname, pid, last_seen, ..., status, status_reason)`** — 心跳快照 upsert;已有行的字段用 `COALESCE` 保留旧值(心跳缺字段不清空),status 总是覆盖。
- **`update_learner_status(learner_id, status, reason)`** / **`list_learners()`**。

全量模式 update 状态机:

- **`insert_update_metadata(metadata, *, pointer_path) -> bool`** — latest-wins 摄取固定 pointer:相同 frontier 重放忽略;新的 pending proposal 终态化同 learner 旧 pending,但不覆盖 selected;同时推进 `proposal_frontiers`。
- **`pending_updates()`** — 全部 pending,按 committed_at 升序。
- **`eligible_updates(current_version, max_staleness_versions)`** — pending 且 staleness 在窗口内。
- **`mark_updates_selected(update_ids, selected_by_run)`** — 条件转移 `pending → selected`(记 selected_at/selected_by_run)。
- **`mark_updates_applied(updates, *, applied_version, effective_weights)`** — `→ applied`,记录 applied_at、staleness_versions(=applied_version−1−base)、实际合并权重。
- **`reset_selected_to_pending(update_ids)`** — 合并中途失败的回滚(仅 `selected → pending`)。
- **`reset_all_selected_to_pending()`** — DB-first resume 时回滚崩溃遗留的全部 selected。
- **`drop_updates(update_ids, reason)`** — `pending|selected → dropped`,记 drop_reason。
- **`drop_obsolete_updates(current_version, max_staleness) -> int`** — 批量把过窗 pending 置为 `dropped("stale")`,返回行数。
- **`drop_superseded_updates(selected_updates, reason="superseded") -> int`** — 对每个被选中更新,把同 learner 的更旧 pending(local_step_end 更小,或同步数且 committed 更早)置为 dropped。
- **`get_update(update_id)`**。

归档/GC 支持:

- **`active_payload_paths()` / `proposal_frontiers()` / `current_fragment_versions()`** — 计算活跃引用集合;
- **`terminal_update_rows()` / `historical_version_rows()` / `delete_archived_rows(...)`** — 读取待归档终态/历史行并在 JSONL fsync 后按精确 identity 删除;
- **`finalize_unconsumed_updates(fragment_mode, reason)`** — 已证明输入闭合的正常停机中把剩余 pending/selected 终态化。

fragment 模式(与上面逐一对应,多了 fragment 维度):

- **`upsert_fragment_definition(fragment, *, strategy)`** — fragment 定义行(numel、slices JSON)。
- **`upsert_fragment_version(...)`** — 每片每版本一行,含 global_merge_event。
- **`insert_fragment_update_metadata(metadata)`** — 唯一约束 `(learner_id, fragment_id, local_step_end, base_fragment_version)`。
- **`pending_fragment_updates(*, fragment_id=None)`** / **`eligible_fragment_updates(*, fragment_id, current_fragment_version, max_staleness_versions)`**。
- **`mark_fragment_updates_selected / mark_fragment_updates_applied / reset_fragment_selected_to_pending / drop_fragment_updates / drop_obsolete_fragment_updates / drop_superseded_fragment_updates`** — 语义同全量版;applied 时额外记录 applied_global_merge_event 与两种 staleness。
- **`get_fragment_update(update_id)`** / **`list_fragment_versions()`** / **`current_fragment_versions()`**。

## storage/maintenance.py — 归档与引用驱动 GC

- **`archive_and_prune(store, paths)`** — 把 terminal update 与非 current version 行追加到 `metrics/update_history.jsonl` / `global_version_history.jsonl`,flush+fsync 成功后才从活跃 DB 删除。崩溃重试可形成重复 archive 行,分析器按 identity 去重。
- **`collect_runtime_artifacts(store, paths, orphan_grace_seconds, extra_terminal_paths=...)`** — 保留 current global/fragment checkpoint、latest 引用的 materialized full、active DB payload;删除不再引用的 checkpoint、终态 payload/meta 与临时文件。未发布 proposal/orphan payload 至少等待 grace。
- **`run_maintenance(..., input_closed=False)`** — 按顺序执行 archive → prune → GC。正常输入闭合时终态引用可零 grace 删除;其他时刻 orphan grace 为 `max(2×heartbeat interval, 2×scan interval)`。
