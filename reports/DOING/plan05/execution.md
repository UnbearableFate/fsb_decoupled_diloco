# plan05 执行包

## 身份与边界

- Plan ID：`plan05`
- 分支：`plan05`
- 启动 commit：`6fe4e8f3880151600afdca16ba55b8a77387717b`
- branch point：`2433f59a5109675d79423e6c2ddb71b72bf5be74`
- workflow pin：`7288dc8086a95d2294a92fb999e8539991d86ec1`
- 启动 worktree：clean
- 执行环境：Miyabi login host `miyabi-g3`；login node 只做静态检查、编辑和 PBS 控制，项目测试与 runtime 只在 PBS compute node 执行。

本 plan 把固定 stream pool、instance admission 和 `ContributorFence` 收敛为唯一 membership 协议。删除 static/dynamic membership 选择、旧 wire/schema、兼容解析、两套 authority 路径、旧配置与旧当前文档。`scaling.enabled` 只决定 capacity service 是否自动 replacement/scale-out，不改变身份或 admission 数据模型。

保留 learner 训练循环、DiLoCo optimizer/merge/staleness/token accounting 的研究语义。`plans/DONE/**`、`reports/**`、既有 artifacts 不因当前协议重写；`plans/00-RESEARCH_PLAN.md` 中旧 matched-run 结果仅作为明确标记的冻结历史 baseline。

## 当前状态与删除边界

启动 CodeGraph 显示两套 fence/scope 从 `protocol/contributor.py` 扩散至 authority、admission、syncer、learner、工具、Checker 和大量通用事务测试。初始仓库搜索在排除历史报告和本 plan 后仍得到 654 个相关命中；高风险中心为约 6,000 行的 `storage/authority.py`、统一 DDL、admission request/response repair、full-protocol Checker 和 independent scheduler evidence。

最终实现必须删除旧类型、字段、目录、CLI、schema 和只为双模式存在的分支，而不是提供 alias、fallback 或 migration。旧 run root、descriptor、config、admission payload、proposal/receipt/control payload 均应被严格拒绝。

## Formal source scopes

最终 source fingerprint 覆盖以下 tracked current-state 范围：

- `fs_diloco/**`
- `configs/**`
- `scripts/miyabi/agent/**`
- `tools/**`
- `do_experiments/experiment04/**`
- `tests/**`
- `website/app/**`
- `website/scripts/**`
- `README.md`
- `pyproject.toml`
- `plans/00-RESEARCH_PLAN.md`

执行报告、review packet 和 formal artifacts 记录 target identity，但不进入产品 source fingerprint。

## 验证阶梯与预算

1. Login node 静态检查：CodeGraph、repository-wide dead-surface search、`bash -n scripts/miyabi/agent/*.pbs`、配置/JSON/TypeScript 生成物一致性与 Python compile/format/lint 的无 runtime 部分。
2. 1-node interactive compute allocation：focused tests，随后完整 test suite；复用同一 allocation，初始预算 60 分钟。
3. 1-node/必要的多进程 functional harness：唯一协议 no-failure、syncer takeover 与 independent authorized-replacement oracle。资源按既有实测缩到能完整结束且至少 10 分钟安全余量。
4. PREFORMAL：clean candidate commit 的 current-state 全量审查；authority、持久化、replacement fault oracle 和正式多节点成本使外部 reviewer materially beneficial，按 workflow 使用只读 PBS review runner。外部 reviewer 仅运行 OpenCode 的 `opencode-go/deepseek-v4-flash`，不运行 Claude Code 或其他 OpenCode 模型。
5. FORMAL：同一 `FINAL_COMMON_TARGET` 上运行 fresh-root 的 no-failure、failure/no-replacement、failure/authorized-replacement，并完成独立 8 learner + 1 syncer 的 GPT-2/WikiText-2、200 local × 10 global 正常运行。
6. FINAL：逐 requirement evidence 审查、active/queued job 与 cleanup ownership 审查，然后独立 archive/move commit。

所有 PBS 提交前必须对全部 agent PBS 执行 `bash -n`、确认 literal group `xg24i002`、基于既有运行时间选择最短可行 walltime（不得少于 10 分钟），并预先绑定 durable success oracle。

## 正式实验预注册

三组唯一协议实验使用同一 GPT-2/WikiText-2 workload identity、model/data revision、seed、8-stream pool 和 quorum 4。每个 proposal 执行 200 个 local step，运行提交 10 个 global version。Fixed-capacity 与 authorized-replacement 配置只在 scaling 段不同，并使用 fresh run root：

| 场景 | 唯一变量 | Durable PASS oracle |
|---|---|---|
| no-failure | `scaling.enabled=false`，无故障 | 8 个 bootstrap instance 完成；v10 merge/terminal/token ledger/checkpoint 均成立；无 launch request/replacement |
| failure/no-replacement | `scaling.enabled=false`，删除一个 learner | 被删除 instance 不再产生 effect；无 replacement/launch request；剩余 contributor 在 quorum 允许范围内完成 v10，ledger 与 terminal fence 一致 |
| failure/authorized-replacement | `scaling.enabled=true`，删除一个 learner | scheduler qdel history、capacity observation、launch request、qsub receipt、replacement admission、stream epoch 前进、旧 fence 拒绝和 cursor continuity 全部关联到同一 stream，最终完成 v10 |

与 plan04 冻结 dynamic baseline 比较的预注册指标：terminal stream 最后 proposal loss 的均值、artifact-to-terminal wall time、global versions、accepted proposal/merge 数、每 stream local steps、token ledger totals、replacement count 和 duplicate/stale rejection。正确性计数必须满足 config/Checker 的精确不变量；loss 相对 baseline 绝对差异不超过 20%；no-failure wall time 相对同 workload baseline 增幅不超过 20%。故障场景 wall time 仅报告，不用于训练质量等价结论。若 baseline source/config/workload/seed、terminal workload 或输入 revision 无法严格匹配，则结论为 incomparable，不用替代数据放宽阈值。

## 高风险 ownership

- Identity/authority：`storage/authority.py` 与唯一 `schema.sql` 必须共同证明 current instance/placement/stream incarnation；`fence_json` 是唯一 fence wire representation。
- Fault oracle：independent topology 的 scheduler qdel/qsub receipt 与 authority launch/admission rows共同证明 replacement；co-allocated harness 不伪造 scheduler receipt。
- Cleanup：正式 supervisor 拥有其 fresh run roots 和 submitted job IDs；只删除已 terminal 且 artifact 已发布的临时 state，保留最终唯一证据。
