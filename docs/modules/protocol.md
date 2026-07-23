# 模块参考:fs_diloco/protocol

合并选择与加权、liveness 规则、fragment 索引/编解码/调度。`merge.py` 和 `fragment_scheduler.py` 是纯函数模块；`liveness.py` 会读心跳/写 DB，fragment index/codec 也包含 JSON 和 safetensors I/O 薄封装。

---

## protocol/merge.py — 合并选择与 token/staleness 加权

- **`staleness(current_version, base_global_version) -> int`** — `max(0, current − base)`；纯函数本身把 future base 截为 0，不负责拒绝，runtime/SQLite eligible 查询必须先执行 `base≤current` 准入。
- **`raw_update_weight(tokens, staleness_versions, staleness_lambda) -> float`** — `tokens / (1 + λ·staleness)`。
- **`normalized_update_weights(updates, *, current_version, staleness_lambda) -> dict[update_id, float]`** — 对选中集合计算原始权重并归一化(和为 1);总权重非正时抛 `ValueError`(如全部 tokens=0)。
- full proposal 的加权仍假设整个 upload interval 基于行内单一 `base_global_version`；replace + inner poll 的混合 base 由 `mid_cycle_adoption_count/base_switched_at_step` 标注为可观测近似，不改变本模块计算。
- **`normalized_fragment_update_weights(updates, *, current_fragment_version, staleness_lambda)`** — 同上,staleness 以 `base_fragment_version` 计。
- **`select_one_per_learner(updates, *, policy, quorum_max) -> list`** — 每 learner 至多留一份:
  - `most_recent_per_learner`:取 `(local_step_end, committed_at)` 最大者,结果按 `(learner_id, local_step_end)` 排序;
  - `oldest_pending`:取 committed_at 最早者,结果按 `(committed_at, learner_id)` 排序(可配置的备选策略;terminal drain 与常规合并共用 `sync.selection_policy`,不做末端切换);
  - 最后截断到 `quorum_max`。
- **`stale_update_ids(updates, *, current_version, max_staleness_versions)`** / **`stale_fragment_update_ids(...)`** — 挑出过窗更新的 id(供测试/工具;运行时用 SQL 版 `drop_obsolete_*`)。
- **`weighted_average_tensors(tensors, weights)`** — `Σ wᵢ·tᵢ`；先做 `tensors[0].mul(w0)`，再逐项用返回新 tensor 的 `result.add(tensor, alpha=w)` 累加。它不 stack 全部 update，也不就地改写输入 tensor；空列表或长度不匹配报错。

## protocol/liveness.py — 心跳与存活分类

- **`valid_learner_ids(num_learners) -> set[str]`** — 合法 id 集合 `{learner_000...}`。
- **`validate_heartbeat(payload, *, run_id, num_learners) -> (bool, reason)`** — 校验 format_version / run_id / learner_id / timestamp;不合法返回失败原因字段名。
- **`_read_heartbeat(path)`** — 一次读取原子发布文件的精确 bytes，JSON 必须是 object；同时返回 bytes 的 SHA256。OSError/JSON 损坏/非 object 返回 None。
- **`capture_heartbeat_fences(...)`** — resume 前只为通过 run/format/learner 校验的 pointer 保存 `learner_id → exact-bytes SHA256`；不存在的目录得到空 dict。
- **`ingest_heartbeats(store, heartbeat_dir, *, run_id, num_learners, heartbeat_fences)`** — 扫描 `heartbeats/learner_*.json`,跳过无效 JSON、identity 不符或与当前 generation fence 完全相同的 bytes；其余 `upsert_learner` 入库(status 缺省 active，status_reason 优先显式 reason、否则 phase),返回实际 upsert 数。fence 不会因看到新内容而在内存中删除，但新 fingerprint 自然不匹配。
- **`classify_liveness(*, now, last_seen, current_status, stale_after_seconds, dead_after_seconds) -> (status, reason)`** — 分类规则:
  - `stopped` 粘性(learner 自报退出后不再重分类);
  - 从未见过 → `dead("never_seen")`;
  - 心跳年龄 ≤ stale_after → `active`;≤ dead_after → `stale`;否则 `dead`(reason 带年龄)。
- **`update_liveness_statuses(store, *, stale_after_seconds, dead_after_seconds, now=None) -> dict[status, count]`** — 对库中每个 learner 重分类并写回,返回各状态计数。
- **`no_progress_timed_out(last_progress_time, timeout_seconds, now=None) -> bool`** — 使用 `time.time()` wall clock，严格 `now-last > timeout` 才为 true；与 adaptive grace/learner watchdog 的 monotonic 时钟不同。

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
- **`extract_fragment_from_model(model, fragment_index, fragment_id, *, dtype, device="cpu") -> Tensor`** — 根据 slices 的参数名/参数内 offset 直接读取目标参数并拼成连续 fragment;只搬运目标片,同时校验参数名、切片边界与总 numel。
- **`scatter_fragment(flat, fragment_index, fragment_id, fragment_tensor) -> Tensor`** — 逆操作:把片写回完整向量的对应区间(在 clone 上操作,返回新张量);numel 不符报错。
- **`load_fragment_into_model(model, fragment_tensor, param_index, fragment_index, fragment_id)`** — flatten 当前模型 → scatter 该片 → `load_flat_into_model` 写回(`@torch.no_grad`)。
- **`save_fragment_update(path, fragment_tensor, dtype)`** / **`load_fragment_update(path, device, dtype=float32)`** — learner 上传/ syncer 读取的分片更新(存盘与加载 dtype 分别可配)。
- **`save_fragment_weight(path, fragment_tensor, dtype=None)`** / **`load_fragment_weight(path, device)`** — syncer 按可选 dtype 发布；加载函数无论落盘 dtype 如何都转换为 FP32，这与 `load_fragment_update(..., dtype)` 的可配 compute dtype 不同。
- **`materialize_full_from_fragments(fragment_tensors, fragment_index, total_numel) -> Tensor`** — 非空时以第一片的 dtype/device 建完整 uninitialized flat，再逐片 scatter；缺片报错。输入 dict 为空时直接返回 CPU FP32 空 tensor，不检查 `total_numel`。

## protocol/fragment_scheduler.py — 调度

- **`select_fragment(index, num_fragments, *, schedule="round_robin_global") -> int`** — `index mod num_fragments`;syncer 传 `global_merge_event`,learner 传 `local_update_index`,两侧节奏天然对齐。
- **`expected_fragment_versions_after_events(num_fragments, global_merge_events, *, schedule) -> dict[fragment_id, version]`** — 模拟 E 次事件后各片应达到的版本(分析断言用)。
