# plan04 失败记录

## 2026-08-12 — normal comparison 错误拒绝更短训练时间

- 类别：`harness-failure`；comparison gate 第一次有效失败。
- Source：`d0acc5f9f95cb3d3885baf947319b93d84caeff1`，fingerprint `sha256:b5b51b507d339a2a61d8b937023a61131cb35fb588dc5131ecf814ae63c5b8c7`。
- Experiment：normal，supervisor `2540602.opbs`，run `plan04_e1_normal_20260812_212502`，artifact `reports/DOING/plan04/artifacts/20260812_212502_e1_normal.json`。
- 实际产品结果：version 25 完成；25 个 version 均有四个 200-step applied update；八 learner 与一个 syncer 均有 immutable attestation；所有 actor scheduler history 为 FINISH；live syncer scope 只保留 version 25 weight；最终 loss `3.1977`，相对 DDP/periodic baseline 仅高 `8.93%`/`6.63%`。
- 失败症状：normal 训练时间 `604.33s`，比 DDP `1129.11s` 和 periodic `1058.37s` 分别短 `46.48%` 和 `42.90%`，却被 absolute-difference gate 判为 FAIL。
- 根因：quorum 4 的 Full Protocol 在 25 个 global version 上只要求 100 个 applied learner cycle，共 20,000 个 applied optimizer steps；八 rank baseline 各执行 5,000 steps，共 40,000 rank optimizer steps。更短 wall time 是当前 merge workload 的预期结果，不是性能回退。
- 修复：30% gate 改为 signed regression gate，只拒绝 loss 或 training time 相对 baseline 升高超过 30%；identity mismatch 仍拒绝。新增 mutation 覆盖超过 30% 的指标下降不得触发调查。
- 证据有效性：该 run 的 durable 产品证据保留用于根因分析，但 source scope 将因 checker 修复变化，不能用于最终共同 target。修复后从 PREFORMAL 重新冻结，并重跑 baseline、normal 和后续正式实验。

## 2026-08-12 — 并行提交超过作业并发容量

- 类别：`infra-invalid`；不计为产品失败或验证工具失败。
- Source：`7145197f3209fa67727bb0d458d0db38a81eb86d`，fingerprint `sha256:fcecd04f3ca5720cc3cd576889d8c20ad5d3f9dd9dd5725c7415b0549cdffa35`。
- Experiment 4：learner simultaneous failure，supervisor `2540902.opbs`，run `plan04_e4_learner_failure_simultaneous_20260812_220533`，artifact `reports/DOING/plan04/artifacts/20260812_220533_e4_learner_failure_simultaneous.json`。
- Experiment 5：learner staggered failure，supervisor `2540903.opbs`，run `plan04_e5_learner_failure_staggered_20260812_220534`，artifact `reports/DOING/plan04/artifacts/20260812_220534_e5_learner_failure_staggered.json`。
- Experiment 7：dual syncer，supervisor `2540906.opbs`，run `plan04_e7_dual_syncer_20260812_220534`，artifact `reports/DOING/plan04/artifacts/20260812_220534_e7_dual_syncer.json`。
- Experiment 2：stagger 4+4，supervisor `2540900.opbs`，run `plan04_e2_stagger_4_4_20260812_220533`。只有两个 learner 在 admission deadline 内存活，其余 learner 因排队延迟而超时或被已占用的 admission fence 拒绝，无法形成 quorum；确认该 run 不可能推进后，精确取消 supervisor、syncer 和两个存活 learner，为仍可完成的并行场景释放槽位。
- Experiment 3：stagger 3+3+2，supervisor `2540901.opbs`，run `plan04_e3_stagger_3_3_2_20260812_220533`，artifact `reports/DOING/plan04/artifacts/20260812_220533_e3_stagger_3_3_2.json`。五个存活 learner 将训练推进至 version 25，证明终态不依赖八个 learner 同时存活；但 stream 3 的 actor 在排队后刚 admission 即退出，终态只能记录一个计划外 `hard_crash` fence，因此该 run 不满足无故障 stagger 场景的全 `acked` oracle。
- Experiment 6：syncer failure，supervisor `2540905.opbs`，run `plan04_e6_syncer_failure_20260812_220534`。多个 bootstrap learner 在排队后错过 admission deadline；successor syncer 虽已接管并为一个过期 stream 启动 replacement，但当前 run 已包含计划外 learner 故障，不再满足只注入 syncer 故障的场景边界。确认无法成为正式证据后，精确取消 supervisor、存活 actor 和 scheduler 启动的 replacement。
- 失败症状：Experiment 4 和 5 在 60 秒故障边界到达时没有已 admission 的 bootstrap learner；Experiment 7 的 candidate syncer 在 180 秒等待期内没有进入运行状态。
- 根因：按并行策略一次提交六个场景后，actor 作业数量超过当前用户可同时运行的作业容量。关键 actor 长时间排队，因此这些 run 未进入其预期故障注入阶段。
- 清理：三个 supervisor 均按 ownership 精确取消了各自仍存活的 actor，所有 `qdel` 返回成功；失败 artifact 保留用于基础设施诊断。
- 后续：不修改产品代码或 oracle；释放足够运行槽位后重跑受影响场景。可以继续并行准备和提交，但运行时并发度需服从实际作业容量。
- `debug-g` 切换后的补充事实：`qstat --rscuse` 反映 queue 总节点容量，但 `qstat --limit` 显示项目同时运行作业上限为 `16`。因此即使 debug-g 尚有空闲节点，同时启动多个各含 10–11 个作业的故障场景仍会使 actor 排队。
- 在 Experiment 4 已运行时并行提交的 Experiment 5 `plan04_e5_learner_failure_staggered_20260812_231510` 和 Experiment 6 `plan04_e6_syncer_failure_20260812_231510` 触及该上限。两者按计划完成了故障 qdel，但多数 actor 无法在 admission deadline 内运行；为避免产生混合故障证据，已按 exact run ownership 取消 supervisor、actor、successor 和 scheduler-authorized replacement。这两次仍为 `infra-invalid`，未修改产品逻辑或 oracle。
- Experiment 5 `plan04_e5_learner_failure_staggered_20260812_231858` 与仍在运行的 Experiment 4 重叠后，七个 bootstrap learner 已 admission 并推进至 version 8，但 replacement job 受 `16` 个同时运行作业上限阻挡，未能在 recovery deadline 内 admission。Artifact `reports/DOING/plan04/artifacts/20260812_231858_e5_learner_failure_staggered.json` 因此为 `infra-invalid`；supervisor 已清理全部 owned job。后续故障场景保持代码和脚本预先就绪，但正式运行不再互相重叠。

## 2026-08-12 — learner fault 终态 oracle 拒绝已裁决 hard crash

- 类别：`harness-failure`；learner-fault oracle 第一次有效失败。
- Source：`d2117d0c83f205eabd2246feec82a77c2e0230c0`，fingerprint `sha256:2c651ee95fda24539fa597b85753706f16d40b847cde4cbeedba208f01a7998d`。
- Experiment：learner simultaneous failure，supervisor `2541414.opbs`，run `plan04_e4_learner_failure_simultaneous_20260812_231146`，artifact `reports/DOING/plan04/artifacts/20260812_231146_e4_learner_failure_simultaneous.json`。
- 实际产品结果：八个 bootstrap learner 均 admission；随机删除 stream 1 后，production capacity service 提交并 admission replacement `2541434.opbs`；训练完成 version 25 和 100 个 200-step applied update。Replacement 以更高 stream epoch 接续。终态五个 stream graceful ack，三个未被 merge 选中的 learner 在等待 receipt barrier 时超时，authority 将其裁决为带 bounded-gap 的 `hard_crash` fence。
- 失败症状：plan04 oracle 只接受终态 fence state 集合恰好为 `{acked}`，因此在产品 authority 已完成裁决的情况下报 `terminal fences contain a foreign or unacknowledged stream`。
- 根因：oracle 比生产 terminal contract 和统一 summary parser 更严格；后两者明确接受 `acked` 与带 bounded-gap 证据的 `hard_crash`。这也与 plan 只约束存活 actor 的权重清理、允许死亡 actor 遗留物的设计不一致。
- 修复：plan04 oracle 接受协议定义的两个已裁决状态，仍拒绝外部 stream 和其他状态；hard-crash 的 bounded-gap/current-shape 校验继续由统一 summary parser 执行。新增 owning regression test。新 target `a064ef8837ca033db883fe60879c4578b921b09c` 在 Miyabi compute `2541627.opbs` 上通过 focused `26 passed` 和 full suite `596 passed`。

## 2026-08-12 — Experiment 4 的旧诊断规则覆盖最新通过标准

- 类别：`harness-adjudicated`；产品结果有效，不需要重跑。
- Experiment：learner simultaneous failure，supervisor `2541648.opbs`，run `plan04_e4_learner_failure_simultaneous_20260812_233703`，artifact `reports/DOING/plan04/artifacts/20260812_233703_e4_learner_failure_simultaneous.json`。
- 终态证据：最终 Syncer `2541649.opbs` 的 PBS `Exit_status` 为 `0`；authority final version 为 `25`；`runs/summary.csv` 中最终平均 loss 为 `3.288647`。
- 旧 artifact 失败原因：replacement 专用诊断认为 successor 与 expired stream 的绑定证据不完全一致，并报 `replacement does not prove exact expired-stream succession`。该检查发生在三个终态条件全部满足之后。
- 判定：按最新要求，Experiment 4 通过。除最终 Syncer 正常退出、global version 25 和最终平均 loss < 3.5 之外，其他检查只保留为诊断信息。
- 修复：supervisor 的 PASS/FAIL 仅由三个终态条件决定。场景专用 oracle 仍生成诊断，但诊断失败不再覆盖终态结果。该 harness 变更不修改训练或协议功能，因此不废弃此前已完成的实验结果。
