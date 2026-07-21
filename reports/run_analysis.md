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

### 全部完整 5000-step run 盘点与统一统计

本节按统一规则重扫 `runs/fs_diloco/`:run 必须已经生成 `control/summary.json`,且 resolved config 中 `training.max_local_steps=5000`。这里的“5000-step run”指配置的本地步数上限为 5000,不等于每个 learner 最终都必须执行到 5000;`global_only` 或先到达 global target 的 run 会更早停止部分 learner。截至本次整理共找到 8 个完整结果。仍未生成终态 summary 的在运行 run、50x10/smoke 和 fragment run 不纳入本节统计。

为便于后文引用,8 个 run 按开始时间记为:

| 代号 | run |
|---|---|
| A | `20260716_175316_fs_diloco_gpt2_wikitext2_8l_5000steps` |
| B | `codex_plan01_full5000_20260716_221526` |
| C | `codex_plan01_full5000_bf16_s2_20260716_233341` |
| D | `codex_rebase_full5000_20260717_013647` |
| E | `codex_wait2p5_full5000_20260717_022332` |
| F | `codex_predict_full5000_20260717_023302` |
| G | `codex_adaptive_eta_global50_full5000_20260717_055909` |
| H | `codex_predict_gc_retry_wait0_fixed20_global50_full5000_20260717_073011` |

配置矩阵如下。A–F 没有显式设置 `completion_mode`,按默认 `local_or_global` 解释;G/H 显式使用 `global_only`。所有 run 都是 8 learners、`inner_steps=100`、global target=50。

| 代号 | upload | max staleness | global adoption / upload 后行为 | grace | completion |
|---|---|---:|---|---|---|
| A | FP32 | 2 | replace,不在 inner step 轮询 | fixed 20s | `local_or_global` |
| B | FP32 | 0 | replace,不在 inner step 轮询 | fixed 20s | `local_or_global` |
| C | BF16 | 2 | replace,不在 inner step 轮询 | fixed 20s | `local_or_global` |
| D | BF16 | 2 | `rebase_post_publish_delta`,inner-step polling | fixed 20s | `local_or_global` |
| E | BF16 | 2 | replace,upload 后最多等待 latest 2.5s | fixed 20s | `local_or_global` |
| F | BF16 | 2 | `predict_post_publish_global`,wait=0,inner-step polling | fixed 20s | `local_or_global` |
| G | BF16 | 2 | `rebase_post_publish_delta`,inner-step polling | adaptive fastest-upload ETA,初始 10s | `global_only` |
| H | BF16 | 2 | prediction 时 reset,reconcile 时保留 optimizer/scheduler,wait=0,GC-race retry | fixed 20s | `global_only` |

统一终态指标如下。利用率为 applied/produced;local steps 是八个 learner 之和;complete time 从 syncer 启动计至八个 learner stopped。

| 代号 | final / stop reason | produced / applied | 利用率 | dropped | local steps | complete time |
|---|---|---:|---:|---|---:|---:|
| A | v49 / `no_progress_timeout` | 400 / 362 | 90.50% | 38,旧版摘要未保留原因细分 | 40,000 | 4872.680s¹ |
| B | v25 / `input_exhausted` | 400 / 190 | 47.50% | 205 superseded + 5 too-stale | 40,000 | 1257.338s |
| C | v48 / `input_exhausted` | 400 / 372 | 93.00% | 28 superseded | 40,000 | 1250.194s |
| D | v47 / `input_exhausted` | 400 / 365 | 91.25% | 35 superseded | 40,000 | 1275.066s |
| E | v46 / `input_exhausted` | 400 / 354 | 88.50% | 46 superseded | 40,000 | 1326.836s |
| F | v50 / `stop_after_outer_steps` | 399 / 396 | 99.25% | 3 stop-finalized | 39,692 | 1209.721s |
| G | v50 / `stop_after_outer_steps` | 379 / 371 | 97.89% | 8 stop-finalized | 37,533 | 1085.107s |
| H | v50 / `stop_after_outer_steps` | 406 / 398 | 98.03% | 8 stop-finalized | 40,338 | 1180.456s |

¹ A 在约 1270.977 秒时已经发布 v49,随后因所有 learner 都已到达 5000、没有第 50 轮合法输入而等待完整的 3600 秒 `no_progress_timeout`;因此 4872.680 秒不是正常活跃训练吞吐。A 的旧版终态没有 `update_history.jsonl`,其 applied staleness 由 manifest 的 base version 与 syncer 的 selected version 对齐重建。

| 代号 | selected 分布 | applied staleness 0 / 1 / 2 | applied tokens | loss first-10 → last-10 | 全部 loss 均值 |
|---|---|---:|---:|---:|---:|
| A | `43×8 + 1×6 + 2×4 + 1×2 + 2×1` | 30 / 329 / 3 | 593.101M | 3.80088 → 3.18385 | 3.26118 |
| B | `22×8 + 1×7 + 1×5 + 1×2` | 190 / 0 / 0 | 311.296M | 3.80964 → 3.11796 | 3.22230 |
| C | `45×8 + 1×5 + 1×4 + 1×3` | 27 / 345 / 0 | 609.485M | 3.80223 → 3.18917 | 3.26410 |
| D | `44×8 + 1×6 + 1×4 + 1×3` | 359 / 6 / 0 | 598.016M | 3.80957 → 3.09629 | 3.17152 |
| E | `41×8 + 2×7 + 1×5 + 1×4 + 1×3` | 113 / 241 / 0 | 579.994M | 3.80238 → 3.16122 | 3.24271 |
| F | `46×8 + 4×7` | 318 / 76 / 2 | 648.806M | 3.83873 → 3.18335 | 3.24274 |
| G | `27×8 + 17×7 + 6×6` | 133 / 207 / 31 | 607.846M | 3.80891 → 3.17030 | 3.22712 |
| H | `48×8 + 2×7` | 333 / 65 / 0 | 652.083M | 3.83459 → 3.12398 | 3.19691 |

跨 run 汇总只用于核对 artifact 完整性,不能当作同一训练过程累加:8 次实验共产生 3184 份 proposal,其中 2808 份 applied、376 份 dropped,总体利用率 88.19%;365 次 merge 平均 selected=7.693。重复实验合计写入约 991.537 GB proposal payload,applied update 携带 4.600627B tokens。final version 排序为 25、46、47、48、49、50、50、50,中位数 v48.5,F/G/H 三次达到配置目标 v50。8 次 run 的 learner local loss 都从 first-10 降到 last-10,未检测到明显发散;逐 run 等权均值为 3.81337 → 3.15327,但这些是本地训练 loss,且配置、applied tokens 和终态版本不同,不能据此给各策略做最终模型质量排名。

在配置最接近的 BF16/staleness=2/fixed-20s 四组 C–F 中,单纯 upload 后等待 2.5 秒的 E 并未改善完成度:相对 C,final 从 v48 降到 v46,利用率从 93.0% 降到 88.5%,完整时间从 1250.194 秒增至 1326.836 秒。D 的 rebase 把 fresh applied 比例提高到 98.36%,并得到最低的 last-10 local loss,但仍在 v47 耗尽输入。F 的 prediction 是这四组中唯一达到 v50 的 run:利用率 99.25%、平均 selected=7.92,训练期间没有 superseded/too-stale drop;代价是仍有 19.70% applied update 的版本 staleness 为 1 或 2,且 last-10 local loss 高于 D 0.08706。G 用更短的 adaptive grace 得到全表最短完成时间,但 selected 均值降至 7.42、staleness=1/2 占比升至 64.15%。这些结果表明“能到 v50”和“版本 fresh/local loss 较低”目前不是同一个优化目标,后续必须用统一 final checkpoint validation loss/perplexity 决策。

此前未单独记录的 A、E、F 还有以下终态证据:

- A 的 400 份 FP32 proposal 中 362 份 applied,49 次 merge 的平均 global interval 为 25.439 秒;它已经接近 v50,但 local horizon 先耗尽后没有 terminal input-exhausted 机制,最终靠一小时 no-progress timeout 收尾。该 run 更适合作为旧实现/FP32-s2 旁证,不应把 4872.680 秒与新持久 runtime 的正常终态直接比较。
- E 一共启动 131 次 upload 后等待,其中 79 次在窗口内发现新版、52 次耗尽 2.5 秒;累计等待 258.188 秒。它仍产生 46 个 superseded proposal,global interval 均值 28.300 秒,说明固定 sleep 会消耗 learner compute slack,但不能可靠对齐 syncer 的下一次 publication。
- F 记录 321 次 global prediction start、316 次 prediction reconcile 和 78 次 publish 后直接 adoption,合计 394 次 global adoption。v50 发布时 learner final local step 为 4822–5000,其中三个 partial-cycle proposal 被统一标记为 `dropped(stop_after_outer_steps)`;其余 396 份全部 applied。这个 run 证明 prediction 可以在固定 20 秒 grace 下达到 v50,但它仍使用默认 `local_or_global`,不能验证“learner 到 5000 后继续等待 stop”的 `global_only` 语义。

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

### 基础 FP32 fresh-only 与 BF16/staleness=2 对比

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

终态持久状态大部分正确:SQLite `integrity_check=ok`,active global 只有 v50,没有 pending/selected update、proposal payload、临时文件或 WAL,只保留 v50 weight/outer。379 份 update 中 371 份 applied;stop 发布后每个 learner 各有一份部分 cycle update 被统一终态化为 `dropped(stop_after_outer_steps)`。但发现一个终态一致性缺口:八份磁盘 heartbeat 都为 `stopped`,summary 也写 `all_learners_stopped=true`,SQLite `learners` 表却仍保留 stop 发布前的八行 `active` 状态。原因是 `wait_for_learner_shutdown()` 直接读取 heartbeat 判断停止,期间只调用 `ingest_update_metadata()`,没有把最终 heartbeat 摄取进 DB。旧 run 因 learner 先在本地 5000 步退出,主循环曾摄取 stopped heartbeat,所以没有暴露该问题。它不影响本次 v50 checkpoint。后续源码已改为 shutdown wait 每轮统一摄取 heartbeat、liveness 与 update 元数据,并且只有 SQLite 中全部预期 learner 都为 stopped 才返回成功;同时增加了 active→stopped 终态摄取回归测试。后续 H 的九节点 run 已复验这一修复:磁盘 heartbeat、summary 和 SQLite `learners` 表均为 8/8 stopped。

综合结论:adaptive 10 秒 grace 与 global-only 完成语义达成了主要工程目标——v50、零活跃期 supersession、更高 proposal 利用率和更短 walltime;代价是平均 selected count 下降、版本 staleness 显著增加,且不再保证每个 learner 执行 5000 步。最终 heartbeat 摄取修复已经由后续 H 复验。本文后部的 Q2/Q4 同 fingerprint 三 seed validation 已补齐当时缺失的质量门禁；“global 与所有 learner 本地 horizon 同时满足”的联合完成谓词仍是独立后续方向。run root 为 `runs/fs_diloco/codex_adaptive_eta_global50_full5000_20260717_055909/`。

### Predictor GC-race 修复与 reconcile 保留状态的 5000-step 对比

九节点作业 `2400033.opbs`、run `codex_predict_gc_retry_wait0_fixed20_global50_full5000_20260717_073011` 以 PBS `Exit_status=0` 完成,walltime 为 19 分 50 秒。DB/latest/stop/summary 均为 v50,停止原因为 `stop_after_outer_steps`,日志没有 `FileNotFoundError`、traceback、error 或 uncaught exception。这里用 H 表示新 run,并与此前 prediction run F=`codex_predict_full5000_20260717_023302` 对比。

两者共同使用 GPT-2/WikiText-2、BF16 训练与 proposal、8 learners、`inner_steps=100`、staleness=2、fixed 20 秒 grace、wait=0、cosine scheduler 和相同 seed。resolved config 的有效差异是 F 使用默认 `local_or_global`,H 显式使用 `global_only`;H 还多出一个在 fixed grace 模式下不生效的 `initial_seconds=10`。运行时 predictor 语义也有关键差异:F 在构造 prediction 时 reset optimizer/scheduler,在 reconcile 到真实 global 时再次 reset;H 仍在 prediction 时 reset,但 reconcile 时完整保留 optimizer 与 scheduler 状态。此外,H 加入 cached checkpoint 被 current-only GC 回收时的 bounded retry/latest 重读,以及 syncer 终态 heartbeat 摄取修复。因此这不是只改变单一变量的严格质量对照。

| 指标 | F:旧 predictor | H:reset-on-predict/preserve-on-reconcile | 变化/解释 |
|---|---:|---:|---|
| final / stop reason | v50 / `stop_after_outer_steps` | v50 / `stop_after_outer_steps` | 两者都完成目标 |
| complete training time | 1209.721s | 1180.456s | -29.265s(-2.42%) |
| learner local steps 总和 | 39,692 | 40,338 | +646(+1.63%) |
| produced / applied | 399 / 396 | 406 / 398 | H 多 applied 2 份 |
| proposal 利用率 | 99.25% | 98.03% | -1.22 个百分点;差异来自终态 proposal |
| dropped | 3 `stop_after_outer_steps` | 8 `stop_after_outer_steps` | 两者活跃训练期间均无 superseded/too-stale drop |
| applied tokens | 648.806M | 652.083M | +3.277M(+0.50%) |
| selected/merge 均值 | 7.920 | 7.960 | H 为 `48×8+2×7` |
| applied staleness 0/1/2 | 318 / 76 / 2 | 333 / 65 / 0 | fresh 比例 80.30% → 83.67% |
| commit→selection age 均值 | 12.546s | 11.529s | -8.10% |
| global interval 均值 | 23.674s | 23.147s | -2.23% |
| learner step time 均值 | 0.21707s | 0.21111s | -2.75%;不同节点,不能归因于 predictor |
| prediction start / reconcile | 321 / 316 | 341 / 335 | H 的运行周期稍多 |
| publish 后直接 adoption | 78 | 65 | 直接 adoption 仍 reset |
| optimizer/scheduler reset | 723 | 414 | -309(-42.74%) |
| reconcile state preserved | 0 | 335 | 与 H 的 335 次 reconcile 一一对应 |
| loss first-10 → last-10 | 3.83873 → 3.18335 | 3.83459 → 3.12398 | H last-10 低 0.05936(1.87%) |
| 全部 local loss 均值 | 3.24274 | 3.19691 | H 低 0.04584(1.41%) |

状态机日志计数自洽。F 的 723 次 reset 恰好等于 8 次初始化 + 321 次 prediction start + 394 次 global adoption;也就是说 316 次 reconcile 全部再次 reset。H 的 414 次 reset 等于 8 次初始化 + 341 次 prediction start + 65 次 publish 后直接 adoption,而 335 次 reconcile 全部记录 `inner_training_state_preserved`,没有额外 reset。H 总计 400 次 global adoption,最终八个 learner 都加载到 v50;F 虽然也发布到 v50,但五个达到本地 horizon 的 learner 以 v49 退出。

local loss 的改善与“减少 reconcile reset”的方向一致。对齐相同 learner 和相同 local step 后共有 395 个可比点,H 在其中 384 个(97.22%)更低,平均 `H-F=-0.04435`。step 100 时两者均值为 3.90287 与 3.90291,几乎完全一致;step 500、2500、4000、4800 的 H-F 分别为 -0.04175、-0.04523、-0.04171、-0.08258,差距在训练后段仍然存在。这是保留 optimizer/scheduler state 避免反复重启局部学习率轨迹的强一致性信号,但两次 run 的代码版本、completion mode、节点和异步选择序列并不完全相同,不能据此建立严格因果关系;更不能把 learner local loss 直接解释为最终 v50 global checkpoint 的 validation loss/perplexity。

`global_only` 也按设计生效。H 中六个 learner 记录 `local_step_horizon_reached` 后继续到 5023–5100,另外两个在 v50 到达时停在 4906/4961;八个 learner 最终都响应 stop 并加载 v50。它仍然只保证“5000 不触发本地退出”,不保证所有 learner 至少训练 5000 步。H 的 8 份 dropped update 都是在 v50 后终态化的 proposal,不是活跃期效率故障。

GC-race 修复没有在本次实际调度中进入 recovery 分支:`global_prediction_preparation_recovered` 计数为 0。因此这次九节点结果证明修复没有引入回归、原先的 `outer_v*.safetensors` 缺失崩溃没有重现,但不能单独证明真实 current-only GC 竞态下的 bounded retry 已被动态命中;该精确分支目前由定向单元测试覆盖,后续运行应继续监控 recovery event 的 reason、等待时间和采用版本。

终态一致性通过:SQLite `integrity_check=ok`,磁盘 heartbeat、summary 与 SQLite `learners` 表均为 8/8 stopped;只保留 v50 weight/outer,proposal payload、临时文件和 WAL 均为零。对照 F 的磁盘 heartbeat 虽全为 stopped,SQLite 表仍是 3 active + 5 stopped,H 因而完成了 shutdown heartbeat 摄取修复的九节点复验。综合而言,H 保持 v50 和接近满额的 contributor batch,显著减少 scheduler/optimizer reset,并伴随更低的 learner local loss。后续已完成四个存量 checkpoint validation 与 B2+Q3 后三 seed 受控矩阵；正式结果支持 replace 保持默认，不能从 H 的历史 local loss 推导 prediction 默认。run root 为 `runs/fs_diloco/codex_predict_gc_retry_wait0_fixed20_global50_full5000_20260717_073011/`。

### Rebase reconcile 保留 optimizer/scheduler 状态的 5000-step 对比

九节点作业 `2400100.opbs`、run `codex_rebase_preserve_full5000_20260717_082018` 直接以 `runs/fs_diloco/codex_rebase_full5000_20260717_013647/run_config.resolved.yaml` 作为输入,以新的 run ID/shared root 复刻固定 20 秒 grace、8 learners、每 learner 5000 local steps、BF16 proposal、staleness=2 和 `rebase_post_publish_delta` 实验。PBS `Exit_status=0`,无异常节点,walltime 为 21 分 01 秒;八个 learner 均到达 step 5000。新 resolved snapshot 相对输入仅多写了当前代码中已有的默认字段(`initial_seconds=10` 在 fixed 模式下不生效、syncer dtype/device 默认值、`completion_mode=local_or_global` 和 learner wait/poll 默认值),有效实验配置不变。目标运行时差异是:learner 在保存 publish anchor、继续训练并随后以新版 global 完成 rebase 时,只替换模型权重,不再重建 AdamW 或 cosine scheduler;发布后第一次检查就已发现新版的直接 adoption 仍保留原语义并重置状态。旧、新作业使用了不同计算节点,且两次运行间没有记录可复现的源码 commit,因此下面是强相关的单次异步对照,不是 bitwise-identical executable 的严格消融。

| 指标 | 旧 rebase:每次 adoption reset | 新 rebase:reconcile 保留状态 | 变化/解释 |
|---|---:|---:|---|
| final / stop reason | v47 / `input_exhausted` | v49 / `input_exhausted` | 多完成 2 次 merge,但仍未达到 v50 |
| learner local steps 总和 | 40,000 | 40,000 | 本地计算步数一致 |
| produced / applied | 400 / 365 | 400 / 379 | applied 增加 14 份 |
| proposal 利用率 | 91.25% | 94.75% | +3.50 个百分点 |
| dropped | 35 `superseded` | 21 `superseded` | 少 14 份;两者均无 `too_stale` |
| applied tokens | 598.016M | 620.954M | +22.938M(+3.84%) |
| selected 分布 | `44×8,1×6,1×4,1×3` | `46×8,1×5,1×4,1×2` | 新 run 多两轮满额 merge,末轮以 2 份 terminal update 推进到 v49 |
| applied staleness 0/1/2 | 359 / 6 / 0 | 376 / 3 / 0 | fresh 比例 98.36% → 99.21% |
| global interval 均值 | 26.662s | 25.043s | -6.07%;包含不同 terminal tail |
| learner step time 均值 | 0.21618s | 0.21703s | +0.39%,本地训练吞吐基本相同 |
| update write mean / p95 | 0.20359 / 0.31183s | 0.18001 / 0.25585s | -11.58% / -17.95%;节点不同,不能归因于状态保留 |
| syncer read mean | 0.35127s | 0.33748s | -3.93%;同样受节点和共享 FS 波动影响 |
| complete training time | 1275.066s | 1250.164s | -24.902s(-1.95%) |
| optimizer/scheduler reset | 365 | 10 | -355(-97.26%) |
| rebase state preserved | 0 | 369 | 与新 run 的 369 次 rebase 一一对应 |
| loss first-10 → last-10 | 3.80957 → 3.09629 | 3.79641 → 3.05501 | 新 run last-10 低 0.04128(1.33%) |
| 全部 local loss 均值 | 3.17152 | 3.12387 | 低 0.04765(1.50%) |

新状态机日志计数严格闭合。400 次发布后检查中,398 次保存 CPU FP32 anchor,2 次发布后立即采用新版;398 个 anchor 中 369 个后续完成 rebase,共产生 `369 + 2 = 371` 次 global adoption。10 条 `inner_optimizer_reset` 恰好等于 8 次启动初始化加 2 次直接 adoption;369 条 `inner_training_state_preserved(reason=global_rebased)` 与 369 次 rebase 一一对应,并且每条都记录完整的 148 项 AdamW parameter state、`optimizer_state_preserved=true` 和 `scheduler_state_preserved=true`。六个从未直接 adoption 的 learner 上,scheduler epoch 在 rebase 序列中始终单调递增并最终达到 4913–4999;另外两个 learner 只在 v33/v42 的直接 adoption 处各出现一次预期中的 scheduler 重启。由此九节点真实训练路径验证了目标语义:rebase 写入新权重后,参数对象、AdamW moments、当前 learning rate 和 cosine 进度均延续;不会因 rebase 再次 warmup/重启 schedule。

369 次 rebase 共迁移 280.986M carried-delta tokens,每次平均 761,479 tokens 或 46.48 个 optimizer steps,中位数 47 steps,p95 为 94 steps,最大 99 steps;anchor 到 rebase 的等待均值为 10.64 秒,p95 为 20.76 秒,与旧 run 的 10.36/20.95 秒接近。29 个 anchor 没有完成 rebase,其中 21 个在下一次发布时被替换,8 个随 learner 退出释放。369 个已 rebase anchor 中仍有 9 个对应 proposal 最终被 `superseded`;两次直接 adoption 中也有 1 个刚发布 proposal 最终被 `superseded`。因此旧实验中指出的“发布即移交所有权”风险仍存在;optimizer/scheduler 状态保留修复的是训练状态连续性,不会保证 anchor proposal 一定进入 global。

对齐两次 run 的相同 learner 和相同 local step 后共有 400 个可比 loss 点,新 run 在其中 394 个(98.5%)更低,配对差 `new-old` 平均为 -0.04765。step 100 的八 learner 均值差仅 +0.00011,说明起点一致;step 500、2500、4000、4800、5000 的差依次为 -0.07666、-0.05221、-0.02866、-0.01917、-0.01578。差距在早中期形成后逐步收窄,但到 step 5000 仍有七个 learner 更低。这个轨迹与“不再反复重启 AdamW moments 和 cosine schedule”的方向一致,证据也比未对齐的 last-10 更强;不过新 run 同时多应用 14 份 proposal、推进到 v49,global adoption 时序也不同,所以不能把全部 loss 差异归结为状态保留,更不能直接外推为最终 global checkpoint 质量提高。

持久状态与终态清理通过。49 次 merge 的 SQLite commit p95 为 0.02718 秒;commit 与 maintenance 合计 5.952 秒,占完整训练时间 0.476%。DB/latest/stop/summary 均为 v49,SQLite `integrity_check=ok`、`journal_mode=delete`、`synchronous=FULL`,SQLite learner 状态为 8/8 stopped;目录只保留 v49 weight/outer、八个固定 proposal pointers 与 DB,proposal payload、临时文件和 WAL 均为零。八个 learner 因 `local_or_global` 在 step 5000 先退出,其退出时已加载版本为 45/45/45/46/47/47/48/48;syncer 随后在 v48 terminal drain 中用最后两份 pending update 发布 v49,最后一个 learner 退出到 stop 发布间隔为 22.06 秒。独立 Checker 按实际 v49 返回 `PASS`,按配置目标 v50 返回 `BLOCKED`。

综合结论:目标代码路径已通过单元测试、单节点 3-global-step 冒烟和本次九节点 5000-step 实验验证;所有延迟 rebase 都保留 optimizer/scheduler 状态,只有两次发布后直接 adoption 按设计重置。单次运行同时观察到更少 supersession、更高 applied tokens、v47→v49、更短完成时间和更低的对齐 local loss,但其中系统吞吐差异受异步调度、节点和共享 FS 波动影响,质量结论还缺相同 final version 的 validation loss/perplexity 与多 seed 重复。run root 为 `runs/fs_diloco/codex_rebase_preserve_full5000_20260717_082018/`,PBS 汇总为 `fsdiloco_gpt2_5k.o2400100`,分析 JSON 为 `logs/qsub_codex_rebase_preserve_full5000_20260717_082018/summary.json`。

### 事件化 ingestion 与 publish-wait 摄取的三 seed 对照

九个 9 节点 v50 run 使用同一 source fingerprint 和 seeds 1337/2027/4049，按相邻单变量比较 `scan=2/no-ingest`、`scan=.2/no-ingest`、`scan=.2/publish-ingest`。所有作业均正常到达 `stop_after_outer_steps`、8/8 learners stopped 且 SQLite integrity OK。

| 三 seed 均值 | scan=2 / no ingest | scan=.2 / no ingest | scan=.2 / ingest |
|---|---:|---:|---:|
| 完整训练时间 | 1101.633 s | **1075.273 s** | 1076.155 s |
| global interval | 21.6515 s | **21.1251 s** | 21.1431 s |
| quorum discovery+idle | 13.1646 s | **12.2652 s** | 12.3868 s |
| quorum-max 触发率 | 70.00% | 61.33% | **72.67%** |
| update 利用率 | 97.453% | 97.395% | **97.788%** |
| publish-wait 摄取 update | 0 | 0 | 8.0/run |

把 scan 从 2 秒缩短到 0.2 秒使完整时间改善 2.39%、interval 改善 2.43%、quorum 检测改善 6.83%；共享盘 8-pointer/0.2s 实测仅占单核 0.0402% CPU，因此可用于 latency-sensitive 实验。publish-wait 摄取确实命中 24 次 metadata，但相邻三 seed 完整时间反而轻微增加 0.08%，没有形成端到端收益；它只保留为 opt-in。三组 interval residual ratio 均值为 0.084%/0.283%/0.284%，遥测闭合。原始矩阵见 `reports/imp_plans/perf_fix-E/E2/artifacts/20260718-1300_e2-formal-matrix.csv`。

遥测口径勘误（2026-07-21）：本修复之前，full caller 在 `ingest_during_publish=false`
时仍传入一个返回 None 的 callable，因此历史 `publish_ingest_passes` 可能包含 checkpoint
等待轮询空转，不能解释为真实 ingestion callback 次数。历史
`publish_ingested_updates/heartbeats` 的非零值仍表示实际插入；上表的“24 次 metadata”来自
非零 inserted update 计数，不受该空转 passes 污染。修复后的 false 模式四字段严格为零。

### Syncer 第九节点资源账本与 CPU 可行性

按冻结口径，syncer active time 定义为每轮 `read + aggregation + outer_step + publish`，完整训练墙钟为分母；publication 与 merge compute 仍分开报告。既有 9 节点 run H=`codex_predict_gc_retry_wait0_fixed20_global50_full5000_20260717_073011` 的 50 轮总 merge compute 为 18.345 秒、publication 为 44.946 秒，而完整训练为 1180.456 秒：第九节点 duty cycle 只有 5.362%。该作业占用 0.3279 syncer node-hours，其中按此口径估计有 0.3103 GPU node-hours处于非 syncer-active 状态。merge-compute p95 为 0.4912 秒，publication p95 为 0.9906 秒。

同一 Miyabi 节点上，使用同一 GPT-2 124M authoritative vector、8 份 BF16 proposal 和 float32 syncer compute/publish 的五次设备基准显示：CPU `read+aggregation+outer` p50/p95 为 0.0914/0.2866 秒，CUDA 为 0.1771/0.1843 秒；CPU publication p50/p95 为 0.1645/0.1667 秒，CUDA 为 0.8033/0.9737 秒。CPU p95 远低于预先冻结的 4 秒门槛（20 秒参考 interval 的 20%），即使 CPU 冷读最大样本也只有 0.3345 秒。因此工程门禁允许进入“CPU syncer 与 learner_000 共置”的 8 节点实验，但在三 seed 完整训练中位数相对同 fingerprint 9 节点基线的劣化不超过 10% 之前，不改变默认部署。

原始证据位于 `reports/imp_plans/perf_fix-E/E5/artifacts/20260718-0647_snc01-existing-run-ledger-pass.json`、`20260718-0710_snc02-gpt2-cpu-pass.json` 和 `20260718-0713_snc02-gpt2-gpu-pass.json`。

### 专用 validation 协议对存量 checkpoint 的首批结果

新的主指标严格使用各 run resolved config 的 validation split、GPT-2 tokenizer、逐文本 EOS、1024 non-overlap blocks 和 causal shift 后 predicted-token 加权；四次结果的 protocol hash 完全一致。每个 checkpoint 均评估 243 blocks / 248,589 predicted tokens，结果如下：

| run / checkpoint | validation loss | ppl | learner local last-10 |
|---|---:|---:|---:|
| prediction F v50 | 3.094268 | 22.0711 | 3.183349 |
| prediction H v50 | **3.066410** | **21.4647** | 3.123984 |
| rebase-preserve v49 | 3.073351 | 21.6142 | **3.055005** |
| replace BF16/staleness-2 v48 | 3.091229 | 22.0041 | 3.189174 |

validation 与 local-loss 排序不一致：rebase-preserve 的 local last-10 比 H 低 0.06898，却在 validation 上比 H 高 0.00694。此前“local loss 最低”不能作为最终 checkpoint 质量结论，Q1/Q3 对混杂与记忆化的警告得到直接支持。H 在这四个存量点中 validation 最好，但这些 run 的训练源码身份缺失，版本、completion/grace/策略也不完全一致；因此只属于同批观察，不能据此把 prediction 设为默认。B2+Q3 后同 fingerprint、单变量、三 seed 的 PVE-04 已在后文完成，并据此保持 replace 默认。

完整协议位于 `reports/imp_plans/quality_fix-Q/validation_protocol.md`；结构化比较证据为 `reports/imp_plans/quality_fix-Q/Q4/artifacts/20260718-1020_pve02-03-legacy-comparison-pass.json`。

### 并行 publication 与 publish dtype 的三 seed 正式对照

E1 的 serial/parallel 9 节点 50×10 对照使用同一 source fingerprint、相同 seeds
1337/2027/4049，单变量为 `syncer.parallel_checkpoint_writes`。并行写使三 seed
平均 publication 从 0.8412 秒降到 0.4665 秒（-44.5%），checkpoint 阶段约降低
45.2%；完整训练时间没有一致改善，说明节点/共享盘噪声大于每 run 约十次 publication
所节省的秒数。双文件写完后才提交 DB/latest 的事务边界、两种单侧完成顺序、单侧失败与
六阶段 crash matrix 均保持通过，因此并行写继续作为默认。矩阵见
`reports/imp_plans/perf_fix-E/E1/artifacts/20260718-1605_pio-formal-matrix.csv`。

仅改变 `syncer.publish_dtype` 的 5000-step 三 seed 对照中，BF16 将 weight/outer
文件字节数各减半，但平均 publication 从 0.4118 秒升到 0.6690 秒（+62.5%），平均
完整训练时间从 1092.92 秒升到 1114.84 秒（+2.01%）。质量门禁通过：以 FP32
validation 的样本标准差 0.001345 得到 ε=0.01，BF16 的配对平均 degradation 为
-0.003588，最差 seed 为 -0.001483；round-trip relative-L2 的 slope CI 均为负，
后半/前半均值比为 .351/.420/.270。因性能没有收益，BF16 publish 只作为容量型 opt-in，
默认仍为 FP32。矩阵和门禁分别见
`reports/imp_plans/perf_fix-E/E1/artifacts/20260718-1610_publish-dtype-formal-matrix.csv`
与 `reports/imp_plans/quality_fix-Q/Q6/artifacts/20260718-1600_qgb03-corrected-formal-gate.json`。

### Fragment 全模型物化间隔的三 seed 消融

E3 在同一 source fingerprint 下比较 `materialize_full_every_events=1` 与 `10`，每档
seeds 1337/2027/4049。间隔 10 将每 run 周期物化次数 10→1、物化字节减少 90%、平均
物化墙钟 1.4713→0.2730 秒（-81.45%）；完整训练三 seed 均值 214.42→209.68 秒
（-2.21%），但单 seed 时间方向不一致，所以只能把 I/O 降幅视为强因果结果，不能承诺
固定端到端提速。生产 fragment profile 改为显式 10，debug/tiny 保持 1；所有正常终止仍
强制物化最终 fragment state。原始矩阵见
`reports/imp_plans/perf_fix-E/E3/artifacts/20260718-1620_mat-formal-matrix.csv`。

### Syncer 8 节点共置的三 seed 决策

E5 的同 fingerprint 配对结果如下；两种部署的三条作业均退出 0，CPU merge-compute p95
全部低于预冻结的 4 秒门槛。

| seed | 专用 9 节点 | 共置 8 节点 | 配对变化 |
|---:|---:|---:|---:|
| 1337 | 1129.74s | 1097.80s | -2.83% |
| 2027 | 1141.98s | 1150.39s | +0.74% |
| 4049 | 1120.80s | 1584.88s | +41.40% |
| 中位数 | **1129.74s** | **1150.39s** | **+1.83%** |

中位数满足“不劣化超过 10%”的门槛，故 8 节点 CPU-syncer/GPU-learner 共置 launcher 是
有效的容量节省变体。seed 4049 的大离群同时伴随 learner local-step 不均与较低 selected，
表明尾延迟风险尚未消失；默认部署因此仍为专用 9 节点。证据见
`reports/imp_plans/perf_fix-E/E5/artifacts/20260718-1500_snc03-formal-pairs.csv`。

### Staleness λ 与 fresh-only 的三 seed validation

Q2 的 12 条 train/eval 全部使用 source fingerprint `sha256:122f698...`、同一 evaluator
协议与 seeds 1337/2027/4049。

| 条件 | validation loss 三 seed 均值 | 相对 λ=.25 配对均值 | applied 利用率范围 |
|---|---:|---:|---:|
| λ=.25 | 3.056161 | — | 97.42–97.46% |
| λ=1 | **3.054640** | -0.001521 | 96.04–97.21% |
| λ=4 | 3.058688 | +0.002527 | 96.29–97.68% |
| fresh-only | 3.099213 | **+0.043052** | 61.84–78.68% |

λ=1/4 的全部质量差仍在 ε=.01 内，没有证据支持修改 λ=.25 默认值；fresh-only 的三个
seed 均恶化超过 ε，且丢弃大量旧版贡献，明确不应作为默认。seed 1337 的 per-merge
证据显示 λ=.25/1/4 的 effective staleness mean 为 .4751/.2947/.0795，fresh effective
weight 为 .5527/.7200/.9228，说明消融确实强烈改变了目标权重，却没有带来 validation
改善；因此 base-relative displacement 的研究优先级上调。运行矩阵、validation 与观察性
联动样例分别见 Q2 artifacts 的 `20260718-1605_quality-formal-matrix.csv`、
`20260718-1625_validation-formal.json` 和
`20260718-1630_staleness-observational-s1337.json`。

### Adoption 策略的三 seed validation 决策

Q4 的 rebase/prediction/replace 单变量矩阵使用同一 source 与协议：三 seed 平均 validation
loss 分别为 3.056161、3.053650、3.051954。prediction 相对 rebase 的配对均值为
-0.002511，replace 相对 rebase 为 -0.004207；所有单 seed 差均在 ε=.01 内，但 prediction
均值比 replace 高 0.001696。故 prediction 在当前 horizon 质量兼容，却没有表现出超过
replace 的优势；replace 保持默认，prediction 保持 opt-in。九条 `afterok` 独立一节点 eval
均验证了 243 blocks、248,589 predicted tokens、有限 loss/ppl、checkpoint SHA、source
identity 与原子 summary attachment。矩阵见
`reports/imp_plans/quality_fix-Q/Q4/artifacts/20260718-1610_strategy-formal-matrix.csv`。

### 当前 9 节点 adoption pause 基线

带 E6 新字段的 9 节点 50×10 run `e1_parallel_s1337_20260718` 中，每个 learner 都记录
10 次 adoption；平均停顿 0.3192–0.3359 秒，总停顿 3.1915–3.3587 秒，占明确记录的
completed-cycle elapsed 1.368–1.436%。这项成本可见但并非主导，不新增性能优化优先项。
结构化证据见
`reports/imp_plans/perf_fix-E/E6/artifacts/20260718-1625_nine-node-adoption.json`。

### Terminal partial merge 的三 seed predecessor/post 评估

Q5 的修正版工作负载移除可提前获胜的 global stop target，让八个 learner 均到达 local
step 5000 后再进入 input-closed drain。三个 9 节点 run 都以 `input_exhausted` 正常结束，
每次 terminal merge 都是 selected=3、`quorum_min=4`，并在合并前以 hardlink 冻结且校验
predecessor checkpoint。统一协议结果如下：

| seed | 版本 | predecessor loss | terminal loss | post-pre |
|---:|---:|---:|---:|---:|
| 1337 | 52→53 | 3.055333 | 3.055169 | -0.000164 |
| 2027 | 52→53 | 3.054235 | 3.054110 | -0.000125 |
| 4049 | 51→52 | 3.052862 | 3.052162 | -0.000700 |

配对均值为 -0.000330，最差 seed 仍为 -0.000125；相对预冻结 ε=.01 不仅未恶化，三条
都略有改善。因此 terminal small-quorum merge 在当前 horizon 没有可见质量损失，Q5 按
条件计划停止，不增加 `terminal_merge_outer_lr_scaling` 协议开关。结构化证据见
`reports/imp_plans/quality_fix-Q/Q5/artifacts/20260718-1640_terminal-paired-quality.json`。

### 仍需进一步研究

- 明确正式实验究竟以“每 learner 5000 local steps”、“达到 50 outer merges”还是两者联合为完成条件;当前 `global_only` 会在 v50 到达时停止尚未到 5000 的 learner;
- 若必须同时保证 fresh-only 与 v50，研究显式 round barrier；当前 fresh-only 三 seed validation 已否决其作为默认异步策略；
- telemetry 应同时报告 actual local tokens、applied tokens、produced/applied/dropped updates 和 merge count,避免把 `total_seen_tokens` 误读为总训练计算量;
- 为 rebase 增加 anchor 生命周期、proposal 最终状态、进程 RSS/内存峰值和临时向量峰值 telemetry,量化 dropped anchor 的所有权风险和 CPU 内存成本。
- 最终 heartbeat 摄取修复已由 H 的九节点 run 复验;后续继续断言 SQLite `learners.status`、磁盘 heartbeat 与 summary 的 stopped 状态一致。
- 持续监控 `global_prediction_preparation_recovered`;本次 H 未实际命中 GC-race recovery 分支,真实竞态下的等待时间和新版 adoption 仍缺少九节点动态样本。
- 进一步比较 base-relative displacement 与绝对参数平均；λ=1/4 已显著改变有效权重但没有改善 validation。
- 在更大模型/不同共享盘负载下复验 8 节点共置；当前 GPT-2 124M 中位数通过，但存在一个 +41.4% 离群 seed。

### 2026-07-17 scheduler 语义勘误

B2 实施确认历史 replace 与 preserve 路径不仅改变 optimizer moments，也曾改变 LR 轨迹：scheduler
重建会重复 warmup，preserve 则持续推进并可能在旧 `max_local_steps` horizon 后降到零。因此本文所有
修复前 local-loss 对比都不能作为 reset/preserve 的受控质量结论。当前 worktree 已改为累计 local-step、
独立 horizon 与正 LR 下限；修复后第一批 run 是新基线，不得与上述历史 run 直接比较。
