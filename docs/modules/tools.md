# 模块参考:fs_diloco/tools 与 cli.py

离线工具:run 检查/断言、LM Evaluation Harness 对接,以及命令分发器。

---

## tools/analysis.py — run 摘要与断言

- **`syncer_resource_cost(rows, complete_training_time_seconds)`** — 以 `read+aggregation+outer_step` 为 merge compute、publish 为独立 active 分量，计算 p50/p95、总 active、duty cycle、reserved syncer node-hours 与估算 idle GPU node-hours；旧 run 缺字段或终态墙钟时显式返回 unavailable。

设计约束:只依赖标准库 + `protocol` 少量纯函数,**不需要 torch/GPU**,可在登录节点直接分析共享目录、持久 DB 与 JSONL archive。

### 读取助手(私有)

- **`_read_csv_rows(path)`** / **`_read_csv_summary(path)`** — CSV 全量读取 / `{exists, rows, last}` 概览。
- **`_read_jsonl_deduplicated(path, key)`** — 读取 crash-retry 可能重复的 archive,按稳定 identity 去重。
- **`_table_exists(conn, table)`** — 兼容不同 schema 版本的持久 DB。
- **`_sha256_file(path)`** — 完整性校验用。
- **`_fragment_update_integrity(rows)`** — 对 applied fragment 更新逐个检查文件存在与 sha256(有记录时),返回 `{missing, corrupt, ok}`。
- **`_db_summary(path, root) -> dict`** — 打开持久 DB,执行 `integrity_check`,合并 live update/version 行与 `metrics/*_history.jsonl`,按 identity 去重后生成状态计数、贡献者、full mid-cycle adoption 次数/切换 step、fragment 事件/staleness/完整性等摘要。
- **`_read_heartbeats(root)`** — 全部心跳 JSON。
- **`_distribution(values)`** / **`_numeric_summary(values)`** — 计数分布 / min/max/mean(过滤非有限值)。
- **`_loss_summary(rows)`** — learner loss 概览:整体统计 + 前 10/后 10 均值之比 + **`obvious_divergence`** 判定(末段均值 > max(3×首段, 首段+1))。
- **`_learner_fragment_adoption(heartbeats, learner_metric_rows)`** — 每 learner 是否发生过 fragment 采纳(metrics 计数、心跳字段、版本推断三路证据)。
- **`_syncer_log_flags(root)`** — 扫 syncer.jsonl 文本找 error / no_progress_timeout / traceback 标记。

### 公开接口

- **`summarize_run(shared_root, db_path=None) -> dict`** — 汇总一次 run 的全景:latest/stop、merge/fragment 状态、loss/心跳/CSV、持久 DB integrity、live+archive 历史和日志标记。默认 DB 是 `control/syncer_metadata.sqlite3`。
- **`assert_fragment_run(args, *, require_local_steps)`** — fragment run 验收断言(冒烟/正式两档共用):latest_kind、事件数达标、片 id 齐全、**各片版本等于 round-robin 期望值**(`expected_fragment_versions_after_events`)、stop_reason 正确、materialized checkpoint 存在、指标行数与每事件 selected 数达标、心跳数量(可选:各 learner 本地步数)、DB 存在与 applied 更新完整性、每 learner 都有更新且发生过采纳、loss 无明显发散、syncer 日志无失败标记。任何一条不满足都收集后统一以 `SystemExit` 报出。
- **`main(argv)`** — 子命令:`summary <shared_root> [--db] [--json]`(缺省子命令也走 summary,兼容 `python -m fs_diloco.analysis <root>` 旧用法)、`assert-fragment-smoke`、`assert-fragment-5000`(后者额外要求 `--expected-local-steps` 达标)。

## tools/eval_lm_harness.py — LM Evaluation Harness 对接

三步工作流:解析 checkpoint → 导出 HF 模型目录 → 把 lm-eval 结果拍平为 CSV。torch/transformers 依赖延迟到函数内 import。

- **`resolve_checkpoint(*, project_root, checkpoint=None, run_root=None, config=None) -> manifest dict`** — 决定评测哪个 checkpoint:
  - 显式 `--checkpoint`(不在 `weights/` 下时必须给 `--run-root`);或显式 `--run-root` 取其 latest;或全自动:扫描 `runs/fs_diloco/*/control/latest.json` 按 created_at 取最新可用 run(`_find_latest_run_root`);
  - 返回 manifest:解析模式、run 根/ID、checkpoint 路径与版本号、total_seen_tokens(仅当 checkpoint 就是 latest 版本)、param_index/config 路径(config 优先用 run 内快照 `run_config.resolved.yaml`)。
- **`export_checkpoint(*, project_root, export_dir, eval_id, checkpoint, run_root, config, manifest_output) -> manifest`** — 按 manifest 加载模型骨架 → 校验 param index → 加载扁平权重灌回模型(`strict_shape=True`)→ `tie_weights()` → `save_pretrained`(safetensors)+ tokenizer;manifest(含 eval_id/导出时间)可原子写盘,作为评测溯源记录。
- **`results_to_csv(*, lm_eval_output, output_csv, eval_id, manifest) -> rows`** — 递归找 `results_*.json`,把每个 task 的数值指标拍平为一行(自动配对 `*_stderr`),附 manifest 中的 run/version/token 溯源列;无数值指标时报错。
- 私有助手:`_metric_stderr_base`(stderr 指标名配对,处理 `acc_stderr,none` 形态)、`_as_float`、`_result_json_files`、`_checkpoint_version`(从 `global_v{N}.safetensors` 文件名提取版本)、`_infer_run_root_from_checkpoint`、`_coerce_path`、`_read_json`、`_print_json`。
- **`parse_args` / `main`** — 子命令 `resolve-checkpoint` / `export-checkpoint` / `results-to-csv`,均打印 JSON 结果。

## tools/validation_eval.py — 主 validation loss/ppl

- 使用 run 的 resolved config 加载 `validation_split`，复用训练的 tokenizer、逐文本 EOS
  与 non-overlap block 管线；causal shift 后以 predicted-token 总数归一化 cross entropy，
  输出 loss/ppl、block/token 数和完整 protocol hash。
- 默认只允许 `latest.json` 指向的 checkpoint；结果校验 checkpoint SHA/size、param index、
  training/evaluator source identity，并原子写 `metrics/validation_eval.json` 和 summary attachment。
- `--terminal-predecessor` 只读取 `eval_checkpoints/*.manifest.json` 的最高版本，复核 checksum
  后写独立的 `validation_terminal_predecessor_v*.json`，不覆盖终态 attachment。结果顶层
  `global_version` 是 capture manifest 的 `source_global_version`，不是 run 的 terminal latest。
- `run_1node_validation_eval.pbs` 在退出前再次断言 success、非空 block/token、有限 loss/ppl
  与 checkpoint SHA；`submit_train_with_validation.sh` 用 PBS `afterok` 将它与训练解耦。

## tools/publish_quality_gate.py — publish dtype 质量门禁

- `evaluate_publish_quality_gate` 以至少三组 paired FP32/BF16 validation loss 计算
  `epsilon=max(.01, sample_sd(FP32))`，检查 paired mean 与 worst-seed 上限。
- `roundtrip_trend` 对每个 BF16 run 的 relative-L2/version 做 OLS 斜率及 95% CI，并要求
  CI 不得全正且后半/前半均值比 ≤1.25；显著下降属于通过而不是失败。
- run-root CLI 还 fail closed 校验 seed 配对、source fingerprint、validation protocol、
  normalized config 仅 `syncer.publish_dtype` 不同，以及 FP32/BF16 telemetry 口径。

## cli.py — 命令分发器

- **`main(argv)`** — `python -m fs_diloco.cli {syncer|learner|inspect} <其余参数原样透传>`;延迟 import 对应模块的 main。

## 顶层兼容入口

`fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py` 均为 shim:re-export 对应 `runtime/`、`tools/` 模块的公开函数并提供 `__main__`,保证 `python -m fs_diloco.learner` 等历史命令行不变。
