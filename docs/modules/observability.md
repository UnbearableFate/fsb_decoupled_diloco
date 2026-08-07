# 模块参考:`fs_diloco/observability`

JSONL 事件日志、CSV 指标、learner 资源采样与 W&B 遥测。资源读取失败会降级;W&B 只有 import/init 明确降级,后续 SDK 调用不是全部隔离。

---

## observability/logging_utils.py — JSONL 日志

- **`JsonlLogger(path, actor, mirror_stdout=True)`** — 每进程一个,写 `logs/<actor>.jsonl`:
  - **`event(event_type, **payload)`** — 追加一行 `{timestamp, actor, event_type, hostname, **payload}`(`json.dumps(default=str)` 容忍任意值),flush + fsync 落盘,并镜像打印到 stdout。由于 payload 最后展开,调用方可以覆盖四个基础键;当前 runtime 不应这样做。
  - **`exception(event_type="error", **payload)`** — 自动附带 `traceback.format_exc()` 后走 `event`。
- **`log_uncaught_exception(logger)`** — 安装 `sys.excepthook`:未捕获异常先记一条 `error` 事件(带完整 traceback)再走默认钩子。learner/syncer 启动时都会安装。

## observability/metrics.py — CSV 指标

- **`append_csv_row(path, row, fieldnames=None)`** — 追加一行;文件不存在或为空时先写表头;未传 fieldnames 时使用当行键顺序,`extrasaction="ignore"` 忽略多余键。它依赖文件 close 刷新,没有显式 fsync、原子替换或进程间锁。`syncer_metrics.csv` 是单写者;`learner_metrics.csv` 与 `update_manifest.csv` 由所有 learner 共享追加,存在并发首表头/写入竞态,因此只能作尽力而为遥测,权威生命周期仍看 SQLite + archive。
- **`SYNCER_METRIC_FIELDS`** — `syncer_metrics.csv` 字段:timestamp、version、global_merge_event、fragment_id/fragment_version、selected_count、total_update_tokens、read/fragment_read/aggregation/fragment_aggregation/outer_step/publish/materialize_full 各段秒数;full publish 另含 weight/outer worker 时长、并发 checkpoint walltime、dtype/bytes、I/O 等待期 metadata/heartbeat 摄取计数与量化 round-trip 误差;fragment 含 `materialized_this_event/materialized_bytes`;interval 另以单调时钟给出 `discovery_seconds/idle_seconds/grace_seconds/merge_seconds/interval_residual_seconds/quorum_trigger`,并保留 read/publish/maintenance 独立分量;staleness 同时记录未加权 min/mean/max、effective-weight mean、fresh effective-weight mass 与 count JSON;其余为丢弃数与 selected learners 资源指标平均值。
- **`LEARNER_METRIC_FIELDS`** — `learner_metrics.csv` 字段:timestamp、learner_id、local_step、global_version、global_merge_event、fragment_id、base_fragment_version、train_loss、tokens、tokens_per_sec、update_write_seconds、`local_cycle_elapsed_seconds`、param_norm、fragment_norm、last_loaded_fragment_versions_json、fragment_adopt_count、learning_rate、scheduler_total_steps、phase,以及全训练/上一个 local cycle 的资源峰值、cycle 平均 step 时间、step 数和有效资源样本数。
- **`UPDATE_MANIFEST_FIELDS`** — `update_manifest.csv` 的精确字段为 timestamp、update_id、learner_id、update_kind、fragment_id、base_fragment_version、base_global_merge_event、base_global_version、local_step_start/end、tokens_this_update、tensor_dtype、file_path、file_size_bytes、sha256;full/fragment 各自不适用的列留空。

## observability/phase1_performance.py — 冻结的 Phase 1 性能门禁

- 格式版本固定为 1。business transaction 门禁要求 baseline/observer 各至少 400 个样本,以 25 个样本为一块做细粒度 AB/BA 交错;checkpoint publication 门禁要求 legacy/HA 各至少 100 个交替样本。
- 两项上限都按 `baseline_p99 × 1.25 + 0.002s` 计算。`nearest_rank_percentile(samples, quantile)` 使用 `ceil(q×n)-1` 的 nearest-rank 索引;`matched_p99_limit(...)` 只组合冻结 ratio 和 filesystem timing jitter,不从本次结果自适应放宽阈值。

## observability/resource_monitor.py — learner 资源采样

- **`_bounded_percent(value)`** — 转 float、拒绝非有限值并 clamp 到 `[0,100]`;None 保持 None。
- **`SystemCpuUtilizationReader(stat_path="/proc/stat")`** — `__call__()` 读取 aggregate `cpu` 行,以相邻两次 total/idle 差计算整节点利用率;第一次或非正 total delta 返回 None。
- **`ResourceMonitor(..., sample_interval_seconds=1.0)`** — interval 实际下限 0.1 秒;`start()` 幂等,立即采样后启动 daemon thread;`stop()` 幂等,发 event、最多等待 `max(1, 2*interval)` 秒并再采一次。每次 CPU/GPU reader 异常只增加 error count;只有至少一个值有效时才增加 resource sample count。
- **`begin_cycle()`** — 清空 cycle 峰值、step 累计和 cycle sample 基线;**`record_step_duration(seconds)`** 只接受有限非负值。
- **`sample_now()` / `_sample_loop()`** — 在锁内更新 training/cycle 峰值;后台按 stop event 间隔采样。
- **`cycle_snapshot()`** — 返回全训练峰值、当前 cycle 峰值、`local_cycle_step_time_seconds_mean`、本 cycle 的有效采样数与 step 数。
- **`training_snapshot()`** — 返回全训练峰值、有效样本总数及 reader 错误总数。
- **`finite_resource_metrics(payload)`** — 遍历传入的所有键,跳过 None,原样保留 int,其他值转 float 后只保留有限值;它不校验键名白名单。运行时给它的是 monitor snapshot,用于防止 NaN/None 写入 update metadata。
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

W&B 的初始化在 `runtime/syncer.py: init_wandb_run()`:import 失败、init 失败都记日志并返回 None;`syncer/version` 被定义为全局 step 轴。初始化成功后的部分 `log/summary/finish` 调用没有局部 try/except,SDK runtime 异常可能进入 syncer 的通用 error 收尾。退出时 summary 还会尝试写入完整训练时间和全体 learner 的训练期 CPU/GPU 峰值聚合。

`global_adopted` 与 fragment 对应采纳事件把停顿拆为
`adoption_load_apply_seconds`、`adoption_optimizer_reset_seconds` 与两者之和
`adoption_pause_seconds`;等待未来 latest 的时间由独立 wait 事件记录,不计入 pause。
analysis 按 learner 汇总次数/总和/均值,并用已完成 CSV cycle 的
`local_cycle_elapsed_seconds` 之和作分母;旧 run 缺字段时标为 `unavailable`。
