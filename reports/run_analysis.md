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

### BF16 upload + staleness=2 + post-publish delta rebase 5000-step 终态

九节点作业 `2399377.opbs`、run `codex_rebase_full5000_20260717_013647` 在上述 BF16/staleness=2 配置上启用 `poll_latest_during_inner_steps=true` 和 `global_adoption_strategy=rebase_post_publish_delta`;除 run ID/shared root 外,这是与上一轮 replace 对照的全部配置差异。八个 learner 均到达 local step 5000,运行在 v47 以 `input_exhausted` 正常结束,仍未达到目标 v50。

400 份 update 中有 365 份 applied、35 份 dropped,利用率为 91.25%;35 份全部因 `superseded` 丢弃,没有 `too_stale`。applied update 中 359 份 staleness=0、6 份 staleness=1,即 fresh 比例从 replace 对照的 7.3% 提高到 98.4%。前 44 次 merge 均 selected=8,尾部依次 selected=6、4、3,即 `44×8+6+4+3=365`;最后一个 learner 停止后约 22.9 秒完成 terminal grace、partial merge 和 stop 发布。

每份 BF16 payload 为 248.880 MB,400 份 proposal 总写入量约 99.6 GB,与 replace 对照相同;dropped update 对应约 8.71 GB,高于对照的 6.97 GB。learner update 平均写入为 0.20359 秒,p95 为 0.31183 秒,较对照分别慢 13.1% 和 15.2%;syncer 平均读取为 0.35127 秒,慢 3.0%。完整训练时间为 1275.066 秒,较对照增加 24.873 秒或 2.0%。两轮使用了不同计算节点,这些小幅 I/O 与端到端差异不能完全归因于 rebase。

47 次 merge 的 SQLite commit p95 为 0.02774 秒;commit 与 maintenance 合计 5.686 秒,占完整训练时间 0.446%,与对照持平。loss first-10/last-10 均值为 3.80957 → 3.09629,对照为 3.80223 → 3.18917;新 run 的 last-10 低 0.09288 或 2.91%,且未出现明显发散。

rebase 状态机的日志计数自洽:400 次发布后检查中,392 次保存了 CPU FP32 anchor,8 次在发布后立即采用新版;349 个 anchor 后续完成 rebase,加上 8 次立即采用,共产生 357 次 global adoption。八次初始 optimizer 建立加上 357 次 adoption 后重置,与 365 条 `inner_optimizer_reset` 日志一致。357 次 adoption 之后的下一份 proposal 全部使用了对应新版本作为 `base_global_version`,没有发现状态机违例。

349 次 rebase 共迁移 260.194M carried-delta tokens;每次平均为 745,542 tokens 或 45.5 个 optimizer steps,中位数 42 steps,p95 为 95 steps,最大 100 steps。anchor 到 rebase 的平均等待时间为 10.36 秒,p95 为 20.95 秒。43 个 anchor 没有完成 rebase,其中 35 个在下一次发布时被替换,8 个随 learner 正常退出释放。每份 anchor 为 497,759,232 bytes(约 474.7 MiB),日志证明没有 OOM 或未释放的状态链,但本 run 没有 RSS/内存峰值 telemetry,因此不能定量验收 CPU 内存成本。

发布即移交所有权的风险在实验中实际出现:349 次 rebase 中有 13 次对应的 anchor proposal 最终被 `superseded`;8 次发布后立即 adoption 中也有 3 份刚发布的 proposal 最终 dropped。这些事件中,若新 global 未包含该 proposal,发布点之前的 learner 局部贡献可能被丢弃;因此 260.194M carried-delta tokens 只能解释为“在 rebase 事件中迁移过的工作量”,不能解释为最终 global 中保留的额外训练量。

对齐两次 run 的相同 learner 和相同 local step 后,400 个 loss 点中有 390 个在 rebase run 中更低,平均配对差为 -0.09258。step 100 时两者几乎相同;step 500、2500、5000 的八 learner 均值差分别为 -0.07751、-0.11351 和 -0.11120。这是 rebase 保留发布后局部进度的强一致性信号,但仍然只是 learner local training loss;在没有固定 validation loss/perplexity、相同 applied tokens/global version 和多 seed 重复前,不能据此宣称最终 global checkpoint 质量提升。

终态 DB/latest/stop/summary 均为 v47,SQLite `integrity_check=ok`、`journal_mode=delete`、`synchronous=FULL`;目录只保留 v47 weight/outer、八个固定 pointers 与 DB,终态 proposal tensor/meta、临时文件、WAL 和 dump 均为零。独立 Checker 按实际 v47 返回 `PASS`,按目标 v50 返回 `BLOCKED`。综合结论是:rebase 实现和终态状态机验证通过,并显著改变了 proposal freshness 与 learner local loss 轨迹;但本次没有改善 update 利用率或端到端时间,也仍不能在固定 5000 local steps 下保证 v50。run root 为 `runs/fs_diloco/codex_rebase_full5000_20260717_013647/`。

### Adaptive fastest-upload ETA + global-only 的 v50 终态

九节点作业 `2399961.opbs`、run `codex_adaptive_eta_global50_full5000_20260717_055909` 在上一轮 rebase 配置上只改变完成与 grace 语义:grace 从固定 20 秒改为 `adaptive_fastest_upload_eta`,初始窗口 10 秒;`training.completion_mode=global_only` 使 learner 忽略 5000 本地步硬上限,只响应 syncer 的 stop message。作业以 PBS `Exit_status=0`、无异常节点完成,walltime 为 18 分 16 秒。DB/latest/stop/summary 均为 v50,停止原因为 `stop_after_outer_steps`,首次完成配置目标而没有进入 terminal drain 或 `input_exhausted`。

| 指标 | 固定 20s + local-or-global 基线 | adaptive 10s + global-only | 变化/解释 |
|---|---:|---:|---|
| final version / reason | v47 / `input_exhausted` | v50 / `stop_after_outer_steps` | 配置目标达成 |
| PBS walltime | 1286 s | 1096 s | -14.8% |
| 完整训练时间 | 1275.066 s | 1085.107 s | -14.9% |
| learner local steps 总和 | 40,000 | 37,533 | -6.17%;新 run 在 v50 即停 |
| produced / applied updates | 400 / 365 | 379 / 371 | applied 增加 6,produced 减少 21 |
| update 利用率 | 91.25% | 97.89% | +6.64 个百分点 |
| dropped | 35 `superseded` | 8 `stop_after_outer_steps` | 活跃训练期 supersession 降为 0 |
| applied tokens | 598.016M | 607.846M | +1.64% |
| selected/merge 均值 | 7.766 | 7.420 | 更短窗口以较小 batch 换取更高 merge 频率 |
| selected 分布 | `44×8,1×6,1×4,1×3` | `27×8,17×7,6×6` | 新 run 没有低于 6 的 merge |
| applied staleness 0/1/2 | 359 / 6 / 0 | 133 / 207 / 31 | 更快 global 推进显著提高版本 staleness |
| global interval 均值 | 26.662 s | 21.244 s | -20.3%;基线受末端三轮拖尾影响 |
| loss first-10 → last-10 | 3.80957 → 3.09629 | 3.80891 → 3.17030 | 均未判定发散,但不能作为最终 checkpoint 质量结论 |

50 个 merge 都记录了 adaptive grace telemetry。窗口 elapsed 平均 9.077 秒、中位数 10.103 秒、p95 10.188 秒,范围 4.283–10.197 秒;27 次因收齐 `quorum_max` 结束,21 次耗尽初始窗口,2 次直接由 fastest-upload ETA deadline 结束。共有 12 次 deadline 被 ETA 向前收紧,估计剩余时间均值 5.501 秒、范围 1.553–9.940 秒;其中另外 10 次在收紧后的 deadline 之前先收齐八份 update。相对“每轮固定用满 10 秒”的 500 秒预算,实际 grace 总计约 453.9 秒。与旧 20 秒窗口相比,最重要的实测结果不是单纯节省等待,而是没有任何 proposal 在被 syncer 选择前遭下一 pointer 覆盖:旧 run 的 35 次 `superseded` 在新 run 中降为 0。

完成语义也按实现工作,但需要精确解释“5000-step”。v50 发布时八个 learner 的最终 local step 分别为 4461、5044、4854、4771、4925、4428、4857、4193。最快的 learner_001 在 step 5000 记录 `local_step_horizon_reached`,继续训练到 5044,看到 `stop_after_outer_steps` 后退出;其余七个 learner 因 v50 已先到达而在 5000 之前响应 stop。所有 learner 最终 heartbeat 都为 stopped 且退出时已加载 v50。因此本 run 证明“达到 5000 后不会自行退出”,但 `global_only` 并不保证每个 learner 至少完成 5000 步。如果实验目标是“八个 learner 都达到 5000 且 global≥50”,syncer 还需要一个 `global target AND all learner local horizon` 的联合停止门槛。

吞吐改善伴随明显的异步性变化。虽然 applied update 的 wall-clock selection age 均值从 10.330 秒降到 9.286 秒,但 global version 推进更快,staleness=1/2 的占比从 1.64% 增至 64.15%。对齐相同 learner 与相同 local step 的 371 个完整 cycle loss 点后,新 run 只有 19 个点更低,`new-old` 平均为 +0.05051;step 100、500、2500、4000 的八 learner 均值差依次为 +0.00012、+0.00349、+0.05707、+0.08527。这是 shorter grace/更高 staleness/更频繁 global adoption 改变本地优化轨迹的清晰信号,但仍不能替代两个最终 global checkpoint 的固定 validation loss/perplexity 对照。

因此本轮应明确记为“系统吞吐与完成语义通过,learner local training loss 回退”:first-10 几乎不变,但差距随 local step 持续扩大;last-10 从 3.09629 上升到 3.17030,高 0.07401(2.39%),全部 update 均值从 3.17152 上升到 3.22712。可能机制包括更高版本 staleness、较小 contributor batch,以及更快 global adoption 触发更多 optimizer/scheduler reset。由于两轮终态分别为 v47/v50、各 learner 最终 local step 也不同,这里的“回退”只描述对齐后的本地训练 loss 轨迹,不能外推为最终 global 模型质量下降。

终态持久状态大部分正确:SQLite `integrity_check=ok`,active global 只有 v50,没有 pending/selected update、proposal payload、临时文件或 WAL,只保留 v50 weight/outer。379 份 update 中 371 份 applied;stop 发布后每个 learner 各有一份部分 cycle update 被统一终态化为 `dropped(stop_after_outer_steps)`。但发现一个终态一致性缺口:八份磁盘 heartbeat 都为 `stopped`,summary 也写 `all_learners_stopped=true`,SQLite `learners` 表却仍保留 stop 发布前的八行 `active` 状态。原因是 `wait_for_learner_shutdown()` 直接读取 heartbeat 判断停止,期间只调用 `ingest_update_metadata()`,没有把最终 heartbeat 摄取进 DB。旧 run 因 learner 先在本地 5000 步退出,主循环曾摄取 stopped heartbeat,所以没有暴露该问题。它不影响本次 v50 checkpoint,但应在下一次正式运行前修复并增加终态 DB/heartbeat 一致性断言。

综合结论:adaptive 10 秒 grace 与 global-only 完成语义达成了主要工程目标——v50、零活跃期 supersession、更高 proposal 利用率和更短 walltime;代价是平均 selected count 下降、版本 staleness 显著增加,且不再保证每个 learner 执行 5000 步。下一步质量判断应先修复最终 heartbeat 摄取,再用相同 final global version/applied tokens 做固定 validation loss/perplexity,并明确正式协议需要 `global-only` 还是“global 与所有 learner 本地 horizon 同时满足”。run root 为 `runs/fs_diloco/codex_adaptive_eta_global50_full5000_20260717_055909/`。

### 仍需进一步研究

- 在当前同一代码版本上做只改变 `io.tensor_dtype` 的 FP32/BF16 对照,固定 staleness、seed、applied tokens 或 global version,并重复多个 seed;
- 对最终 global checkpoint 运行固定 validation loss/perplexity,不要用 learner local training loss 推断最终模型质量;
- 明确正式实验究竟以“每 learner 5000 local steps”、“达到 50 outer merges”还是两者联合为完成条件;当前 `global_only` 会在 v50 到达时停止尚未到 5000 的 learner;
- 若必须同时保证 fresh-only 与 v50,研究 upload 后版本确认/等待或显式 round barrier;若保持异步 staleness,应系统扫描 staleness window、lambda 与质量/吞吐关系;
- telemetry 应同时报告 actual local tokens、applied tokens、produced/applied/dropped updates 和 merge count,避免把 `total_seen_tokens` 误读为总训练计算量;
- BF16 已显著降低 proposal I/O,但端到端改善很小;仍需分解 local training、quorum wait、global publication 和第九张 syncer GPU 低利用率的成本。
- 对 replace/rebase 做固定 applied tokens 或 global version 的多 seed 对照,并对最终 global checkpoint 运行相同 validation loss/perplexity;
- 为 rebase 增加 anchor 生命周期、proposal 最终状态、进程 RSS/内存峰值和临时向量峰值 telemetry,量化 dropped anchor 的所有权风险和 CPU 内存成本。
- 修复 stop 发布后的最终 heartbeat 摄取,并断言 SQLite `learners.status`、磁盘 heartbeat 与 summary 的 stopped 状态一致。
- 对 adaptive grace 扫描 initial window 与 staleness penalty;当前 10 秒窗口消除了 supersession,但把 staleness=1/2 比例提高到 64.15%,需要最终 checkpoint 质量评估决定吞吐/质量折中。
