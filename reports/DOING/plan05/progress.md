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
