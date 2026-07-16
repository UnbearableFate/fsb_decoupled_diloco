# 模块参考:fs_diloco/runtime/learner.py 与 failure_sim.py

learner 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 2、3 节。

---

## runtime/learner.py

### CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--run-id`、`--shared-root`、`--learner-id`(必填)、`--num-learners`。
- **`main(argv)`** — `resolve_config` 后调 `run_learner`。
- **`run_learner(config, learner_id)`** — 分派:`fragments.enabled` → `run_fragment_learner`,否则执行全量模式主循环(函数体内)。

### 共享文件交互

- **`write_heartbeat(*, paths, config, learner_id, status, phase, last_loaded_global_version, last_local_step, last_update_id, tokens_per_sec=None, last_loaded_global_merge_event=None, last_loaded_fragment_versions=None, last_adopted_fragments=None, resource_metrics=None)`** — 组装心跳 payload 原子覆盖 `heartbeats/<id>.json`;fragment 相关字段仅在传入时包含;active update 心跳携带上一 local cycle 的资源指标,最终 stopped 心跳携带全训练资源峰值。
- **`wait_for_json(path, *, timeout_seconds=1800, poll_seconds=1)`** — 轮询直到 `safe_read_json` 成功;启动期等待 param_index/fragment_index/latest 用;超时抛 `TimeoutError`。
- **`read_latest_if_newer(paths, last_loaded_global_version) -> dict | None`** — 读 `latest.json`,版本不高于已加载值时返回 None(全量模式轮询原语)。
- **`read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)`** — fragment 版:要求 `latest_kind == "fragment"` 且 `global_merge_event` 更大。
- **`wait_for_fragment_latest_if_newer(paths, last_event, config)`** — 上传后的限时等待轮询:fixed 使用 `fixed_seconds`,adaptive 使用 `initial_seconds`;等不到返回 None。

### 停止判定

- **`stop_requested(paths, local_step, config)`** — 默认 `local_or_global` 在 `max_local_steps` 达标或 `stop.json` 存在时停止;`global_only` 忽略本地 horizon,只认 `stop.json`。
- **`fragment_stop_requested(paths, local_step, config)`** — 默认模式设置 `max_local_steps` 时保持只看步数的 fragment 收尾语义;`global_only` 时只认 `stop.json`。

### 训练组件

- **`build_inner_optimizer_and_scheduler(model, config) -> (optimizer, scheduler|None)`** — 仅支持 AdamW;调度器:`none` 返回 None,否则 LambdaLR 实现"线性 warmup + (可选)cosine 衰减"(cosine 需要 `max_local_steps` 作为周期)。**每次采纳新全局版本后都会重建**(即重置)。
- **`maybe_autocast(device, precision)`** — CUDA + bf16 时启用 autocast,否则禁用的空上下文。
- **`train_one_step(model, batch_iter, optimizer, scheduler, *, device, config) -> (loss, tokens, examples, grad_norm)`** — 一个本地步:`gradient_accumulation_steps` 次前向/反向(loss 除以累积数;**非有限 loss 直接抛 `FloatingPointError`**)、可选梯度裁剪、`optimizer.step()`、`scheduler.step()`;返回本步平均 loss 与计量。
- **`create_resource_monitor(device)`** — 创建整节点 CPU + 当前 CUDA 设备 GPU 的后台利用率采样器。每个 local cycle 开始时清零 cycle 统计,每个 `train_one_step` 以单调时钟计时;训练期峰值跨 cycle 保留。

### 全局权重采纳

- **`adopt_global(*, model, latest, param_index, device) -> int`** — 全量模式:加载 `latest["weight_path"]` 为扁平向量 → 整体写回模型 → 返回新版本号。调用方随后负责重建内层优化器并清零 `tokens_since_global_load`。
- **`rebase_local_delta_onto_global(*, model, latest, param_index, device, reference_flat) -> (version, delta_norm)`** — full 实验模式:在 CPU FP32 中计算 `current_local-reference`,加到新版 global 后写回模型。调用方随后立即释放 reference、保留 carried token 计数并重建 optimizer/scheduler。
- **`load_fragment_latest_into_model(*, model, latest, param_index, fragment_index, device) -> (global_merge_event, {fragment_id: version})`** — 启动期:加载 latest 中**所有**片、materialize 成完整向量后整体写回。
- **`adopt_fragment_updates(*, model, latest, param_index, fragment_index, last_loaded_fragment_versions, device) -> (event, versions, changed)`** — 运行期增量采纳:flatten 当前模型 → 只加载版本更新的片并 scatter → 有变化才写回模型;返回变化片列表(调用方据此清零对应 token 计数、按配置重置优化器)。

### update 提交

- **`write_update(*, ..., resource_metrics, flat) -> (update_id, tensor_path, pointer_path, metadata)`** — 全量模式提交:生成 `update_id = {learner}_{step:08d}_{uuid12}`;**先**原子写不可变 payload(dtype 按 `io.tensor_dtype`),可选 sha256,**后**原子替换 `updates/latest/<learner>.json` 固定 pointer(= 提交点)。metadata 记录 `tensor_dtype`,并携带全训练至今的 CPU/GPU 峰值、上一 local cycle 的 CPU/GPU 峰值和该 cycle 的平均每 step 时间。
- **`write_fragment_update(*, ..., resource_metrics, fragment_id, base_fragment_version, base_global_merge_event, tokens_since_fragment_load, fragment_norm, fragment_tensor)`** — fragment 版,文件名与 update_id 带 `fXXX`,元数据带 `update_kind: "fragment"` 和同一组资源指标。

### 主循环

- **`run_learner` 全量模式主体** — 启动(种子/设备/模型/索引校验/adopt v0/优化器/心跳/数据迭代器)→ 循环:inner_steps 训练(带心跳、日志、可选中途采纳)→ 故障注入 → 按 `io.tensor_dtype` flatten + `write_update`(不可变 payload 后原子替换固定 pointer)→ CSV/心跳 → 可选上传后采纳 → 可选注入崩溃;learner 不删除 proposal payload;finally:记录 stop 原因、写 `stopped` 心跳、`process_exit`。
- **`run_fragment_learner(config, learner_id)`** — fragment 模式主体,差异:
  - 启动时还要等待并校验 fragment index;
  - 上传前 `select_fragment(local_update_index, K)` 选片、`extract_fragment_from_model` 直接抽取目标片,不先构造完整 flatten;
  - proposal metadata 仍以每份独立文件放在 payload 目录,由 syncer maintenance 在消费后统一清理;
  - 采纳走增量 `adopt_fragment_updates`,变化片的 `tokens_since_fragment_load` 清零;
  - finally 中的收尾:若无错且设置了 `stop_after_outer_steps`,在 `no_progress_timeout_seconds` 预算内轮询等待 `global_merge_event` 达标(期间持续采纳),最后再整体采纳一次,保证退出时本地模型为最终版本。

---

## runtime/failure_sim.py — 故障注入

三个函数都以 `failure_sim.enabled` 为总开关,参数容忍任意带同名属性的对象:

- **`maybe_sleep_jitter(config) -> float`** — 上传前随机睡 `U(0, sleep_jitter_seconds)`,返回实际时长(模拟慢节点)。
- **`should_skip_upload(config) -> bool`** — 以 `upload_skip_probability` 概率返回 True(模拟上传丢失;learner 记 `update_skipped` 后直接进入下一区间)。
- **`maybe_crash(config)`** — 以 `crash_probability` 概率 `sys.exit(97)`(模拟进程崩溃;97 便于在调度器日志中识别是注入)。
