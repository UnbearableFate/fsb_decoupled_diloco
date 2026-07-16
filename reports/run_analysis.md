# Run 分析报告

本文件集中保存 run 分析方法和实验结果。运行、提交与故障排查说明仍见 [docs/07-operations.md](../docs/07-operations.md)。

## 分析方法

```bash
# 人类可读摘要(latest/stop/指标行数/DB 计数/心跳/loss 概览)
python -m fs_diloco.analysis summary runs/fs_diloco/<RUN_ID>
python -m fs_diloco.analysis summary runs/fs_diloco/<RUN_ID> --json   # 完整 JSON
# 兼容:python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>

# fragment run 的自动断言(CI/验收用;失败列出所有不满足项并非零退出)
python -m fs_diloco.analysis assert-fragment-smoke \
  --run-root <run_root> --expected-learners 8 \
  --expected-global-merge-events 50 --expected-fragment-ids 0,1,2,3 \
  --min-selected-count 4
python -m fs_diloco.analysis assert-fragment-5000 ... --expected-local-steps 5000
```

分析读取共享目录的 `latest.json/stop.json/heartbeats/metrics/*.csv`、持久 DB 与 `metrics/*_history.jsonl`(可用 `--db` 指定 DB),不需要 torch/GPU。live/archive 行会按 identity 去重;摘要包含 `PRAGMA integrity_check` 结果。

`control/summary.json` 和分析摘要中的 `complete_training_time_seconds` 从 syncer 启动计至 learner 全部 stopped;`learner_resources` 给出每个 learner 的全训练 CPU/GPU 峰值及跨 learner 聚合。若 W&B 未认证,用 `WANDB_MODE=offline qsub ...` 保留完整 run,认证后执行 `wandb sync <run_root>/logs/wandb/offline-run-*`。

也可以直接查询活跃 DB。注意 applied/dropped 与旧版本通常已经归档,所以完整历史应通过分析器查看:

```bash
sqlite3 runs/fs_diloco/<RUN_ID>/control/syncer_metadata.sqlite3 \
  "SELECT status, COUNT(*) FROM updates GROUP BY status"
```

## 2026-07-16 分析结果

### BF16 upload + fragment 直接抽取的 50x10 复测

变更后两个 9 节点作业均为 8 learners、10 次 merge、每次 selected count=8,正常以 `stop_after_outer_steps` 结束,无 error/no-progress/未捕获异常。fragment 作业还通过了脚本内置的 `assert-fragment-smoke`。

| 模式 | run | 单份 update payload | learner 平均写 update | syncer 平均读 update | 完整训练时间 | loss(first-10 → last-10) |
|---|---|---:|---:|---:|---:|---:|
| fragment / FP32 基线 | `20260716_160259_fs_diloco_gpt2_wikitext2_8l_fragment_50x10` | 126.394 MB(四片加权平均) | 134.79 ms | 254.10 ms | 201.63 s | 3.9144 → 3.3156 |
| fragment / BF16 + 直接抽取 | `codex_bf16_fragment_50x10_20260716_1724` | 63.197 MB(-50.0%) | 63.88 ms(-52.6%) | 181.71 ms(-28.5%) | 198.90 s(-1.4%) | 3.9184 → 3.3237 |
| full / FP32 基线 | `20260716_160753_fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10` | 497.759 MB | 206.81 ms | 624.27 ms | 153.62 s | 3.8981 → 3.3621 |
| full / BF16 | `codex_bf16_full_50x10_20260716_1727` | 248.880 MB(-50.0%) | 140.18 ms(-32.2%) | 356.79 ms(-42.9%) | 150.44 s(-2.1%) | 3.8849 → 3.3716 |

结论:BF16 确实把共享文件系统上的 learner update payload 减半,并明显降低 learner 写入与 syncer 读取耗时。端到端时间只改善约 1–2%,说明当前 50x10 的主耗时仍是本地训练、quorum/轮询和 syncer 发布 FP32 全局权重;fragment 行的写入改善是“直接抽取 + BF16”的合并效果,不能仅凭这组对照拆分两项各自贡献。两种新 run 的 loss 都持续下降且分析器未判定发散。

### 持久 SQLite + current-only full 50x10 验收

修订后的 full reference 在 Miyabi-G 九节点作业 `2398380.opbs` 上完成 8 learners × 50 local steps/update × 10 global merges。run `codex_plan01_full50x10_20260716_220849` 以 `stop_after_outer_steps` 正常结束,DB/latest/stop/summary 均为 v10,十次 merge 每次 selected=8,SQLite `integrity_check=ok`,`journal_mode=delete`,`synchronous=FULL`。

结束目录只有 `global_v000010.safetensors`、`outer_v000010.safetensors`、八个固定 proposal pointers 和持久 DB;活跃 update、终态 proposal payload/meta、旧 checkpoint、dump 均为零。完整训练时间 250.496 秒,SQLite commit p95 为 0.006192 秒;十轮 SQLite commit 与 state maintenance 合计占完整时间 0.3612%,低于 5% 门槛。独立 plan-01 invariant checker 返回 `PASS`。原始证据位于 `reports/imp_plans/01/artifacts/20260716-220849_full50x10_pass.log` 与同目录的 structured evidence JSON。

### 持久 full 5000-step 阶段观测

九节点作业 `2398400.opbs`、run `codex_plan01_full5000_20260716_221526` 在保持运行的情况下完成了要求的 v5 阶段门禁:从 v0 连续提交五次 selected=8 的 merge,DB/latest 与 v5 weight/outer 一致,只保留 v5 checkpoint,v0–v4 已归档且旧 checkpoint/终态 proposal tensor 为零;八个 learner 均持续采用新版本。阶段 Checker 为 `PASS_WITH_FOLLOWUPS`,唯一 follow-up 是仍在运行的 50-outer 作业的完整 terminal 结果。按实施计划,到达该门禁后不要取消作业;后续用同一 job/run ID 继续验收。

## 2026-07-17 分析结果

### FP32 upload + fresh-only 5000-step 终态

九节点作业 `2398400.opbs`、run `codex_plan01_full5000_20260716_221526` 最终以 PBS `Exit_status=0` 完成,walltime 为 21 分 08 秒。配置为 8 learners、每 learner 5000 local steps、`inner_steps=100`、`max_staleness_versions=0`、FP32 update payload 和目标 50 outer steps。八个 learner 均到达 local step 5000,但合法输入在 v25 耗尽,因此以 `input_exhausted` 正常结束,而不是达到 v50。

400 份 update 中有 190 份 applied、210 份 dropped;丢弃原因为 205 份 `superseded` 和 5 份 `too_stale`。前 22 次 merge 均 selected=8,尾部依次 selected=7、5、2。每份 payload 为 497.759 MB,全部 proposal 写入量约 199.1 GB,其中约 104.5 GB 对应未被应用的 update。八个 learner 实际执行 655.36M local tokens,而 `total_seen_tokens=311.296M` 只统计 applied update 携带的 token。

完整训练时间为 1257.338 秒;learner update 平均写入 0.21094 秒,p95 为 0.45642 秒;syncer 平均读取 0.56874 秒。SQLite commit p95 为 0.01244 秒,commit 与 maintenance 合计占完整训练时间 0.273%。loss first-10/last-10 均值为 3.80964 → 3.11796,未出现明显发散;该 loss 是 learner 本地训练 loss,不是最终 global checkpoint 的 validation loss。

终态 DB/latest/stop/summary 均为 v25,SQLite `integrity_check=ok`、`journal_mode=delete`、`synchronous=FULL`;目录仅保留 v25 weight/outer、八个固定 proposal pointers 与持久 DB,active update、proposal payload/meta、临时文件、WAL 和 dump 均为零。独立 Checker 按实际 v25 返回 `PASS`,按配置目标 v50 返回 `BLOCKED`。这说明 terminal drain 和 current-only 状态正确,但“固定 5000 local steps + fresh-only + 无 upload 后等待”不能保证 50 次 outer merge:learner 往往在新 global 发布前开始下一 cycle,使下一份 update 基于旧版本并在严格 fresh-only 准入下失效。

原始 PBS 输出位于 `reports/imp_plans/01/artifacts/20260716-221526_full5000_staged_result.log`,run root 为 `runs/fs_diloco/codex_plan01_full5000_20260716_221526/`。

### BF16 upload + staleness=2 5000-step 终态

九节点作业 `2398817.opbs`、run `codex_plan01_full5000_bf16_s2_20260716_233341` 只将上述正式配置的 update payload 改为 BF16,并将 `max_staleness_versions` 放宽为 2。PBS `Exit_status=0`,无异常节点,walltime 为 21 分 00 秒。八个 learner 均到达 local step 5000;运行在 v48 以 `input_exhausted` 正常结束。

400 份 update 中有 372 份 applied、28 份 dropped,利用率从 47.5% 提高到 93.0%;28 份均因 `superseded` 丢弃,没有 `too_stale`。applied update 中 27 份 staleness=0、345 份 staleness=1,没有实际使用 staleness=2。前 45 次 merge 均 selected=8,尾部依次 selected=5、4、3,即 `45×8+5+4+3=372`。最后一个 learner 停止后约 22.3 秒完成 terminal grace、三 learner partial merge和 stop 发布。

BF16 manifest payload 为 248.880 MB,较 FP32 精确减半;400 份 proposal 总流量约 99.6 GB,其中 dropped update 约 7.0 GB。learner update 平均写入为 0.18004 秒,p95 为 0.27075 秒;相对上一轮 FP32 fresh-only 分别改善 14.7% 和 40.7%。syncer 平均读取为 0.34091 秒,改善 40.1%。完整训练时间为 1250.194 秒,只改善 0.6%,说明端到端仍由本地训练、版本等待和 FP32 global/outer publication 主导。

48 次 merge 的 SQLite commit p95 为 0.03078 秒;commit 与 maintenance 合计 5.630 秒,占完整训练时间 0.450%。loss first-10/last-10 均值为 3.80223 → 3.18917,未出现明显发散。终态 DB/latest/stop/summary 均为 v48,SQLite integrity 和安全 PRAGMA 正常;目录只保留 v48 weight/outer、八个固定 pointers 与 DB,终态 proposal tensor/meta、临时文件、WAL 和 dump 均为零。独立 Checker 按实际 v48 返回 `PASS`,按目标 v50 返回 `BLOCKED`。

原始 PBS 输出位于 `reports/imp_plans/01/artifacts/20260716_233341_full5000_bf16_s2_result.log`,run root 为 `runs/fs_diloco/codex_plan01_full5000_bf16_s2_20260716_233341/`。

### 两次正式运行对比

| 指标 | FP32 / fresh-only | BF16 / staleness=2 | 解释 |
|---|---:|---:|---|
| final version | 25 | 48 | 放宽 staleness 后在相同 local work 内 outer goodput 接近翻倍 |
| applied / produced | 190 / 400 | 372 / 400 | update 利用率 47.5% → 93.0% |
| dropped | 210 | 28 | 无效 proposal 与共享 FS 流量显著下降 |
| applied tokens | 311.296M | 609.485M | 两者实际 local compute 都是 655.36M tokens |
| payload / update | 497.759 MB | 248.880 MB | BF16 减少 50.0% |
| proposal 总写入 | 199.1 GB | 99.6 GB | 不含 global/outer publication |
| update write mean / p95 | 0.211 / 0.456 s | 0.180 / 0.271 s | 平均改善 14.7%,尾延迟改善 40.7% |
| syncer read mean | 0.569 s | 0.341 s | 改善 40.1% |
| complete time | 1257.34 s | 1250.19 s | 端到端只改善 0.6% |
| loss first-10 → last-10 | 3.8096 → 3.1180 | 3.8022 → 3.1892 | 不是只改变 upload dtype 的受控质量对照 |
| terminal result | v25 `input_exhausted` | v48 `input_exhausted` | 两者均正确收尾,但均未达到 v50 |

较高的 BF16/staleness=2 last-10 loss 不能归因于 BF16。模型训练精度在两次运行中本来都是 BF16,本轮改变的是 upload payload 精度;同时 staleness、global adoption 频率、inner optimizer reset 次数和 proposal 选择序列都发生了变化。作为旁证,旧 FP32/staleness=2 run `20260716_175316_fs_diloco_gpt2_wikitext2_8l_5000steps` 的 last-10 loss 为 3.18385,本轮 BF16/staleness=2 为 3.18917,差异仅 0.00532(约 0.17%),小于单次异步运行足以产生的调度和批次波动。并且本轮 applied update 为 372,旧 run 为 362,说明 final version 受尾部 batching 影响,不能单独代表有效工作量。

### 仍需进一步研究

- 在当前同一代码版本上做只改变 `io.tensor_dtype` 的 FP32/BF16 对照,固定 staleness、seed、applied tokens 或 global version,并重复多个 seed;
- 对最终 global checkpoint 运行固定 validation loss/perplexity,不要用 learner local training loss 推断最终模型质量;
- 明确正式实验究竟以“每 learner 5000 local steps”还是“达到 50 outer merges”为完成条件;固定 local steps 时,outer version 会受 supersession、quorum batching 和 learner 尾部速度差异影响;
- 若必须同时保证 fresh-only 与 v50,研究 upload 后版本确认/等待或显式 round barrier;若保持异步 staleness,应系统扫描 staleness window、lambda 与质量/吞吐关系;
- telemetry 应同时报告 actual local tokens、applied tokens、produced/applied/dropped updates 和 merge count,避免把 `total_seen_tokens` 误读为总训练计算量;
- BF16 已显著降低 proposal I/O,但端到端改善很小;仍需分解 local training、quorum wait、global publication 和第九张 syncer GPU 低利用率的成本。
