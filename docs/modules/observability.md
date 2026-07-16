# 模块参考:fs_diloco/observability

JSONL 事件日志、CSV 指标、learner 资源采样与 W&B 遥测。观测组件失败不应影响训练(W&B 全程 try/except 降级)。

---

## observability/logging_utils.py — JSONL 日志

- **`JsonlLogger(path, actor, mirror_stdout=True)`** — 每进程一个,写 `logs/<actor>.jsonl`:
  - **`event(event_type, **payload)`** — 追加一行 `{timestamp, actor, event_type, hostname, **payload}`(`json.dumps(default=str)` 容忍任意值),flush + fsync 落盘,并镜像打印到 stdout(便于 PBS 日志查看);
  - **`exception(event_type="error", **payload)`** — 自动附带 `traceback.format_exc()` 后走 `event`。
- **`log_uncaught_exception(logger)`** — 安装 `sys.excepthook`:未捕获异常先记一条 `error` 事件(带完整 traceback)再走默认钩子。learner/syncer 启动时都会安装。

## observability/metrics.py — CSV 指标

- **`append_csv_row(path, row, fieldnames=None)`** — 追加一行;文件不存在或为空时先写表头;`extrasaction="ignore"` 忽略多余键。非原子(单写者追加,可接受)。
- **`SYNCER_METRIC_FIELDS`** — `syncer_metrics.csv` 字段:timestamp、version、global_merge_event、fragment_id/fragment_version、selected_count、total_update_tokens、read/fragment_read/aggregation/fragment_aggregation/outer_step/publish/materialize_full 各段秒数、fragment staleness min/mean/max、stale_updates_dropped、global_interval_seconds,以及本次被选 updates 的 learner 资源指标平均值。
- **`LEARNER_METRIC_FIELDS`** — `learner_metrics.csv` 字段:timestamp、learner_id、local_step、global_version、global_merge_event、fragment_id、base_fragment_version、train_loss、tokens、tokens_per_sec、update_write_seconds、param_norm、fragment_norm、last_loaded_fragment_versions_json、fragment_adopt_count、phase,以及全训练/上一个 local cycle 的资源峰值和 cycle 平均 step 时间。
- **`UPDATE_MANIFEST_FIELDS`** — `update_manifest.csv` 字段:每份 update 的 id/kind/base 版本/步区间/token 数/`tensor_dtype`/文件指针/sha256。

## observability/resource_monitor.py — learner 资源采样

- **`SystemCpuUtilizationReader`** — 从 `/proc/stat` 两次快照的差值计算整节点 CPU 利用率(0–100%),无需额外系统依赖。
- **`ResourceMonitor`** — learner 生命周期内的后台采样器(默认每秒一次),同时维护全训练 CPU/GPU 峰值和当前 local cycle CPU/GPU 峰值;`record_step_duration()` 累计当前 cycle 的逐 step 耗时。
- **`cycle_snapshot()`** — 返回全训练峰值、当前 cycle 峰值、`local_cycle_step_time_seconds_mean` 及样本/step 数。
- **`training_snapshot()`** — 返回最终全训练峰值、样本数及读取失败次数。
- GPU 利用率由 learner 调用 `torch.cuda.utilization(device)` 读取,需要 `nvidia-ml-py`;读取失败被计数并降级为缺失值,不会中断训练。CPU 指标作用域是整节点,GPU 指标作用域是 learner 的 CUDA 可见设备。

## observability/wandb_logging.py — W&B 助手(syncer 侧)

- **`_slug(value, max_length=48)`** / **`_lr_slug(value)`**(私有)— 字符串安全化(小写、非字母数字转 `-`)与学习率格式化(`0.7 → 0p7`)。
- **`syncer_wandb_project_name(config)`** — 固定 `"fs-diloco-miyabi-syncer"`。
- **`syncer_wandb_run_name(config, *, timestamp=None)`** — 可读 run 名:`{时间戳}_{run名}_m-{模型}_d-{数据集}_L{learner数}_q{qmin}-{qmax}_is{inner_steps}_mb{micro_batch}_ga{grad_accum}_outer-{外层名}-lr{lr}`。
- **`syncer_wandb_tags(config)`** — 结构化标签(`syncer`、`model:*`、`data:*`、`learners:*`、`outer:*`)+ 用户自定义 tags。
- **`wandb_config(config, *, device, hostname, shared_root)`** — 完整配置 dict + `runtime`(角色/设备/主机/CUDA_VISIBLE_DEVICES)+ `derived`(每 inner step 的全局 batch token 数)。
- **`selected_update_summary(selected, *, current_version) -> dict`** — 对本次选中更新的 train_loss/param_norm/grad_norm/delta_norm 计算 mean/min/max(过滤非有限值),加 staleness mean/max;作为 `selected/*` 指标上报。
- **`selected_resource_summary(selected) -> dict`** — 从本次选中的 update 元数据读取资源记录,逐指标过滤非有限值并跨 learner 求平均;上报 `learner/training_*_peak_percent_mean`、`learner/local_cycle_*_peak_percent_mean` 与 `learner/local_cycle_step_time_seconds_mean`。
- **`wandb_is_disabled(config) -> bool`** — `$WANDB_DISABLED` 或 `wandb.enabled=false`。

W&B 的初始化在 `runtime/syncer.py: init_wandb_run()`:import 失败、init 失败都记日志并返回 None,训练照常;`syncer/version` 被定义为全局 step 轴。退出时 summary 还会写入完整训练时间和全体 learner 的训练期 CPU/GPU 峰值聚合。
