# 模块参考:fs_diloco/runtime/learner.py、adoption.py 与 failure_sim.py

learner 进程实现。整体流程见 [03-runtime-flow.md](../03-runtime-flow.md) 第 2、3 节。

---

## runtime/learner.py

HA full启动先调用 `load_run_descriptor()` 和 bootstrap identity gate，再用 `prepare_learner_instance_dir()`只创建自身目录。`EpochControlReader`替代 fixed latest/stop读取且不打开 SQLite：它扫描 bounded epoch目录，只接受目录名、run/epoch/owner、自校验 heartbeat、head→immutable pointer SHA和 canonical stop自校验 SHA都一致的最高 epoch；最高 epoch没有 head时不回退旧 epoch。learner只把非空且非 `error` 的 canonical terminal视为最终 stop；`error` generation仍允许 recovery claim和 successor resume。learner watchdog区分普通无进展、current heartbeat陈旧、recovery claim/job仍 outstanding和 canonical repair窗口；后两者在配置预算内等待。`coordination.recovery_submission.enabled=false`时不执行 qsub。

### CLI 与入口

- **`parse_args(argv)`** — `--config`(必填)、`--learner-id`(必填)、`--run-id`、`--shared-root`、`--num-learners`,以及与 syncer 对称的实验覆盖参数:`--training-seed`、`--scan-interval-seconds`、`--syncer-device`、`--syncer-publish-dtype`、`--staleness-lambda`、`--max-staleness-versions`、`--global-adoption-strategy`、`--completion-mode`、`--parallel-checkpoint-writes`、`--materialize-full-every-events`、`--ingest-during-publish`、`--capture-terminal-predecessor-for-eval`(launcher 把同一组覆盖传给两类进程,保证 resolved config 一致)。
- **`main(argv)`** — `resolve_config` 后调 `run_learner`。
- **`run_learner(config, learner_id)`** — 分派:`fragments.enabled` → `run_fragment_learner`,否则执行全量模式主循环(函数体内)。

### 共享文件交互

- **`write_heartbeat(*, paths, config, learner_id, status, phase, last_loaded_global_version, last_local_step, last_update_id, tokens_per_sec=None, last_loaded_global_merge_event=None, last_loaded_fragment_versions=None, last_adopted_fragments=None, resource_metrics=None, learning_rate=None, scheduler_total_steps=None, status_reason=None)`** — 组装心跳 payload 原子覆盖 `heartbeats/<id>.json`;fragment 相关字段仅在传入时包含;心跳恒带当前 `learning_rate` 与 `scheduler_total_steps`;active update 心跳携带上一 local cycle 的资源指标,最终 stopped 心跳携带全训练资源峰值,watchdog 退出时附 `status_reason=syncer_unresponsive`。
- **`wait_for_json(path, *, timeout_seconds=1800, poll_seconds=1)`** — 轮询直到 `safe_read_json` 成功;启动期等待 param_index/fragment_index/latest 用;超时抛 `TimeoutError`。
- **`read_latest_if_newer(paths, last_loaded_global_version) -> dict | None`** — 读 `latest.json`,版本不高于已加载值时返回 None(全量模式轮询原语)。
- **`read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)`** — fragment 版:要求 `latest_kind == "fragment"` 且 `global_merge_event` 更大。
- **`wait_for_latest_if_newer(...)`** — full 发布后的可选 bounded wait；先即时读，随后到 deadline 轮询，stop 出现即提前返回 None。
- **`wait_for_fragment_latest_if_newer(paths, last_event, config)`** — fragment 上传后等待预算为 `max(stop_file_poll_seconds, scan_interval_seconds + configured grace(initial/fixed) + 1)`，按 stop poll 间隔睡眠；不是单纯等一个 grace 值。
- **`LatestLoadResult` / `_wait_for_latest_payload_if_newer(...)`** — 把实际成功使用的 latest 与 callback 结果绑定；helper 只接受严格更大 `version`/`global_merge_event`，并按参数要求/排除 fragment kind。
- **`load_or_refresh_latest(...) -> LatestLoadResult`** — 只有 callback 抛 `FileNotFoundError` 才进入 current-only GC 竞态重试；其他异常立即传播。保留第一次 missing 异常，等待严格更新 pointer 后从整个 callback 重跑；总预算从第一次 missing 开始，stop 出现提前结束，耗尽时给首个异常加 retry note 并以异常链重抛。
- **`_latest_load_retry_kwargs(config)`** — 统一返回 reconcile timeout 与 post-publish poll，供 full/fragment checkpoint load 路径使用。

### 停止判定

- **`stop_requested(paths, local_step, config)`** — 默认 `local_or_global` 在 `max_local_steps` 达标或 `stop.json` 存在时停止;`global_only` 忽略本地 horizon,只认 `stop.json`。
  full 与 fragment learner 共用该谓词，`stop.json` 始终优先于本地 horizon。
- **`SyncerProgressWatchdog.start/observe/seconds_since_signal/deadline_reached`** — 同时保存 monotonic 计时和 wall 诊断时间；只有严格更大的版本刷新信号，等值/回退不会。
- **`syncer_watchdog_timeout_seconds(config)` / `confirm_syncer_unresponsive(...)`** — timeout 显式值优先，null 沿用 no-progress；deadline 确认先看 stop，再即时读 latest，有新版本就刷新 watchdog。确认仍无进展才让 runner 以 `syncer_unresponsive` 受控退出且不再发布 proposal。

### 训练组件

- **`inner_lr_multiplier(config, completed_local_steps)`** — warmup 时为 `(completed+1)/warmup`；cosine progress 为 `(completed-warmup)/(total-warmup)` 并 clamp，结果不低于 `min_lr_ratio`；none 在 warmup 后为 1。
- **`build_inner_optimizer_and_scheduler(model, config, *, completed_local_steps) -> (optimizer, scheduler|None)`** — 仅支持 AdamW；`none` 返回 None，cosine 使用独立 `scheduler_total_steps`。replace/direct adoption 与 fragment adoption 可重建对象，但从累计步恢复相位；rebase/prediction reconcile 按策略结果保留完整状态。
- **`current_inner_learning_rate()` / `inner_training_state_metrics()`** — 取首个 optimizer param group LR，并生成 LR、scheduler horizon 与 state-presence 日志证据。
- **`maybe_autocast(device, precision)`** — CUDA + bf16 时启用 autocast,否则禁用的空上下文。
- **`train_one_step(model, batch_iter, optimizer, scheduler, *, device, config)`** — 先 zero-grad；每个 microbatch 原始 loss 除 accumulation 次数后检查有限性并 backward；返回原始 loss 的算术平均。token 计数是所有 input IDs（每序列第一个虽不被 causal loss 预测仍计入）；只有启用 grad clip 时返回 grad norm，否则为 None。optimizer 后再 scheduler step。
- **`create_resource_monitor(device)`** — 创建整节点 CPU + 当前 CUDA 设备 GPU 的后台利用率采样器。每个 local cycle 开始时清零 cycle 统计,每个 `train_one_step` 以单调时钟计时;训练期峰值跨 cycle 保留。

### 全局权重采纳

- **工作集常量** — `PREDICTION_WORKING_VECTOR_COUNT=14`、`RECONCILE_WORKING_VECTOR_COUNT=5`、`REFERENCE_WORKING_VECTOR_COUNT=3`，只用于估算 CUDA 临时 footprint；安全余量为 `CUDA_COMPUTE_MIN_RESERVE_BYTES=1 GiB` 与 `CUDA_COMPUTE_RESERVE_FRACTION=0.05×total` 的较大者。
- **`adopt_global(*, model, latest, param_index, device) -> int`** — 逐命名 tensor 从 CPU checkpoint 直接 copy 到当前模型参数 dtype/device，不构造完整 flat；随后显式 `model.to(device)` 确保模型在传入设备。调用方决定 optimizer/token reset。
- **`LearnerComputePlacement` / `log_fields()`** — immutable placement 记录；输出实际 device/dtype、参数量、工作向量数、估算 bytes、CUDA free/total/reserve 和 fallback reason。
- **`learner_compute_dtype()` / `choose_learner_compute_placement(...)`** — dtype 跟随 syncer；按 `numel*element_size*working_vector_count` 估算工作集。CUDA 预留 `max(1GiB, 5% total)`，free 减 reserve 足够才选 GPU；无 CUDA、非 CUDA preferred、查询失败或不足均选 CPU并记 reason。
- **`cpu_fallback_placement()`** — 实际 CUDA OOM 后保持 dtype/估算信息改为 CPU placement；只对识别出的 CUDA OOM 重试。
- **`rebase_local_delta_onto_global(*, model, latest, param_index, device, reference_flat, config) -> (version, delta_norm, compute_stats)`** — full 实验模式:按 `syncer.compute_dtype` 计算 `current_local-reference`,加到新版 global 后写回模型。优先在 learner GPU 执行，估算有 OOM 风险或实际 CUDA OOM 时保持 dtype 回退 CPU；调用方随后立即释放 reference 并保留 carried token 计数。
- **`snapshot_model_for_reconcile(...)`** — 按相同 placement/dtype 策略建立 local-delta reference，避免固定 CPU FP32 快照。
- **`_predict_next_global_weight_on_placement()` / `predict_next_global_weight()`** — 要求 Nesterov、outer weight decay=0、local_tokens>0。加载 global/outer 并要求两份 theta 精确相等且有 momentum buffer；previous `total_update_tokens` ≤0 时按 `local_tokens*max(1,quorum_min)` bootstrap。以 `local_weight=min(1,local/estimated)` 混合历史 aggregate delta `momentum_buffer * -(1-outer_momentum)` 与本地 `current-global`，取负为预测梯度，再调用真实 outer step并把模型设为 predicted theta；CUDA OOM 时整条计算在 CPU 重试。
- **`prepare_prediction_or_find_newer_latest()`** — 建预测前再次读 pointer；若已出现严格新版就返回该 latest，避免基于过时版本预测。
- **`load_fragment_latest_into_model(*, model, latest, param_index, fragment_index, device, paths, config) -> (global_merge_event, {fragment_id: version})`** — 启动期:加载 latest 中**所有**片、materialize 成完整向量后整体写回；缺片时以更新 event 的完整快照重试。
- **`adopt_fragment_updates(*, model, latest, param_index, fragment_index, last_loaded_fragment_versions, device, paths, config) -> (event, versions, changed)`** — 运行期增量采纳:flatten 当前模型 → 只加载版本更新的片并 scatter → 全部成功后才一次性提交模型与版本。有片被 GC 时丢弃私有草稿并以整份更新 latest 重试。
- **`FragmentAdoptionResult` / `apply_fragment_adoption(...)`** — result 携带 event/version/changed fragments/token/adopt count/optimizer/scheduler；共同 helper 处理四种采纳语境。调用点显式指定事件名、是否清零 token、是否按配置 reset optimizer/scheduler、是否附片版本。事件拆 checkpoint load/apply、state reset 与总 pause；调用前等待不计入。
- **`wait_for_final_fragment_progress(...)`** — fragment finally 的有界等待器；no-progress deadline 与 heartbeat schedule 使用独立单调时钟。等待中按配置间隔写 `active, phase=final_fragment_wait`；采纳 latest 后只在原心跳 deadline 已到时立即补写，否则等到原调度点。任何心跳/采纳都不延长总 deadline。
- **`finalize_fragment_adoption_and_heartbeat(...)`** — 把 final adoption 的诊断边界与最终 stopped heartbeat 分开；adoption 异常记录后仍调用 stopped heartbeat，随后重新抛出原异常，不能把收尾失败伪装成成功；heartbeat 自身失败同样向外传播并保留异常链。

### update 提交

- **`MidCycleAdoptionTracker.reset/record/metadata`** — full runner 的区间局部计数器；reset 清零，record 要求 completed interval step≥1并增加次数/覆盖最近切换 step，metadata 返回两个 proposal 字段。只记录 replace inner-poll 成功采纳；每 cycle 重置。
- **`write_update(*, ..., resource_metrics, mid_cycle_adoption_count, base_switched_at_step, flat) -> (update_id, tensor_path, pointer_path, metadata)`** — 全量模式提交:生成 `update_id = {learner}_{step:08d}_{uuid12}`;**先**原子写不可变 payload(dtype 按 `io.tensor_dtype`),可选 sha256,**后**原子替换 `updates/latest/<learner>.json` 固定 pointer(= 提交点)。metadata 记录 `tensor_dtype`、恒在的 mid-cycle 两字段，并携带全训练至今的 CPU/GPU 峰值、上一 local cycle 的 CPU/GPU 峰值和该 cycle 的平均每 step 时间。
- **`write_fragment_update(*, ..., resource_metrics, fragment_id, base_fragment_version, base_global_merge_event, tokens_since_fragment_load, fragment_norm, fragment_tensor)`** — fragment 版,文件名与 update_id 带 `fXXX`,元数据带 `update_kind: "fragment"` 和同一组资源指标。

### 主循环

- **`run_learner` 全量模式主体** — 启动(种子/设备/模型/索引校验/adopt v0/watchdog/优化器/首心跳/数据迭代器/资源监控)→ 循环:inner_steps 训练(带心跳、日志、可选中途采纳、每步 watchdog 检查)→ 故障注入 → 按 `io.tensor_dtype` flatten + `write_update`(不可变 payload 后原子替换固定 pointer)→ CSV/心跳 → 可选上传后采纳 → 可选注入崩溃;learner 不删除 proposal payload。只有启动完成并进入训练 `try` 后，finally 才会记录 stop 原因、写 `stopped` 心跳和 `process_exit`；更早的模型/index/latest/data 初始化异常不在该 finally 内。
- **`run_fragment_learner(config, learner_id)`** — fragment 模式主体,差异:
  - 启动时还要等待并校验 fragment index;
  - 上传前 `select_fragment(local_update_index, K)` 选片、`extract_fragment_from_model` 直接抽取目标片,不先构造完整 flatten;
  - proposal metadata 原子替换到固定的 per-(learner,fragment) pointer `updates/latest/learner_XXX_fNNN.json`;payload 目录只保存不可变 tensor,消费后由 syncer maintenance 统一清理;
  - 采纳走增量 `adopt_fragment_updates`,变化片的 `tokens_since_fragment_load` 清零;
  - finally 中的收尾:若无错且设置了 `stop_after_outer_steps`,在 `no_progress_timeout_seconds` 预算内轮询等待 `global_merge_event` 达标，期间持续采纳并周期写 active final-wait heartbeat；最后再整体采纳一次，然后由外层 finally 写 stopped process-exit heartbeat。final adoption 异常记录 `final_fragment_adoption_failed`，仍继续尝试 stopped heartbeat。

`build_adoption_context()` 把当前模型/path/index/device、latest getter/waiter、logger 和 token/version 状态封进 `AdoptionContext`；`finalize_strategy_action()` 是唯一执行 outcome 的 runner helper：更新版本/latest/tokens，按 action preserve 或重建 AdamW/scheduler并记录统一事件。

---

## runtime/adoption.py

- **`PredictionState`** — 保存 prediction reference、累计 local tokens、update ID 和 base global version；`active` 属性**只**检查 reference 非 null，`require_active()` 再严格要求 reference/update ID/base 齐全且 token 非负，`add_tokens()` 先调严格检查再返回新状态。
- **`PredictionReconcileResult` / `AdoptionOutcome` / `PublishResult`** — 分别封装 reconcile 结果、一次 adoption 的版本/latest/tokens/计时与发布后的 update/stop/latest 事实。
- **`StrategyAction.__post_init__()`** — 禁止同一 action 同时携带 adoption outcome 与 standalone optimizer reset，避免执行两次互斥迁移。
- **`AdoptionContext`** — runner state 的回调/数据容器；`read_newer_latest/wait_for_newer_latest` 只取严格新版，`_load_latest` 统一 GC 竞态重试，`adopt_global/rebase_local_delta/snapshot_model/prepare_prediction` 分别调用 learner 算术并计时。
- **`reconcile_prediction()`** — 计算 prediction 后当前模型相对 prediction reference 的 local progress，再加到实际新 global；保留 optimizer/scheduler，并携带预测后累计 token。
- **`GlobalAdoptionStrategy`** — full learner 状态机基类；默认新 latest 走直接 adoption，其他 hook 无动作。`_poll_after_publish` 实现共同即时/限时轮询，`_direct_adoption` 产生 reset optimizer 的 outcome。
- **`ReplaceGlobalAdoptionStrategy`** — inner poll 取决于配置；发现新版或 publish 后轮询命中时直接 replace/reset。
- **`RebaseGlobalAdoptionStrategy`** — 只有发布后无新版且无 stop 时建立 anchor；后续 inner poll 首次看到新版时做 `new_global + (current-anchor)`，保留 optimizer/scheduler 和 anchor 后 tokens，然后清 anchor。
- **`PredictGlobalAdoptionStrategy`** — prediction 必须在下个周期结束前 reconcile，不能叠加第二次 publish。发布后有新版直接 replace；否则建立预测、重置 optimizer/token。inner poll/周期结束等待实际新版；stop 正常 abandon，无 stop 超时才报错。
- 所有策略都实现同一组 hook：`validate()`、`wants_inner_poll()`、`on_newer_latest()`、`on_cycle_end()`、`before_publish()`、`on_after_publish()`、`on_local_tokens()`、`on_stop()`。rebase 的 `_clear_anchor()` 释放 reference/token；predict/rebase 的 `__init__()` 只初始化私有内存状态，不读磁盘。
- **钩子顺序** — 每个 cycle 依次为 `on_local_tokens` → 可选 `on_newer_latest` → `on_stop` 或 `on_cycle_end` → `before_publish` → `on_after_publish`。
- `global_adopted`、`inner_training_state_preserved`、`inner_optimizer_reset` 只由 runner 的 `finalize_strategy_action()` 统一发出；direct replace 重建 optimizer，rebase/reconcile 保留现有对象，prediction start 是独立 reset action。scheduler 即使重建也恢复累计 local-step 相位。
- stop 是策略状态机的正常输入：predict reconcile wait 返回 None 时有 stop 则 abandon/清空 state，无 stop 才抛 timeout；predict/rebase after-publish 仍优先直接采纳已可见新版，只有“无新版且无 stop”才创建新 prediction reference/rebase anchor。
- **`STRATEGY_TYPES` / `strategy_type_for_config()` / `validate_global_adoption_strategy()` / `make_global_adoption_strategy()`** — 唯一策略表，三个键是 `replace/rebase_post_publish_delta/predict_post_publish_global`。rebase/predict 都要求 `adopt_global_after_upload=true` 与 inner poll=true；predict 还要求 Nesterov、outer wd=0 和正 timeout。fragment 在 config resolver 中另行限制只能 replace。策略状态只存在内存，不写磁盘/DB。

---

## runtime/failure_sim.py — 故障注入

三个函数都以 `failure_sim.enabled` 为总开关,参数容忍任意带同名属性的对象:

- **`maybe_sleep_jitter(config) -> float`** — 上传前随机睡 `U(0, sleep_jitter_seconds)`,返回实际时长(模拟慢节点)。
- **`should_skip_upload(config) -> bool`** — 以 `upload_skip_probability` 概率返回 True(模拟上传丢失;learner 记 `update_skipped` 后直接进入下一区间)。
- **`maybe_crash(config)`** — 以 `crash_probability` 概率 `sys.exit(97)`。`SystemExit` 不被 `except Exception` 捕获，但 Python 仍执行外层 `finally`，所以当前 runner 会停止 monitor 并尝试发布 stopped heartbeat后才以 97 退出；fragment runner 的 `had_error` 也不会因此置 true，配置了 outer-step 目标时还可能先进入有界 final-fragment wait。这模拟非零退出，不等价于 SIGKILL/节点突然消失。
