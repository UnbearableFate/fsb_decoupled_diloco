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
- 正常场景使用统一 summary 工具记录与两种 baseline 的比较。比较结果只用于诊断，不影响 PASS/FAIL。
- 每项 Full Protocol 实验只使用三个通过条件：最终接管训练的 Syncer 正常退出、训练达到 25 个 global step、最终平均 loss < 3.5。Terminal maintenance、权重清理和场景专用 oracle 继续记录诊断信息，但不影响实验状态。

## 验证阶梯与资源预算

1. Login node：repository-wide current-state 审查、`git diff --check`、Python compile、PBS/Bash 语法与 literal group 检查。
2. 单节点 `interact-g`：focused plan04、summary、baseline、syncer tests，随后 clean candidate 上运行完整 pytest。
3. PREFORMAL：提交当前实现，保存 current-state 审查，并记录 commit、source scopes 与 fingerprint。
4. FORMAL Baseline：两个 8-node `debug-g` job，walltime 30 分钟，各 1 seed；由 health checker、run summary 和 scheduler history共同判定。
5. FORMAL Full Protocol：七个 1-node supervisor job；每个 supervisor 提交 8 个独立 learner job 和 1 个 syncer job，双 syncer 场景增加 1 个候选 syncer。Supervisor 与 actor 使用 `debug-g`，walltime 30 分钟。
6. FINAL：逐项核对最终 Syncer 的 scheduler history、terminal version、summary loss、source identity 与辅助诊断；确认无 active/queued job 后归档 plan 和报告。

`qstat --rscuse` 显示 `debug-g/interact-g` 使用率低于 `regular-g` 后，用户将预算调整为 30 分钟并指定 `debug-g`。只有实际 scheduler/runtime 证据表明不足或明显过长时才调整；不得以延长 timeout 掩盖产品或 harness 错误。

## 高风险边界

- 八个 learner 是提交拓扑，不是 terminal 前必须全部 admission 的屏障。Authority oracle 的结果只作为诊断信息。
- Learner fault 只删除故障边界时 authority 中已 admitted 的 bootstrap job。Supervisor 记录 replacement 的 launch request、stream 和 placement 信息，用于诊断。
- Syncer fault/conflict 记录 durable `syncer_epochs`、lease owner/epoch、scheduler history 和 terminal authority。最终 PASS/FAIL 只检查最终接管训练的 Syncer 是否正常退出。
- Supervisor 只拥有 submission receipt 与 authority 中解析出的精确 job ID；失败清理不得影响其他 job。
- 用户明确规定：queue、wall-time 或实验判定工具的调整不得废弃既有训练结果。只有训练或协议功能逻辑变化时才重跑受影响实验。
- 最终 manifest 必须列明每项结果的精确 source identity，并说明 source 差异是否涉及训练或协议功能逻辑。不得伪造相同 fingerprint。
