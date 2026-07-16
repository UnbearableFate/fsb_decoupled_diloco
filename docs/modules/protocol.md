# 模块参考:fs_diloco/protocol

合并选择与加权、liveness 规则、fragment 索引/编解码/调度。除 liveness 的入库操作外均为纯函数。

---

## protocol/merge.py — 合并选择与 token/staleness 加权

- **`staleness(current_version, base_global_version) -> int`** — `max(0, current − base)`。
- **`raw_update_weight(tokens, staleness_versions, staleness_lambda) -> float`** — `tokens / (1 + λ·staleness)`。
- **`normalized_update_weights(updates, *, current_version, staleness_lambda) -> dict[update_id, float]`** — 对选中集合计算原始权重并归一化(和为 1);总权重非正时抛 `ValueError`(如全部 tokens=0)。
- **`normalized_fragment_update_weights(updates, *, current_fragment_version, staleness_lambda)`** — 同上,staleness 以 `base_fragment_version` 计。
- **`select_one_per_learner(updates, *, policy, quorum_max) -> list`** — 每 learner 至多留一份:
  - `most_recent_per_learner`:取 `(local_step_end, committed_at)` 最大者,结果按 `(learner_id, local_step_end)` 排序;
  - `oldest_pending`:取 committed_at 最早者,结果按 `(committed_at, learner_id)` 排序(terminal drain 用,保证先进先出);
  - 最后截断到 `quorum_max`。
- **`stale_update_ids(updates, *, current_version, max_staleness_versions)`** / **`stale_fragment_update_ids(...)`** — 挑出过窗更新的 id(供测试/工具;运行时用 SQL 版 `drop_obsolete_*`)。
- **`weighted_average_tensors(tensors, weights)`** — `Σ wᵢ·tᵢ`,用 `mul/add(alpha=)` 原位风格实现避免中间大张量;空列表或长度不匹配报错。

## protocol/liveness.py — 心跳与存活分类

- **`valid_learner_ids(num_learners) -> set[str]`** — 合法 id 集合 `{learner_000...}`。
- **`validate_heartbeat(payload, *, run_id, num_learners) -> (bool, reason)`** — 校验 format_version / run_id / learner_id / timestamp;不合法返回失败原因字段名。
- **`ingest_heartbeats(store, heartbeat_dir, *, run_id, num_learners) -> int`** — 扫描 `heartbeats/learner_*.json`,合法者 `upsert_learner` 入库(status 取心跳自报值,缺省 active;phase 记入 status_reason),返回摄取数。
- **`classify_liveness(*, now, last_seen, current_status, stale_after_seconds, dead_after_seconds) -> (status, reason)`** — 分类规则:
  - `stopped` 粘性(learner 自报退出后不再重分类);
  - 从未见过 → `dead("never_seen")`;
  - 心跳年龄 ≤ stale_after → `active`;≤ dead_after → `stale`;否则 `dead`(reason 带年龄)。
- **`update_liveness_statuses(store, *, stale_after_seconds, dead_after_seconds, now=None) -> dict[status, count]`** — 对库中每个 learner 重分类并写回,返回各状态计数。
- **`no_progress_timed_out(last_progress_time, timeout_seconds, now=None) -> bool`** — syncer 无进展停机判定。

## protocol/fragment_index.py — 分片索引

fragment index JSON 结构:`{format_version, strategy, num_fragments, total_numel, source_param_index_path, fragments: [{fragment_id, numel, size_bytes_float32, slices: [{param_name, param_offset, param_numel, flat_start, flat_end, shape, dtype}]}]}`。当前两种策略都以**整张量**为切片单位(`param_offset` 恒 0、`param_numel` = 张量 numel)。

- **`_slice_from_param(entry)`**(私有)— param index 条目 → 覆盖整个张量的 slice 描述。
- **`_fragment_payload(fragment_id, slices)`**(私有)— slices 按 flat_start 排序、汇总 numel。
- **`build_fragment_index(param_index, *, strategy, num_fragments, source_param_index_path) -> dict`** —
  - `full`:全部张量进 0 号片(要求 num_fragments=1);
  - `balanced_tensor`:张量按 numel 降序,贪心放入当前总量最小的桶(要求 num_fragments ≤ 张量数);
  - 构建后立即 `validate_fragment_index` 自检。
- **`validate_fragment_index(fragment_index, param_index=None)`** — 严格校验:format_version、fragments 非空、id 从 0 连续、每片非空且 numel 与 slices 一致、所有 slice 区间**无缝且不重叠地精确覆盖** `[0, total_numel)`、(可选)引用的参数名都在 param index 中。任何违例抛 `ValueError`。
- **`fragment_by_id(fragment_index, fragment_id)`** — 查找,不存在抛 `KeyError`。
- **`save_fragment_index(fragment_index, path)`** / **`load_fragment_index(path)`** — 原子写 / 读取+校验。
- **`fragment_size_summary(fragment_index) -> {min,max,mean,imbalance_ratio}`** — 分片均衡度统计(分析工具用)。

## protocol/fragment_codec.py — 分片张量操作与存取

常量 `FRAGMENT_TENSOR_KEY = "fragment_params"`(分片 safetensors 的统一键)。

- **`extract_fragment(flat, fragment_index, fragment_id) -> Tensor`** — 按 slices 从完整扁平向量中切出该片(拼接为连续向量)。
- **`scatter_fragment(flat, fragment_index, fragment_id, fragment_tensor) -> Tensor`** — 逆操作:把片写回完整向量的对应区间(在 clone 上操作,返回新张量);numel 不符报错。
- **`load_fragment_into_model(model, fragment_tensor, param_index, fragment_index, fragment_id)`** — flatten 当前模型 → scatter 该片 → `load_flat_into_model` 写回(`@torch.no_grad`)。
- **`save_fragment_update(path, fragment_tensor, dtype)`** / **`load_fragment_update(path, device)`** — learner 上传/ syncer 读取的分片更新(存盘 dtype 可配,读取转 float32)。
- **`save_fragment_weight(path, fragment_tensor)`** / **`load_fragment_weight(path, device)`** — syncer 发布/learner 采纳的分片全局权重(存盘保持原 dtype)。
- **`materialize_full_from_fragments(fragment_tensors, fragment_index, total_numel) -> Tensor`** — 把全部片 scatter 进一个新向量,重建完整参数;缺片抛 `ValueError`。

## protocol/fragment_scheduler.py — 调度

- **`select_fragment(index, num_fragments, *, schedule="round_robin_global") -> int`** — `index mod num_fragments`;syncer 传 `global_merge_event`,learner 传 `local_update_index`,两侧节奏天然对齐。
- **`expected_fragment_versions_after_events(num_fragments, global_merge_events, *, schedule) -> dict[fragment_id, version]`** — 模拟 E 次事件后各片应达到的版本(分析断言用)。
