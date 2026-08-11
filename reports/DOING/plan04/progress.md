# plan04 进度

## 2026-08-11 17:01 JST — INIT 盘点

- Source：branch `plan04`，branch point `2433f59a5109675d79423e6c2ddb71b72bf5be74`，workflow pin `7288dc8086a95d2294a92fb999e8539991d86ec1`。
- 当前无 active/queued PBS job，worktree 在启动时为 clean。
- 代码盘点确认 dynamic admission、quorum merge、scheduler-backed learner replacement 和 lease-based syncer takeover 已存在；当前缺失的是 plan04 的正式配置、七个场景调度器、fault oracle 和 baseline/Full 统一汇总。
- 2026-08-11 已有两个 8-node/500-step baseline run 成功，但 source commit 为 `ecfbc40f06eed1690d5dcaaee005a2695845e37e`，只作预检参考；正式 baseline 必须在冻结的共同目标上重跑。
- 资源与门禁已记录于 `execution.md`；下一步为实现配置、场景调度和统一 summary schema。

## 2026-08-11 17:28 JST — 实现与开发验证复盘

- 已实现唯一正式配置、七场景 supervisor、单行提交入口、PBS actor cache 绑定，以及 baseline/Dynamic Full 统一汇总器。
- Login-node 静态检查与 43 个 focused tests 已通过。
- 前三次 compute 验证均归类为 `harness-failure`；第 3 次失败后已按 workflow 完成完整失败复盘，根因和处置见 `failures.md`。
- 当前尚未创建 formal candidate，也未将任何开发期运行作为正式证据。

## 2026-08-11 17:37 JST — 单节点门禁通过

- Source-scoped 实现已提交为开发候选 `c1b4036`，Full Protocol checker 保持 clean-source fail-closed 行为。
- PBS job `2528967.opbs` 通过 Ruff、43 个 focused tests 和完整测试集；完整测试结果为 `575 passed`，证据见 `logs/plan04/development_tests_20260811_174000.log`。
- 下一步在 `debug-g` 上预热精确 Hub revision，并执行一个真实 GPT-2/WikiText-2 GPU micro-step；随后完成 PREFORMAL 单 Codex 审查。

## 2026-08-11 17:43 JST — 精确输入预热与 PREFORMAL finding

- PBS job `2528984.opbs` 在 `debug-g` 上加载正式配置固定的 GPT-2 与 WikiText-2 revision，完成一个 2,048-token BF16 GPU forward/backward micro-step；loss 为 `3.7773797512054443`，证据见 `logs/plan04/prewarm_20260811_174400.log`。
- 当前 Codex 对 candidate `c1b4036` 完成 current-state 审查，发现一项 High finding：syncer fault 在 `qdel` 前未证明第一个 syncer 正持有 active leader lease。
- 已增加 pre-qdel lease owner 检查和负向测试；修复结论见 `reviews/preformal.md`。受影响测试通过前不冻结正式目标。

## 2026-08-11 17:55 JST — PREFORMAL 通过并冻结正式目标

- PF-01 修复后的 focused tests 为 `5 passed`；clean candidate 的最终单节点门禁为 44 个 focused tests 和 `576 passed` full suite，证据分别见 `logs/plan04/focused_prefreeze_20260811_175000.log` 与 `logs/plan04/development_tests_final_20260811_175200.log`。
- `FINAL_COMMON_TARGET` 冻结为 commit `463f0769e2fefe5c793de03a7c7edddff619de7c`、source fingerprint `sha256:40323ecabc764a1836bc50e6786b40effed335ff867c644d36ecd8ed3d646a24`、`dirty=false`。
- 正式 gate、performance 方法、20% 偏差规则和 cleanup owner 已预注册于 `formal_manifest.json`。后续 source scope 如有变化，全部正式证据失效并返回 PREFORMAL。

## 2026-08-11 18:04 JST — normal 暴露 proposal scan 粒度缺陷

- 两个 8-node baseline gate 均在原冻结目标上完成：PBS jobs `2529095.opbs` 与 `2529096.opbs`，各 500 optimizer steps，health 均为 PASS。
- 第一个 Dynamic Full normal run 在约 7 分钟时只有 2 个全局版本，无法满足 20 分钟 actor walltime。实测定位到 syncer 在每次 merge decision 前串行验证全部可见约 248 MB payload，并因 learner 零等待产生 stale backlog。
- 已精确取消本 supervisor 拥有的 10 个 job，保留 authority、telemetry 和 scheduler history；详细记录见 `failures.md`。
- 该 finding 修改 source/config，旧 `FINAL_COMMON_TARGET` 的 baseline 与 normal 证据均不再构成最终证据。当前返回 IMPLEMENT/PREFORMAL，先验证“每次 scan 只接纳一个新 payload并立即重试 merge”的当前实现。

## 2026-08-11 18:12 JST — Ingestion 修复门禁与增量 PREFORMAL 通过

- 修复验证 focused tests 为 `51 passed`，clean commit `2adb886` 的完整门禁为 44 个 plan04 focused tests 和 `577 passed` full suite；证据见 `logs/plan04/focused_ingestion_fix_20260811_181300.log` 与 `logs/plan04/development_tests_ingestion_fix_20260811_181500.log`。
- 当前 Codex 已完成受影响 current-state 增量审查，无 open blocking finding；结论追加于 `reviews/preformal.md`。
- 下一步捕获 `2adb886` 的 canonical fingerprint、重注册 formal manifest，并从 baseline/normal gate 重新开始。旧 target 的成功 baseline 只保留为诊断，不进入最终 requirement evidence。

- 新 `FINAL_COMMON_TARGET` 已重注册为 commit `2adb8868f930b1645ce587c34d7984c4bb155fb4`、fingerprint `sha256:31b379bed1d3a0b04e943d96776d8307dac5ccfe859a3128d1b8ffcdf3698219`、`dirty=false`；identity evidence 为 `artifacts/source_identity_2adb886.json`。

## 2026-08-11 18:30 JST — Command replay 缺陷修复并重新冻结

- `2adb886` 的第二次 normal run 在 global version 0 重复扫描同一个已接纳 proposal；根因、scheduler cleanup 和保留证据见 `failures.md`。该 target 的两个成功 baseline 与失败 normal run 均不进入最终验收。
- accepted command replay 现返回 `exact_replay`，使有界 scan 能继续检查后续 proposal，同时继续避免重复读取 payload。
- 修复后的 focused tests 为 `57 passed`；clean candidate 通过 Ruff、44 个 plan04 focused tests 和 `578 passed` full suite。当前 Codex 完成增量 PREFORMAL 审查，未发现 open blocking finding。
- 新 `FINAL_COMMON_TARGET` 为 commit `c6aa324570ddf176568e6efe9f65dcb9239399a0`、fingerprint `sha256:9845e47c83d24898a2bd6c4d5cec8dae618a717dd23415890b7eb46434e400be`、`dirty=false`。identity evidence 为 `artifacts/final_source_identity.json`；正式 baseline 与七个 Dynamic Full gate 从该 target 重新开始。

## 2026-08-11 18:44 JST — 统一 2,000-step 工作量

- 用户澄清 baseline 也必须执行与 Dynamic Full `200 local steps × 10 global steps` 对齐的 2,000 optimizer steps；此前所有 500-step baseline 仅保留为无效诊断数据，不进入正式比较，后续不再提交 500-step 实验。
- 已停止尚未完成的 `c6aa324` normal run及其 9 个 actor，避免在即将变化的共同 target 上继续消耗资源。该 normal workload 本身为 `200 × 10`，停止原因仅是共同 target 将纳入修正后的 baseline source 和 walltime。
- 当前唯一 baseline 配置改为每 rank 2,000 steps；Periodic Average 每 200 steps 同步，共 10 次。旧 500-step 配置、PBS 入口和提交入口已直接替换，不保留兼容路径。
- `torch_ddp_baselines` 纳入 canonical source scopes。根据刚才 normal 的实测 payload 验证吞吐，Dynamic actor/supervisor 预算分别调整为 25/30 分钟，为 10 次 exact merge 与 terminal teardown 保留安全余量。正式证据将在新的 clean target 上全部重跑。
- 2,000-step 对齐后的 focused tests 为 `69 passed`，clean candidate 完整门禁为 `578 passed`。当前 Codex 增量 PREFORMAL 审查无 open blocking finding。
- 新 `FINAL_COMMON_TARGET` 为 commit `2debadd68e71f99c5564eba59d977cfe06d517d4`、fingerprint `sha256:fa8d335bfe6a98286d27aa0abdea092edf32a7ca5243354de3703e5fbdac06b4`、`dirty=false`；identity evidence 为 `artifacts/final_source_identity.json`。

## 2026-08-11 19:36 JST — Terminal proposal 预判修复并重新冻结

- `2debadd` normal 已达到 global version 10 和 8 个 terminal ack，但 terminal callback 重复验证已 terminalized fence 的 payload；该 target 的 2,000-step baseline 和 normal evidence 均已作废。根因、精确 cleanup 和保留证据见 `failures.md`。
- proposal ingest 现以 current/controller/terminal snapshot 在 payload read 前排除不允许的 terminal input，并保留 authority 事务内二次校验。
- 修复后 focused tests 为 `70 passed`，clean candidate 完整门禁为 `579 passed`。当前 Codex增量 PREFORMAL 审查无 open blocking finding。
- 新 `FINAL_COMMON_TARGET` 为 commit `bace678a97c378ef115116e4d5ca933c7abd24b0`、fingerprint `sha256:d684fd1371c0967048375b23258393fc2e304b8f104db8e67901521b9c8236b1`、`dirty=false`；identity evidence 为 `artifacts/final_source_identity.json`。

## 2026-08-11 19:37 JST — 最终 target 正式 gate 启动

- 已并行提交两个 8-node、每 rank 2,000-step baseline：DDP `2529632.opbs`，Periodic Average `2529633.opbs`。run IDs 分别为 `20260811_193646_torch_ddp_gpt2_wikitext2_8n_2000` 与 `20260811_193646_torch_periodic_average_gpt2_wikitext2_8n_2000`。
- baseline 运行期间已准备独立 compute summary 入口；它只接受两个精确 baseline run 与一个完成的 Dynamic Full run，并原子生成统一 CSV 和 20% 比较 JSON，不会递归混入旧 target 数据。

## 2026-08-11 19:46 JST — 最终 baseline 通过并启动 normal

- DDP job `2529632.opbs` 在 8 节点完成每 rank 2,000 steps，scheduler 状态为 `FINISH`、health 为 `PASS`，耗时 8 分 17 秒；正式 health artifact 为 `logs/torch_ddp_baselines/20260811_193646_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json`。
- Periodic Average job `2529633.opbs` 在 8 节点完成每 rank 2,000 steps，并在 steps 200、400、…、2,000 完成 10 次参数平均；scheduler 状态为 `FINISH`、health 为 `PASS`，耗时 7 分 25 秒。正式 health artifact 为 `logs/torch_ddp_baselines/20260811_193646_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json`。
- 两个 baseline 的 source identity 均精确匹配 `bace678`。在 baseline 完成后立即提交 normal supervisor `2529735.opbs`；run ID 为 `plan04_normal_20260811_194547`。normal 运行期间并行整理已完成 gate 的 manifest 和报告，不并发启动另一组重 I/O Dynamic Full workload，以免共享文件系统争用污染正式时延证据。

## 2026-08-11 20:04 JST — 并行完成 payload 验证性能修正

- `bace678` normal 运行期间并行分析 authority 时间戳并检查 payload boundary：四个连续 proposal 各耗时约 28 秒，根因是对 248 MB BF16 tensor 执行 Python scalar finite scan。该吞吐已足以证明最终 wall time 会超过两个 baseline 20% 以上，因此不把该 target 的运行继续登记为正式证据。
- finite scan 已替换为 PyTorch 向量化 CPU kernel，所有 identity、digest、schema 和 non-finite fail-closed 检查保持不变。独立 PBS `2529803.opbs` 对真实 248,879,712-byte proposal 的验证耗时为 0.436 秒，状态为 `ok`。
- 扩展 focused tests 为 `81 passed`；clean candidate 的 Ruff、44 个 plan04 focused tests与完整测试均通过，完整测试为 `580 passed`。当前 Codex增量 PREFORMAL 审查无 open blocking finding。
- 新 `FINAL_COMMON_TARGET` 为 commit `0e229273ff5522bc58fb77d624926b1930a8f659`、fingerprint `sha256:522770d2aaf18880a41eb1084d0b4d80ef22db95ea1e0b9b1509508b88b14ec6`、`dirty=false`；identity evidence 为 `artifacts/final_source_identity.json`。正式 baseline、normal 和六个故障/错峰 gate 从该 target 重跑。

## 2026-08-11 20:21 JST — Archive-aware 终态证据门禁通过并重启 baseline

- 对已完成的 `bace678` normal 执行最终检查时发现，maintenance 已按设计归档 versions 1–9 和早期 updates，但 supervisor 与汇总器只读取 hot table。该运行实际已正确完成 v10、10 次 exact four-way merge 和 8 个 terminal fence；原 supervisor 的 FAIL 属于终态证据 harness failure。
- 已增加统一 logical authority read API，对不可变 archive 执行 manifest、partition、regular-file 与摘要验证，再与 hot rows 冲突检测后合并。scenario supervisor、canonical checker 与统一汇总器均改用该 API；2,000 optimizer steps 由 terminal `final_cycle_seq=10 × inner_steps=200` 严格证明。
- 新实现以真实已归档 run 重放：scenario oracle 通过，统一 diagnostic summary 成功。archive-aware focused tests 为 `92 passed`；clean candidate 的 Ruff、45 个 plan04 focused tests和完整测试均通过，完整测试为 `582 passed`。
- 新 `FINAL_COMMON_TARGET` 为 commit `5b4dab6fe000867183d6008b268a14b1a6c9e1a2`、fingerprint `sha256:a9a85660a6c0d797ed734b03811c20f27f6da831f15289331c915205f177415c`、`dirty=false`。两个 8-node、每 rank 2,000-step baseline 已并行提交为 `2529911.opbs` 与 `2529912.opbs`；等待期间继续整理正式证据，不串行空等。

## 2026-08-11 20:30 JST — Current-target baseline 通过并启动 normal

- DDP `2529911.opbs` 与 Periodic Average `2529912.opbs` 均在 8 节点完成每 rank 2,000 optimizer steps，health 为 `PASS`；Periodic Average 精确记录 steps 200、400、…、2,000 的 10 次参数平均。两者 source commit 均为 `5b4dab6`。
- 正式 health artifacts 分别为 `logs/torch_ddp_baselines/20260811_202130_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json` 和 `logs/torch_ddp_baselines/20260811_202130_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json`。
- Baseline 完成后立即提交 current-target normal supervisor `2529960.opbs`，run ID 为 `plan04_normal_20260811_203043`。normal 独占正式性能窗口；运行期间继续登记 baseline evidence 与准备 unified summary。

## 2026-08-11 20:37 JST — 拒绝未达到 2,000 local steps 的 normal

- `2529960.opbs` 正确完成 v10、10 次 exact four-way merge 和 8 个 terminal ack，但 8 个 terminal fence 的 `final_cycle_seq` 均为 7，只代表每 learner 1,400 optimizer steps。严格 oracle 按用户明确的 2,000-step 统一工作量拒绝该运行。
- 当前实现正在把停止条件收敛为 `local_and_global`：每 learner 必须到 2,000 local steps，authority 同时必须恰好停在 v10；syncer 在 v10 后只 ingest、不再 merge v11。修复期间已启动 focused compute validation，等待时同步记录失效 target 和证据。
- `5b4dab6` 上的两个成功 baseline和 normal 均不再是正式 gate。新 source 通过完整测试和单 Codex PREFORMAL 后，baseline 仍并行重跑，再执行隔离的 normal 性能窗口。

## 2026-08-11 20:49 JST — local/global 双 horizon 门禁通过

- `local_and_global` 实现覆盖正式配置、learner horizon、replacement resume coordinate、syncer v10 merge fence和 terminal close predicate。focused tests 为 `116 passed`；clean candidate 通过 Ruff、49 个 plan04 focused tests和完整测试集 `589 passed`。
- 当前 Codex增量 PREFORMAL 审查无 open blocking finding。新的唯一 `FINAL_COMMON_TARGET` 为 commit `0f7d3aa9167b0656e9baacf93c26834e392f95c5`、fingerprint `sha256:80b8dc5f5f06aaba02ed8becf4895dbdca4e30efc9047ea5026e34cc4e74d050`、`dirty=false`。
- 下一组 DDP 与 Periodic Average baseline 将继续并行提交；等待时继续更新正式证据。随后 normal 使用独立性能窗口验证每 learner 2,000 steps、global v10与 20% 指标门禁。
- Current-target baseline 已并行提交：DDP `2530049.opbs`，Periodic Average `2530050.opbs`；两者 run ID 均以 `20260811_204941` 开头。

## 2026-08-11 21:05 JST — 最终 target baseline 通过并启动 exact-workload normal

- DDP `2530049.opbs` 与 Periodic Average `2530050.opbs` 均完成 8 ranks × 2,000 optimizer steps，health 为 `PASS`；后者精确记录 10 次 200-step 参数平均。受当时节点可用量约束，两个已同时提交的 job 由 scheduler 连续执行。
- 正式 health artifacts 位于 `logs/torch_ddp_baselines/20260811_204941_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json` 与 `logs/torch_ddp_baselines/20260811_204941_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json`。
- Baseline 完成后立即提交 normal supervisor `2530098.opbs`，run ID `plan04_normal_20260811_210535`。该窗口不并发其他重 workload，用于严格生成 current-target loss 与 wall-time 比较。

## 2026-08-11 21:13 JST — 并行定位 v10 后 adoption 空等

- Normal 运行期间 authority 已正确停在 v10，同时 durable stream progress 继续增长，证明 joint horizon 的 merge fence 生效。并行检查 learner telemetry 发现每个已加载 v10 的 stream 仍等待不存在的 v11，单次精确耗时约 120.002 秒，并会对剩余 cycle重复。
- 该吞吐已足以证明 wall time 会越过 20% 门禁，因此立即清理 `2530098.opbs`–`2530107.opbs`。当前修正保留一次非阻塞 newer-latest read，但在 joint horizon 的最终 global target 后跳过阻塞等待；等待 compute tests 时同步更新失效 evidence。
- `0f7d3aa` 上的 baseline和 normal 均失效。新的 clean target 通过 current-Codex PREFORMAL 后再次重跑共同证据。

## 2026-08-11 21:21 JST — v10 non-blocking adoption 门禁通过

- 最终 global horizon 后的 adoption 不再等待 v11，也不会创建依赖 v11 的 rebase/prediction state。扩展 focused tests 为 `136 passed`；clean candidate 通过 Ruff、49 个 plan04 focused tests和完整测试集 `592 passed`。
- 当前 Codex增量 PREFORMAL 无 open blocking finding。新的唯一 `FINAL_COMMON_TARGET` 为 commit `12ae38993d94cce8d15b1e842c9123d22d5148b3`、fingerprint `sha256:73614127d64bf8ae1dd23b763c5d8c883ba3084e7d2e97dfb5af1bf8591765ee`、`dirty=false`。
- 继续并行提交两个 current-target baseline；等待调度和训练时同步更新正式证据，再执行隔离 normal。

## 2026-08-11 21:23 JST — 按用户要求停止任务

- 任务停在 clean target `12ae38993d94cce8d15b1e842c9123d22d5148b3`。未再提交 current-target baseline 或场景实验；已确认此前用于 source identity 的 PBS job `2530192.opbs` 结束，无未完成作业。
- 已完成 Dynamic Full 配置、启动脚本、七个场景 supervisor/oracle、统一汇总入口及 2,000-step DDP/Periodic Average baseline harness。正式工作量统一为每个 learner 或 rank 2,000 optimizer steps；Dynamic Full 使用 200 local steps × 10 global steps，不保留 500-step 正式实验路径。
- 实验诊断推动并验证了有界 proposal ingest、`exact_replay`、terminal proposal 预过滤、向量化 BF16 finite scan、hot+archive 统一证据视图、`local_and_global` 双 horizon，以及 v10 后 non-blocking adoption。最终 source 验证结果为 focused `136 passed`，Ruff、49 个 plan04 focused tests和完整测试集 `592 passed`。
- latest target 尚未生成正式 baseline、normal 及六个故障或错峰场景证据，因此不填写 PASS、不生成 FINAL 审查，也不归档 plan04。`formal_manifest.json` 的 gate artifact 保持 `null`，`requirements.csv` 保持 `pending`。
- 若恢复任务，应从 commit `12ae389` 并行运行两个 2,000-step baseline 开始；baseline 完成后执行隔离 normal，再运行六个故障或错峰场景，最后生成统一性能比较和 FINAL 证据审查。
