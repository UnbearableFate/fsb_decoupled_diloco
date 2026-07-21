# 模块参考:fs_diloco/runtime/learner.py、adoption.py 与 failure_sim.py

learner 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 2、3 节。

---

## runtime/learner.py

### CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--learner-id`(必填)、`--run-id`、`--shared-root`、`--num-learners`,以及与 syncer 对称的实验覆盖参数:`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`(launcher 把同一组覆盖传给两类进程,保证 resolved config 一致)。
- **`main(argv)`** — `resolve_config` 后调 `run_learner`。
- **`run_learner(config, learner_id)`** — 分派:`fragments.enabled` → `run_fragment_learner`,否则执行全量模式主循环(函数体内)。

### 共享文件交互

- **`write_heartbeat(*, paths, config, learner_id, status, phase, last_loaded_global_version, last_local_step, last_update_id, tokens_per_sec=None, last_loaded_global_merge_event=None, last_loaded_fragment_versions=None, last_adopted_fragments=None, resource_metrics=None, learning_rate=None, scheduler_total_steps=None, status_reason=None)`** — 组装心跳 payload 原子覆盖 `heartbeats/<id>.json`;fragment 相关字段仅在传入时包含;心跳恒带当前 `learning_rate` 与 `scheduler_total_steps`;active update 心跳携带上一 local cycle 的资源指标,最终 stopped 心跳携带全训练资源峰值,watchdog 退出时附 `status_reason=syncer_unresponsive`。
- **`wait_for_json(path, *, timeout_seconds=1800, poll_seconds=1)`** — 轮询直到 `safe_read_json` 成功;启动期等待 param_index/fragment_index/latest 用;超时抛 `TimeoutError`。
- **`read_latest_if_newer(paths, last_loaded_global_version) -> dict | None`** — 读 `latest.json`,版本不高于已加载值时返回 None(全量模式轮询原语)。
- **`read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)`** — fragment 版:要求 `latest_kind == "fragment"` 且 `global_merge_event` 更大。
- **`wait_for_fragment_latest_if_newer(paths, last_event, config)`** — 上传后的限时等待轮询:fixed 使用 `fixed_seconds`,adaptive 使用 `initial_seconds`;等不到返回 None。
- **`load_or_refresh_latest(...) -> LatestLoadResult`** — 所有由可推进 latest pointer 引用的权重快照的有界加载入口。`FileNotFoundError` 后只接受严格更新的 version/global merge event，重跑整份 callback 并返回实际 latest；耗尽时给原文件异常追加 retry 统计。预算/轮询复用 `prediction.reconcile_timeout_seconds` 与 `post_publish_latest_poll_seconds`。

### 停止判定

- **`stop_requested(paths, local_step, config)`** — 默认 `local_or_global` 在 `max_local_steps` 达标或 `stop.json` 存在时停止;`global_only` 忽略本地 horizon,只认 `stop.json`。
  full 与 fragment learner 共用该谓词，`stop.json` 始终优先于本地 horizon。
- **`SyncerProgressWatchdog` / `confirm_syncer_unresponsive(...)`** — 首次 latest 成功加载后用单调时钟跟踪严格更新的 full version 或 fragment global merge event；deadline 到达时固定重读一次 latest 并优先尊重 stop，避免策略轮询尚未发生造成误报。触发后两个 runner 都以 `syncer_unresponsive` 受控退出，不再发布 proposal。

### 训练组件

- **`inner_lr_multiplier(config, completed_local_steps)`** — 纯调度函数；按累计已完成本地步计算一次性 warmup、cosine 与非零下限，不读取停止上限或 adoption 状态。
- **`build_inner_optimizer_and_scheduler(model, config, *, completed_local_steps) -> (optimizer, scheduler|None)`** — 仅支持 AdamW；`none` 返回 None，cosine 使用独立 `scheduler_total_steps`。replace/direct adoption 与 fragment adoption 可重建对象，但从累计步恢复相位；rebase/prediction reconcile 按策略结果保留完整状态。
- **`maybe_autocast(device, precision)`** — CUDA + bf16 时启用 autocast,否则禁用的空上下文。
- **`train_one_step(model, batch_iter, optimizer, scheduler, *, device, config) -> (loss, tokens, examples, grad_norm)`** — 一个本地步:`gradient_accumulation_steps` 次前向/反向(loss 除以累积数;**非有限 loss 直接抛 `FloatingPointError`**)、可选梯度裁剪、`optimizer.step()`、`scheduler.step()`;返回本步平均 loss 与计量。
- **`create_resource_monitor(device)`** — 创建整节点 CPU + 当前 CUDA 设备 GPU 的后台利用率采样器。每个 local cycle 开始时清零 cycle 统计,每个 `train_one_step` 以单调时钟计时;训练期峰值跨 cycle 保留。

### 全局权重采纳

- **`adopt_global(*, model, latest, param_index, device) -> int`** — 全量模式:加载 `latest["weight_path"]` 为扁平向量 → 整体写回模型 → 返回新版本号。调用方随后负责重建内层优化器并清零 `tokens_since_global_load`。
- **`choose_learner_compute_placement(...)`** — 按参数量、`syncer.compute_dtype`、临时向量数和 CUDA 当前 free/total memory 估算 prediction/reconcile 工作集；预留至少 1 GiB 且不少于设备容量 5%，可安全容纳时选 learner CUDA，否则选 CPU 并返回 fallback reason。
- **`rebase_local_delta_onto_global(*, model, latest, param_index, device, reference_flat, config) -> (version, delta_norm, compute_stats)`** — full 实验模式:按 `syncer.compute_dtype` 计算 `current_local-reference`,加到新版 global 后写回模型。优先在 learner GPU 执行，估算有 OOM 风险或实际 CUDA OOM 时保持 dtype 回退 CPU；调用方随后立即释放 reference 并保留 carried token 计数。
- **`snapshot_model_for_reconcile(...)`** — 按相同 placement/dtype 策略建立 local-delta reference，避免固定 CPU FP32 快照。
- **`predict_next_global_weight(...)`** — 在选定 placement 上加载 global/outer、构造 token-weighted delta 并执行真实 outer Nesterov step；返回的 reference 保留计算 dtype/device 供后续 reconcile 使用，日志包含 placement 与 OOM fallback 证据。
- **`load_fragment_latest_into_model(*, model, latest, param_index, fragment_index, device, paths, config) -> (global_merge_event, {fragment_id: version})`** — 启动期:加载 latest 中**所有**片、materialize 成完整向量后整体写回；缺片时以更新 event 的完整快照重试。
- **`adopt_fragment_updates(*, model, latest, param_index, fragment_index, last_loaded_fragment_versions, device, paths, config) -> (event, versions, changed)`** — 运行期增量采纳:flatten 当前模型 → 只加载版本更新的片并 scatter → 全部成功后才一次性提交模型与版本。有片被 GC 时丢弃私有草稿并以整份更新 latest 重试。
- **`apply_fragment_adoption(...) -> FragmentAdoptionResult`** — 四个 fragment 采纳语境的统一收尾；调用点显式指定事件名、是否清零对应 token、是否允许按配置重置 optimizer/scheduler、是否附带完整片版本。inner poll/upload 后、final wait、最终 latest 的既有差异由参数表达，不再复制状态转换块。事件分别记录 checkpoint load/apply、optimizer/scheduler reset 与两者总 pause；调用前的 latest 等待不计入 pause。
- **`wait_for_final_fragment_progress(...)`** — fragment finally 的有界等待器；no-progress deadline 与 heartbeat schedule 使用独立单调时钟。等待中按配置间隔写 `active, phase=final_fragment_wait`，采纳 latest 后立即补带新版本的心跳，但任何心跳/采纳都不延长 deadline。
- **`finalize_fragment_adoption_and_heartbeat(...)`** — 把 final adoption 的诊断边界与最终 stopped heartbeat 分开；adoption 异常记录后仍调用 stopped heartbeat，随后重新抛出原异常，不能把收尾失败伪装成成功；heartbeat 自身失败同样向外传播并保留异常链。

### update 提交

- **`MidCycleAdoptionTracker`** — full runner 的区间局部计数器；每个 cycle 开始时清零，只记录 replace inner-poll 的成功采纳次数与最近一次切换前已完成的区间 step。`mid_cycle_global_adopted` 和 `update_written` 事件保留同一快照供核对。
- **`write_update(*, ..., resource_metrics, mid_cycle_adoption_count, base_switched_at_step, flat) -> (update_id, tensor_path, pointer_path, metadata)`** — 全量模式提交:生成 `update_id = {learner}_{step:08d}_{uuid12}`;**先**原子写不可变 payload(dtype 按 `io.tensor_dtype`),可选 sha256,**后**原子替换 `updates/latest/<learner>.json` 固定 pointer(= 提交点)。metadata 记录 `tensor_dtype`、恒在的 mid-cycle 两字段，并携带全训练至今的 CPU/GPU 峰值、上一 local cycle 的 CPU/GPU 峰值和该 cycle 的平均每 step 时间。
- **`write_fragment_update(*, ..., resource_metrics, fragment_id, base_fragment_version, base_global_merge_event, tokens_since_fragment_load, fragment_norm, fragment_tensor)`** — fragment 版,文件名与 update_id 带 `fXXX`,元数据带 `update_kind: "fragment"` 和同一组资源指标。

### 主循环

- **`run_learner` 全量模式主体** — 启动(种子/设备/模型/索引校验/adopt v0/watchdog/优化器/心跳/数据迭代器)→ 循环:inner_steps 训练(带心跳、日志、可选中途采纳、每步 watchdog 检查)→ 故障注入 → 按 `io.tensor_dtype` flatten + `write_update`(不可变 payload 后原子替换固定 pointer)→ CSV/心跳 → 可选上传后采纳 → 可选注入崩溃;learner 不删除 proposal payload;finally:记录 stop 原因、写 `stopped` 心跳、`process_exit`。
- **`run_fragment_learner(config, learner_id)`** — fragment 模式主体,差异:
  - 启动时还要等待并校验 fragment index;
  - 上传前 `select_fragment(local_update_index, K)` 选片、`extract_fragment_from_model` 直接抽取目标片,不先构造完整 flatten;
  - proposal metadata 原子替换到固定的 per-(learner,fragment) pointer `updates/latest/learner_XXX_fNNN.json`;payload 目录只保存不可变 tensor,消费后由 syncer maintenance 统一清理;
  - 采纳走增量 `adopt_fragment_updates`,变化片的 `tokens_since_fragment_load` 清零;
  - finally 中的收尾:若无错且设置了 `stop_after_outer_steps`,在 `no_progress_timeout_seconds` 预算内轮询等待 `global_merge_event` 达标，期间持续采纳并周期写 active final-wait heartbeat；最后再整体采纳一次，然后由外层 finally 写 stopped process-exit heartbeat。final adoption 异常记录 `final_fragment_adoption_failed`，仍继续尝试 stopped heartbeat。

---

## runtime/adoption.py

- **`GlobalAdoptionStrategy`** — full learner 的采纳状态机接口；runner 只持有工厂构造的一个策略对象。
- **`ReplaceGlobalAdoptionStrategy` / `RebaseGlobalAdoptionStrategy` / `PredictGlobalAdoptionStrategy`** — 分别封装直接覆盖、发布点 local-delta rebase、prediction/reconcile 的私有 reference、token 和 update-id 状态。
- **钩子顺序** — 每个 cycle 依次为 `on_local_tokens` → 可选 `on_newer_latest` → `on_stop` 或 `on_cycle_end` → `before_publish` → `on_after_publish`。
- **`StrategyAction` / `AdoptionOutcome`** — 把新版本、latest metadata、token 计数与 preserve/reset 决策返回 runner；`global_adopted`、`inner_training_state_preserved`、`inner_optimizer_reset` 只由 learner 的统一收尾函数发出。
- stop 是策略状态机的正常输入：predict reconcile wait 返回 None 时有 stop 则 abandon/清空 state，无 stop 才抛 timeout；predict/rebase after-publish 仍优先直接采纳已可见新版，只有“无新版且无 stop”才创建新 prediction reference/rebase anchor。
- **`validate_global_adoption_strategy(config)` / `make_global_adoption_strategy(config)`** — 配置解析经同一策略类型表调用当前策略的 `validate`，运行期再由唯一工厂构造状态机；非法策略名启动即拒绝。prediction 专属超时位于 `learner.prediction.reconcile_timeout_seconds`。策略状态只存在于进程内，不写入磁盘或 DB。

---

## runtime/failure_sim.py — 故障注入

三个函数都以 `failure_sim.enabled` 为总开关,参数容忍任意带同名属性的对象:

- **`maybe_sleep_jitter(config) -> float`** — 上传前随机睡 `U(0, sleep_jitter_seconds)`,返回实际时长(模拟慢节点)。
- **`should_skip_upload(config) -> bool`** — 以 `upload_skip_probability` 概率返回 True(模拟上传丢失;learner 记 `update_skipped` 后直接进入下一区间)。
- **`maybe_crash(config)`** — 以 `crash_probability` 概率 `sys.exit(97)`(模拟进程崩溃;97 便于在调度器日志中识别是注入)。
