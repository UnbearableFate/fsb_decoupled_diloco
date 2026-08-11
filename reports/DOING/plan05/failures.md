# plan05 失败记录

## U1 candidate 1：focused suite 识别迁移缺陷

- 分类：1 个 product failure 与 4 组 harness failure；计入 U1 验证域第一次有效失败。
- Source：commit `fbd2a7021f787a8a45c16b0cb24ed6bec08337cd`，clean source fingerprint `sha256:c20cc8b763d3f835e802c7ef2491748fdb18c59860cda8900a8275717df27563`。
- 环境：PBS job `2530766.opbs`，compute node `mg0216`；focused suite 共 196 项，其中 55 项失败。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate2.json`、`validation_candidate2.log` 与对应 JUnit。
- 最小症状：authority row decoder 仍构造 proposal format 2；公共 receipt/proposal fixture 的 contributor key 仍使用旧逻辑名称；replacement fixture 使用过期的绝对时间；Syncer candidate fake 未实现唯一 membership 初始化；terminal ack fixture 未使用 instance identity；module coverage 顺序与 tracked surface 不一致；无 outer-step 的配置测试先触发 terminal-policy 错误。
- 处置：decoder 改用唯一版本 owner；fixture 全部改用 stream key/current instance；注入确定性 authority clock；补齐 fake current API；重新排列 module coverage；把 completion/stop-target 校验放到 terminal-policy 校验前，并把 direct-token target 作为有效 global stop target。
- 下一验证：提交修复后的 clean candidate，在 fresh one-node PBS job 上重跑完整 U1 ladder。

## U1 candidate 2：terminal oracle 仍保留已删除的 ownership 假设

- 分类：5 个 harness/oracle failure；计入 U1 验证域第二次有效失败。
- Source：commit `fc7e41f9241f1883fc6226730e1b96ad211220cd`。
- 环境：PBS job `2530831.opbs`，compute node `mg0837`；focused suite 共 196 项，其中 5 项失败。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate3.json`、`validation_candidate3.log` 与对应 JUnit。
- 最小症状：aggregate Checker 仍要求 terminal stream 保留 current instance，且把 admission 持久化生成的 bootstrap launch row 误判为 capacity launch；replacement authorization 不匹配测试仍假设旧 fence 保持 current，但生产事务已经依据精确 scheduler terminal evidence 释放该 instance。
- 处置：Checker 改为验证 terminal stream 已释放、terminal fence 与 instance history 一致、每个 stream 恰有一个 bootstrap admission，且只禁止非 bootstrap capacity launch；replacement 拒绝测试改查旧 instance 已过期且 current fence 为空。正式实验 oracle 同步分离 bootstrap admission 与非 bootstrap launch request。
- 下一验证：提交修复后的 clean candidate，在 fresh one-node PBS job 上重跑完整 U1 ladder；若仍失败，则先完成连续三次失败审查，再决定后续候选。

## U1 candidate 3：完整测试发现两个 identity 缺陷

- 分类：2 个 product failure 与 2 个 harness migration failure；计入 U1 验证域第三次有效失败，并在第四次提交前触发全面 failure review。
- Source：commit `af7c1b980203113870cdded20c8aa8d6de9727f6`，clean source fingerprint `sha256:3a6b5708d6df083ce19d98ab8976d0ce827a490581b9c91a7d3c6bc91e94ce36`。
- 环境：PBS job `2530890.opbs`，compute node `mg0837`；focused suite 196 项全部通过，full suite 共 578 项，其中 4 项失败。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate4.json`、`validation_candidate4.log` 与 focused/full JUnit。
- 最小症状：`.identity` 接受 checksum 正确的未知旧字段；authority reopen 没有把调用者的 stream-pool scope 与 durable identity 比较；receipt path 测试仍要求旧 `learner-` key；summary 测试的改名后顺序期望与 fixture 的 `full_protocol/`、`torch_ddp_baselines/` 目录顺序不符。
- 全面审查：见 `reports/DOING/code_review/plan05/failure-U1-one-node-validation-round1/codex-gpt_af7c1b980203113870cdded20c8aa8d6de9727f6_2530890.md`。审查已追踪输入、状态转换、持久化、恢复、PBS 生命周期、oracle 与输出，并拒绝间接 config-hash scope 校验、延迟 streams row-count 校验、generic identity producer 和恢复旧 receipt 前缀等方案。
- 下一验证：先实施审查确定的 identity 逻辑改写与两项测试迁移，再从 fresh clean commit 运行不变的 U1 ladder；不得放宽通过条件。

## U1 candidate 4：compute 环境缺少 Node.js 工具链

- 分类：`infra-invalid`，不计入三连失败；Python 产品与 harness 门禁均已通过。
- Source：commit `6495c2d04f2f0b0c393d8f86ee8b99c68384ff14`，clean source fingerprint `sha256:a27cda2861e099b55571a4ff2072102d571ae1ff5f52ee727d691372bc1d78c8`。
- 环境：PBS job `2530971.opbs`，compute node `mg0837`；focused suite 250 项、full suite 579 项全部通过。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate5.json`、`validation_candidate5.log` 与 focused/full JUnit。
- 最小症状：Miyabi compute PATH 中没有 `npm`，runner 在启动 `website-lint` 前以 `FileNotFoundError` 发布 `BLOCKED`。
- 处置：validation CLI/PBS 显式接收并验证唯一 `NPM_BIN`；在 compute job 中安装满足 `website/package.json` 的固定 Node.js 22 工具链和 lockfile dependencies，不删除或跳过网站门禁。
- 下一验证：使用显式 Node/npm 路径提交 fresh U1 job；Python 与网站步骤必须在同一 clean target 上全部通过。

## U1 candidate 5：npm 的 Node 解释器未进入 PATH

- 分类：`harness-failure`；这是 failure review 后的新验证序列第 1 次有效失败。
- Source：commit `195fab9ed37c800e0dd4c48f3b7b73f8bb883cf5`，clean source。
- 环境：PBS job `2531033.opbs`，compute node `mg0357`；focused suite 251 项、full suite 580 项全部通过。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate6.json`、`validation_candidate6.log` 与 focused/full JUnit。
- 最小症状：runner 已执行显式绝对 `NPM_BIN`，但 npm 的 `#!/usr/bin/env node` 无法在继承 PATH 中定位同目录的 `node`，`website-lint` 以 127 退出。
- 处置：PBS wrapper 解析并验证 npm 的规范绝对路径，把其 `bin` 目录加入 PATH 后再启动 validation CLI；仍使用同一固定 Node/npm 工具链。
- 下一验证：fresh U1 job 必须实际进入并通过 website lint/test，而不是只通过 npm 路径存在性检查。

## U1 candidate 6：生成的 API source revision 落后一个提交

- 分类：`source-invalid`，不计入有效失败；全部执行门禁本身均通过。
- Source：commit `3ec7a5276bf7586c753b1fb44ffc8637ff52b72d`，启动时 clean。
- 环境：PBS job `2531053.opbs`，compute node `mg0100`；Ruff、focused 251 项、full 580 项、website lint 和 14 项 rendered-site test 全部通过。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate7.json`、`validation_candidate7.log` 与 focused/full JUnit。
- 最小症状：`api-manifest.json.sourceRevision` 在包含 identity 修复的 commit 创建前生成，仍指向更早的 `fc7e41f…`；website test 按设计重新生成到 `6495c2d…`，导致 validation 的 source-before/source-after identity 不一致。
- 处置：在 fs_diloco 修改已提交后重新生成并提交 API reference；不关闭 source mutation 检查，也不忽略生成物差异。
- 下一验证：fresh clean U1 job 必须证明 reference generation 是无差异操作，并再次通过全部门禁。

## Functional harness candidate 1：高吞吐 stream 饿死其他 proposal ingestion

- 分类：`product-failure`；计入 unique-protocol functional harness 验证域第一次有效失败。
- Source：commit `f8509c71baefcbc509fb4a41c074fc0f8a423126`，clean source fingerprint `sha256:8bd50e63c3fda0b31c0c7eb8a55e307fe7c6602d6ce54ff4e4aa86a0c660e8cd`。
- 环境：PBS job `2531131.opbs`，compute nodes `mg0201`、`mg0993`、`mg0994`、`mg0996`、`mg0997`；4-stream synthetic functional config，no-failure 场景。
- 证据：`runs/full_protocol/plan05_functional_none_20260812/`、`logs/qsub_plan05_functional_none_20260812/` 与 `reports/DOING/plan05/artifacts/functional_no_failure.pbs.log`。job 在确认 durable non-progress 后以 exact owned ID `2531131.opbs` 取消，未产生 PASS artifact。
- 最小症状：4 个 learner 均持续发布并收到 receipt ack，但约 2 分 29 秒后 global version 仍为 0。authority 中 stream 0 已 ingest 273 个 update，stream 1 仅 1 个，stream 2/3 为 0；syncer 每轮按路径重新从 stream 0 开始扫描，并在首个新 payload 后返回，快 stream 因而永久占用唯一 ingest slot。
- 根因：`_ingest_proposals` 的“每次只验证一个新 payload”成本边界缺少跨 stream 的 durable fairness。目录顺序与单次 early return 组合后，merge quorum 无法形成；单元测试只覆盖同一 stream 的 early return/replay，未覆盖已有 pending stream 与未服务 stream 竞争。
- 处置：authority read model 暴露当前 pending update 的 stream key；proposal scan 优先处理 current 且尚未进入 pending quorum 的 stream，再处理已有 pending 和 stale stream。新增回归测试证明 fast stream 已有 pending proposal 时，下一个 ingest slot 必须服务缺失 stream。
- 下一验证：在 1-node compute 上先运行新增 focused owner，再从 fresh 5-node roots 重跑 no-failure 与 syncer-takeover functional harness；两者必须由 Checker 发布 create-only PASS artifact。

## Functional harness candidate 2：receipt ack 缺少 proposal backpressure，且 co-allocated oracle 不一致

- 分类：1 个 `product-failure` 与 2 个 harness/config failure；计入 unique-protocol functional harness 验证域第二次有效失败。
- Source：commit `f622710d6fac3d7acc622c0e6f4dd4ced1d6a4e1`，clean source fingerprint `sha256:df2b91be373b1b2a3b7a72370c002a0fdbc9ad617324d3d46fc4272c32143a5a`。
- 环境：PBS job `2531221.opbs`，compute nodes `mg0864`、`mg0866`、`mg0968`、`mg0969`、`mg0970`；4-stream synthetic functional config，no-failure 场景。
- 证据：`reports/DOING/plan05/artifacts/functional_no_failure_candidate2.json`、对应 PBS log、`runs/full_protocol/plan05_functional_none_c2_20260812/` 与 `logs/qsub_plan05_functional_none_c2_20260812/`。
- 最小症状：fair ingestion 已使训练和 terminal 正常结束，但 authority 仅 ingest 17 个 proposal，而 receipt 有 50 个；learner 在 receipt 写入 authority 后立即收到 ack，因而可在对应 proposal 尚未 ingest 时继续产生下一周期。配置允许 1 次 terminal merge，使 final version 从注册的 4 变为 5；Checker 同时把一个跨 5 个节点的 co-allocated PBS job 错当成必须单 host 的 independent scalar job。
- 根因：receipt ack 表达了 receipt durable，却被 learner 当作完整 cycle ingestion barrier；proposal backlog 因此只能在 terminal 时按 receipt token fate adjudicate，无法满足无故障场景的 receipt/proposal 一一对应。另有 functional config 与 exact final-version oracle 冲突，以及 scheduler host oracle 没有先按 co-allocated/independent topology 分支。
- 处置：含 proposal 的 receipt 只在匹配 proposal 已被 authority accepted 或 exact-replay 后发布 ack；receipt-only cycle 仍在 receipt durable 后 ack。functional config 禁止 terminal extra merge；多 host 约束只应用于 independent scalar actor jobs。新增 ack 顺序、co-allocated topology 与 functional terminal 配置回归测试。
- 下一验证：提交并完成一轮 fresh U1 后，从 fresh 5-node roots 重跑 no-failure 和 syncer-takeover；若同一 functional 域再次出现有效失败，必须在下一次提交前完成全面 failure review。

## Functional harness candidate 3：Checker 把 quorum_max 误作每版固定贡献者数

- 分类：`harness-failure`；计入 unique-protocol functional harness 验证域第三次有效失败，并在第四次提交前触发全面 failure review。
- Source：commit `15159829d9e196b70fb19e30af136474073773ea`，clean source fingerprint `sha256:07041f03098e6a33ffc359c4a1f99a7918de88b4e7b54b12fe62b30679c68cfd`。
- 环境：PBS job `2531299.opbs`，compute nodes `mg0846`、`mg0653`、`mg0654`、`mg0657`、`mg0660`；4-stream synthetic functional config，no-failure 场景。
- 证据：`reports/DOING/plan05/artifacts/functional_no_failure_candidate3.json`、对应 PBS log、`runs/full_protocol/plan05_functional_none_c3_20260812/` 与 `logs/qsub_plan05_functional_none_c3_20260812/`。
- 最小症状：run 按配置在 version 4 正常 terminal；15 个 receipt 与 15 个 proposal 一一对应，token ledger balance 为 0，拓扑和 ownership oracle 均通过。每个 normal merge 在 3 个 eligible contributor 达到 `quorum_min=3` 时提交，共应用 12 个 proposal、3840 direct token。Checker 错误要求每版固定使用 `quorum_max=4`，因此期望 16 个 proposal 和 5120 token。
- 全面审查：见 `reports/DOING/code_review/plan05/failure-functional-harness-round1/codex-gpt_15159829d9e196b70fb19e30af136474073773ea_2531299.md`。审查确认 selection transaction 的契约是 `[quorum_min, quorum_max]`，commit/selection credit/global-version/token ledger 均与实际 3-way batch 一致；不能通过改 merge 时序、把 functional quorum 改成 4/4 或放宽 token balance 解决。
- 下一验证：按审查结论改写 Checker 的 variable-quorum 公式和 regression fixture；随后运行 fresh U1，再从 fresh 5-node roots重跑两种 functional 场景。

## U1 candidate 11：variable-quorum fixture 复用了跨 stream command ID

- 分类：`harness-failure`；这是 functional failure review 后 U1 回归验证的第一次有效失败。
- Source：commit `e144ac8bb1aa610743136677e5ec3063b0f5e854`，clean source。
- 环境：PBS job `2531357.opbs`，compute node `mg0854`；focused suite 共 256 项，其中 1 项失败。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate11.json`、`validation_candidate11.log` 与 focused JUnit。
- 最小症状：新增 variable-quorum fixture 为 stream 0 和 stream 1 的第一个 receipt 都使用 `receipt-1` command ID。authority 正确把第二个不同 request 识别为 command replay conflict，因此测试在调用 Checker 前失败。
- 处置：fixture 的 receipt、proposal 和 selection command ID 全部包含 stable stream key；不修改 authority replay contract 或 Checker acceptance contract。
- 下一验证：提交 fixture 修复后，在 fresh one-node PBS job 上重新运行完整 U1 ladder。
