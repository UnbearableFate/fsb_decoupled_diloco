结论：按计划中的主要研究主题粗略计算，本轮实现与实验将整体完成度推进到了约 **70%**。如果只看 full reference 和 full 模式的有界状态，完成度约 **85%**；剩余工作主要集中在独立重启、fragment 推广和训练质量结论。

需要注意，[原计划](/work/xg24i002/x10041/fsb_decoupled_diloco/plans/FULL_REFERENCE_AND_BOUNDED_STATE_PLAN.md)本身不是验收清单，因此这里的比例是按研究主题和证据完整性估算的。

| 计划部分 | 当前状态 | 判断 |
|---|---|---|
| Full 权威提交边界、DB-first resume、DB/latest 对账 | 已完成 | 单事务提交和六阶段 crash matrix 已验证 |
| Terminal drain 与有限输入结束语义 | 基本完成 | 两次 5000-step 均以 `input_exhausted` 正常结束，无无限等待 |
| Full 长运行实验 | 已完成 | FP32 到 v25，BF16/staleness=2 到 v48，终态 invariant 均通过 |
| Full 有界 proposal surface、current-only retention、GC | 基本完成 | 1000-cycle、50×10 和两次长运行均证明活跃状态不随历史线性增长 |
| accepted-token efficiency 与 latest-wins 取舍 | 已完成一轮 | 已量化 47.5% 与 93% update 利用率 |
| Fresh-only/stale 参考语义 | 部分完成 | 运行语义可用，但两次都没有达到 v50；staleness=2 实际只观测到 stale 0/1 |
| Learner 重启与独立生命周期 | 尚未完成 | 缺少 incarnation identity、单 learner 重启、syncer 重启和独立 PBS 作业实验 |
| Fragment 有界状态与恢复 | 部分完成 | checkpoint/payload retention 已接入，但固定 learner×fragment proposal surface 和 fragment resume 未完成 |
| 长期观测与审阅材料 | 部分完成 | 有终态数据，但缺少 discovery latency、GC backlog、DB/live bytes 随 update 变化的正式曲线 |

严格说，两次 5000-step 实验直接完成的是：

- §3.8 的“较长 full 运行”检查点；
- §4.10 的一部分真实模型有界性观测；
- §4.11 的 replacement rate / accepted-token efficiency 取舍；
- 对 terminal drain、DB/cache 一致性和终态 GC 的长运行验证。

尚未完成的核心工作，建议按下面顺序推进：

1. 明确“5000 local steps”和“50 outer versions”的完成条件，避免以后再次出现训练正常结束但 `expected v50` 不满足。
2. 做 full 的真实进程级恢复：单 learner 重启、syncer 重启、整个 PBS 作业重启，以及不同启动顺序。
3. 在独立 PBS 作业拓扑下重复恢复和终止实验，验证不依赖 co-allocated MPI 生命周期。
4. 完成 fragment 固定 proposal surface、per-fragment authority/version vector、引用窗口和 fragment resume。
5. 对 fragment 做同等级的 crash matrix、1000-cycle 有界性和长运行验证。
6. 做受控 FP32/BF16/staleness 实验：同代码、单变量、多 seed、validation perplexity，并主动构造真正的 staleness=2 样本。
7. 补齐计划 §6 的状态图、崩溃对账表以及 live bytes、DB size、GC backlog、discovery latency 联合曲线。

因此，当前可以认为“**full 路径的系统正确性底座已经接近闭环**”，但整个计划还不能结束；剩下约 30% 恰好是把结论从“同一作业内运行正确”扩展到“独立生命周期可恢复、fragment 同样有界，并且具有可信质量结论”。详细实验依据在 [progress.md](/work/xg24i002/x10041/fsb_decoupled_diloco/reports/imp_plans/01/progress.md) 和 [run_analysis.md](/work/xg24i002/x10041/fsb_decoupled_diloco/reports/run_analysis.md)。