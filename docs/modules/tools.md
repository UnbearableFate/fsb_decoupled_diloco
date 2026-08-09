# 模块参考：tools

- `init_run.py`：strict v4 config/source/descriptor 初始化；唯一 current bootstrap path。
- `launch_independent_run.py`：先验证 walltime，再初始化和独立提交 candidate/learners；保留每个 scheduler receipt。
- `migrate_config_v3_to_v4.py`：dry-run、no-clobber output、SHA-fenced in-place config migration；不迁旧 run state。
- `analysis.py`：current/legacy summary；SQLite 通过 query-only adapter 打开。
- `eval_lm_harness.py`：checkpoint resolve/export/results flatten；config 使用 current-or-explicit-legacy query projection。
- `validation_eval.py`：dataset/model/index/checkpoint identity validation 与 loss/perplexity output。
- `publish_quality_gate.py`：多 seed FP32/BF16 配对质量 gate。
- `authorize_static_replacement.py`：create-no-replace static authorization；collision 要求 fresh attempt ID。
- `resolve_scheduler_uncertainty.py`：expected-state CAS operator request。
- `request_terminal_close.py`：manual policy 的 descriptor-bound immutable close request。
- `clean_run.py`：completion-evidence/artifact-policy/inode/symlink/live-reference gated cleanup，默认 dry-run。
- `paired_performance.py` / `check_workload_equivalence.py`：signed paired non-inferiority 与 workload identity。
- `compare_event_traces.py` / `run_metrics_csv.py`：离线 evidence/compatibility analysis。

旧 run 工具通过 `legacy.load_query_config_snapshot` 和 `LegacyRunReader` 显式 opt in。strict production loader 不为工具兼容而接受 old runtime key。
