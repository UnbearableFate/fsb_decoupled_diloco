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
