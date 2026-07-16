# 模块参考:fs_diloco/storage

文件系统 I/O 与元数据持久化:原子写、safetensors 编解码、路径布局、SQLite 状态机、保留策略。

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

- **`RunPaths(shared_root)`**(frozen dataclass)— 共享目录布局的**唯一权威定义**。属性:`control / weights / optim / updates_pending / updates_processed / updates_dropped / fragments / fragment_weights / fragment_optim / heartbeats / db_dumps / logs / metrics`,文件:`latest_json / stop_json / param_index_json / fragment_index_json / resolved_config_yaml`。方法:
  - **`global_weight_path(version)`** / **`outer_optim_path(version)`** — 按模板拼版本化文件名;
  - **`fragment_weight_path(fragment_id, version)`** / **`fragment_outer_optim_path(...)`** — `fragments/{weights|optim}/fragment_{id:03d}/v{version:06d}.safetensors`;
  - **`db_dump_path(timestamp, version)`**。
- **`prepare_run_dirs(paths, num_learners)`** — 建齐全部目录 + 每 learner 的 pending/processed/dropped 子目录(幂等;learner 和 syncer 启动时都会调用)。

## storage/tensor_codec.py — safetensors 编解码

- **`dtype_from_name(name) -> torch.dtype`** — `"float32"/"bf16"/"fp16"` 等别名 → torch dtype;未知报错。
- **`save_safetensors_atomic(path, tensors)`** — 张量搬到 CPU、contiguous 后用 `atomic_write_with_writer` + `safetensors.save_file` 原子保存。
- **`load_safetensors(path, device)`** — `safetensors.load_file` 薄封装。
- **`save_update_vector(path, flat, dtype)`** / **`load_update_vector(path, device)`** — update 扁平向量,单键 `local_params`;可用 BF16 落盘,加载时统一转 float32。
- **`save_global_weights(path, theta, param_index)`** — 扁平 θ 先经 `flat_to_named_tensors` 还原为命名张量再存(每参数一键,可独立加载/导出)。
- **`load_global_weights_flat(path, param_index, device) -> Tensor`** — 逆向:命名张量 → 扁平向量。
- **`save_outer_state(path, theta, state)`** / **`load_outer_state(path, device) -> (theta, state)`** — 外层优化器状态,`theta` + 各状态张量平铺为键;加载时 theta 转 float32、缺 `step` 时补 0。

## storage/sqlite_store.py — syncer 本地元数据库

仅 syncer 单进程使用;数据库位于节点本地盘。schema 见 `schema.sql` 与 [04-data-flow.md](../04-data-flow.md) 第 4 节。

### 模块级函数

- **`connect(path) -> sqlite3.Connection`** — 建父目录、连接(`timeout=30s`)、`row_factory=Row`、`journal_mode=WAL`、`synchronous=NORMAL`、执行 schema(全部 `IF NOT EXISTS`,幂等)。
- **`row_to_dict(row) -> dict | None`** — Row → dict 便捷转换。

### class SQLiteStore

通用:

- **`__init__(path)` / `close()` / `execute(sql, params)`** — 连接管理与直通执行(每次调用后 commit)。
- **`set_run_state(key, value)` / `get_run_state(key)`** — JSON 值的 kv upsert/读取。
- **`backup_to(dest_path, *, global_version) -> Path`** — 用 sqlite3 在线 backup API 产生一致快照,并在 `db_dumps` 表记台账。
- **`restore_from_dump(dump_path)`** — 关连接 → 整文件拷贝覆盖本地库 → 重开。

全局版本 / learner:

- **`upsert_global_version(version, weight_path, optim_path, *, num_updates, total_update_tokens, total_seen_tokens, outer_optimizer, status, notes)`** — 版本行插入或整行更新。
- **`get_global_version(version)`**。
- **`upsert_learner(learner_id, *, hostname, pid, last_seen, ..., status, status_reason)`** — 心跳快照 upsert;已有行的字段用 `COALESCE` 保留旧值(心跳缺字段不清空),status 总是覆盖。
- **`update_learner_status(learner_id, status, reason)`** / **`list_learners()`**。

全量模式 update 状态机:

- **`insert_update_metadata(metadata, *, ingested_at) -> bool`** — `INSERT OR IGNORE` 入库为 `pending`;返回是否新插入(唯一约束:`(learner_id, local_step_end, base_global_version)`,保证重扫/重启幂等)。
- **`pending_updates()`** — 全部 pending,按 committed_at 升序。
- **`eligible_updates(current_version, max_staleness_versions)`** — pending 且 staleness 在窗口内。
- **`mark_updates_selected(update_ids, selected_by_run)`** — 条件转移 `pending → selected`(记 selected_at/selected_by_run)。
- **`mark_updates_applied(updates, *, applied_version, effective_weights)`** — `→ applied`,记录 applied_at、staleness_versions(=applied_version−1−base)、实际合并权重。
- **`reset_selected_to_pending(update_ids)`** — 合并中途失败的回滚(仅 `selected → pending`)。
- **`drop_updates(update_ids, reason)`** — `pending|selected → dropped`,记 drop_reason。
- **`drop_obsolete_updates(current_version, max_staleness) -> int`** — 批量把过窗 pending 置为 `dropped("stale")`,返回行数。
- **`drop_superseded_updates(selected_updates, reason="superseded") -> int`** — 对每个被选中更新,把同 learner 的更旧 pending(local_step_end 更小,或同步数且 committed 更早)置为 dropped。
- **`get_update(update_id)`**。

fragment 模式(与上面逐一对应,多了 fragment 维度):

- **`upsert_fragment_definition(fragment, *, strategy)`** — fragment 定义行(numel、slices JSON)。
- **`upsert_fragment_version(...)`** — 每片每版本一行,含 global_merge_event。
- **`insert_fragment_update_metadata(metadata)`** — 唯一约束 `(learner_id, fragment_id, local_step_end, base_fragment_version)`。
- **`pending_fragment_updates(*, fragment_id=None)`** / **`eligible_fragment_updates(*, fragment_id, current_fragment_version, max_staleness_versions)`**。
- **`mark_fragment_updates_selected / mark_fragment_updates_applied / reset_fragment_selected_to_pending / drop_fragment_updates / drop_obsolete_fragment_updates / drop_superseded_fragment_updates`** — 语义同全量版;applied 时额外记录 applied_global_merge_event 与两种 staleness。
- **`get_fragment_update(update_id)`** / **`list_fragment_versions()`**。

## storage/retention.py — 保留策略

- **`_safe_unlink(path, logger)`**(私有)— 删除失败只记日志不抛。
- **`_versioned_files(directory, pattern, regex)`**(私有)— 收集 `(version, path)` 列表。
- **`cleanup_global_artifacts(paths, *, keep_last, logger) -> int`** — syncer 侧:合并 weights/optim 两目录中的版本号集合,只保留最新 `keep_last` 个版本的文件,其余删除;`keep_last=None` 关闭。返回删除文件数。
- **`_inside_directory(path, directory)`**(私有)— 防御:确认元数据里的 file_path 确实在本 learner 目录内才敢删。
- **`cleanup_learner_update_artifacts(update_dir, *, keep_last, logger) -> int`** — learner 侧:按 `(local_step_end, committed_at, 文件名)` 排序,保留最新 `keep_last` 份 meta+tensor 对,其余删除;同时清理没有对应保留元数据的孤儿 `update_*.params.safetensors`。**只在全量模式的主循环里调用**(fragment 更新按片消费,按步序删除不安全)。
