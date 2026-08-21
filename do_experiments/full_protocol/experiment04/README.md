# Experiment 04 手动重跑指南

本目录用于在 Miyabi 上手动提交 plan04 的 Baseline 和 Full Protocol 场景。用户只需选择场景并运行统一提交脚本；Learner 分批启动、故障注入、Syncer 接管、终态等待和验收均由 Supervisor 自动完成。

实验统一使用固定版本的 GPT-2 和 WikiText-2，训练规模为 `200 local steps × 25 global steps`，merge 阈值为 4。Supervisor 和 actor 默认提交到 `debug-g`，wall-time 为 `00:30:00`。

## 前置条件

1. 登录 Miyabi 的 `miyabi-g*` login host。不要在 PBS allocation 内运行提交命令。
2. 进入仓库根目录，确认 `.venv/bin/python` 可执行。
3. 提交正式证据前，提交或清理 formal source scopes 内的改动。本 README 也属于 `do_experiments` scope，未提交时会触发拒绝。
4. 检查当前队列和项目限制：

   ```bash
   qstat --rscuse
   qstat --limit
   qstat
   ```

   一个 Full Protocol 场景初始会使用 1 个 Supervisor、1 个 Syncer 和 8 个 Learner job。故障恢复或双 Syncer 场景还会产生一个额外 actor job。可以并行提交不同场景，但必须先确认队列资源和 concurrent-job limit 容得下全部 job。

`submit.sh` 会在 `qsub` 前再次检查 host、PBS 上下文、formal source scopes、全部 agent PBS 脚本的 shell 语法，以及 `#PBS -W group_list=xg24i002` 是否为有效字面值。请始终通过该脚本提交，不要绕过这些检查直接运行 PBS 脚本。

## 选择场景

| 参数 | 实验行为 |
|---|---|
| `baseline` | 同时提交 8 节点 DDP 和 periodic-average 两个 5,000-step Baseline |
| `normal` | 同一批提交 8 个 Learner，不注入故障 |
| `stagger_4_4` | 按 `4 + 4` 分两批提交 Learner，批次间隔 30 秒 |
| `stagger_3_3_2` | 按 `3 + 3 + 2` 分三批提交 Learner，批次间隔 30 秒 |
| `learner_failure_simultaneous` | 同一批提交 8 个 Learner，约 60 秒后终止一个已 admission 的 Learner |
| `learner_failure_staggered` | 按 `4 + 4` 提交 Learner，约 60 秒后终止一个已 admission 的 Learner |
| `syncer_failure` | 约 60 秒后终止主 Syncer，再等待约 30 秒提交后继 Syncer |
| `dual_syncer` | 约 30 秒后提交候选 Syncer，观察双 Syncer 约 30 秒后终止原主 Syncer |

故障场景中的 `qdel` 由 Supervisor 在预定时间执行。不要人工终止 Learner 或 Syncer，否则实际时序与场景定义不一致。

## 提交 Baseline

Baseline 不应随每个 Full Protocol 场景重跑。只有在没有可比的已完成 Baseline，或者模型、数据、优化器、workload、运行语义或必要环境发生了影响 Baseline 的变化时，才提交：

```bash
bash do_experiments/full_protocol/experiment04/submit.sh baseline
```

该命令会输出 `DDP_JOB`、`DDP_RUN_ID`、`PERIODIC_JOB` 和 `PERIODIC_RUN_ID`。两个 job 完成后，结果位于：

```text
runs/torch_ddp_baselines/<RUN_ID>/
logs/torch_ddp_baselines/<RUN_ID>/
```

`normal` 的当前比较实现会从 `runs/summary.csv` 中选择与当前 run 具有相同 `git_commit` 和 `source_fingerprint` 的 Baseline，并要求 `ddp` 和 `periodic_average` 各恰好一条。已满足该条件时不要重复提交 Baseline；否则候选结果不唯一，`normal` 的结果汇总会失败。如需提交新 Baseline，应等待两种模式都完成并写入 `runs/summary.csv` 后再提交 `normal`。

## 提交 Full Protocol 场景

在仓库根目录执行：

```bash
bash do_experiments/full_protocol/experiment04/submit.sh SCENARIO
```

例如，重跑 `learner_failure_staggered`：

```bash
bash do_experiments/full_protocol/experiment04/submit.sh learner_failure_staggered
```

提交成功后，脚本会输出以下字段：

```text
supervisor_job_id=<PBS_JOB_ID>
source_commit=<GIT_COMMIT>
experiment_id=<EXPERIMENT_ID>
scenario=<SCENARIO>
run_id=<RUN_ID>
run_root=<ABSOLUTE_RUN_ROOT>
log_root=<ABSOLUTE_LOG_ROOT>
evidence_output=<ABSOLUTE_EVIDENCE_JSON>
supervisor_log=<ABSOLUTE_SUPERVISOR_LOG>
```

保留这段输出。后续查询、排错和验收应使用其中的精确 job ID 和路径，不要依赖“最新目录”或模糊匹配。每次提交都会生成新的时间戳 `RUN_ID`，不会覆盖旧 run。

## 监控运行

查看 Supervisor 的当前状态和日志：

```bash
qstat -f <supervisor_job_id>
tail -f <supervisor_log>
```

Supervisor 启动 actor 后，完整的 Syncer 和 Learner 提交回执位于：

```text
<log_root>/submission_receipt.json
<log_root>/scenario_state.json
```

PBS 不再把 job 列为当前 job 后，查看历史退出状态：

```bash
qstat -H -f <supervisor_job_id>
```

## 验收结果

以提交时输出的 `evidence_output` JSON 为该次场景的最终结果。可使用仓库虚拟环境查看：

```bash
.venv/bin/python -m json.tool <evidence_output>
```

Full Protocol 场景只有三个 PASS 条件：

1. 最终获得 authority 的 Syncer 在 PBS 历史中以 `Exit_status=0` 正常退出；
2. 终态 `global_version` 等于 25；
3. 最终平均 loss 为有限数，且小于 3.5。

通过时，JSON 中应满足：

```text
status = "PASS"
acceptance.criteria.final_syncer_normal_exit = true
acceptance.criteria.global_version_25 = true
acceptance.criteria.final_mean_loss_below_3_5 = true
errors = []
```

`diagnostics` 保留时序、authority 和 Baseline 比较等调查信息，不改变上述 PASS/FAIL 公式。其他主要证据位于：

```text
<run_root>/control/summary.json
<run_root>/control/syncer_metadata.sqlite3
<log_root>/submission_receipt.json
<log_root>/scenario_state.json
runs/summary.csv
```

## 失败后重跑

1. 保留失败 run 的 `run_root`、`log_root`、Supervisor 日志和 evidence JSON。不要删除或覆盖旧证据。
2. 先根据 evidence JSON 的 `errors`、`diagnostics` 和 `cleanup`，再结合 `scenario_state.json` 与 actor 日志确认失败原因。
3. 只重跑被变更影响的场景。文档、Checker、queue、wall-time 或其他不改变 workload 和运行语义的变更，不会自动使旧训练结果失效。训练、协议、模型、数据、优化器、状态转换、持久化语义、运行时配置或 workload 的变化，才会使对应实验失效。
4. 确认队列中没有上一次 run 遗留的 owned job，然后使用同一条场景提交命令。脚本会创建全新的 run、log 和 evidence 路径。

Supervisor 在自身捕获到异常时会尝试终止已记录的 owned job。如果 Supervisor 被人工 `qdel`、超时或受到不可捕获的终止，该清理不一定执行。此时必须先从 `submission_receipt.json` 和 `scenario_state.json` 解析该 run 的精确 job ID，只处理该 run 拥有的 job，并通过 `qstat` 确认已无遗留。不要使用模糊匹配或批量终止其他 run。

