# plan05 进度

## 2026-08-11 INIT：启动盘点

- Source：branch `plan05`，commit `6fe4e8f3880151600afdca16ba55b8a77387717b`，clean worktree；workflow pin `7288dc8086a95d2294a92fb999e8539991d86ec1`。
- 环境：`miyabi-g3`，无 `PBS_JOBID`/`PBS_NODEFILE`，因此按 control-plane 路径只执行静态盘点。
- 检查：读取 plan/workflow/Miyabi skill；通过 CodeGraph 确认 fence/scope 到 authority/runtime/tool/test 的调用扩散；repository-wide 初始搜索在排除历史报告和本 plan 后得到 654 个待收敛命中。
- 结论：当前仓库仍是完整双模式实现，尚无 plan05 产品改动；已冻结删除边界、formal source scopes、验证阶梯、正式三场景指标/阈值与 12 项 requirement。
- 下一动作：先固定新版本和唯一 config/descriptor/protocol shape，再收敛 authority/DDL/admission/runtime，并随行为迁移现有 test owner。

## 2026-08-11 IMPLEMENT：唯一协议候选完成

- 实现：配置只保留 `membership.stream_pool_size` 与 `bootstrap_instances`；descriptor、fence、admission、authority、DDL、runtime、launcher、Checker 和 summary 已收敛为唯一 stream/instance 协议。旧类型、旧字段、静态 binding/replacement、附加 DDL、重复配置与旧 completion Checker 已删除。
- Capacity：`scaling.enabled` 只控制自动容量管理；desired contributor 数直接约束于 stream pool，不再错误受 merge quorum 上限限制。正式 scaling 场景以 8 为恢复目标，并在 7 个 productive instance 时触发容量不足判定。
- 测试与文档：现有 test owner 已迁移到唯一协议；实验入口仅保留无故障、故障不替换和授权替换三个场景；README、网站与生成 API reference 已同步，研究计划中的旧对照仅保留为明确标记的冻结历史事实。
- Login 静态检查：Python compile、Ruff format/lint、JSON 解析、全部 PBS/shell `bash -n`、literal group `xg24i002`、配置加载、reference 生成、`git diff --check` 和 dead-surface 搜索通过。旧名称只剩严格拒绝或 absence 断言，以及排除范围内的冻结历史记录。
- 下一动作：提交 clean candidate，在 1-node PBS compute job 上运行 focused、full pytest 和网站 build/lint 验证阶梯。

## 2026-08-12 VALIDATE：完整 U1 门禁通过

- 连续失败审查：前三次有效 U1 失败后，已在第四次尝试前完成全面 failure review。审查定位并修复严格 run identity、durable stream-pool identity、terminal/bootstrap oracle 和迁移测试问题；完整结论见 `reports/DOING/code_review/plan05/failure-U1-one-node-validation-round1/`。
- 工具链：Miyabi compute 环境未预装 Node.js。使用 PBS job `2531022.opbs` 安装固定 Node.js `22.13.1`、npm `10.9.2` 和 lockfile dependencies；validation runner 现在显式接收唯一 npm 路径并把对应 Node runtime 加入 PATH，不跳过网站门禁。
- 最终 U1：clean commit `08f10085b8894186f271b2942efdc6ca7df72469`、fingerprint `sha256:8bd50e63c3fda0b31c0c7eb8a55e307fe7c6602d6ce54ff4e4aa86a0c660e8cd` 在 PBS job `2531067.opbs`、compute node `mg0850` 上通过 Ruff format/lint、focused `251 passed`、full `580 passed`、website lint 和 14 项 rendered-site test。
- 证据：`reports/DOING/plan05/artifacts/validation_candidate8.json` 状态为 `PASS`，无 errors；raw log 与两份 JUnit 均由同一 job create-only 发布。生成的 API reference 在 validation 前后无差异。
- 下一动作：对全部 formal source scopes 完成 coordinator current-state 审查，并通过固定 OpenCode `opencode-go/deepseek-v4-flash` 运行唯一外部 PREFORMAL reviewer；处置所有有效 finding 后再冻结 `FINAL_COMMON_TARGET`。

## 2026-08-12 VERIFY：唯一协议 functional harness 稳定通过

- Runtime 修复：三次有效 functional 失败依次定位出 proposal ingestion 的跨 stream 饥饿、receipt ack 未等待 proposal ingestion，以及 Checker 把 `quorum_max` 误作固定 batch size。实现现在优先服务尚未进入 pending quorum 的 current stream；含 proposal 的 cycle 只在 proposal durable accepted/exact replay 后 ack；Checker 按每版 `[quorum_min, quorum_max]` 和 durable applied proposal 推导 selection credit 与 direct token。全面 failure review 见 `reports/DOING/code_review/plan05/failure-functional-harness-round1/`。
- 最终 U1：clean commit `b44cb01bc4f4e22042aea614e349b1849bd0d16a`、fingerprint `sha256:2b5364a6d61f7cad546cbf5ace795d3b6149623abbce88a3976bdef7c2cb8a25` 在 PBS job `2531385.opbs`、compute node `mg0840` 上通过 Ruff、focused `256 passed`、full `585 passed`、website lint 和 14 项 rendered-site test。证据为 `reports/DOING/plan05/artifacts/validation_candidate12.json` 及同名前缀日志/JUnit。
- No-failure：同一 clean target 在 PBS job `2531398.opbs` 的 5-node co-allocated harness 上完成 4-stream、20 inner step、4 global version；12 个 proposal applied、4 个 dropped，direct applied/dropped 为 3840/1280，ledger balance 为 0，4 个 terminal fence 全部 ack。`functional_no_failure_candidate4.json` 为 create-only `PASS`。
- Syncer takeover：PBS job `2531406.opbs` 在相同 5-node workload 的 version 2 边界终止 primary；durable evidence 证明 transaction inactive、renewer quiesced、epoch 1 expired/superseded、epoch 2 完成 v4 且无 stale commit。15 个 receipt/proposal 一一对应，direct applied/dropped 为 3840/960，ledger balance 为 0；`functional_syncer_takeover_candidate1.json` 为 create-only `PASS`。
- 下一动作：对全部 formal source scopes 完成 coordinator current-state 审查，再只用固定 OpenCode `opencode-go/deepseek-v4-flash` 执行唯一外部 PREFORMAL review；关闭 blocking finding 后冻结 `FINAL_COMMON_TARGET`。
