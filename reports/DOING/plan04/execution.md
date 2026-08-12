# plan04 执行包

## 身份与边界

- Plan：`plans/DOING/plans/plan04.md`
- Branch：`new_plan04`
- Branch point：`f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`
- Workflow pin：`f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`
- Host：Miyabi login/control plane `miyabi-g3`；项目测试和训练仅在 PBS compute node 运行。
- Formal source scopes：以 `fs_diloco.core.source_identity.SOURCE_SCOPES` 为准，包括 `fs_diloco`、`configs`、`do_experiments`、`scripts/miyabi`、`tests`、`tools`、`torch_ddp_baselines`、`pyproject.toml`、`README.md`、`docs`、`plans/00-RESEARCH_PLAN.md`、`website/app` 和 `website/scripts`。

## 当前设计

- Baseline 只保留 GPT-2/WikiText-2 的 8-rank DDP 与 periodic-average 两种模式。两者均执行 5,000 optimizer steps；periodic-average 每 200 steps 同步一次，共 25 次。
- Full Protocol 只保留 `experiment.yaml` 与 `fault_experiment.yaml` 两份当前配置。两者均为 GPT-2/WikiText-2、200 local steps、25 global steps、8 个 bootstrap job 和 4 个 proposal 的固定 merge 阈值。
- `submit.sh baseline` 提交两种 standalone baseline；其余七个场景由一个 supervisor 提交独立 learner/syncer actor。旧 Full Protocol baseline、旧 timed config 和 2,000-step baseline 已删除。
- Terminal 由 `global_target` 自动关闭，不等待全部 8 个 learner admission。3+3+2 场景在第二批提交前记录低于 quorum 时 global version 未推进的证据。
- 正常场景使用统一 summary 工具与两种 baseline 比较；模型、数据、优化器身份或注册 workload 不一致时不可比较，最终平均 loss 或训练时间相对差异超过 30% 时要求调查。
- Terminal maintenance 在 reader grace 后执行第二次清理；正式 oracle 要求最终存活 syncer epoch 的权重作用域只包含最新模型权重。死亡 actor 的遗留物不用于否定通过结论。

## 验证阶梯与资源预算

1. Login node：repository-wide current-state 审查、`git diff --check`、Python compile、PBS/Bash 语法与 literal group 检查。
2. 单节点 `interact-g`：focused plan04、summary、baseline、syncer tests，随后 clean candidate 上运行完整 pytest。
3. PREFORMAL：提交当前实现，保存 current-state 审查，冻结唯一 `FINAL_COMMON_TARGET` commit、source scopes 与 fingerprint。
4. FORMAL Baseline：两个 8-node `regular-g` job，初始 walltime 40 分钟，各 1 seed；由 health checker、run summary 和 scheduler history共同判定。
5. FORMAL Full Protocol：七个 1-node supervisor job；每个 supervisor 提交 8 个独立 learner job 和 1 个 syncer job，双 syncer 场景增加 1 个候选 syncer。Supervisor 与 actor 初始 walltime 40 分钟。
6. FINAL：逐项核对 create-only artifacts、authority、scheduler history、summary/comparison、source identity 与 cleanup；确认无 active/queued job 后归档 plan 和报告。

40 分钟是用户指定的初始预算。只有实际 scheduler/runtime 证据表明不足或明显过长时才调整；不得以延长 timeout 掩盖产品或 harness 错误。

## 高风险边界

- 八个 learner 是提交拓扑，不是 terminal 前必须全部 admission 的屏障。Authority oracle 只接受已 admitted 的提交 job，拒绝外部 actor，并要求实际每个 global version 恰好应用四个 200-step update。
- Learner fault 只删除故障边界时 authority 中已 admitted 的 bootstrap job；replacement 必须来自 scheduler-authorized launch request，并以同一 stream 的更高 stream/placement epoch admission。
- Syncer fault/conflict 以 durable `syncer_epochs`、lease owner/epoch、scheduler history 和 terminal authority 为准，不能仅依据日志或进程退出。
- Supervisor 只拥有 submission receipt 与 authority 中解析出的精确 job ID；失败清理不得影响其他 job。
- 所有正式 gate 必须绑定同一 clean candidate commit 与 source fingerprint。正式 source scope 发生变化后，已有正式实验失效并返回 PREFORMAL。
