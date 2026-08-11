# plan05 PREFORMAL current-state 最终 coordinator 审查

- Review kind：`preformal-plan-complete`
- Target commit：`27c5f5d74beeabe8845e8e714bca07ed14ff7296`
- Target tree：`d271eec69215b127569cb158656b7258269d5c23`
- Source fingerprint：`sha256:806def91d254af1a5372e903e0d014c6911ba91701004f883850d0804b5c71d3`
- 验证：PBS `2532041.opbs`，`mg0845`；Ruff、focused `261 passed`、full `592 passed`、website lint、14 项 rendered-site test 全部通过；生成 API reference 无差异。

本结论由 coordinator 在读取仍在运行的外部 reviewer 结果前独立完成。它汇总并取代此前 coordinator 报告的 target identity；PF-01 至 PF-06 和 CP-01 的详细处置仍见同目录既有报告。

## Current-state 复核

- 唯一协议：config、descriptor、wire types、authority DDL/API、admission、runtime、launcher、Checker、summary、当前文档和生成 reference 只保留固定 stream pool、instance incarnation 和唯一 `ContributorFence`。旧字段只存在于 strict rejection/absence test 或明确标记的冻结历史范围。
- Admission/replacement：bootstrap slot 与 launch request exact-one；authorized replacement 串联 capacity observation、scheduler loss、qsub receipt、launch row、admitted instance、stream epoch、receipt predecessor/cursor 和 stale-fence rejection。
- Terminal/accounting：正式 oracle 使用 archive-aware rows 重建 receipt chain、proposal、token fate、rollup、每版四个 200-step merge 和零余额；fixed loss 只接受一个有精确一周期 upper bound 的 hard crash，不把 gap 当作 processed token。
- Identity/topology：每个初始 learner、syncer 和 replacement 的 immutable attestation 绑定 run/descriptor/source/config/model/data、authority actor、PBS allocation 和 canonical path；初始九个 job 必须位于九个不同 host，重复 scheduler identity fail closed。
- Checkpoint：v0 至 v10 的 22 个 weight/outer-state object 必须位于 epoch/owner/publication 推导的 canonical path，且是 byte size/SHA-256 与 authority identity 一致的 immutable regular file。
- Python documentation：对 branch point 至当前 target 的全部 modified handwritten Python 做 AST/diff ownership audit；每个 modified module/class/function/method 均有英文 docstring，新 class/instance field 均有 declaration/inline comment。generated API reference 已刷新并通过无差异门禁。
- Baseline：plan04 latest target 没有正式、严格同 identity 的 Dynamic Full baseline；plan05 最终比较只能是 `incomparable`，不能计算 20% threshold 或用 DDP、Periodic Average、diagnostic data 替代。

## 结论

当前没有 open blocking finding。外部旧 target 报告完成后仍须逐项处置所有有效 finding；因为其 target 早于关键 oracle 修缮，正式实验前还必须让固定 OpenCode `opencode-go/deepseek-v4-flash` 对当前关键路径完成 `critical-incremental` 复审。

Verdict: APPROVE
