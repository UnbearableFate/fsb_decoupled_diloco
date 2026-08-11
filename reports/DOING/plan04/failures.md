# plan04 失败记录

## 2026-08-11 17:24 JST — 开发验证 attempt 1

- 类别：`harness-failure`；连续失败计数：1。
- 环境：PBS job `2528897.opbs`，`debug-g`，节点 `mg0028`，walltime `00:10:00`，实际耗时不足 1 秒。
- 预期：执行 plan04 focused tests 和 full suite。
- 实际：验证器在 repository-wide `ruff format --check .` 停止。新增 plan04 文件已通过格式检查，但最近合入的 `torch_ddp_baselines` 文件和 `website/scripts/generate_reference.py` 仍未满足当前 repository-wide 格式门禁。
- 证据：`logs/plan04/development_tests_20260811_172354.log`。
- 处置：对 Ruff 列出的 8 个当前 Python 文件执行机械格式化，再重跑相同验证。

## 2026-08-11 17:25 JST — 开发验证 attempt 2

- 类别：`harness-failure`；连续失败计数：2。
- 环境：PBS job `2528901.opbs`，`debug-g`，节点见 scheduler history，walltime `00:10:00`。
- 预期：Ruff 与 42 个 focused tests 通过，然后执行 full suite。
- 实际：Ruff 通过；focused tests 为 `3 failed, 40 passed`。两个配置测试失败来自同一个配置错误：`desired_contributors=8` 超过精确 merge 上界 `quorum_max=4`。另一个失败是 summary 测试把单 contributor 的 token 数误写为两个 rank 的总数。
- 证据：`logs/plan04/development_tests_20260811_172500.log`。
- 处置：将 dynamic capacity 目标改为与精确 merge 阈值一致的 4，将低容量阈值改为 3。Learner replacement 仍由 scheduler 终态确认路径独立触发。同时将单 contributor 的 token 期望值修正为 16,384。

## 2026-08-11 17:28 JST — 开发验证 attempt 3 与强制失败复盘

- 类别：`harness-failure`；连续失败计数：3，已在第 4 次提交前完成完整失败复盘。
- 环境：PBS job `2528924.opbs`，`debug-g`，walltime `00:10:00`，focused tests 为 `43 passed`，full suite 为 `33 failed, 542 passed`。
- 证据：`logs/plan04/development_tests_20260811_173000.log`。
- 根因 1：source identity 新增 `do_experiments` 与 `tools` 后，Full Protocol 的合成 fixture 在包含本次未提交改动的开发树上执行 formal bootstrap，因 `git_dirty=true` 被预期的生产门禁拒绝。由此引发 27 个同源级联失败，并非 27 个独立产品缺陷。
- 根因 2：plan completion fixture 仍硬编码旧 source scope 列表，导致 5 个测试在进入各自目标断言前被 canonical scope 检查拦截。
- 根因 3：module coverage manifest 遗留已迁至顶层 `tools/` 的旧路径 `fs_diloco/tools/clean_run.py`，同时缺少新增 Dynamic Full 正式配置的边界映射。
- 处置：合成 Full Protocol fixture 显式启用仅供测试的 dirty snapshot；plan completion fixture 直接引用唯一的 `SOURCE_SCOPES`；删除 obsolete manifest 项并登记正式 Dynamic Full 配置。生产 formal bootstrap 的 clean-source 门禁保持不变。
- 复盘结论：三次失败均为验证环境或测试数据问题，尚未发现需要放宽协议验收条件的证据。第 4 次提交继续使用相同 focused/full 门禁，只有门禁全部通过后才进入 runtime smoke test。

## 2026-08-11 17:33 JST — 开发验证 attempt 4

- 类别：`harness-failure`；复盘后连续失败计数：1。
- 环境：PBS job `2528931.opbs`，`debug-g`，walltime `00:10:00`，focused tests 为 `43 passed`，full suite 为 `11 failed, 564 passed`。
- 证据：`logs/plan04/development_tests_20260811_173600.log`。
- 实际：6 个 plan completion 失败来自注册 manifest fixture 自身仍携带旧 `source_scopes`；其余 5 个 Full Protocol checker 测试正确拒绝了 dirty validation target。前一轮允许合成 bootstrap 读取 dirty snapshot 只越过了初始化门禁，无法也不应越过最终 checker 的 clean-source 门禁。
- 处置：注册 manifest fixture 同步唯一 `SOURCE_SCOPES`。不放宽 Full Protocol checker；改为先提交 source-scoped 开发实现，在 clean commit 上执行同一 full suite。开发测试 PBS 移到 source scope 之外，避免验证脚本自身制造 dirty target。

## 2026-08-11 18:04 JST — 正式 normal attempt 1 提前终止

- 类别：`product-failure`；normal product experiment 连续失败计数：1。
- Source：已冻结目标 `463f0769e2fefe5c793de03a7c7edddff619de7c`；PBS supervisor `2529108.opbs`，actor jobs `2529109.opbs`、`2529110.opbs`、`2529111.opbs`、`2529112.opbs`、`2529114.opbs`–`2529118.opbs`。
- Workload：正式 GPT-2/WikiText-2，8 个独立 learner，exact quorum 4，`200 × 10`；run root 为 `runs/full_protocol/plan04_normal_20260811_175638`。
- 预期：actor walltime `00:20:00` 内完成 10 个全局版本。
- 实际：运行约 7 分钟仅提交 2 个全局版本；authority 已有 8 个 applied、8 个 dropped 和 6 个 pending update。每个新 BF16 GPT-2 payload 的完整验证约 18 秒，而一个 reconcile pass 在重新检查 merge 条件前串行验证了所有可见新 proposal。第一版本因此先验证 8 个约 248 MB payload，期间 learner 又从旧 base 继续发布，形成无收益的 stale backlog。按实测线性投影无法在 walltime 内完成。
- 证据：`runs/full_protocol/plan04_normal_20260811_175638/control/syncer_metadata.sqlite3`、`runs/full_protocol/plan04_normal_20260811_175638/metrics/`、`logs/plan04/plan04_normal_20260811_175638/` 和 scheduler history。为避免继续消耗 10 个节点，coordinator 仅对该 supervisor 明确拥有的 10 个 job ID 执行 `qdel`；运行数据保留。
- 根因：syncer proposal scan 的工作边界大于一次 quorum decision；`adopt_global_after_upload` 没有等待窗口，放大了旧 base proposal backlog。
- 处置：每次 proposal scan 最多接纳一个新 payload，立即返回主循环重新尝试 merge；formal learner 在发布后最多等待 120 秒观察新 global，避免在 syncer 正验证当前 quorum 时继续从旧 base 生成周期。新增负向行为测试，要求一次 scan 不得越过第一个新 payload。

### Fix verification attempt 1

- 类别：`harness-failure`；ingestion-fix harness 连续失败计数：1。
- PBS job `2529224.opbs`；结果为 `1 failed, 50 passed`。
- 新测试为两个 proposal 使用了不同 `stable_contributor_key`，却沿用默认 `learner-0` fence，严格 proposal constructor 在到达目标行为前正确拒绝 fixture。
- 处置：两个 proposal 改为同一合法 fence 的连续 cycle；目标断言仍是一次 scan 只接纳第一个新 payload。

## 2026-08-11 18:25 JST — 正式 normal attempt 2 提前终止

- 类别：`product-failure`；normal product experiment 连续失败计数：2。
- Source：已冻结目标 `2adb8868f930b1645ce587c34d7984c4bb155fb4`；PBS supervisor `2529278.opbs`，actor jobs `2529279.opbs`–`2529287.opbs`。
- Workload：正式 GPT-2/WikiText-2，8 个独立 learner，exact quorum 4，`200 × 10`；run root 为 `runs/full_protocol/plan04_normal_20260811_181658`。
- 预期：一次 scan 接纳一个新 proposal；下一次 scan 越过该 proposal 的 exact replay，继续接纳后续 proposal。
- 实际：运行约 8 分钟仍停留在 global version 0。16 个 proposal 已持久化，但 authority 仅接纳了第一个。相同 command ID 的持久化重放返回首次执行结果 `accepted`，runtime 因而在每次 scan 都把同一个 proposal 误判为“本轮新接纳”，立即返回。该运行的 10 个明确 job ID 已执行 `qdel`，运行数据保留。
- 证据：authority 中 `ingest_proposal=1`、`try_select_batch=71`，且唯一 update 为 pending；同步器仍持续更新 leader lease 和 capacity observation，排除进程停滞。证据位于 `runs/full_protocol/plan04_normal_20260811_181658/control/syncer_metadata.sqlite3`、同一 run 的 `metrics/` 以及 `logs/plan04/plan04_normal_20260811_181658/`。
- 根因：proposal adjudication 对“同 command 的 accepted 重放”和“首次 accepted”返回相同 disposition，不能满足目录型消费者区分新对象与已消费对象的协议要求。
- 处置：已成功接纳的 command replay 统一返回 `exact_replay`，且不重新读取 payload；冲突、拒绝和 command mismatch 的原有语义保持严格。新增 runtime 回归测试，要求第二次目录 scan 越过第一个 exact replay 并接纳第二个 proposal。

## 2026-08-11 19:26 JST — 正式 normal attempt 3 在 terminal drain 提前终止

- 类别：`product-failure`；2,000-step 统一 target 上的 normal product experiment 连续失败计数：1。
- Source：已冻结目标 `2debadd68e71f99c5564eba59d977cfe06d517d4`；PBS supervisor `2529491.opbs`，actor jobs `2529492.opbs`–`2529500.opbs`。
- Workload：8 个 learner 均按 200 local steps 执行 10 个周期，authority 已提交 global version 10；run root 为 `runs/full_protocol/plan04_normal_20260811_190218`。
- 预期：8 个 terminal ack 到齐后，syncer 在 drain deadline 后完成 token fate 收敛并发布 terminal record。
- 实际：global version 10 和 8 个 ack 均已持久化，但 terminal ingest callback 仍逐个重新验证已 terminalized fence 的约 248 MB proposal。authority 在 payload 验证后才以 `MembershipFenceError` 拒绝；下一轮 callback 又从同一批对象开始。actor walltime 内无法完成 terminalization。已对该 supervisor 明确拥有的 10 个 job ID 执行 `qdel`，运行数据保留。
- 证据：`control/latest.json` 为 version 10；`terminal_contributor_fences` 为 8 个 `acked`；syncer telemetry 从 `1786443833` 起约每 18–20 秒记录同一 stream 的 `proposal_ingest_rejected`，错误均为 stale/not admitted fence。证据位于 `runs/full_protocol/plan04_normal_20260811_190218/` 与 `logs/plan04/plan04_normal_20260811_190218/`。
- 根因：目录消费者在进入 authority 的昂贵 payload 验证前，没有使用同一轮 authority snapshot 排除已不属于 current contributor fences 的 proposal。
- 处置：一次 ingest pass 先捕获 current fence snapshot，并在解析小型 proposal descriptor 后跳过不在 snapshot 中的 stale fence，避免读取 payload；authority 内的最终 fence 检查继续保留，用于覆盖 snapshot 后的并发变化。新增回归测试，要求 stale proposal 不得到达 payload verification 边界。

### Fix verification attempt 1

- 类别：`harness-failure`；terminal-ingest-fix harness 连续失败计数：1。
- PBS job `2529597.opbs`；结果为 `6 failed, 64 passed`。
- 六个失败同源：proposal-only runtime fixtures 在引入 current fence snapshot 后提供了 fence，却未提供 receipt scan 固有的 `contributor_progress` read method，因而在到达 proposal 行为前触发 `AttributeError`。
- 处置：为四个 proposal fixture 补充返回空进度的 read method；产品实现和目标断言不变。

## 2026-08-11 20:03 JST — 正式 normal 的性能诊断触发 target 作废

- 类别：`product-failure`；性能比较验证域连续失败计数：1。
- Source：`bace678a97c378ef115116e4d5ca933c7abd24b0`；normal supervisor `2529735.opbs`，run root 为 `runs/full_protocol/plan04_normal_20260811_194547`。该运行提交后仍继续到终态，用于保留完整诊断，但不再构成正式 gate。
- 预期：normal 完成后与两个 2,000-step baseline 比较；任何 loss 或 wall time 差异超过 20% 时定位并解决实现原因。
- 实际：在 global versions 6–8，四个 proposal 的相邻 `ingested_at` 差值稳定为 27.7–29.0 秒，单个 global version 约需 114–138 秒；baseline 总时延仅为 7 分 25 秒和 8 分 17 秒。无需等待 run 结束即可确定 normal wall time 必然超过 20% 阈值。
- 根因：`_inspect_safetensors` 已经从打开并绑定 inode 的 payload 读取 tensor bytes，却仍以 Python scalar iteration 检查约 1.24 亿个 BF16 元素是否 finite。结构、digest、inode、mtime/ctime 和路径绑定校验不是瓶颈；逐元素 Python 解释开销才是单 proposal 约 28 秒的主因。
- 处置：finite-value 检查改为 `torch.frombuffer` 与 `torch.isfinite` 的向量化 CPU kernel；payload 的 SHA-256、结构、schema、size、dtype、numel、非 finite 拒绝和前后文件身份检查全部保留。真实 248,879,712-byte BF16 proposal 在独立 compute node 上验证耗时 0.436 秒、状态 `ok`，证据见 `logs/plan04/vectorized_payload_scan_benchmark_20260811_195800.log`。
- 验证：扩展后的 focused tests 为 `81 passed`；clean commit `0e22927` 通过 Ruff、44 个 plan04 focused tests和完整测试集 `580 passed`。证据见 `logs/plan04/focused_vectorized_payload_scan_20260811_195600.log` 与 `logs/plan04/development_tests_vectorized_payload_scan_clean_20260811_200300.log`。旧 target 的两个成功 baseline 和 diagnostic normal 一并失效，必须在新 target 上重跑。

## 2026-08-11 20:20 JST — 已归档 authority rows 导致终态证据误判

- 类别：`harness-failure`；archive-aware evidence harness 连续失败计数：1。
- Source：`bace678a97c378ef115116e4d5ca933c7abd24b0` 的已完成 diagnostic normal，supervisor `2529735.opbs`，run root `runs/full_protocol/plan04_normal_20260811_194547`。后续 `0e22927` 仍沿用相同 hot-only evidence consumer。
- 预期：最终 oracle 和统一汇总读取完整 authority history，证明 v10、10 次 exact four-way merge、8 个 terminal fence和每 stream 2,000 optimizer steps。
- 实际：maintenance 在运行中按设计把 versions 1–9 与早期 updates 移入不可变 audit archive；hot table 只保留末尾窗口。运行已正确 terminalize，但 supervisor 报告 global versions 不完整，汇总器报告 terminal fence 没有 ingested update。两个 `0e22927` baseline job `2529855.opbs`、`2529856.opbs` 在确认 target 需修改后立即停止，未登记证据。
- 根因：三个终态消费者未统一使用 logical hot+archive authority view；汇总器还错误地把“每个 terminal stream 必须有 ingested update”当作工作量证明，而 exact-quorum 允许某些 stream 在最终 cycle 被 terminal ack 后不进入 merge。
- 处置：在 `audit_archive.py` 提供唯一受验证 logical read API，合并 archive 与 hot rows并对冲突 fail closed；supervisor、canonical checker和汇总器统一调用。工作量由 terminal fence 的 `final_cycle_seq × inner_steps` 推导，同时保留完整 global merge与 terminal fence oracle。
- 验证：archive-aware focused tests `92 passed`；clean commit `5b4dab6` 通过 Ruff、45 个 plan04 focused tests和完整测试集 `582 passed`。新逻辑对真实 `bace678` run 的 scenario oracle 与统一 summary 均通过，证据见 `logs/plan04/diagnostic_bace_archive_aware_oracle_2.log`、`artifacts/diagnostic_bace_summary.csv` 与 `logs/plan04/development_tests_archive_aware_clean_20260811_202100.log`。

## 2026-08-11 20:36 JST — 优化后 normal 暴露 local/global 停止条件不一致

- 类别：`product-failure`；统一 2,000-step workload 连续失败计数：1。
- Source：`5b4dab6fe000867183d6008b268a14b1a6c9e1a2`；normal supervisor `2529960.opbs`，run root `runs/full_protocol/plan04_normal_20260811_203043`。
- 预期：8 个 learner 各完成 `200 local steps × 10 cycles = 2,000 optimizer steps`，同时 authority 精确停止在 global version 10。
- 实际：向量化验证使 global version 10 在约 4 分钟到达，learner 随即按原 `global_only` 语义等待 terminal close；8 个 terminal fence 的 `final_cycle_seq` 均为 7，即每个 learner 只完成 1,400 optimizer steps。authority 的 v10、10 次 merge和 8 个 ack 均正确，scenario oracle 以“每个 terminal contributor 必须恰好 10 cycles”拒绝该运行。所有 owned actor 已正常结束，无遗留 job。
- 根因：`stop_after_outer_steps=10` 只约束全局 merge 数；exact quorum 4 允许 8 个 learner 的 40 个 applied updates在每 stream 完成 10 个 local cycles 前达到 v10。旧 scalar payload 瓶颈曾让 learner 先行积累至 cycle 10，掩盖了配置语义缺口。
- 处置：引入单一 `local_and_global` completion mode。正式配置同时声明 `max_local_steps=2000` 与 global target 10；learner 在任一目标先到时等待另一目标，syncer 在 v10 后继续 ingest receipt但禁止 v11 merge，并仅在全部 current stream 的 durable progress 达到 cycle 10 后开始 terminal close。`local_or_global` 与 `global_only` 的现有语义不变。
- 影响：`5b4dab6` 的两个成功 baseline和该 normal 一并失效；修复通过 clean 门禁后必须在新共同 target 上重跑。
- 验证：focused tests `116 passed`；clean commit `0f7d3aa` 通过 Ruff、49 个 plan04 focused tests和完整测试集 `589 passed`。证据见 `logs/plan04/focused_local_and_global_3_20260811_204700.log` 与 `logs/plan04/development_tests_local_and_global_clean_20260811_204900.log`。

## 2026-08-11 21:12 JST — v10 后等待不存在的 v11

- 类别：`product-failure`；性能比较验证域连续失败计数：1。
- Source：`0f7d3aa9167b0656e9baacf93c26834e392f95c5`；normal supervisor `2530098.opbs`，run root `runs/full_protocol/plan04_normal_20260811_210535`。
- 预期：syncer 在 v10 后禁止 v11 merge，同时 learner 继续无空等地完成剩余 local cycles，最终每 stream 恰好到 cycle 10。
- 实际：authority 正确保持 v10 且 controller 为 open，stream progress 从 cycle 7 向 cycle 10 推进；但 Replace adoption 在每次 proposal ingest 后仍执行 `post_publish_latest_wait_seconds=120`。Telemetry 精确记录 `current_version=10` 的等待完整耗时 `120.002` 秒；由于设计已禁止 v11，该等待不可能成功并会在剩余每个 cycle 重复，必然使 wall time 超出 baseline 20% 阈值。
- 处置：在 joint horizon 模式且 learner 已加载最终 global target 时，post-publish 仍先执行一次非阻塞 newer-latest read；若没有 newer latest，则记录 skip event 并立即继续下一个 local cycle，不进入有界等待。global target 前的等待和其他 completion mode 保持不变。
- 清理：确认诊断充分后，对 supervisor 明确拥有的 `2530098.opbs`–`2530107.opbs` 执行 exact-ID `qdel`；运行数据与 telemetry 保留，未继续消耗 10 个节点。
- 影响：`0f7d3aa` 的两个成功 baseline与该 normal 一并失效；修复通过 clean 门禁后在新共同 target 上重跑。
- 验证：扩展 focused tests `136 passed`；clean commit `12ae389` 通过 Ruff、49 个 plan04 focused tests和完整测试集 `592 passed`。证据见 `logs/plan04/focused_final_horizon_wait_2_20260811_211800.log` 与 `logs/plan04/development_tests_final_horizon_wait_clean_20260811_211900.log`。
