# Plan 构建与实施入口规则

本文件递归适用于 `plans/` 下的计划、设计、矩阵和实施记录。它只保留必须自动加载的入口约束；plan 构建方法位于 [`plans_create_guide.md`](plans_create_guide.md)，完整实施流程位于 [`workflow.md`](workflow.md)。

## 强制加载

- 实施、测试、审查、迁移或关闭 `plans/DOING/**` 下任何 plan 前，必须完整读取并遵循 `plans/workflow.md`。
- 同时涉及 plan 构建和实施时，两份文件都必须读取；不得只依据 plan 正文、旧报告或记忆推断规则。

约束优先级如下：

1. 仓库根目录 `AGENTS.md`；
2. 本文件；
3. `plans/plans_create_guide.md`（构建/修订时）与 `plans/workflow.md`（实施时）；
4. 具体 plan、design、requirement matrix 和 phase 说明。

Plan 可以增加更严格的测试或验收条件，但不能静默削弱上述规则。若 plan 与 workflow 冲突，停止冲突动作，在对应报告中记录差异，并以更严格且不违反上级规则的一方为准；需要扩大权限或降低门禁时必须由用户明确决定。

## 职责边界

- Plan 定义需要完成的功能、代码、测试、实验和验收条件。
- `plans/plans_create_guide.md` 定义如何从当前代码和证据构建可实施、可验证、可审计且成本合理的 plan。
- `plans/workflow.md` 定义如何实施 plan，包括记录、失败升级、多智能体审查、Miyabi/PBS 路由、phase/plan 完成门禁和 artifact 清理。
- Codex/GPT 主实例是唯一实现协调者和主工作树 writer。外部 reviewer 只能读取冻结目标并返回报告，不得修改实现、测试、配置、计划、Git 状态、scheduler 状态或 run 数据。
- 所有外部 reviewer agent 进程必须通过 `scripts/miyabi/run_multi_agent_review.pbs` 在 Miyabi compute node 上运行。Miyabi login 节点只允许准备 prompt、运行静态 shell 检查、`qsub`、`qstat` 和读取结果；不得直接启动 Claude/OpenCode review 实例。

## 记录与完成声明

- 实施记录写入 `reports/DOING/<plan-id>/`，多智能体审查写入 `reports/DOING/code_review/<plan-id>/`；不得写回 plan 正文。
- 未通过 `plans/workflow.md` 对应状态门禁前，不得宣布 work unit、phase 或 plan 完成，也不得开始下一 phase。
- Plan 完成后才可把 plan/report 移入 `plans/DONE/` 和 `reports/checked/`；移动提交不得改写已冻结 artifact 内容或丢失 evidence 路径映射。
- 正在执行的旧 plan 采用新 workflow 时必须先记录 migration checkpoint；不得把新增加的前置步骤倒推为已完成，也不得无证据重做或改写历史结果。
