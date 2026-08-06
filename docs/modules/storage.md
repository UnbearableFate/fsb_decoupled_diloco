# 模块参考:fs_diloco/storage

文件系统 I/O 与元数据持久化:原子写、safetensors 编解码、路径布局、持久 SQLite 状态机、归档与引用驱动 GC。

---

## storage/atomic_io.py — 原子文件系统助手

JSON/safetensors 共享状态的发布原语是:**同目录临时文件 → 文件 fsync → chmod → `os.replace()`**。读者不会看到半截目标文件；实现没有 fsync 父目录，因此只承诺运行期原子可见性，不承诺突然断电后的目录项持久。SQLite 和追加日志使用各自原语。

- **`ensure_dir(path) -> Path`** — `mkdir -p`。
- **`atomic_write_bytes(path, data, mode=0o644) -> Path`** — 在目标目录内建 `mkstemp` 临时文件(`.{name}.*.tmp`),写入 + flush + fsync,chmod 后 `os.replace` 到目标名;失败时清理临时文件并重抛。
- **`atomic_write_text(path, text)`** — UTF-8 编码后走 `atomic_write_bytes`。
- **`atomic_write_json(path, payload)`** — `json.dumps(sort_keys=True, indent=2)` 后原子写;所有心跳/元数据/latest/stop 都经此发布。
- **`atomic_write_with_writer(path, writer)`** — 先建临时文件,把路径交给回调 `writer(tmp_path)` 写内容(供 safetensors 的 `save_file` 使用),再 fsync + replace。
- **`read_json(path) -> dict`** — 直接读,失败抛异常。
- **`safe_read_json(path) -> dict | None`** — 仅捕获 OSError/JSON decode error 并返回 None；它不检查顶层一定是 object，合法 JSON list 等会原样返回并可能在调用方使用 `.get` 时失败。
- **`sha256_file(path, chunk_size=1MiB) -> str`** — 分块计算摘要。
- **`file_size(path) -> int`** — `stat().st_size`。
- **`wait_for_file(path, timeout_seconds, poll_seconds=1.0)`** — 轮询等待文件出现;超时抛 `TimeoutError`(当前 runtime 使用的是 learner 侧更严格的 `wait_for_json`)。

## storage/paths.py — 目录布局

- **`RunPaths(shared_root)`**(frozen dataclass)— 共享目录布局的**唯一权威定义**。目录属性:`control / weights / optim / updates_latest / updates_payloads / fragments / fragment_weights / fragment_optim / heartbeats / logs / metrics / eval_checkpoints`；`updates_pending` 是 `updates_payloads` 的兼容别名。文件属性:`latest_json / stop_json / summary_json / param_index_json / fragment_index_json / run_root_config_yaml / resolved_config_yaml / sqlite_db / update_history_jsonl / global_version_history_jsonl`。方法:
  - **`update_pointer_path(learner_id)`** / **`update_payload_dir(learner_id)`** — 全量固定 proposal pointer 与不可变 payload 目录;
  - **`fragment_update_pointer_path(learner_id, fragment_id)`** — per-pair 固定 pointer，fragment ID 三位补零；
  - **`global_weight_path(version)`** / **`outer_optim_path(version)`** — 按模板拼版本化文件名;
  - **`fragment_weight_path(fragment_id, version)`** / **`fragment_outer_optim_path(...)`** — `fragments/{weights|optim}/fragment_{id:03d}/v{version:06d}.safetensors`。
- **`prepare_run_dirs(paths, num_learners)`** — 建齐 control/weight/optim/update/fragment/heartbeat/log/metric 目录和每 learner payload 子目录；不创建 `eval_checkpoints`，后者只在 terminal capture 实际发生时按需创建。

属性的逐项映射为：`control`、`weights`、`optim`、`heartbeats`、`logs`、`metrics` 直接位于 run 根；`updates_latest`=`updates/latest`，`updates_payloads`=`updates/payloads`；`fragments` 下再分 `fragment_weights`=`weights` 与 `fragment_optim`=`optim`。`latest_json`、`stop_json`、`summary_json`、`param_index_json`、`resolved_config_yaml`、`sqlite_db` 位于 control；`fragment_index_json` 位于 fragments；`run_root_config_yaml` 位于根；`update_history_jsonl` 与 `global_version_history_jsonl` 位于 metrics。

## storage/tensor_codec.py — safetensors 编解码

- **`dtype_from_name(name) -> torch.dtype`** — `"float32"/"bf16"/"fp16"` 等别名 → torch dtype;未知报错。
- **`save_safetensors_atomic(path, tensors)`** — 张量搬到 CPU、contiguous 后用 `atomic_write_with_writer` + `safetensors.save_file` 原子保存。
- **`load_safetensors(path, device)`** — `safetensors.load_file` 薄封装。
- **`save_update_vector(path, flat, dtype)`** / **`load_update_vector(path, device, dtype=float32)`** — update 扁平向量,单键 `local_params`;落盘和加载 dtype 可分别配置。
- **`save_global_weights(path, theta, param_index, dtype=None)`** / **`load_global_weights_flat(path, param_index, device, dtype=float32)`** — 命名 global 权重与扁平向量互转；syncer 可直接按计算 dtype 加载。
- **`load_global_weights_into_model(path, model, param_index, strict_shape=True)`** — 用 `safe_open` 在 CPU 上逐个命名 tensor 读取并直接 copy 到参数 device/dtype，校验模型名、checkpoint 名、numel 和可选 shape；不会物化完整 host flat，直接 replace adoption 使用它。
- **`save_outer_state(path, theta, state, dtype=None)`** / **`load_outer_state(path, device, dtype=None) -> (theta, state)`** — 外层优化器状态,`theta` + 各状态张量平铺为键;可统一转换浮点状态的发布/加载 dtype,整数状态不转换,缺 `step` 时补 0。

## storage/sqlite_store.py — 权威持久元数据库

数据库固定在共享 run 的 `control/syncer_metadata.sqlite3`;schema 见 `schema.sql` 与 [04-data-flow.md](../04-data-flow.md) 第 4 节。业务写入由 syncer 串行执行,节点切换后可直接重开同一文件。

### 模块级函数

- **`_schema_text()`** — 通过 `importlib.resources` 读取随包分发的 `schema.sql`。
- **`RESOURCE_COLUMNS`** — 旧 full/fragment update 表的共用迁移白名单：训练期/local-cycle CPU/GPU peak 四个 REAL、cycle step-time mean REAL、step count/resource sample count 两个 INTEGER。
- **`FULL_UPDATE_METADATA_COLUMNS`** — 只对 full `updates` 表迁移 `mid_cycle_adoption_count INTEGER NOT NULL DEFAULT 0` 与 `base_switched_at_step INTEGER`。
- **`_ensure_resource_columns(conn, table)`** / **`_ensure_full_update_metadata_columns(conn)`** — connect-time 幂等 `ALTER TABLE`，为旧 DB 补资源字段及 full mid-cycle 两列。
- **`connect(path) -> sqlite3.Connection`** — 建父目录、连接(`timeout=60s`)、`row_factory=Row`,强制 `journal_mode=DELETE`、`synchronous=FULL`、busy timeout,执行幂等 schema/资源列及 full mid-cycle adoption 列迁移。
- **`row_to_dict(row) -> dict | None`** — Row → dict 便捷转换。

### `SQLiteStore`

通用:

- **`__init__(path)` / `close()` / `execute(sql, params)`** — 连接管理；`execute` 每条语句后立即 commit，因此不能用它拼装多语句原子事务。
- **`pointer_signature_is_cached()` / `cache_pointer_signature()`** — 仅进程内的 `(inode,size,mtime_ns,ctime_ns)` cache，用于 fragment fixed pointer 解析短路；持久重放权威仍是 frontier 表。
- **`integrity_check()` / `pragma_settings()`** — 前者要求 `PRAGMA integrity_check` 唯一结果为 `ok`，否则抛错；后者当前只报告 `journal_mode` 与 `synchronous`，不返回 busy-timeout 字段。
- **`committed_global_count()` / `latest_global_version()`** — 查询活跃 DB 中唯一 current committed global。
- **`set_run_state(key, value)` / `get_run_state(key)`** — JSON 值的 kv upsert/读取。
- **`_set_run_state_in_transaction(conn, key, value, now)`** — 不 commit 的静态 helper，供 initialize/resume 把 identity/config/generation 与其他状态放入同一事务。

全局版本 / learner:

- **`initialize_full_run(...)`** — 一个事务写入 committed v0、run identity 与配置快照;拒绝覆盖已有 committed run。
- **`prepare_full_resume(...)`** — full resume 的单一事务边界：`selected → pending`，把全部预期 learner 行重置为 `unknown/resumed` 并清空本代 hostname/pid/last_seen/heartbeat path，同时把 resume ID/时间/旧 heartbeat 内容 fence 写入 `run_state.resume_generation`。事务失败不会暴露部分切代。
- **`commit_full_merge(...)`** — 全量 `N→N+1` 的唯一事务边界:校验前驱/目标、selected 行与 learner 唯一性、future/staleness 准入、effective-weight key 精确匹配，插入 global row、写 applied 字段并终态化 `superseded/too_stale/future_base` 行；任何异常整笔 rollback。
- **`upsert_global_version(...)`** — standalone global-version upsert helper；当前 full runtime 使用 `initialize_full_run/commit_full_merge`，fragment runtime 使用 `upsert_fragment_version`，因而生产主路径不调它。
- **`get_global_version(version)`**。
- **`upsert_learner(learner_id, *, hostname, pid, last_seen, ..., status, status_reason)`** — 心跳快照 upsert;已有行的字段用 `COALESCE` 保留旧值(心跳缺字段不清空),status 总是覆盖。
- **`update_learner_status(learner_id, status, reason)`** / **`list_learners()`**。
- **`learner_resource_peaks(fragment_mode)`** — 只查询当前 live update 表按 learner 聚合的资源峰值；历史已归档行不会被回读，summary 层会再用最终 heartbeat 补充。

全量模式 update 状态机:

- **`insert_update_metadata(metadata, *, pointer_path) -> bool`** — latest-wins 摄取固定 pointer:相同 frontier update ID 立即 rollback/返回 false；否则先终态化同 learner 旧 pending（不覆盖 selected），以 `INSERT OR IGNORE` 插新行，再推进 `proposal_frontiers`。因此若新 ID 撞上 active 表的主键或 `(learner_id, local_step_end, base_global_version)` 唯一约束，插入返回 false但 frontier 仍在同一事务推进，且旧 pending 已 superseded；该 pointer 不会自动重试。正常 learner 的单调 local step + UUID 避免此路径。full 的 `mid_cycle_adoption_count/base_switched_at_step` 经显式列白名单持久化并随终态行归档，旧 metadata 缺字段按 `0/null` 兼容。
- **`insert_fragment_update_metadata(metadata, *, pointer_path) -> bool`** — 同一事务内按 `(learner_id, fragment_id)` 做 frontier 重放短路、只终态化同 pair 的旧 pending（不覆盖 selected）、`INSERT OR IGNORE` 新行并推进 `fragment_proposal_frontiers`。撞上 update ID 或 `(learner_id, fragment_id, local_step_end, base_fragment_version)` 唯一约束时，与 full 一样可能“返回 false但 frontier 已推进”。
- **`pending_updates()`** — 全部 pending,按 committed_at 升序。
- **`eligible_updates(current_version, max_staleness_versions)`** — pending 且 staleness 在窗口内。
- **`mark_updates_selected(update_ids, selected_by_run)`** — 条件转移 `pending → selected`(记 selected_at/selected_by_run)。
- **`mark_updates_applied(updates, *, applied_version, effective_weights)`** — standalone `→ applied` helper，记录 applied_at、staleness_versions(=applied_version−1−base)、实际合并权重。当前 full runtime 的同类转换已内联到 `commit_full_merge` 单事务，这个 helper 主要供独立状态机操作/测试。
- **`reset_selected_to_pending(update_ids)`** — 合并中途失败的回滚(仅 `selected → pending`)。
- **`reset_all_selected_to_pending()`** — standalone 回滚全部 selected。DB-first resume 在 `prepare_full_resume` 内执行相同 SQL 语义，以便和 liveness/fence 切代处在同一事务，不单独调该 helper。
- **`drop_updates(update_ids, reason)`** — `pending|selected → dropped`,记 drop_reason。
- **`drop_obsolete_updates(current_version, max_staleness) -> int`** — standalone 批量把过窗 pending 置为 `dropped("too_stale")`,返回行数；当前 full merge 的 obsolete 终态化在 `commit_full_merge`。
- **`drop_ineligible_updates(...)`** — 同一事务分别把 base 超前者标为 `future_base`、过窗者标为 `too_stale`，返回两类计数；terminal selector 使用。
- **`drop_superseded_updates(selected_updates, reason="superseded") -> int`** — standalone 实现：对每个被选中更新,把同 learner 的更旧 pending(local_step_end 更小,或同步数且 committed 更早)置为 dropped。当前 full runtime 在 `commit_full_merge` 事务内完成这一步。
- **`get_update(update_id)`**。

归档/GC 支持:

- **`active_payload_paths()` / `proposal_frontiers()` / `fragment_proposal_frontiers()` / `current_fragment_versions()`** — 计算 pending/selected payload、frontier 和各片 current version 的活跃引用集合;
- **`terminal_update_rows()` / `historical_version_rows()` / `delete_archived_rows(...)`** — 读取待归档终态/历史行；JSONL fsync 后，在同一 SQLite 事务内把终态 payload path 幂等 stage 到 `gc_pending` 并按精确 identity 删除 active 行；
- **`gc_pending_paths()` / `gc_pending_count()` / `clear_gc_pending_paths(...)`** — 以尚未完成物理删除的 payload 数为界的持久集合；archive 后、unlink 前崩溃可在下一次 maintenance 恢复；
- **`finalize_unconsumed_updates(fragment_mode, reason)`** — 已证明输入闭合的正常停机中把剩余 pending/selected 终态化。

fragment 模式(与上面逐一对应,多了 fragment 维度):

- **`upsert_fragment_definition(fragment, *, strategy)`** — fragment 定义行(numel、slices JSON)。
- **`upsert_fragment_version(...)`** — 每片每版本一行,含 global_merge_event。
- **`insert_fragment_update_metadata(metadata, *, pointer_path)`** — 唯一约束 `(learner_id, fragment_id, local_step_end, base_fragment_version)`；与 per-pair frontier 推进和旧 pending supersession 位于同一事务，selected 行不被覆盖。
- **`pending_fragment_updates(*, fragment_id=None)`** / **`eligible_fragment_updates(*, fragment_id, current_fragment_version, max_staleness_versions)`**。
- **`mark_fragment_updates_selected()`** / **`mark_fragment_updates_applied()`** / **`reset_fragment_selected_to_pending()`** / **`reset_all_fragment_selected_to_pending()`** / **`drop_fragment_updates()`** / **`drop_obsolete_fragment_updates()`** / **`drop_ineligible_fragment_updates()`** / **`drop_superseded_fragment_updates()`** — 语义同全量版；字面过期/未来 reason 仍为 `too_stale/future_base`；applied 时额外记录 applied_global_merge_event 与两种 staleness。
- **`get_fragment_update(update_id)`** / **`list_fragment_versions()`** / **`current_fragment_versions()`**。

## storage/maintenance.py — 归档与引用驱动 GC

### HA storage modules

`storage/schema_bootstrap.py` 把 schema创建从普通连接中拆出：`initialize_new_run()` 在同目录临时 DB中一次性建完整schema、写identity/PRAGMA并原子发布DB，最后写bootstrap marker；static HA为v2，dynamic HA为v3并追加`learner_instances/placements/streams/registration_requests/launch_requests/capacity_observations`及update membership fence列。`open_existing()`只打开并核验marker、`schema_meta`、`run_state.schema_version`和`PRAGMA user_version`三重一致，不执行DDL/ALTER；`open_readonly()`用URI `mode=ro` + `query_only=ON`。

`storage/leader_lease.py` 定义不可变 `LeaderToken(run_id, epoch, owner_id)`、`make_owner_id()`、`LeaderLeaseStore`和线程安全的 `LeaseSafetyTracker`。acquire/renew/release都使用 `BEGIN IMMEDIATE`，epoch由 history最大值递增且不复用；renew和release必须 exact-owner匹配，过期 token抛 `StaleLeaderTokenError`。renew线程只在 DB renew提交成功后推进 tracker；每个业务 transaction在开始和 commit前同时检查 exact token、DB wall-clock安全边界与共享的本地 monotonic安全边界。

`storage/fenced_store.py` 保留 legacy `SQLiteStore`作为内部数据实现，同时封闭 raw connection：

- `FencedSQLiteStore` 的 HA mutator都显式接收 token，并在同一个持锁 transaction内校验 current epoch/owner、执行业务 SQL、提交；SQL wrapper拒绝 transaction控制、DDL/PRAGMA/ATTACH等逃逸。
- `LeaderBoundSQLiteStore` 把固定 token绑定成 runtime兼容接口，不允许调用者切换 token。
- `ReadOnlySQLiteStore` 只暴露查询，供 Checker和analysis使用；learner canonical control reader是纯 filesystem reader，不打开 SQLite。

`RunPaths` 为 HA增加 descriptor/bootstrap、epoch weight/optim/control、candidate/epoch log、syncer heartbeat、launch claim和 HA history路径；dynamic再增加registration request、bootstrap scheduler manifest、manual close、epoch membership/admission/drain及五类history路径。`iter_epoch_*`、syncer/learner log、learner heartbeat与instance pointer/payload/registration iterator统一递归或mode-aware发现，analysis、metrics、Checker和probe复用这些入口。`prepare_authority_dirs()`只供initializer/leader，`prepare_learner_instance_dir()`只创建该learner自己的heartbeat/update/log目录。

HA maintenance在归档 legacy active历史之外，还归档/压缩 `syncer_epochs`、逐项登记 `gc_candidates`、删除前重新验证 DB live引用，并删除旧 epoch无引用 orphan。dynamic registration支持receipt绑定出现前的持久pending状态；dynamic merge把global row与唯一`merge:<version>` observation放在同一fenced transaction，starvation generation也与对应observation原子推进。maintenance另把expired instance/registration/launch request和retention窗口外capacity observation先append+fsync到独立JSONL，再以fenced transaction剪枝；active observation默认最多64，current/grace instance受配置上限约束。任何ledger或DB mutation都经过current leader token；文件删除发生在transaction外，下一轮以幂等ledger完成。

- **`_append_jsonl_fsync()`** — 先 materialize rows，非空时逐行 JSON append、flush+fsync，返回行数；不做去重或原子替换。
- **`_unlink()`** — 删除成功 true，不存在 false，其他错误传播；**`_resolved_paths()`** 对集合做 `resolve(strict=False)`；**`_pointer_state()`** 只读固定 pointer 的合法 `update_id/learner_id/file_path/fragment_id`，按 pair 保留最后扫描值。
- **`archive_and_prune(store, paths)`** — 把 terminal update 与非 current version 行追加到 `metrics/update_history.jsonl` / `global_version_history.jsonl`,flush+fsync 成功后才以 `gc_pending + active row delete` 的 SQLite 事务剪枝。崩溃重试可形成重复 archive 行,分析器按 identity 去重。
- **`collect_runtime_artifacts(store, paths, orphan_grace_seconds, extra_terminal_paths=...)`** — 保留 current global/fragment checkpoint、active DB payload；legacy才把fixed latest materialization加入引用集，HA不信任该cache。未发布 proposal/orphan payload 至少等待 grace。HA旧epoch orphan必须先在fenced transaction登记进`gc_candidates`，每个文件unlink前再用独立短transaction精确claim并复查全部DB引用；current epoch永不按orphan删除。HA authority目录中的atomic temp即使input已闭合也至少等待fenced store的`gc_grace_seconds`，避免final maintenance与仍在续租/发布heartbeat的线程互删临时文件。
- **`run_maintenance(..., input_closed=False)`** — 按顺序 archive/prune 后 GC。输入闭合时先把已登记GC候选的`not_before`推进到当前时间，再逐文件fenced claim，使terminal maintenance收敛；普通proposal orphan grace为0，否则 `max(2×heartbeat interval, 2×scan interval)`，HA旧epoch checkpoint与authority temp仍至少使用lease+clock-skew grace。`maintenance_scanned_rows` 只是本轮读取的 `gc_pending` path 数，不是 glob 过的文件总数；`gc_pending_rows` 是清理后的剩余行数，scan seconds 只包 artifact collection。
