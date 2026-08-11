# plan04 PREFORMAL current-state 审查

## 审查身份

- Reviewer：当前 Codex coordinator；按用户要求未执行 multi-agent review。
- Candidate：`c1b4036`。
- 范围：当前 Dynamic Full 配置、独立 actor PBS 入口、scenario supervisor、authority/fence/fault oracle、统一汇总器、相关测试、source identity 和正式提交入口。
- 已核对证据：`logs/plan04/development_tests_20260811_174000.log`、`logs/plan04/prewarm_20260811_174400.log`。

## Findings

### PF-01 — Syncer fault 在 qdel 前未证明目标持有当前 leader lease

- 严重度：High。
- 影响：若第一个 syncer 因启动波动尚未获得 lease，原 harness 仍会执行 `qdel`。最终 oracle 虽会失败，但该次昂贵实验没有实际覆盖“active syncer 掉线”故障层。
- 处置：`fixed`。在删除前读取 `syncer_leader`，要求唯一 active lease 的 `pbs_job_id` 精确匹配第一个 syncer job，并把 pre-qdel lease row 写入 fault evidence。新增负向测试证明错误目标会 fail closed。

## 修复验证与结论

- PF-01 focused tests：`5 passed`；证据见 `logs/plan04/focused_prefreeze_20260811_175000.log`。
- Clean candidate `463f076`：44 个 focused tests 与 576 个 full-suite tests 全部通过；证据见 `logs/plan04/development_tests_final_20260811_175200.log`。
- 精确 source identity：commit `463f0769e2fefe5c793de03a7c7edddff619de7c`，fingerprint `sha256:40323ecabc764a1836bc50e6786b40effed335ff867c644d36ecd8ed3d646a24`，`dirty=false`。

Verdict：`APPROVE`。无 open blocking finding，冻结该 identity 为唯一 `FINAL_COMMON_TARGET`。

## 正式 normal failure 后增量 PREFORMAL 审查

- 旧 target `463f076` 的 normal run 证明 proposal scan 在一次 merge decision 前执行了 8 次约 248 MB payload 验证，并允许 learner 从旧 base 继续产生 backlog；该 target 的正式 evidence 已作废。
- 当前实现把一次 scan 的新 payload 接纳上界收敛为 1，使主循环在每次昂贵验证后立即重试 merge；formal learner 发布后等待新 global 的上界为 120 秒。没有新增 schema、兼容分支或第二套 runtime 路径。
- RED/behavioral test 证明一次 scan 不会越过第一个 accepted payload；受影响 focused tests 为 `51 passed`，完整测试为 `577 passed`。
- 已重新检查 syncer 主循环、terminal ingest callback、authority exact replay、quorum selection、learner post-publish adoption、formal config、normal oracle 和 PBS walltime。terminal service 可通过重复有界 ingest 推进，不依赖一次调用清空目录；exact replay 保持 idempotent。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `2adb8868f930b1645ce587c34d7984c4bb155fb4`、fingerprint `sha256:31b379bed1d3a0b04e943d96776d8307dac5ccfe859a3128d1b8ffcdf3698219`、`dirty=false`。

## 正式 normal attempt 2 后增量 PREFORMAL 审查

- `2adb886` 的 normal run 暴露 accepted command replay disposition 不可区分：相同 proposal 的持久化重放仍返回 `accepted`，使有界目录 scan 每轮都在同一对象后返回。该 target 的正式 baseline 和 normal evidence 已作废。
- 当前实现仅收紧 proposal adjudication 语义：首次成功接纳返回 `accepted`；同 command 的成功重放返回 `exact_replay`，不重新读取 payload；冲突、拒绝和 command mismatch 保持原有 fail-closed 行为。没有新增 schema、兼容路径或运行模式。
- 新 runtime 回归测试覆盖两次连续目录 scan：第一次在首个新 payload 后返回；第二次越过首个 exact replay，并在第二个新 payload 后返回。authority 测试同时证明 payload 删除后 command replay 仍可由持久化结果判定。
- 受影响 focused tests 为 `57 passed`；clean candidate 的 Ruff、44 个 plan04 focused tests和完整测试均通过，完整测试为 `578 passed`。证据见 `logs/plan04/focused_replay_fix_20260811_182700.log` 与 `logs/plan04/development_tests_replay_fix_20260811_182900.log`。
- 已重新检查 `LeaderSession.ingest_proposal`、`_ingest_proposals`、terminal ingest callback、exact quorum selection、正式配置、场景 oracle、source identity 与 PBS 提交边界。当前未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `c6aa324570ddf176568e6efe9f65dcb9239399a0`、fingerprint `sha256:9845e47c83d24898a2bd6c4d5cec8dae618a717dd23415890b7eb46434e400be`、`dirty=false`。

## 统一 2,000-step 工作量后的增量 PREFORMAL 审查

- 用户澄清 baseline 必须与 Dynamic Full 的 `200 local steps × 10 global steps` 对齐为每 rank 2,000 optimizer steps。当前实现已直接删除 500-step 配置与提交入口，只保留唯一 2,000-step baseline：DDP 每步同步；Periodic Average 每 200 steps 同步，共 10 次。
- canonical source scopes 已纳入 `torch_ddp_baselines`，因此 baseline 配置、实现、测试和启动脚本与 Dynamic Full 一同绑定到 source fingerprint；不存在两套 formal identity。
- 根据中止前 normal run 实测每个 248 MB proposal 的验证吞吐，actor/supervisor walltime 调整为 25/30 分钟，supervisor timeout 为 1,500 秒。配置、PBS 默认值、CLI 默认值和一行提交入口一致，不保留旧 20 分钟路径。
- 新 baseline 行为测试覆盖 2,000 steps、每 200 steps 的十个同步边界、完整健康证据和末次同步缺失的 fail-closed 行为。受影响 focused tests 为 `69 passed`；clean candidate 的 Ruff、44 个 plan04 focused tests 和完整测试均通过，完整测试为 `578 passed`。证据见 `logs/plan04/focused_2000step_alignment_20260811_184700.log` 与 `logs/plan04/development_tests_2000step_alignment_20260811_185000.log`。
- 当前 Codex 已重新检查 baseline config/train/health/submit 路径、Dynamic config、scenario supervisor、统一汇总器、source identity、实验 oracle 和 PBS 静态边界，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `2debadd68e71f99c5564eba59d977cfe06d517d4`、fingerprint `sha256:fa8d335bfe6a98286d27aa0abdea092edf32a7ca5243354de3703e5fbdac06b4`、`dirty=false`。

## Terminal drain failure 后增量 PREFORMAL 审查

- `2debadd` 的 normal run 已完成 10 个 exact merge 和 8 个 terminal ack，但 terminal callback 对 ack 后不再允许的 proposal 重复执行大 payload 验证，无法及时发布 terminal record；该 target 的正式 evidence 已作废。
- 当前 proposal ingest pass 先捕获 current contributor、controller 和 terminal fence 三者的同一轮只读 snapshot。小型 descriptor 只有同时满足 current fence 和 terminal frozen-input 规则时才进入 payload verification；authority 事务内继续执行最终 fence 检查，覆盖 snapshot 后的并发状态变化。
- 新行为没有添加 schema、配置、兼容分支或第二套输入路径。open/preclosing 行为不变；closing/draining 仅允许 awaiting-ack fence 或 ack 明确声明的 final update；finalized 状态不再读取 contributor payload。
- 回归测试证明 acked fence 的非 final proposal 不会到达 payload boundary。修正 fixture 后 focused tests 为 `70 passed`；clean candidate 的 Ruff、44 个 plan04 focused tests 和完整测试均通过，完整测试为 `579 passed`。证据见 `logs/plan04/focused_terminal_ingest_fix_2_20260811_193300.log` 与 `logs/plan04/development_tests_terminal_ingest_fix_20260811_193400.log`。
- 当前 Codex已检查 terminal close/ack/input/finalize 状态机、proposal scan、authority 二次校验、正常与故障场景 oracle、source identity 和 PBS cleanup，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `bace678a97c378ef115116e4d5ca933c7abd24b0`、fingerprint `sha256:d684fd1371c0967048375b23258393fc2e304b8f104db8e67901521b9c8236b1`、`dirty=false`。

## Formal-size payload 性能修正后的增量 PREFORMAL 审查

- `bace678` normal 的 authority 时间戳证明每个 248 MB BF16 proposal 验证稳定耗时约 28 秒；`_inspect_safetensors` 的 BF16 Python scalar finite scan 是主导开销，会使 Dynamic Full wall time 必然超过 baseline 20% 以上。旧 target 的正式 evidence 已作废。
- 当前实现只替换 finite-value 检查的执行方式：同一已打开 descriptor 的 tensor bytes 通过 `torch.frombuffer` 和 `torch.isfinite` 执行向量化 CPU 检查。payload path、inode、前后 stat、两次 digest、safetensors 结构、schema、dtype、numel 和 non-finite 拒绝语义不变；没有新增配置、schema、兼容分支或运行模式。
- 新 BF16 回归测试以包含 NaN 的 wire payload 证明向量化路径继续 fail closed。已有 float32 finite/non-finite、digest/schema mismatch、rename、same-inode mutation、symlink/FIFO 和 transient I/O 测试继续通过。
- 独立 compute benchmark 对正式运行中的真实 248,879,712-byte BF16 proposal 返回 `ok`，耗时 0.436 秒；扩展 focused tests 为 `81 passed`。clean candidate 的 Ruff、44 个 plan04 focused tests和完整测试均通过，完整测试为 `580 passed`。
- 当前 Codex已重新检查 `verify_proposal_payload`、`_inspect_safetensors`、authority caller、mutation fail-closed tests、正式配置与场景时延边界，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `0e229273ff5522bc58fb77d624926b1930a8f659`、fingerprint `sha256:522770d2aaf18880a41eb1084d0b4d80ef22db95ea1e0b9b1509508b88b14ec6`、`dirty=false`。

## Authority archive 证据修正后的增量 PREFORMAL 审查

- `0e22927` 启动正式 baseline 后，对已完成的 `bace678` normal 诊断执行最终 oracle，发现 maintenance 已把 versions 1–9 和早期 update 移入不可变 audit archive，而 scenario supervisor 与统一汇总器只读取 hot table。运行本身已正确到达 v10、10 次 exact four-way merge 和 8 个 terminal fence；错误发生在终态证据读取层。旧 target 的正式 evidence 已作废，两个刚启动且尚未进入有效 workload 的 baseline job 已立即停止。
- 当前实现以 `fs_diloco.storage.audit_archive` 作为唯一 logical authority read API。它逐批验证 archive root、partition、manifest、文件类型与摘要，把已验证 archive rows 和 hot rows 合并，并在同一主键出现不一致内容时 fail closed；SQL 标识符只接受受限 ASCII identifier。scenario supervisor、canonical Full Protocol checker 和统一汇总器复用该 API，不保留各自的 archive reader。
- final oracle 现在从 logical `updates`、`global_versions` 和 terminal fence 重建完整事实；正式 2,000-step workload 由每个 terminal fence 的 `final_cycle_seq=10` 与 `inner_steps=200` 推导，不再错误要求 terminal-acked 但最终 proposal 未被 merge 的 stream 必须保留 hot update row。
- archive-aware focused tests 为 `92 passed`；clean candidate 的 Ruff、45 个 plan04 focused tests和完整测试均通过，完整测试为 `582 passed`。真实 `bace678` run 由新 oracle 重放通过，并成功生成统一 diagnostic summary；证据见 `logs/plan04/focused_archive_aware_evidence_2_20260811_201800.log`、`logs/plan04/development_tests_archive_aware_clean_20260811_202100.log`、`logs/plan04/diagnostic_bace_archive_aware_oracle_2.log` 与 `artifacts/diagnostic_bace_summary.csv`。
- 当前 Codex已检查 archive trust boundary、冲突合并、终态 workload 推导、三个生产 consumer 及其回归测试，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `5b4dab6fe000867183d6008b268a14b1a6c9e1a2`、fingerprint `sha256:a9a85660a6c0d797ed734b03811c20f27f6da831f15289331c915205f177415c`、`dirty=false`。

## 统一 local/global horizon 后的增量 PREFORMAL 审查

- `5b4dab6` normal 的严格 oracle 证明原 `global_only` 会在每 stream 的 `final_cycle_seq=7` 时关闭；global v10 只代表 40 个 quorum-applied updates，不能证明 8 个 learner 各自执行 2,000 optimizer steps。旧 target 的两个 baseline 与 normal 均已作废。
- 当前正式配置使用唯一 `local_and_global` completion mode：`max_local_steps=2000`、`inner_steps=200`、`stop_after_outer_steps=10`。配置验证要求 local horizon 包含完整 inner cycle 且 global target 存在，不增加 alias、fallback 或旧名称。
- Learner 只有在 local 2,000 steps 与 global v10 都可见时才等待 leader close；任一 horizon 先到时保持存活。replacement/restart 从 stable stream 的 `next_cycle_seq` 恢复 local step 与 scheduler coordinate，因此 victim 与 replacement 合计恰好完成 cycle 10，不会额外重跑 2,000 steps。
- Syncer 在 v10 到达而任一 current stable stream 的 durable progress 尚未到 cycle 10 时继续 ingest receipt、执行 capacity recovery，但把 normal merge 结果固定为 `NO_BATCH`，禁止 v11。全部 stream 到 cycle 10 后沿用既有 terminal freeze/ack/finalize 路径。
- focused tests 为 `116 passed`；clean candidate 的 Ruff、49 个 plan04 focused tests和完整测试均通过，完整测试为 `589 passed`。当前 Codex已检查 normal、replacement、late-failure capacity tick、strict v10边界、terminal fence oracle与 summary 2,000-step 推导，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `0f7d3aa9167b0656e9baacf93c26834e392f95c5`、fingerprint `sha256:80b8dc5f5f06aaba02ed8becf4895dbdca4e30efc9047ea5026e34cc4e74d050`、`dirty=false`。

## 最终 global horizon adoption 修正后的增量 PREFORMAL 审查

- `0f7d3aa` normal 证明 syncer 正确把 authority 固定在 v10 并继续 ingest local cycles，但 learner telemetry 记录 `current_version=10` 后仍完整等待不存在的 v11，每次 `120.002` 秒。该 target 的 baseline 与 normal 已作废并完成 exact-ID cleanup。
- 当前 adoption flow 在每次 publish 后仍先执行非阻塞 newer-latest read；仅当 `local_and_global` learner 已加载 configured final global target 且没有 newer latest 时跳过阻塞等待。global target 前、其他 completion mode及真正可见 successor的 adoption 语义不变。
- Replace strategy 立即继续 local cycle；Rebase 和 Predict strategy 同时禁止创建只能由 v11 reconcile 的 anchor/prediction state。没有新增配置、schema、兼容分支或第二套 runtime 路径。
- 扩展 focused tests 为 `136 passed`；clean candidate 的 Ruff、49 个 plan04 focused tests和完整测试均通过，完整测试为 `592 passed`。当前 Codex已检查三个 adoption strategy、v10 non-blocking read、terminal observation、local cycle推进和 source identity，未发现 open blocking finding。

Verdict：`APPROVE`。新的唯一 `FINAL_COMMON_TARGET` 为 commit `12ae38993d94cce8d15b1e842c9123d22d5148b3`、fingerprint `sha256:73614127d64bf8ae1dd23b763c5d8c883ba3084e7d2e97dfb5af1bf8591765ee`、`dirty=false`。
