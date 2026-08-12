# plan04 执行包

## 身份与边界

- Plan：`plans/DOING/plans/plan04.md`
- Branch：`new_plan04`
- Branch point：`f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`
- Workflow pin：`f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`
- Host：Miyabi login/control plane `miyabi-g3`；项目 runtime 与测试仅在 PBS compute node 运行。
- Formal source scopes：以 `fs_diloco.core.source_identity.SOURCE_SCOPES` 为唯一来源，包含 `fs_diloco`、`configs`、`do_experiments`、`scripts/miyabi`、`tests`、`tools`、`torch_ddp_baselines`、`pyproject.toml`、`README.md`、`docs`、`plans/00-RESEARCH_PLAN.md`、`website/app` 和 `website/scripts`。

## 当前状态

- 当前产品已有 8 个 scalar learner 加独立 syncer 的 PBS actor 入口、scheduler-backed replacement、leader lease/takeover、terminal authority checker 和统一 run summary 工具。
- `runs/full_protocol/` 在 INIT 时为空；`runs/summary.csv` 只有两个 8-rank、2,000-step torch baseline 行。Plan 所称已完成 Full Protocol baseline 的原始 run 当前不存在，因此不能把它当作可验证证据；正常场景将作为当前 Full Protocol reference，并与 CSV 中 workload 相同的 periodic-average baseline 比较。
- `tests/harness/test_plan04_experiment.py` 仍描述已删除的旧 200×10 三场景实验包和旧路径，属于 obsolete harness，必须按当前七场景 100×10 设计重写。
- 保留当前 protocol、actor PBS 入口和 summary schema；新增一个独立的 `do_experiments/full_protocol/experiment04/` 启动包。删除旧 50×10 命名和只服务旧 plan04 harness 的配置/测试引用。
- 不修改或清理 `logs/` 中历史诊断数据；它们不进入本 plan 的正式证据。

## 验证阶梯与资源预算

1. Login node：CodeGraph/源码盘点、shell 语法、PBS literal group、配置和 source-scope 静态检查。
2. 单节点 `interact-g`：focused harness/config/summary tests，随后相关或完整测试集；复用同一 allocation。
3. PREFORMAL：创建 clean candidate commit，按 `plans/review_prompts/review_prompt.md` 完成 current-state 审查并冻结唯一 `FINAL_COMMON_TARGET`。
4. FORMAL：七个场景各自通过一行 shell 入口提交一个 30 分钟 regular-g supervisor；supervisor 再提交 8 个 scalar learner、1 个 syncer，双-syncer场景提交第二个 syncer。Actor 同样显式使用 `regular-g` 和 `00:30:00`。
5. 每个场景以 terminal SQLite authority、immutable actor attestations、scheduler history 和 create-only artifact 为 oracle；正常场景另由 `tools/summarize_runs.py` 追加 CSV，并对 workload 相同 baseline 的 final mean loss 与 training time 执行 20% 检查。

## 高风险边界

- Learner fault 只删除 authority 已 admitted 的 bootstrap learner job；replacement 必须由当前 scheduler-authorized launch request 提交并以同一 stream 的更高 epoch 获得 admission。
- Syncer fault/conflict 以 durable `syncer_epochs`、lease owner/epoch 和 terminal authority为准，不能仅依据进程退出或日志文字。
- Supervisor 只拥有本次 receipt 中的精确 job ID；失败清理仅允许作用于该集合。
- 所有正式 gate 必须绑定同一 clean commit、source scopes 和 fingerprint；source scope 变更后重新 PREFORMAL 并重跑全部正式 gate。

