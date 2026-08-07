# 模块参考:`fs_diloco/tools` 与入口 shim

离线工具默认只读 run;明确的导出、CSV、validation 附加命令例外。`analysis`、轨迹比较和 CSV 抽取主要用标准库,可在没有 GPU 的环境运行。

## `tools/init_run.py` 与 `tools/launch_independent_run.py`

- `init_run.initialize_run()` 是 HA 新 run 唯一 initializer。它拒绝 HA 关闭、fragment、缺失 source identity、非显式允许的 dirty snapshot 和任何已存在 run root;写解析后配置、source manifest、不可变 descriptor 后,以 `schema_bootstrap.initialize_new_run()` 发布 static schema v2 或 dynamic schema v3 DB 和 bootstrap marker。
- `python -m fs_diloco.tools.init_run` 接受 `--config/--run-id/--shared-root/--project-root`;`--allow-dirty-snapshot` 只用于受控验证,正式 run 应从 clean source 初始化。
- `launch_independent_run.launch()` 组合 initializer 与独立 syncer qsub、可重跑 learner array qsub;默认只返回命令,显式 `--submit` 才提交。`--submit` 要求先提供按 workload 和相邻实测估算的 `--syncer-walltime/--learner-walltime`:取尽可能短、但仍为启动、运行和收尾保留充分余量的值,并在创建 immutable run root 前完成格式校验。两个 qsub 都显式带 `-l walltime=...`,不会继承通用 PBS 脚本的保守默认值。每次 qsub 立即返回结构化回执;若 syncer 已提交而 learner array 被拒绝,结果为 `submission_status=partial` 并保留 syncer job ID,CLI 以非零退出且不会自动 qdel。

## `tools/launch_phase1_acceptance.py`

- `submit_acceptance_jobs(...)` 先校验 crash/successor/learner 三个显式 walltime 并写 `status=pending` 产物,再顺序提交故障候选、后继者和 8-element 可重跑 learner array。故障候选使用 `after_db_commit` + `SIGKILL`;后继者依赖 `afterany:<crash-job>`,learner 依赖 `after:<successor-job>`,即后继者开始后即可调度。
- 每次 qsub 回执和已知 job ID 都会立即原子写回 pending 产物。首项失败为 `failed`,后续失败为 `partial`;两者都保留已提交 job 且不自动 qdel。三项全部提交后才原子写 `PASS` 产物并删除 pending 文件;CLI 仅对 `PASS` 返回 0。
- 三个 walltime 都应由 workload 和相邻实测估算,取尽可能短但仍覆盖启动、正常执行和完整收尾的值。可靠完成优先于继续缩短 walltime。

## `tools/phase1_matched_performance.py`

- `run_matched_performance(run_root)` 要求目标 descriptor 为 clean source,在 run 同级临时目录执行两组 matched 门禁。business 组用持久只读 candidate observer 和细粒度 25-sample AB/BA 块,分别收集 400 个静默 baseline 与 400 个 observer 下的隔离业务事务样本;SQLite trace 要求 observer 尝试的 `BEGIN IMMEDIATE/EXCLUSIVE` 严格为 0。
- checkpoint 组从目标 descriptor 重建相同 config/model/training seed/tensor/dtype,在同一 filesystem 上交替执行 100 个 Plan 01 legacy publication 与 100 个 HA publication 样本。输出包含 run/descriptor/source/config identity、原始块证据、nearest-rank p99、冻结阈值、tensor 元素数和 digest mode。
- 两组都使用 `observability.phase1_performance` 冻结的 `baseline p99 × 1.25 + 0.002s` 上限;任一采样、observer 活动、只读约束、identity 或 p99 条件失败即返回 `BLOCKED`。`main` 用原子 JSON 写入 `--output`,只有整体 `PASS` 时退出 0。

## Phase 2 launch 与 evidence 工具

- `launch_phase2_acceptance.submit_jobs()` 提交 G8/G9 crash leader、后继者、独立 dynamic bootstrap、可选 duplicate 和 chaos checker。每次 qsub 后立即原子更新 pending receipt,并逐 slot 重写 identity-bound bootstrap scheduler manifest;中途失败返回 partial 且不自动 qdel。
- `launch_phase2_matched` 顺序隔离同 source/config/model/data/seed/target 的 static 与 dynamic 1+8 runs,再提交 matched checker。因站点 array 约束,当前实现逐个提交 8 个 learner job 并显式传 static index 或 dynamic bootstrap slot。
- `phase2_test_evidence` 聚合 focused/full pytest 结果;`phase2_chaos_evidence` 从 run DB/control/log/scheduler receipts 验证 G8/G9 takeover、replacement、duplicate、pause、node-cap 和 drain;`phase2_matched_evidence` 按 `max(0,dynamic-static)/static` 执行冻结 5% 完整时间门禁。completed Checker 还要求每个 committed v>0 恰有一个匹配的 `merge:<version>` observation、没有额外 merge key,并要求 starvation generation 连续。
- `request_dynamic_close` 加载并校验 run descriptor,拒绝非 dynamic run,然后以 run/source/config identity 发布带自摘要的 manual close request。它不直接写 drain、stop 或 DB controller state。
- Phase 2 最终 completed Checker 仍由 `scripts/miyabi/check_plan02_phase2.py --mode phase2-completed` 执行只读复核;上述工具产物必须与同一 descriptor/source/config 绑定,不能跨 run 拼接。

## `tools/clean_run.py`

- `build_cleanup_plan()` 要求目标是 project `runs/` 下的精确 run 目录,`control/stop.json` 与 `summary.json` 的 run ID/final version 一致、全部 learner 已停止,并要求 `reports/` 内同 run、绑定当前 terminal final version 的 error-free `PASS` evidence;SQLite WAL/SHM/journal 存在、目标过宽、symlink 或 identity 不符都 fail closed。
- dry-run 输出逐文件 relative path、size、mtime 和原因。正式 `--delete` 必须指定新的 report-side `--manifest-output`,先 fsync planned inventory,逐文件重新核对 size/mtime 后 unlink,再把 manifest 原子更新为 `complete`;部分失败会记录已删除数量并标为 `failed`。
- cleaner 保留 authority DB、weight/outer checkpoint、control/config、所有 fsync-before-prune history(包括 `update_history.jsonl`)、syncer/candidate 日志和一份代表性 learner 日志;候选仅包括重复成功 learner 日志、offline W&B cache、terminal heartbeat/pointer/payload、可重建的 learner metrics/update manifest 及临时文件。早期 `20260807-0150` 清理已不可恢复地删除主 G9 run 的 update archive;其 cleanup 前 completed artifact 和未清理的 detached coherent run 仍分别保留冻结结论与原始复查证据,但主 run 不再可完整重跑 Checker。

## `tools/analysis.py`

### 读取与统计 helper

- `_read_csv_rows()` 用 `csv.DictReader` 读取行,并过滤「首列值再次等于首列表头」的重复 header(缓解多 learner 首写竞态);其他畸形/并发破坏的行不做结构校验。`_read_csv_summary()` 返回 `{exists,rows,last}`,文件不存在得到空结果。
- `_read_jsonl_deduplicated(path,key)` 忽略 JSON decode 失败的空/损坏行并按稳定 key 去重 archive crash-retry 重复项;合法 JSON 非 object 会在 `.get` 处失败,不在容错边界内。`_table_exists` 做旧 schema 兼容。
- `_sha256_file` 分块摘要;`_fragment_update_integrity` 实现了对传入 fragment payload 行的存在性/可选 SHA 检查,但当前 `analysis.py` 没有调用该 helper。`_db_summary` 对已归档 fragment payload 仅报告「预期已删除」,不会重新 hash。
- `_db_summary` 对 live DB 跑 integrity check,合并 live 与两个 archive JSONL,按 identity 去重后统计 update/version/fragment/frontier;不会把 archive 重新作为 runtime 权威。
- `_read_heartbeats()` 收集 `safe_read_json` 返回的 truthy JSON,不再校验顶层 object;正常 runtime 心跳是 object,手工写入的合法 list 可在后续 `.get` 处使分析失败。`_distribution()` / `_numeric_summary()` / `_percentile()` 对可用有限数值统计。
- `staleness_observational_summary` 从 syncer merge 行和 learner upload 行形成观测性分布;证据缺失时显式 unavailable,不把墙钟推断成协议陈旧度。
- `syncer_resource_cost` 把 read+aggregation+outer-step 视为 merge compute、publish 视为独立 active,给出 p50/p95、active/duty-cycle、reserved syncer node-hours 和估算 idle GPU node-hours。
- `_loss_summary` 比较 first/last 10,末段均值超过 `max(3*first, first+1)` 标记 obvious divergence。
- `_learner_fragment_adoption` 合并 metrics、heartbeat 和版本推断;`_learner_adoption_pause` 从 learner JSONL 只取 `adoption_pause_seconds`,聚合 count/total/mean,并以已完成 CSV cycle elapsed 求占比。它不分别聚合 load/apply 与 optimizer-reset 分量;任一已选采纳事件缺总 pause 时该 learner 标为 unavailable。
- `_syncer_log_flags` 检查 error/no-progress/traceback 文本标志。

### 公开接口

- `summarize_run(shared_root, db_path=None)` 汇总 latest/stop/summary、CSV、heartbeats、live+archive DB 生命周期、loss、adoption、staleness、资源和日志 flags。
- `_parse_fragment_ids` 解析逗号分隔片 ID;`assert_fragment_run(args, require_local_steps)` 验证 fragment latest kind、事件数、round-robin 期望版本、stop/materialized/metrics/DB/update 完整性、贡献者/adoption/loss/logs,集中报告全部违例。
- `_summary_parser()` / `_assert_parser()` / `_print_summary()` 构造 CLI 并选择 JSON/人类输出;`main` 支持 `summary`、`assert-fragment-smoke`、`assert-fragment-5000`,没有显式子命令时兼容旧的 summary 调法。

## `tools/compare_event_traces.py`

轨迹比较只建立**同一 actor 内**的事件序列,不从多个进程 wall timestamp 构造全局总序。

- `DEFAULT_STABLE_FIELDS` 是内置 profile 的参与比较字段白名单;`OBSERVATIONAL_EVENTS` 是 heartbeat/ingest/liveness/quorum 六个默认噪声事件集;`BUILTIN_PROFILES` 映射三个内置 profile。`_RANDOM_ID_SUFFIX` 只匹配 `..._<8位步数>[_f<3位片>]_<至少8位hex>` 的整个字符串。
- `TraceInputError` 是输入/profile 错误类型;`TraceProfile.fields_for()` 取事件专属字段,否则 default fields;`NormalizedEvent.as_dict()` 只输出 actor、event_type 与被 profile 选中的 fields(不保留 timestamp/source line);`TraceDivergence` 保存首个差异及上下文;`TraceComparison.equivalent` 等价于没有 divergence。
- `_string_list` 严格校验 custom profile 的唯一字符串 list。`load_profile` 支持内置 `default`、`learner-adoption`、`core-pipeline` 或 JSON 文件;当前后两个内置 profile 的实现完全相同,都使用 default stable fields 并忽略 heartbeat/liveness/metadata/quorum 六种观测事件。
- `_normalize_identifier` 把带随机 suffix 的 update ID 稳定化为 `<id>`;`_normalize_value` 把 list→tuple、dict→排序 tuple,标量保持可比,其他对象转 str。
- `_trace_files` 接受单 JSONL 或 run/logs/普通目录;`_actor_role` 把 `syncer` 和 `learner*` 归类,其他 actor 的 role 就是 actor 原字符串。`normalize_trace` 逐行验证 object/actor/event_type,按 roles/actors/profile 过滤并保留 actor 内文件/行序。
- `_first_difference` 找字段首差;`compare_traces` 对 actor 集和每 actor 序列做稳定比较并截取 context;`_event_dicts()` / `format_comparison()` 生成诊断。
- `build_parser()` 定义两个 root、profile、role/actor filter 和 context;`main`:等价退出 0,首差退出 1,输入/profile 无效退出 2。

## `tools/eval_lm_harness.py`

- `DEFAULT_SOURCE_RUN_ID` 与由它构造的 `DEFAULT_CHECKPOINT_RELATIVE` 是保留的历史常量,当前 resolver/parser 不使用它们作默认 checkpoint。`DEFAULT_CONFIG_RELATIVE=configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml` 仍在 run 没有 control resolved-config 时作 config fallback。
- `_GLOBAL_WEIGHT_RE` 只识别恰好六位数字的 `global_vNNNNNN.safetensors`;`_STDERR_SUFFIX="_stderr"` 用于把 lm-eval metric 与同名 stderr 配对。
- `_read_json()` / `_coerce_path()` / `_checkpoint_version()` / `_infer_run_root_from_checkpoint()` 处理 manifest/path/version;checkpoint 文件名只识别恰好六位的 `global_vNNNNNN.safetensors`。
- `_find_latest_run_root` 扫 `runs/fs_diloco/*/control/latest.json`,优先用可转 float 的 `created_at`,缺失/非法时回退 latest 文件 mtime,取排序键最大的 run。
- `resolve_checkpoint` 优先显式 checkpoint,其次 run root latest,最后自动 run;返回 run/config/param-index/checkpoint/version/token 溯源。显式 checkpoint 不在标准 weights 布局时需给 run root。自动 latest 分支只读 `weight_path`,不读 fragment latest 的 `materialized_weight_path`;fragment run 必须显式传物化 checkpoint 和 run root。
- `export_checkpoint` 按 resolved manifest 加载模型骨架和 param index、灌入 checkpoint、`tie_weights`、`save_pretrained(safe_serialization=True)` 及 tokenizer,并可原子写导出 manifest。
- `_metric_stderr_base()` / `_as_float()` / `_result_json_files()` 配对 lm-eval stderr、过滤数值、递归找结果;`results_to_csv` 每 task/metric 一行并附 run/version/token identity,无数值结果报错。
- `_print_json()` / `parse_args()` / `main()` 实现 `resolve-checkpoint/export-checkpoint/results-to-csv`。

## `tools/validation_eval.py`

- `causal_cross_entropy_sum(logits,input_ids)` 把 logits 转 FP32,对 shift 后 token 做 reduction=sum;predicted-token 数是 `batch*(seq-1)`,不含每块首 token。
- `finalize_validation_metrics` 要求 block/token 非零且 loss 有限,返回 token 归一化 loss 与 `exp(loss)` 困惑度。
- `validate_checkpoint_identity` 默认要求 resolved checkpoint 路径等于 latest `weight_path`,并计算 size/SHA;它不单独比较 version。`allow_non_latest` 只跳过 latest path 比较,文件仍必须存在;param-index 兼容性是 `run_validation` 后续的独立检查。
- `resolve_terminal_predecessor_checkpoint` 选择最高 source version 的 committed manifest,要求路径在 run root 内、文件存在、checksum 一致;`evaluated_global_version` 对 terminal capture 返回 source version。
- `attach_validation_to_summary` 先原子写独立 result,再原子替换带 validation 附加的 summary;两个文件各自原子,但不是跨文件事务。
- `_dataset_identity()` / `_protocol_hash()` 形成 dataset 与评估算法 identity;`_source_identity` 默认要求训练 config 和 evaluator 环境都有 git commit+fingerprint,`--allow-missing-source-identity` 才放宽。evaluator git-dirty 是原始环境字符串证据。
- `evaluate_blocks` batch 推理并累计 CE;`run_validation` 用历史 resolved-snapshot loader,拒绝 synthetic 数据,复用训练 tokenizer/EOS/non-overlap block 协议,可截最大 blocks,校验 param index 并直接加载模型参数。实现先把模型转到 `--dtype`,再构建本地 param index 做严格 dtype 比较,因此该覆盖必须与训练 index 中的参数 dtype 相同;它目前不是可独立改变的评估精度旋钮。默认写/附加 `metrics/validation_eval.json`;terminal predecessor 写独立版本文件且不覆盖主附加。
- `parse_args()` / `main()` 校验 batch/max-block/device/dtype/attachment/terminal 组合并打印结果。

## `tools/publish_quality_gate.py`

- `_T_CRITICAL_95` 内置 df=1..120 的分段 95% 双侧临界值;`_t_critical(df)` 取第一个 `df≤bound` 的值,df>120 回退 1.96。
- `roundtrip_trend(values)` 至少 3 点;对索引 1..N 做 OLS,返回 slope/CI/半段均值比。CI lower≤0 且 second-half/first-half≤1.25 才算 bounded;显著下降通过。
- `evaluate_publish_quality_gate(fp32_losses,bf16_losses,bf16_trends)` 要求两边完全相同 seed 集且至少 3 seed;`epsilon=max(0.01, FP32 sample stdev)`,paired mean degradation≤epsilon、每 seed degradation≤2epsilon、全部 trend bounded 才 PASS。证据不足为 `NEEDS_MORE_SEEDS`,充分但不满足为 `FAIL`。
- `_read_json()` 读取 validation/source object;`_read_metric_values()` 只从 CSV 的指定列提取非空 float。`_normalized_pair_config` 把 run id/root/name 和 publish dtype 中性化;`_run_evidence` 读取 source fingerprint、validation protocol/checkpoint SHA、seed/dtype/loss 与 roundtrip telemetry。
- `_parse_seed_roots()` / `evaluate_run_roots()` 解析并配对 `SEED=RUN_ROOT`,要求除 publish dtype 外 normalized config 一致,且 FP32 relative-L2 telemetry 全为零;当前 CLI 只有重复 `--fp32 SEED=ROOT` / `--bf16 SEED=ROOT` 的 run-root 输入,可选原子写 `--output`。checkpoint SHA 被保留为证据字段,但门禁不要求 FP32/BF16 checkpoint SHA 相等。

## `tools/run_metrics_csv.py`

- `CSV_COLUMNS` 是固定 75 列和精确输出顺序:

  `run_id, run_path, mode, final_version, stop_reason, all_learners_stopped, num_learners, produced_updates, applied_updates, update_utilization_ratio, update_utilization_percent, dropped_updates, pending_or_unclassified_updates, drop_reasons_json, dropped_superseded, dropped_stale, dropped_stop_finalized, dropped_missing_file, dropped_future_base, dropped_unknown, local_steps_total, local_steps_min, local_steps_max, local_steps_mean, local_steps_by_learner_json, complete_training_time_seconds, source_fingerprint, training_seed, sync_scan_interval_seconds, ingest_during_publish, merge_count, selected_per_merge_min, selected_per_merge_max, selected_per_merge_mean, selected_count_distribution_json, global_interval_seconds_mean, global_interval_seconds_p50, global_interval_seconds_p95, quorum_detection_seconds_mean, quorum_detection_seconds_p95, quorum_max_trigger_count, quorum_max_trigger_ratio, quorum_trigger_distribution_json, publish_ingest_passes_total, publish_ingested_updates_total, interval_residual_ratio_mean, syncer_merge_compute_seconds_p95, syncer_duty_cycle_percent, estimated_idle_gpu_node_hours, applied_staleness_0, applied_staleness_1, applied_staleness_2, applied_staleness_gt_2, applied_staleness_mean, applied_staleness_distribution_json, produced_tokens, applied_tokens, loss_count, loss_first_10_mean, loss_last_10_mean, loss_mean, loss_last_vs_first_ratio, model_name_or_path, update_tensor_dtype, syncer_device, syncer_compute_dtype, syncer_publish_dtype, max_staleness_versions, inner_steps, max_local_steps, completion_mode, global_adoption_strategy, grace_window_mode, grace_window_seconds, db_integrity_ok`.
- `_read_json()` / `_read_csv()` / `_read_jsonl()` / `_as_int()` / `_as_float()` / `_mean()` / `_percentile()` / `_json_cell()` 是容错读取/转换;损坏 JSON 返回空,JSONL 跳过坏行。
- `_read_db` 以只读 URI 打开 live DB 并查存在表;`_update_records` 先读 archive 再用同 ID live row 覆盖;`_manifest_records` 以 update manifest 构造 produced 集。
- `_committed_merge_versions` 从 syncer metrics 取 committed merge;`_selection_fallback` 只把能对应 committed metric 的 `updates_selected` 事件当旧 run applied 证据。
- `_loss_metrics()` / `_local_steps()` / `_staleness_for_update()` / `_nested()` 计算 loss、每 learner 最大本地步、优先 DB/manifest/log fallback 的陈旧度及嵌套配置。
- `extract_run_metrics(run_path)` 输出一行 75 列实验矩阵。`completed` 当前按 summary 是否存在,而目录发现按 stop marker;无 summary 的未知 produced 计 `pending_or_unclassified`。drop reason JSON 保留原字面值。当前 `dropped_future_base` 兼容计数只识别历史 `future_base_version/future_fragment_version`,因此新 runtime 的 `future_base` 仍会出现在 `drop_reasons_json`,但不会进入该专列;使用该列时要注意这一实现限制。
- `find_finished_run_roots` 递归找 `control/stop.json`,遇到任意 `control` 目录后不再向该 run 内递归。
- `_row_identity_tokens()` / `_new_unique_records()` 以 run_id 或规范化 run_path 任一相交去重。`write_metrics_csv` 追加时要求既有表头精确一致并依赖 append+fsync(非原子);新建/overwrite 用同目录 temp+fsync+replace(不 fsync parent)。
- `parse_args()` / `main()` 默认追加 `reports/run_metrics.csv`,`--overwrite` 重建。

## `cli.py` 与顶层 shim

`fs_diloco.cli.main(argv)` 只识别 `syncer/learner/inspect`,延迟 import 并透传剩余参数。`fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py` 是 re-export + `__main__` 兼容层;真实实现分别在 `runtime/` 或 `tools/`。console scripts 以 `pyproject.toml` 为准。
