# 2026-07-17 Plans 与 fs_diloco 综合审查

审查范围：`plans/`（00 研究计划、01 主线计划、imp_plans/01、01-1、AGENTS、AGENTS-2、todo）、`fs_diloco/` 全部源码（重点 runtime、storage、protocol、core、modeling）、以及 `reports/`（run_analysis.md、imp_plans/01 的 progress/failures/finish）作为对照证据。

审查目标按用户要求分四层：代码实现与逻辑正确性、设计模式与代码结构、plan 设计对 agent 实施效率的影响、整体系统设计对训练运行效率与准确率的影响。每条发现标注证据位置；确认的事实与推断分开表述。

---

## 1. 结论摘要

系统正确性底座（持久 SQLite 权威、单事务提交、crash matrix、current-only GC、terminal drain）确实已经接近闭环，plan 01 的测试矩阵和验证阶梯质量很高。当前最大的风险不在持久化协议，而在三处：

1. **学习率调度语义已经在污染所有质量对照**（见 Q1/B2）。`warmup_steps=100 == inner_steps=100` 加上每次 global adoption 重建 scheduler，意味着 replace 模式的 learner 几乎全程处于 warmup 锯齿中，从未按配置的 cosine 曲线训练。preserve-state 实验显示的 loss 改善，很大程度上是在测量"少踩这个坑几次"，而不是在测量 rebase/prediction 本身。`todo/cosine_scheduler_decoupling.md` 已识别此问题但尚未排期——它应当先于一切后续质量对照。
2. **一个会直接改变实验行为的逻辑 bug**：fragment learner 在 `local_or_global` 模式且设置了 `max_local_steps` 时完全忽略 `stop.json`（见 B1），`configs/fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml` 正好命中该组合。
3. **实验设计缺少受控性约束**：run_analysis 中反复出现"不同节点、不同代码版本、非单变量、不能归因"，且"两次运行间没有记录可复现的源码 commit"。这不是执行失误，而是 plan 模板没有把"冻结 commit + 单变量 + 多 seed + 固定 validation 评估"写成硬性门槛。

下表为全部发现的索引，按严重度排序：

| ID | 类别 | 严重度 | 一句话描述 |
|---|---|---|---|
| B1 | bug | 高 | fragment learner 在 `local_or_global`+`max_local_steps` 下忽略 stop.json |
| Q1/B2 | 准确率 | 高 | cosine scheduler 以 `max_local_steps` 为分母且随 adoption 重置 → LR 锯齿；`global_only`+preserve 组合下超过 horizon 后 LR≈0 |
| P6 | plan | 高 | 实验无 commit 记录、非单变量、无多 seed —— 结果大量不可归因 |
| P7 | plan | 高 | eval harness 已存在但未纳入任何 run 验收链，"缺 validation perplexity"成为每节分析的固定结尾 |
| P1 | plan | 高 | 完成语义（5000 steps vs 50 merges）未预先定义，消耗多个 9 节点作业迭代补救 |
| B5 | 有界性 | 中 | maintenance 每轮全量读 `update_history.jsonl`，热路径 O(history)，违反自家 BND 不变量 |
| B4 | 逻辑 | 中 | fragment syncer 无 input-closed/terminal drain，learner 全停后要等满 1 小时 no_progress_timeout |
| B3 | 配置 | 中 | `reset_on_global_update`/`upload_mode`/`quorum_policy` 为未消费配置，resolved snapshot 记录无效字段 |
| B6 | 健壮性 | 中 | current-only GC 竞态只在 prediction 路径修复，`adopt_global`/rebase 加载权重仍无重试 |
| B7 | 健壮性 | 中 | `global_only` learner 对 syncer 无声死亡（无 stop.json）没有任何防护 |
| E1 | 效率 | 中 | 每次 merge 同步写 FP32 weight+outer 约 1GB 在发布关键路径上 |
| E2 | 效率 | 中 | round-based batching 是 supersession 的结构性来源；E/G 两轮实验证明调 grace 只是折中而非解法 |
| Q2 | 准确率 | 中 | 绝对参数平均混合不同 base 的快照；G run staleness 64% 伴随 loss 回退与此一致 |
| Q3 | 准确率 | 中 | WikiText-2 每 learner 分片约 0.29M tokens，5000 步约 280 epochs 固定顺序无 shuffle，local loss 主要是记忆化信号 |
| S1–S5 | 结构 | 中 | learner 三策略内联巨型函数、full/fragment 大面积重复，每轮实验都在同一处高风险改动 |
| E3 | 效率 | 中 | fragment 的 50×10 配置 `materialize_full_every_events=1` 抵消了分片的 payload 节省；配置缺省值等价于每事件物化 |
| B8 | 健壮性 | 低 | `wait_for_learner_shutdown` 上限硬编码 120s；超时则 pending 不 finalize、目录残留 |
| B9 | 遥测 | 低 | adaptive ETA 混用 learner 与 syncer 两个节点的 wall clock |
| B10 | 语义 | 低 | replace+inner-poll 下 mid-cycle adoption 使 proposal 的 base 与其前半段训练不一致 |
| P2–P5, P8–P9 | plan | 低–中 | 文档冗余、失效链接、predictor 无 plan、telemetry 先行原则未执行等 |
| E4–E6, Q4–Q6 | 效率/质量 | 低–中 | fragment 发现面、syncer 节点占用、prediction 启发式验证等 |

---

## 2. 代码实现与逻辑问题

### B1（高）fragment learner 忽略 stop.json

`fs_diloco/runtime/learner.py:337`：

```python
def fragment_stop_requested(paths, local_step, config):
    if config.training.completion_mode == "global_only":
        return paths.stop_json.exists()
    if config.training.max_local_steps is not None:
        return local_step >= config.training.max_local_steps   # 到达前从不看 stop.json
    return paths.stop_json.exists()
```

对比 full 版 `stop_requested`（learner.py:324）总是先检查 `stop_json.exists()`。后果：在 `local_or_global` 模式且设置了 `max_local_steps` 时，syncer 到达 outer 目标发布 stop 后，fragment learner 会继续训练直到本地 5000 步才退出——白白消耗 GPU，且拖长 shutdown wait 与完整训练时间统计。`configs/fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml`（`max_local_steps: 5000`，未设 completion_mode → 默认 `local_or_global`）正好命中。已跑完的 fragment 50×10 用的是 `max_local_steps: null`，所以未暴露。

**建议**：与 full 版合并为一个函数：`stop_json.exists() or (completion_mode != "global_only" and local_step >= max_local_steps)`。补一个"fragment learner 在 stop 发布后一个 cycle 内退出"的单元测试。

**修复状态（2026-07-17）**：已由 commit `dab45e8` 合并为 full/fragment 共用的 `stop_requested`。STP-03 单元回归修复前 RED、修复后 GREEN；1 节点 tiny fragment 管线中两个 learner 均在 `local_step=10 < max_local_steps=12` 看到 `stop_after_outer_steps` 后退出。

### B2（高）scheduler 语义缺陷（训练质量影响见 Q1）

`fs_diloco/runtime/learner.py:362-368`：

```python
def lr_lambda(step):
    if warmup_steps and step < warmup_steps: return ramp
    if scheduler == "cosine" and max_local_steps:
        progress = min(1.0, step / max(1, max_local_steps))
        return 0.5 * (1.0 + cos(pi * progress))
```

三个叠加问题：

1. `step` 是 scheduler 重建后的相对步数。replace 模式每次 adoption 重建（learner.py:1642、1896），正式配置 `warmup_steps=100 == inner_steps=100`，adoption 大约每个 cycle 一次 → learner 几乎所有时间都在 warmup 斜坡上（LR 从 0.01×base 锯齿到 1.0×base 再归零），配置的 cosine 衰减从未真正生效。
2. `max_local_steps=null` 时 warmup 后退化为常数 LR（todo 已记录）。
3. **`global_only` + preserve-state 的组合陷阱**：H/G 型实验中 learner 会训练超过 `max_local_steps`（run_analysis 记录到 step 5100）。一旦 scheduler 状态被保留（rebase-preserve 语义）且 epoch 超过 `max_local_steps`，`progress` 被 clamp 到 1.0，LR 乘子恰好为 0——超过 horizon 的所有步在近零学习率下训练，仍会发布"看起来正常"的 proposal。run_analysis 建议的下一步实验（rebase-preserve + global_only）**正好会触发这个组合**，且不会有任何报错。已完成的 `codex_rebase_preserve_full5000` 因为是 `local_or_global`（5000 即停）恰好躲过；H 因为 prediction-start 会重置 scheduler 也躲过。

**建议**：在跑任何 rebase-preserve + global_only 实验之前完成 `todo/cosine_scheduler_decoupling.md`：引入 `inner_optimizer.scheduler_total_steps` 独立 horizon；明确 scheduler 进度用累计 local step 还是 adoption 后相对 step；LR 乘子加下限（如 `min_lr_ratio`）避免 clamp 到 0。

### B3（中）未消费配置字段

- `inner_optimizer.reset_on_global_update`（config.py:118）：全仓库无任何消费点（已 grep 确认）。实际 reset/preserve 行为由 adoption strategy 硬编码。resolved config snapshot 里记录着一个不生效的 `reset_on_global_update: true`，对照实验读配置时会得出错误结论。todo 文件也点名了此项。
- `sync.upload_mode`（config.py:66）、`liveness.quorum_policy`（config.py:92）：同样无消费点。

**建议**：要么删除，要么让其成为 preserve/reset 语义的真实开关（后者与 run_analysis "为 reset/preserve 增加同一代码版本下的显式消融开关" 的诉求一致，推荐）。

### B4（中）fragment syncer 缺少 input-closed / terminal drain

`run_fragment_syncer`（syncer.py:1161-1557）主循环只有 `fragment_quorum_wait + no_progress_timeout`，没有 full 路径的 `all_expected_learners_stopped → terminal drain → input_exhausted` 分支（syncer.py:1642-1662 仅在 full 循环中）。fragment learner 全部到达 local horizon 停止后，syncer 会空等满 `no_progress_timeout_seconds`（正式配置 3600s）——这正是 plan 01 在 full 上花大力气修掉的 "A run 4872s" 问题在 fragment 上的原样复刻。plan 01 只声明 fragment resume 出范围，terminal drain 未被显式排除，属于范围描述与实现的缝隙。

**建议**：把 input-closed 判定与 terminal drain 从 full 循环提炼为共享函数后接入 fragment 循环；或在 plan 中显式记录"fragment terminal drain 属于下一轮"，避免下次 fragment 5000-step 实验踩一小时空等。

**前置重构状态（2026-07-17）**：commit `777e913` 已证明并保留共享 `all_expected_learners_stopped` 的 exact-set 语义，同时把 full/fragment grace 收集与缺文件降级收敛为 `UpdateProposalSource` 参数化骨架。fragment 主循环按 S3 的行为保持边界仍未接入 input-closed。B4 剩余工作只有接线、terminal drain 选择/提交语义与对应 RED/管线测试，规格见 `plans/followups/B4-fragment-terminal-drain.md`。

### B5（中）maintenance 热路径 O(history)

`fs_diloco/storage/maintenance.py:140` 每次 `run_maintenance`（即每次 merge 后）调用 `_archived_terminal_paths(paths.update_history_jsonl)`，逐行读取**整个**归档 JSONL 来重建 terminal payload 路径集合。归档文件随历史线性增长（每 merge 约 +8 行），因此第 N 次 merge 的 maintenance 成本 ~O(N)，累计 O(N²)。这直接违反 plan 01 自己的不变量（"active discovery 和单次操作成本不依赖历史 update 数"）。BND-10/11 的 1000-cycle 测试只断言了 SQLite 行数与 page 数，没有覆盖 maintenance 的文件扫描成本，所以这条从测试矩阵漏了出去。当前 5000-step 规模（约 400 行）无感知，但长运行/更频繁 merge 下会变成结构性退化。

**建议**：terminal path 集合不需要从归档反推——`archive_and_prune` 已经在返回值里带出本轮 `terminal_paths`（maintenance.py:58），历史部分只需在 GC 时对"文件已不存在"幂等跳过即可；或维护一个小型 `gc_pending` 表/游标，记录已归档但尚未删除 tensor 的路径，删除后清除。同时给 BND 系列补一条"maintenance 扫描行数/耗时不随 cycle 增长"的断言。

### B6（中）current-only GC 竞态只修了 prediction 路径

`prepare_prediction_or_find_newer_latest`（learner.py:702-782）为 prediction 增加了 cached checkpoint 被 GC 回收时的 bounded retry。但同类竞态在其它加载点仍未防护：

- `adopt_global`（learner.py:435）与 rebase 的 `load_global_weights_flat`（learner.py:488）在读到 latest 后加载 weight 文件。因为读取的是刚出炉的最新版本，窗口只有"下一次 merge 完成 + GC"整个 interval（约 20s），当前风险很低；但 interval 缩短（adaptive grace 方向）、存储变慢或 learner 停顿（GC 暂停/换页）时窗口会闭合。
- resume/初次加载 `wait_for_json(paths.latest_json)` 后的 `adopt_global`（learner.py:1437-1443）：learner 启动慢于两次 merge 时理论上可命中。

**建议**：提炼一个通用的 `load_or_refresh_latest(path, base_version, ...)` helper（FileNotFoundError → 有界等待更新的 latest → 重读），让所有全量权重加载走同一路径；prediction 的专用逻辑退化为该 helper 的调用方。这同时消除 S2 的重复。

### B7（中）`global_only` learner 对 syncer 死亡无防护

`stop_requested`（learner.py:324-334）在 `global_only` 下只认 stop.json。若 syncer 被 SIGKILL/OOM 而没有走到 finally 的 `publish_stop`，8 个 learner 会永远训练并发布无人消费的 proposal，直到 PBS walltime 强杀——没有任何日志级别的告警。full 的 `local_or_global` 有本地 horizon 兜底，`global_only` 没有。

**建议**：learner 侧加一个廉价的对称 liveness：`latest.json`/`stop.json` 都超过 `no_progress_timeout_seconds` 无变化即自行退出并记录 `syncer_unresponsive`。这与 plan 01 §3.6 "learner 独立生命周期" 方向一致，实现成本很小。

### B8（低）`wait_for_learner_shutdown` 超时硬编码

syncer.py:1018：`timeout = max(30, min(120, 2×heartbeat_interval))`。超时后 `all_learners_stopped=False` → `finalize_unconsumed_updates` 被跳过（syncer.py:1900）→ 残留 pending 行和 payload（引用驱动 GC 会正确地保留它们），终态目录不再满足 BND-14。当前模型 20 秒左右能收尾，问题未暴露；更大模型的最后一次 adoption + 退出可能超过 120s。G run 暴露的"SQLite learners 表 stale"问题正是在这个函数里修的，说明该路径已经出过一次问题。

**建议**：超时改为可配置（默认取 `max(120, 2×heartbeat, 平均 cycle 时间×2)`），并在超时分支明确记录哪些 learner 未确认。

### B9（低）adaptive ETA 混用跨节点时钟

`fastest_next_upload_eta_seconds`（syncer.py:685-694）用 learner 写 metadata 时的 `committed_at`（learner 节点 wall clock）加 cycle 估计，减 syncer 节点的 `time.time()`。HPC 内 NTP 偏差通常在亚秒级，但该函数收紧的是秒级 deadline（run_analysis：估计剩余 1.5–9.9s），时钟偏差直接进入决策。同样，run_analysis 里的 commit→selection age 等指标也混用两端时钟。

**建议**：至少在文档/字段名中标注 cross-node wall clock 语义；如需更稳，改用 syncer 侧观察到 pointer 变化的 `ingested_at` 作为参考起点（单一时钟源）。

### B10（低）mid-cycle adoption 后 proposal 的 base 语义

replace + `poll_latest_during_inner_steps` 下，inner step 中途 adoption 会把 `base_global_version` 更新为新版本（learner.py:1630），但 cycle 前半段是在旧 base 上训练的，`tokens_this_update` 仍统计整个 interval。proposal 是绝对参数快照所以数值上无害，但 staleness 加权和 `tokens_since_global_load` 的解释会轻微失真。当前正式配置里 replace+poll 组合未使用，风险为零；记录在案以防未来组合。

---

## 3. 设计模式与代码结构

这一节的问题不是"错"，而是它们直接推高了每轮实验的实现与验证成本——reports 里那些逐条对账日志计数（"414 次 reset = 8 初始化 + 341 prediction + 65 直接 adoption"）之所以必要，正是因为状态机分散在一个巨型函数的分支里，无法用类型/结构保证。

### S1（中）`run_learner` 巨型函数 + 三策略内联

`run_learner`（learner.py:1411-1970，约 560 行）内联了 replace / rebase / prediction 三种 adoption 策略的全部状态：`rebase_reference_flat`、`carried_delta_tokens`、`last_published_anchor_update_id`、`prediction_reference_flat`、`prediction_carried_tokens`、`prediction_update_id`、`prediction_base_version` 七个平行变量加两个布尔开关，靠 if/elif 链维持互斥。每加一种策略（这已经发生了两次）都要在 4 处以上插入分支：inner-step poll（1546-1647）、cycle 末 reconcile-wait（1649-1716）、publish 前防御（1729-1734）、publish 后 adoption（1820-1943）。

**建议**：提炼 `GlobalAdoptionStrategy` 接口（`on_after_publish(latest_found)` / `on_newer_latest(latest)` / `wants_inner_poll()` / `on_cycle_end()` / `on_stop()`），replace/rebase/predict 各自成类，携带自己的 reference/token 状态。收益：新策略只写一个类；`inner_optimizer_reset` vs `inner_training_state_preserved` 的事件语义由基类统一发出，报告对账从"数日志"降为"看单元测试"。这是对后续 agent 实施效率回报最大的一项重构。

**完成状态（2026-07-17）**：commit `4ce7262` 增加独立策略状态机与单元测试；commit `f2c6961` 将三种 full adoption 路径统一经工厂、hook 与 `StrategyAction` 收尾，`run_learner` 中原 7 个并行状态变量和 2 个策略布尔均已清零。replace/predict 可重复轨迹与基线一致；rebase 用受控 latest-read 轨迹和真实 tiny 终态不变量验收，避免把 publish 后即时 poll 的合法竞态误判为回归。

### S2（中）prediction reconcile 两处近似重复

learner.py:1556-1589（inner poll 内）与 1657-1716（cycle 末等待）是同一段 reconcile 逻辑的两份拷贝（差异只有 `reconcile_waited_seconds` 字段）。B6 的通用 helper + S1 的策略类可一并消除。

**完成状态（2026-07-17）**：commit `19414a1` 抽取显式 `PredictionState`/`reconcile_prediction`，并加入可复用的 profile-driven 逐 actor 事件轨迹比较器；S1 随后把 reconcile helper 迁入无 torch 导入的策略模块，inner poll 与 cycle-end wait 共用同一实现。

### S3（中）full/fragment 双份主循环与工具函数

- `collect_with_grace_window` vs `collect_fragment_with_grace_window`（syncer.py:725-882）逐行平行；
- `drop_missing_update_files` vs `drop_missing_fragment_update_files`（787-812）；
- full/fragment 两个 syncer 主循环共享约 70% 结构（selection→read→merge→outer→publish→mark→maintenance→metrics）；
- learner 侧 `run_fragment_learner` 内 fragment adoption 块出现三次（inner poll 1096-1132、upload 后 1263-1306、final wait 1332-1381）。

后果之一就是 B4：terminal drain 修在 full 循环里，fragment 循环没有同步获得。**建议**：至少把"quorum 收集 + 缺文件降级"与"input-closed 判定"参数化共享；fragment adoption 块提炼为单个函数。

**完成状态（2026-07-17）**：commit `777e913` 增加 `UpdateProposalSource`，full/fragment 共享唯一 grace-window 与缺文件降级实现；四个 fragment adoption 语境共用 `apply_fragment_adoption`，并以显式参数保留 token、optimizer reset 和事件字段差异。`all_expected_learners_stopped` 保持共享 exact-set 实现，fragment terminal-drain 接线仍属于 B4 后续语义变更。

### S4（低）`stop_requested`/`fragment_stop_requested` 应合并

见 B1。两个函数语义本应只差 completion_mode 解释，分开维护直接导致了不一致。

**完成状态（2026-07-17）**：commit `dab45e8` 已删除 `fragment_stop_requested` 并统一两个 learner 主循环的停止判定。

### S5（低）配置分组与校验位置

`resolve_config`（config.py:248-355）承担全部跨字段校验，策略专属参数（prediction timeout、post-publish wait、rebase 开关）平铺在 `LearnerSection`。随策略数量增长建议按策略分组（如 `learner.rebase.*` / `learner.prediction.*`），校验挪到各自策略类的 `validate(config)`。低优先级，可与 S1 同批做。

其余为正面确认：`commit_full_merge` 的事务校验（predecessor、selected 状态、重复 learner、future base、staleness 边界，sqlite_store.py:243-407）与 `insert_update_metadata` 的 frontier 去重 + latest-wins supersession（492-593）实现干净、边界完整；`atomic_io` 的 temp+rename+fsync 与 payload-first/metadata-last 协议一致；crash matrix failpoint 的注入位置（publish_global，syncer.py:185-237）与 plan 六阶段一一对应。

---

## 4. Plan 设计审查（影响 agent 实施效率）

先说结论：plan 01（imp_plans/01.md）是一份高质量的可实施计划——权威链、故障模型、默认决策、带稳定 ID 的测试矩阵、九级验证阶梯、三值 Checker 契约俱全，reports 里的执行轨迹（一次 lint 失败、两次 smoke 失败、零次三连败升级）证明了它的有效性。AGENTS-2.md 又把执行中踩的坑（qsub Exit_status=0 假阳性、orphan grace 与 input-closed 的区分、逻辑/物理有界分测）提炼成了规则。以下问题是在这个较高基线上的改进点。

### P1（高）完成谓词缺失是本轮最贵的 plan 缺陷

plan 01 精确定义了 terminal drain 的机制，但从未定义"这个 5000-step 实验成功的谓词是什么"。结果：v25/v48 `input_exhausted` 之后 Checker 按 v50 判 `BLOCKED`，接着 E（wait 2.5s）、F（predict）、G（adaptive+global_only）、H 一连串 9 节点作业实质上都在补救"如何到 v50"，而 `global_only` 又引出"不再保证每 learner 5000 步"的新歧义——run_analysis 和 finish.md 都把"明确完成条件"列为第一遗留项。每个 9 节点作业约 20 分钟 walltime ×9 节点，这是本轮最大的可避免开销。

**建议**：给 plan 模板（AGENTS-2 §8）增加强制章节"完成谓词"：以配置字段显式声明（如 `completion: all_learners_local_horizon AND global_target` / `global_target_only` / `local_horizon_only`），Checker 直接读取该字段而不是事后由人指定 `--expected-version`。syncer 相应支持联合停止条件（run_analysis 已建议 "global target AND all learner local horizon"，代码上是 full 主循环 break 条件的小改动）。

### P6（高）实验受控性没有制度化

run_analysis 几乎每节都有免责声明："两轮使用了不同计算节点""没有记录可复现的源码 commit""不是只改单一变量的严格对照""F 与 H 的 completion mode 不同"。这些不是分析者的失误，而是提交实验的流程没有强制：

1. run identity（`run_identity()`，syncer.py:120-128）不包含 git commit hash 与 dirty 标志；
2. 没有"对照组必须与实验组同 commit、同 completion mode、单变量"的提交前检查；
3. 多 seed 从未执行（所有 run seed=1337）。

**建议**：(a) `run_identity` 与 resolved config 加入 `git_commit`/`git_dirty`（launcher 里 `git rev-parse HEAD` 一行）；(b) AGENTS-2 §6.4 的提交前检查表加两条："对照 run 的 resolved config diff 已审查，差异仅含目标变量"、"作业启动后代码已在该 commit 上打 tag 或记录"；(c) 质量结论必须 ≥3 seeds 才允许写入 run_analysis 的结论段。

### P7（高）validation 评估未纳入验收链

`fs_diloco/tools/eval_lm_harness.py` 与 `scripts/miyabi/run_1node_lm_eval.pbs` 已存在，但没有任何 run 的验收流程调用它。于是"对最终 checkpoint 做相同 validation loss/perplexity"在 run_analysis 的"仍需进一步研究"里出现了 6 次，成为每个实验的固定欠账。对 124M 模型 + WikiText-2 validation，评估成本是分钟级的。

**建议**：9 节点 PBS 脚本在 syncer 正常退出后（或 launcher 尾部）自动对 `latest.json` 指向的 checkpoint 跑固定 validation set，把 `validation_loss/ppl` 写入 `control/summary.json` 与 syncer metrics。这一项能把后续所有策略对照从"local loss 不可比"直接升级为"有统一质量数"。注意与 current-only GC 的交互：评估要在 stop 后、目录冻结时进行，checkpoint 不会再被 GC。

### P2（中）plan 文档层级冗余、入口过多

一个新 agent 要理解现状需要读：根 `AGENTS.md` → `plans/imp_plans/AGENTS.md` → `plans/imp_plans/AGENTS-2.md`（500 行）→ `00-RESEARCH_PLAN.md` → `01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md` → `imp_plans/01.md` → `01-1.md`，约 2500 行，其中 00 与 01 的 bounded-state/terminal-drain 论述有大量重叠，AGENTS-2 与 imp_plans/AGENTS.md 也部分重复（失败记录规则）。没有任何文件声明优先级/冲突时以谁为准。

**建议**：在 `plans/` 加一个 30 行以内的 `INDEX.md`：文档清单、各自角色（研究方向 / 设计展开 / 实施规格 / 执行规范）、优先级顺序、当前 active plan 指针。把 AGENTS-2 中"规则"（必须遵守）与"经验叙述"（背景）分节标注，agent 可只精读规则节。

### P3（低，立即可修）失效引用与错位文件

- `reports/imp_plans/01/finish.md` 和 `plans/01.5-chat_log.md` 都引用 `plans/FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md`——该文件已改名为 `01-FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md`，链接失效。
- `plans/01.5-chat_log.md` 内容与 `reports/imp_plans/01/finish.md` 完全相同（33 行），既非 chat log、又违反 AGENTS-2 §7.5 "run 分析结果属于 reports 不属于 plans" 的分工。**建议**：删除 `plans/01.5-chat_log.md`（或替换为指向 finish.md 的一行链接），修正两处路径。

### P4（中）predictor 策略没有 plan/设计文档

rebase 有 `01-1.md`（状态机、配置约束、风险、事件表俱全，质量很好），但 `predict_post_publish_global`——学习率预测公式、momentum→displacement 代理、`estimated_total_tokens` 启发式、GC-race recovery——只存在于代码（learner.py:555-782）和 run_analysis 的事后描述中。这违反了本仓库自己建立的"先规格后实现"循环，也让 H run 的对照解释花了额外力气（F 与 H 的语义差异要靠读代码还原）。同样，"reconcile 保留 optimizer/scheduler"这个语义变更（影响 rebase 与 predict 两条路径）也没有对应的 plan 增量。

**建议**：补一份 `imp_plans/01-2.md`（predictor + preserve-state 语义），哪怕是事后补写——它将是下一轮消融实验（reset/preserve 显式开关，run_analysis 最后一条建议）的规格底稿。

### P5（中）"telemetry 先行"原则在 01-1 执行中被跳过

AGENTS-2 §3 明确要求验收所需 telemetry 在昂贵实验前实现；`01-1.md` §6 也写了"后续实验需要统计 learner CPU 内存峰值"。但 rebase 的 9 节点 run 跑完后，run_analysis 只能写"本 run 没有 RSS/内存峰值 telemetry，因此不能定量验收 CPU 内存成本"，且该欠账在后续 preserve run 中仍未补上（run_analysis 再次列入待办）。anchor 是每 learner 475MiB 的 CPU 常驻，规模上去后这正是要出事的指标。

**建议**：把"RSS/anchor 生命周期 telemetry"列为下一次 rebase/predict 类实验的启动门禁（G0 检查项），而不是 follow-up。

### P8（低）需求→测试→证据链路只建了一半

plan 01 的测试矩阵有稳定 ID（DB-xx/TX-xx/RES-xx/BND-xx/TERM-xx），但 `progress.md` 的记录基本不回引这些 ID（只有一处 "BND-01/02/03/04/07/10/11/12"）。Checker 输出也不按 ID 报告。**建议**：progress 记录模板加一列"覆盖的测试 ID"，Checker evidence JSON 按 ID 键控——审阅者核对遗漏的成本会显著下降。

### P9（中）todo 的优先级与实验路线错位

`todo/cosine_scheduler_decoupling.md` 内容准确、验收标准清晰，但它还躺在 todo 里，而受它污染的质量对照实验（C–H 六轮）已经跑完并写进了结论。按 Q1 的分析，它应当被提升为 `imp_plans/02`，排在"reset/preserve 消融"与"validation 对照"之前——否则下一批实验的 loss 差异仍然无法归因。

---

## 5. 系统设计审查——训练运行效率

### E1（中）发布关键路径上的 FP32 双 payload

每次 merge，syncer 同步写 `global_vN.safetensors` + `outer_vN.safetensors` 各约 498MB FP32（publish_dtype 默认 float32，config.py:83），然后才能提交 DB、切 latest（publish_global，syncer.py:167-238）。learner 上传已减半到 BF16，但发布侧仍是每 merge 约 1GB 串行写。run_analysis 也把"FP32 global/outer publication"列为端到端主耗时之一。

可选改进（按侵入性排序）：
1. **BF16 publish 实验**：`syncer.publish_dtype=bfloat16` 已实现且有 `align_state_to_publication_dtype`（syncer.py:89-104）保证"发布值即权威值"，`fs_diloco_gpt2_wikitext2_8l_5000steps_predict_bf16all_cuda.yaml` 似乎已备好配置但 run_analysis 无对应结果。注意质量风险：theta 每个 outer step 都经 bf16 round-trip，属于累积量化，必须配 validation 对照（见 P7）。
2. **weight 与 outer 并行写**：两文件互不依赖，提交点在 DB；用两个线程写可省约一半发布墙钟。
3. **outer state 延迟发布**：多数 learner 只读 weight；只有 prediction 路径读 outer。若 latest 分两阶段（weight-ready → outer-ready），可让 learner 提前 adoption。侵入协议语义（theta==outer theta 校验、crash matrix 需扩展），建议只有在 profile 证明必要时再做。

### E2（中）round-based batching 是 supersession 的结构性来源

已有的实验把这条线摸得很清楚：B（fresh-only）52.5% 浪费 → C（staleness=2）93% 利用率但 fresh 比例仅 7% → E（upload 后固定等待）无效且更慢 → G（短 grace）消灭 supersession 但 staleness=1/2 占 64% → F/H（prediction）99% 利用率但训练在预测权重上。这一系列结果的共同根因是：syncer 以"收齐一批→合并→发布"为节拍，而 learner 以自己的 cycle 为节拍，二者相位差被 grace 窗口吸收，吸收不了的就变成 supersession 或 staleness。

plan 层面建议把下面两个候选列入 00 计划 §5.3/5.4 的对照矩阵，而不是继续在 grace 参数上扫描：

1. **发布流水线化**：merge 计算+发布（约 2–3s I/O）期间 syncer 不做 ingestion；把"写文件"与"收集下一批"重叠（发布线程化）后，global interval 的下限从 `grace + merge + publish` 变为 `max(grace, publish)`。
2. **事件化 ingestion**：目前 scan_interval=2s 轮询 + grace 内 sleep（collect_with_grace_window，syncer.py:775）。8 个固定 pointer 的 stat 成本足够低，可以把 scan 降到 0.2s 量级，让 quorum_max 提前触发的概率提高（G run 有 27/50 次靠 quorum_max 提前结束，说明缩短检测延迟直接转化为吞吐）。

### E3（中）fragment 的物化策略抵消其收益

`should_materialize_fragment_full`（syncer.py:275-284）：`materialize_full_every_events=None 或 ≤0` 时**每个事件都物化**完整 FP32 checkpoint——"不配置"的缺省是最贵的行为，与直觉相反。且 50×10 的 fragment 配置显式写了 `=1`：每次 fragment merge（本应只写 63MB）附带写 498MB 物化全模型。这足以解释 50×10 中 fragment 比 full 慢 32% 的相当一部分，也让"fragment 减少 I/O"的对照失真。00 计划 §4.4 把等待列为 fragment 主因（timing breakdown 证据），但物化 I/O 与之叠加，两者应分开计量。

**建议**：缺省值改为一个明确的正整数（如 10）或要求显式配置；下一次 fragment 对照跑 `=1` vs `=10` 两组把物化成本从协调等待中剥离出来。

### E4（低）fragment 发现面仍 O(history)

`ingest_update_metadata` fragment 分支 glob `updates/payloads/learner_*/update_*.meta.json`（syncer.py:617-618）。plan 01/00 都已把"fragment 固定 proposal surface"列为后续项，此处仅确认：终态 metadata 会被 GC 删除（maintenance.py:161-172），所以稳态扫描面 ≈ 活跃 proposal 数，实际增长风险比字面 glob 小；真正的成本是每轮对每个 meta.json 的重复 JSON 读取（无 frontier 短路，对比 full 模式 pointer+frontier 去重）。

### E5（低）syncer 独占 GPU 节点

9 节点部署中 syncer 占一个 GPU 节点，duty cycle 低（merge 计算约 1–2s / 20s interval）。对当前 124M 模型，syncer 完全可以 CPU 化（compute_dtype fp32 的 8 向量平均 + Nesterov 是内存带宽型）或与一个 learner 同节点。这在 00 计划 §4.7 有相关讨论（多 syncer 容量），但"第 9 节点利用率"作为成本项应先进 run_analysis 的时间账本（run_analysis 末尾已提及，未见后续动作）。

### E6（低）adoption 停顿

replace 模式每次 adoption：读 498MB checkpoint → 逐参数 copy → optimizer/scheduler 重建（learner.py:435-444, 1896）。约每 cycle 一次、每次亚秒到秒级，8 learner 累计可观但非主导。preserve-state 与 rebase 已在减少重建部分；读文件部分若 E1 做了 BF16 publish 会同步减半。

---

## 6. 系统设计审查——训练准确率

### Q1（高）LR 调度污染所有质量对照（机制见 B2）

量化：正式配置 base lr=5e-5、warmup=100、cosine horizon=5000。replace 模式下每次 adoption 重置 scheduler，adoption 间隔约一个 cycle（100 步）→ learner 实际 LR 轨迹是 0.01×→1.0×base 的锯齿，平均约 0.5×base，且从不进入 cosine 衰减段；而 preserve 模式（H、rebase-preserve）scheduler epoch 连续累积，实际执行了 warmup 一次 + 完整 cosine。**因此 reset 与 preserve 两组的差异同时包含"optimizer moments 保留"和"完全不同的 LR 日程"两个因子**，现有 run 无法区分。run_analysis 已谨慎地不下质量结论，但"preserve 带来 384/395 点 loss 改善"的机制解释里应加入这一混杂因子。修复顺序：先做 scheduler decoupling（P9），再重跑 reset/preserve 消融，否则消融开关（run_analysis 最后一条建议）做了也白做。

### Q2（中）绝对参数平均在混 base 下的语义

merge 是绝对快照的 token/staleness 加权平均（merge.py:16-33, 115-123；syncer.py:1761-1770），stale proposal 缺少最近 1–2 个版本的全局进展，把它平均进来等价于把全局参数往回拉，`staleness_lambda=0.25` 只有 1/(1+0.25s) 的温和降权。G run 是现成证据：staleness≥1 占 64%，对齐 local loss 全面回退（+0.05）。00 计划 §4.5 已规划 base-relative displacement 路线，这里补充两条可低成本先行的事：
1. 把 staleness_lambda 纳入扫描（当前所有 run 固定 0.25，从未消融）；
2. 在 syncer metrics 里加"per-merge 平均 base 落后量 × 权重占比"，让 Q2 的影响能被回归分析，而不是只能靠整 run 对照。

### Q3（中）数据路径使 local loss 接近记忆化信号

`wikitext_batches`（hf_data.py:75-119）：WikiText-2 train 约 2.4M tokens，8 learner contiguous 分片后每片约 0.3M tokens ≈ 290 个 1024-block。每 learner 每步消耗 16×1024 tokens，5000 步 = 82M tokens ≈ **对同一 290 个 block 按固定顺序循环约 280 遍**（`_batched_blocks` 是顺序模循环，无任何 shuffle）。后果：(a) local train loss 的下降很大成分是记忆化，跨策略比较噪声敏感；(b) 固定循环顺序 + 固定 seed 使不同策略的 batch 序列在 adoption 时刻不同后完全发散，放大"异步调度差异"这一混杂因子；(c) 8 个 learner 的分片互不相交，merge 平均的是 8 个互相记忆不同子集的模型。00 计划 §4.6 已识别数据升级方向；短期低成本改进：block 级按 epoch 重排（带 seed 的 shuffle），以及 validation loss（P7）取代 local loss 作为主指标。

### Q4（中）prediction 启发式需要 validation 级验证

`predict_next_global_weight`（learner.py:555-699）用 `momentum×-(1-μ)` 作为历史聚合位移代理、`previous_total_update_tokens` 估计自身权重，构造预测全局权重并**在其上继续训练**直到 reconcile。预测误差期间计算的梯度都是在偏离真实轨迹的点上取的；reconcile 用 delta 迁移修正参数但不修正这些梯度对 optimizer moments 的影响（H 保留 moments）。H 的系统指标最好、local loss 也好，但按 Q1/Q3 该信号不足以证明质量。这是 P7（validation 门槛）最直接的受益者：F/H/rebase-preserve 三个 v50/v49 checkpoint 应该首批过 eval。

### Q5（低）尾部小 quorum merge 的噪声

terminal drain 允许 selected=2/3 的 merge（如 B run 尾部 7、5、2），outer step 对 2 个 learner 的平均照常走 lr=0.7+momentum 的全步长。对最终 checkpoint 的影响未经评估——若 P7 落地，可顺带对比"最后一次 partial merge 前后"的 validation。可选改进：terminal merge 的 outer lr 按 `selected/quorum_max` 缩放（作为显式实验策略记录，plan 01 §3.4 本就允许）。

### Q6（低）量化点核对（当前实现无问题）

upload BF16 → syncer FP32 聚合 → FP32 发布：单次量化，安全。`align_state_to_publication_dtype` 确保 publish_dtype≠compute_dtype 时内存权威与磁盘一致，逻辑正确。仅提醒：若 E1-1 启用 BF16 publish，theta 将每 outer step 经历 bf16 round-trip（约 3 位十进制有效数），50 步累计影响未知——必须配 validation 对照后再作为默认。

---

## 7. 建议路线图

**R0 —— 立即、低成本（半天内）**
1. 修 B1（fragment stop 语义合并）+ 单元测试；
2. 删除或接管 B3 三个死配置字段（推荐把 `reset_on_global_update` 变成 preserve/reset 的真实开关，供消融用）；
3. 修 P3（两处失效链接；删除 `plans/01.5-chat_log.md` 重复副本）；
4. 修 B5（terminal_paths 不再全量读归档）并给 BND 加 maintenance 扫描成本断言；
5. `run_identity`/resolved config 记录 git commit（P6-a）。

**R1 —— 下一轮实验之前（质量结论的前置条件）**
1. 把 `todo/cosine_scheduler_decoupling.md` 升级为 `imp_plans/02` 并实现（P9/Q1）：独立 scheduler horizon、进度语义、`global_only` 超 horizon 的 LR 下限；
2. validation 评估接入 run 验收链（P7），先对已有 F/H/rebase-preserve 的 v50/v49 checkpoint 补跑；
3. 补 `imp_plans/01-2.md`（predictor + preserve 语义规格，P4），同时把 RSS/anchor telemetry 列为该类实验的启动门禁（P5）；
4. 完成谓词进配置与 Checker（P1），syncer 支持 `global AND all-local-horizon` 联合停止。

**R2 —— 系统效率与健壮性（可与 R1 并行）**
1. 通用 GC-race retry helper 覆盖所有权重加载点（B6，顺带消 S2）；
2. fragment terminal drain / input-closed 接入（B4）；`materialize_full_every_events` 缺省值修正 + 物化成本剥离实验（E3）；
3. BF16 publish 与并行写 outer 的对照（E1，必须带 validation）；
4. `global_only` learner 的 syncer-liveness 兜底（B7）；`wait_for_learner_shutdown` 超时可配（B8）。

**R3 —— 结构性重构（在下一批策略实验前做完最划算）**
1. `GlobalAdoptionStrategy` 抽象（S1/S2）；
2. full/fragment 共享收集与 adoption 函数（S3/S4）；
3. `plans/INDEX.md` 与规则/背景分层（P2）；progress 记录回链测试 ID（P8）。

**R4 —— 研究路线补充（进入 00 计划的对照矩阵）**
1. staleness_lambda 与 staleness window 扫描 + per-merge staleness 权重占比 telemetry（Q2）；
2. 数据 shuffle 与更大语料（Q3，与 00 §4.6 一致）；
3. syncer 发布流水线化 / 事件化 ingestion 作为与 grace 扫描并列的候选（E2）；
4. 第 9 节点成本入账与 CPU syncer 对照（E5）。

---

## 8. 事实与推断的边界声明

- B1、B3、B5、E3 的缺省行为、Q3 的数据循环、P3 的失效链接为**代码/文件级确认**。
- B2/Q1 的"replace 模式几乎全程 warmup"是由配置（warmup=100=inner_steps）+ 代码（重建 scheduler）+ run_analysis 的 adoption 频率数据推断的强结论，但未用逐步 LR 日志直接验证——scheduler decoupling 实施时建议顺带把当前 LR 写入 learner metrics 以坐实。
- B6/B7/B8 是窗口/边界条件分析，当前规模下未观察到实际触发（B6 的 prediction 变体已实际触发过一次并已修复，是该类风险真实存在的证据）。
- E1/E2 的吞吐改进幅度是定性判断，实施前应先用现有 syncer_metrics 的 publish/read/grace 分解确认占比。
- Q2/Q4/Q5 的质量影响方向有 run 级证据支持（G run staleness vs loss、H 的系统优势），但幅度必须等 P7 的 validation 数据。
