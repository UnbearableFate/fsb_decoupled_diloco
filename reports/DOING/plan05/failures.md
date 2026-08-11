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
