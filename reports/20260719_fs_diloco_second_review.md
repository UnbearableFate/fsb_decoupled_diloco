# 2026-07-19 fs_diloco 第二轮审查（对照 2026-07-17 报告的闭环核验 + 新发现）

审查范围：`fs_diloco/` 当前全部源码（runtime/learner.py、runtime/syncer.py、runtime/adoption.py、core/config.py、storage/*、protocol/*、modeling/hf_data.py 为重点）、`docs/` 全部 16 篇、`reports/imp_plans/`（bug_fixing S1–S5、bug_fix-B B1–B10、perf_fix-E、quality_fix-Q 的 progress/审计与部分 run 工件）、`tests/` 覆盖面抽查。静态检查：`python -m compileall -q fs_diloco tests` 通过（登录节点，按仓库规范未运行 pytest/torch）。

结论先行：**2026-07-17 报告的全部代码类问题（B1–B10、S1–S5、E1–E6、Q1–Q6 的代码部分）在当前源码中均已闭环**，与 `reports/imp_plans/20260718_perf-E_quality-Q_completion-audit.md` 的结论一致。本轮新发现 **1 个高严重度问题（N1：resume 与粘性 stopped 状态的交互使 DB-first 恢复实际不可用）**、1 个中等（N2）与若干低严重度问题；docs 存在 8 处与代码脱节，已在本轮直接修复（见 §4）。

---

## 1. 原审查问题闭环核验

逐项以当前源码为证据核验（行号为本轮审查时的位置）：

| 原编号 | 修复声明 | 本轮核验结果 |
|---|---|---|
| B1/S4 | stop 谓词统一 | ✅ `stop_requested`（learner.py:483-493）full/fragment 共用；stop.json 恒优先；`fragment_stop_requested` 已删除 |
| B2/Q1 | scheduler 解耦 | ✅ `inner_lr_multiplier`（learner.py:620-649）以累计 local step 计progress、`scheduler_total_steps` 独立 horizon（config 校验 cosine 时必填且 > warmup，config.py:484-493）、`min_lr_ratio` 下限（默认 0.1，范围 (0,1] 校验）；全部 7 类重建调用点均传 `completed_local_steps` 恢复相位（初始=0、adoption=当前 local_step）；LR 已进心跳与 learner_metrics.csv |
| B3 | 死配置字段删除 | ✅ `REMOVED_CONFIG_KEYS` fail-closed（config.py:18-25），旧键报"字段已移除"，prediction timeout 指向新路径；`load_resolved_config_snapshot` 只迁移已知移除键 |
| B4 | fragment terminal drain | ✅ fragment 主循环有 `input_closed` → terminal grace → `select_terminal_drain_fragment_updates` → `input_exhausted`（syncer.py:1784-1826） |
| B5 | maintenance 有界扫描 | ✅ 全量读归档 JSONL 的 `_archived_terminal_paths` 已消失；`gc_pending` 表与 active 行删除同事务（sqlite_store.py:950-989）；`maintenance_scanned_rows/gc_pending_rows/maintenance_scan_seconds` 进 syncer_metrics |
| B6 | GC 竞态通用防护 | ✅ `load_or_refresh_latest`（learner.py:411-474）覆盖初始 adopt（2037）、策略 direct/rebase 加载（经 AdoptionContext._load_latest）、prediction、fragment initial/incremental 全部加载点 |
| B7 | learner 侧 syncer watchdog | ✅ `SyncerProgressWatchdog` + `confirm_syncer_unresponsive`（learner.py:496-584），full/fragment 都接入，deadline 触发前重读 latest/stop 防误报，`syncer_unresponsive` 受控退出、心跳带 status_reason |
| B8 | shutdown 超时可配 | ✅ `liveness.learner_shutdown_timeout_seconds`，null → `max(120, 2×heartbeat)`，120s 上限已移除；超时事件逐 learner 列 status/last_seen（syncer.py:1502-1560） |
| B9 | ETA 单一时钟 | ✅ `UpdateFirstSeenRegistry` 进程内 monotonic 首见时钟（syncer.py:884-946），`fastest_next_upload_eta_seconds` 不再读 learner `committed_at`；resume 后旧 update 保守无 ETA |
| B10 | mid-cycle 元数据 | ✅ `MidCycleAdoptionTracker` + `write_update` 校验（learner.py:1293-1303,1344-1366），SQLite 列迁移幂等 |
| S1 | 采纳策略抽象 | ✅ `runtime/adoption.py`（无 torch 导入），run_learner 中 7 个平行状态变量与策略布尔清零；事件由 `finalize_strategy_action` 统一发出 |
| S2 | reconcile 去重 | ✅ `PredictionState`/`reconcile_prediction` 唯一实现，inner poll 与 cycle-end 共用；`tools/compare_event_traces.py` 交付在案 |
| S3 | full/fragment 去重 | ✅ `UpdateProposalSource` 参数化 grace 收集与缺文件降级；`apply_fragment_adoption` 统一四个采纳语境 |
| S5 | 配置分组与策略校验 | ✅ `learner.prediction.reconcile_timeout_seconds`；resolve 经策略类型表调用 class-level `validate`（config.py:516-518） |
| E1 | publish 并行/telemetry | ✅ `parallel_checkpoint_writes`（默认 true）双 worker、DB 提交在双写成功后；publish_weight/outer_seconds、bytes、roundtrip 误差三指标全落 metrics（但见 N3） |
| E2 | scan/publish 重叠 | ✅ `sync.ingest_during_publish` opt-in（但见 N3 的实现缺陷）；scan_interval 可 CLI 覆盖 |
| E3 | 物化缺省 fail-closed | ✅ `materialize_full_every_events` 必须正整数（config.py:505-511 与 should_materialize 双保险）；事件 0/达标/正常终止强制物化 |
| E4 | fragment 固定发现面 | ✅ 每 (learner,fragment) 固定 pointer + `fragment_proposal_frontiers` + 文件 signature 短路（syncer.py:949-1013）；不再 glob payload 目录 |
| E5 | syncer 节点成本 | ✅ `tools/analysis.syncer_resource_cost` + `run_8node_colocated_*.pbs` 存在；9 节点保持默认（离群 seed 证据在案） |
| E6 | adoption 停顿 telemetry | ✅ `adoption_load_apply_seconds/optimizer_reset_seconds/pause_seconds` 三段（learner.py:1250-1258, 1961-1994） |
| Q2 | staleness 证据字段 | ✅ `merge_staleness_evidence`（syncer.py:1684-1714）effective mean/fresh weight/count JSON，full/fragment 双口径 |
| Q3 | 数据 shuffle | ✅ `data.shuffle_blocks`（默认 true），epoch 级 splitmix64(seed, learner, epoch) 置换（hf_data.py:105-146），false 为旧行为锚点 |
| Q4 | validation 链路 | ✅ `tools/validation_eval.py` + `run_1node_validation_eval.pbs` + `submit_train_with_validation.sh`（afterok 解耦） |
| Q5 | terminal 前驱捕获 | ✅ `sync.capture_terminal_predecessor_for_eval` + `eval_checkpoints/` 非权威目录（syncer.py:101-170；但见 N7 边角） |
| Q6 | 质量门禁 | ✅ `tools/publish_quality_gate.py` 三态判定；publish_dtype 默认保持 float32 |
| P6 | 源身份 | ✅ `run_identity` 含 git_commit/git_dirty/source_fingerprint；`FS_DILOCO_REQUIRE_SOURCE_IDENTITY` fail-closed（config.py:360-374） |

P 系列其余（P1/P2/P4/P5/P7/P8/P9）为流程/文档类，其载体（plans/INDEX、validation 门禁、telemetry 先行）在 E/Q 执行审计中均有对应证据，不再逐条展开。

---

## 2. 新发现——代码问题（按严重度排序）

### N1（高）DB-first resume 与粘性 stopped 状态的交互：恢复的 run 会在首轮被误判 input_exhausted

**机制**（三个各自正确的部件叠加成错误行为）：

1. 上一代进程正常/watchdog 退出时，learner 写 `status=stopped` 最终心跳；该状态同时存在于**持久 SQLite `learners` 表**与**共享盘心跳文件**两处，且跨进程代际残留；
2. `classify_liveness` 把 stopped 设计为**粘性**——一旦 stopped 永不重分类（liveness.py:86-87）；
3. `resume_run` 只重置 selected 更新行、重建 latest（syncer.py:814-826），**不触碰 learners 表，也不清理旧心跳文件**；
4. 恢复后的主循环第一次迭代即计算 `input_closed = all_expected_learners_stopped(...)`（syncer.py:2348）——直接读 learners 表 → **恒为 True**；随后 terminal grace → `select_terminal_drain_updates`；由于上一代收尾已 finalize 全部 proposal，选择结果为空 → `stop_reason="input_exhausted"` → 发布 stop.json → **恢复的 run 在数秒内自行终止**，刚启动的 learner 看到 stop 即退出。

时序上几乎**确定触发**：resume 的 syncer 加载 checkpoint 后立即进入主循环，而 learner 的第一份 active 心跳要等模型加载 + 初始 adopt 完成（GPT-2 规模数十秒）。即使 active 心跳恰好在 terminal grace 期间被摄取，`select_terminal_drain_updates` 内部的 `all_expected_learners_stopped` 复检返回 False 后**仍返回空列表**，主循环同样把空选择解读为 `input_exhausted`——没有任何路径能回到正常合并。

**证据**：syncer.py:2348-2380（主循环）、syncer.py:1329-1343（drain 入口复检后返回 `[]` 的歧义）、liveness.py:86-87（粘性）、syncer.py:771-845（resume_run 无 learners 处理）；`tests/test_resume.py` 全部只测 `resume_run` 函数本身，无一条覆盖"resume 后主循环第一轮"的行为。

**影响**：plan 01 遗留 30% 的核心（learner/syncer 独立重启、进程级恢复实验）在当前代码上无法开展；这是 finish.md 排名第一的后续工作的直接阻塞项。

**建议**：(a) `resume_run` 中把 learners 表全部行重置为非 stopped（如 `unknown`，reason=`resumed`），并删除或忽略时间戳早于 resume 时刻的心跳文件（心跳本非权威，删除安全）；(b) `select_terminal_drain_updates` 把"输入已不闭合"与"输入闭合但无 proposal"区分为两种返回，主循环只在后者 `input_exhausted`；(c) 补 RED 测试：初始化→模拟一轮 stopped 收尾→resume→断言主循环首轮 `input_closed=False` 且不产生 stop.json。长期解仍是 plan 01 §3.6 的 incarnation identity。

### N2（中）predict 策略 reconcile 等待期间 stop 到达 → 正常停机被折叠成 TimeoutError 崩溃

`wait_for_latest_if_newer` 在 stop.json 出现时**提前返回 None**（learner.py:324），与预算耗尽不可区分；`PredictGlobalAdoptionStrategy.on_cycle_end` 对 None 一律 `raise TimeoutError`（adoption.py:532-533）。于是存在一个真实竞态：learner 在预测状态下走到 cycle-end 等待 reconcile，syncer 恰在此窗口达到全局目标并发布 stop（不再发布新版本）——learner 以未捕获异常退出（exit≠0，无 stopped 心跳），syncer 的 shutdown 等待随之超时、跳过 finalize，终态目录不满足有界性验收。`global_only` + predict 的**最后一个 cycle** 是天然触发场景（该 learner 的最终 proposal 被合并达标后不再有新版本可 reconcile）。

现有测试只覆盖"真超时保留状态供诊断"（test_adoption_strategy.py:225-236）与"等待前已见 stop 的 on_stop 放弃"（193-222），恰好漏掉两者之间的竞态窗口。

**建议**：等待返回 None 后检查 `ctx.paths.stop_json.exists()`：stop 在场则记录 `global_prediction_abandoned_on_stop`（复用 on_stop 语义）并返回无操作 StrategyAction，让 runner 的 while 条件自然退出；仅在无 stop 时保持 TimeoutError。补该竞态的单元测试。

### N3（低）`publish_global` 的 `during_checkpoint_wait` 恒非 None：条件表达式写进了 lambda 体内

syncer.py:2568-2578：

```python
during_checkpoint_wait=(
    lambda: sync_liveness_and_metadata(...)
    if config.sync.ingest_during_publish
    else None
),
```

Python 语法下条件属于 **lambda 体**，实参永远是一个 lambda 对象；`publish_global` 里 `while during_checkpoint_wait is not None` 恒成立。后果：即便 `ingest_during_publish=false`（默认），并行写 checkpoint 时主线程也走 0.2s 轮询循环而非阻塞等待，`publish_ingest_passes` 对所有默认配置 run 记出非零值——**telemetry 语义被污染**（"passes"本应表示实际摄取轮数）。行为面影响极小（lambda 体内条件为假时不执行摄取），E2 的正式对照两臂同受影响，方向性结论不变。

**建议**：改为 `during_checkpoint_wait=(lambda: sync_liveness_and_metadata(...)) if config.sync.ingest_during_publish else None`；补断言"flag 关闭时 `publish_ingest_passes == 0`"的回归（现有 test_parallel_publication 恰好未覆盖该字段的关闭态）。

### N4（低）stop 已在场时的最终发布后，predict/rebase 仍会构造新 reference

run_learner 在 stop.json 已存在、发布完最后一份 partial proposal 后仍无条件调用 `on_after_publish`（learner.py:2366-2374）。replace 的"直接采纳最终版本"是期望行为；但 predict 会完整执行一次 `prepare_prediction`（加载 weight+outer 各 ~500MB、真实 outer step），rebase 会做一次全参数 snapshot——随后 while 条件退出，全部丢弃。纯浪费，发生在每个带 stop 竞态的收尾 cycle。**建议**：`on_after_publish` 分派前检查 stop_json，predict/rebase 退化为只做一次直接采纳检查。

### N5（低）maintenance 中 `learner_*/*.meta.json` 清理分支已无生成者

全量与 fragment 的 proposal metadata 现均写在 `updates/latest/` 固定 pointer 上，payload 目录只有张量文件；maintenance.py:158-169 对 payload 目录 `*.meta.json` 的遍历是旧布局的遗留兼容。无害，但会误导读者以为仍存在按份 metadata。**建议**：删除该分支（或注释标注"仅清理 pre-E4 布局残留"），同步删除相应测试假设。

### N6（低）fragment learner 收尾等待期间心跳停更

`run_fragment_learner` finally 中的 final wait（learner.py:1846-1871）最长可达 `no_progress_timeout_seconds`，循环内不写心跳；超过 `dead_after_seconds` 后 liveness 会把一个仍在正常收尾的 learner 分类为 dead。因 terminal 判定只认 stopped，正确性不受影响，但观测（liveness 计数、排障）失真。**建议**：等待循环内按 heartbeat_interval 补写 active 心跳（phase=`final_fragment_wait`）。

### N7（低，记录在案）terminal 前驱捕获的 FileExistsError 边角

`maybe_capture_terminal_predecessor_for_eval`（syncer.py:138-144）：`os.link` 抛 FileExistsError 且 `samefile` 为假时直接标 `capture_method="copy"` 并对既有文件计算 checksum 写 manifest——若前次捕获在"写完 checkpoint、未写 manifest"窗口崩溃且文件内容与本次 source 不同（跨版本重试），会把错误内容登记为本版本证据。窗口极窄且该开关仅研究用。**建议**：samefile 为假时校验既有文件 checksum 与 source 一致，不一致则原子覆盖重拷。

### N8（低）input_closed 分支下的重复 discovery

full 与 fragment 主循环在 `input_closed` 为真、terminal drain 已完成选择后，仍执行一遍 `eligible_updates + drop_missing + select_one_per_learner`（syncer.py:2384-2402 / 1830-1844），结果 `one_per_learner` 在该分支从未使用。每次 drain 迭代多付一次 SQL 查询 + N 次 stat。**建议**：把该段移动到 `else`（非 input_closed）分支内。

---

## 3. 正面确认（本轮重点复核无问题）

- `commit_full_merge` 事务校验链（前驱、selected 状态、learner 唯一、future/stale、权重集合精确匹配、superseded/future/too_stale 同事务终态化）完整（sqlite_store.py:271-435）；
- `insert_update_metadata`/`insert_fragment_update_metadata` 的 frontier 重放短路 + latest-wins supersession + `INSERT OR IGNORE` 幂等组合正确；
- `delete_archived_rows` 的 gc_pending stage 与 active 行删除同事务（B5 的崩溃窗口封死）；
- `inner_lr_multiplier` warmup→cosine 接点连续（warmup 末步乘子 1.0，cosine progress 从 0 起），`LambdaLR(last_epoch=N-1)` 的相位注释与语义一致；
- 采纳策略 hook 顺序与 runner 的调用序一致；rebase 的 anchor 生命周期（before_publish 清 → after_publish 无新版才建 → on_newer_latest 消费即清）无泄漏；
- fragment 初始 latest 含全部片（initialize_fragment_run），`tokens_since_fragment_load`/`base_fragment_version` 无 KeyError 路径；
- `interval_breakdown` 的分量不重叠断言在两条主循环的计时归集下结构性成立。

---

## 4. docs 审查结果与已完成修复

docs 总体在 S/B/E/Q 修复轮中被同步维护，质量良好（watchdog、gc_pending、mid-cycle 元数据、materialize fail-closed、terminal capture、BF16 门禁结论均已入文）。本轮发现 8 处与当前代码脱节，**已直接修复**：

| 文件 | 问题 | 修复 |
|---|---|---|
| `docs/01-overview.md` | terminal drain 说"按最旧优先合并"（实际用配置的 `sync.selection_policy`，默认 most_recent_per_learner） | 改为"按配置的选择策略"，并注明 full/fragment 均覆盖 |
| `docs/02-architecture.md` §4.2 | `oldest_pending` 标注"仅 terminal drain 使用" | 删除错误标注，注明常规合并与 terminal drain 共用同一 selection_policy |
| `docs/03-runtime-flow.md` §4 | 伪代码沿用旧流程："quorum 不足→尝试 terminal drain、以 oldest_pending 选取"；实际是 input_closed 先行判定、drain 用配置策略 | 重写伪代码为 input_closed 优先分支 |
| `docs/modules/protocol.md` | 同上的 oldest_pending/terminal drain 错误关联 | 同步修正 |
| `docs/modules/runtime-learner.md` | ① parse_args 只列 5 个参数（实际 17 个）；② 称 fragment "metadata 仍以每份独立文件放在 payload 目录"（E4 后在固定 pointer）；③ write_heartbeat 签名缺 learning_rate/scheduler_total_steps/status_reason | 三处均已更新 |
| `docs/modules/runtime-syncer.md` | ① parse_args 参数列表过时；② fragment 主循环节仍写"quorum 不足只能等待或 no_progress_timeout（无 terminal merge）"——与同文件上文自相矛盾（B4 已实现） | 两处均已更新 |
| `docs/04-data-flow.md` | ① fragment payload 文件名模式写成 `<update_id>_f{FFF}...`（实际 `update_{uuid12}_fragment_{FFF}...`）；② 心跳字段清单缺 learning_rate/scheduler_total_steps/status_reason | 两处均已更新 |
| `docs/06-configuration.md` | 开头只提 `--run-id/--shared-root/--num-learners` 三个 CLI 覆盖 | 补全整组实验覆盖参数 |

未改动但确认正确的关键声明：06 的 scheduler/materialize/watchdog/timeout 字段语义、02 的通信契约 8 条、04 的目录布局与 SQLite schema 表、05 的模块结构与入口清单、07 的脚本清单（逐一与 `scripts/` 实物比对）、modules/core 的 `PROTOCOL_VERSION=3` 说明。

---

## 5. 建议处理顺序

1. **N1**（resume 交互）——阻塞独立重启实验线，建议按 loop-engineering 风格立一份小计划（RED：resume 后首轮不得 input_closed；含 drain 空选择歧义拆分）；
2. **N2**（predict stop 竞态）——单文件小修 + 一条单元测试，建议与 N4 同批（同为策略收尾语义）；
3. **N3**（telemetry 污染）——一行修复 + 回归断言；修复后 `publish_ingest_passes` 的历史数据解读需加注（修复前的值含空转轮数）；
4. N5–N8 为清理/观测项，可攒批处理，不单独立项。

## 6. 事实与推断边界

- N1 的机制链为**代码级确认**（每一环都有行号证据）；"数秒内终止"的时序是基于模型加载耗时的强推断，未做实际 resume 运行验证（登录节点限制）——N1 计划的 RED 测试即是验证载体。
- N2/N4 的竞态窗口为代码级确认，触发概率依赖 stop 发布与 cycle-end 的相位，未在既有 run 日志中回溯搜索实例。
- N3 的语法解析为确定事实（`lambda: A if C else B` 的结合性），对 E2 正式结论"方向不变"的判断基于两臂同受影响的对称性。
- §1 的闭环核验全部基于当前 worktree 源码与既有测试/审计工件，未重跑任何实验。
