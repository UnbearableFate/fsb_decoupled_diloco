# Plan 实施工作流

本文件只规定实施 plan 时必须遵守的最小流程。Plan 负责说明“做什么、达到什么结果”；实施细节必须从当前仓库事实逐步形成，不在 plan 中预先复制架构说明、文件清单、测试数量或完整状态机。

## 1. 核心原则

0. 参照project root下 `AGENTS.md`的 `## 最简设计&实现原则`一节
1. **只实现当前设计**：旧 API、旧配置、旧 schema、旧路径、旧行为和兼容层不属于当前设计时直接删除，不为历史调用增加 alias、fallback 或 migration wrapper。
2. **目标先于细节**：初始 plan 只冻结目标、非目标和验收结果。模块划分、函数名、文件落点和测试数量在检查当前代码后决定。
3. **单一实现 writer**：Codex/GPT 主实例协调并修改主工作树。Reviewer 只读取冻结 snapshot 中明确列出的相对文件路径，不修改实现、Git、scheduler 或 run 数据；commit/tree 只用于 coordinator 绑定证据身份。
4. **证据与风险成比例**：普通静态检查和单元测试不需要各自产生复杂 artifact；跨进程、故障恢复、多节点、soak、性能和最终验收必须保存可核验的结构化证据。
5. **从便宜到昂贵**：先做静态、focused 和 harness 验证，再运行完整测试、真实 pipeline、多节点、soak 或性能实验。
6. **昂贵实验前审查 current state**：完整源码审查必须发生在正式多节点、长跑和性能门禁之前，避免晚发现 dead surface、identity 或 harness 问题后重跑整套证据。
7. **验证 durable effect**：故障测试以 authority row、fence、immutable object、scheduler history、ledger 或 terminal state 为 oracle；exit code、PID 和日志仅用于诊断。
8. **同一正式目标**：相互组成最终结论的正式 gate 必须绑定同一 clean commit、source scopes 和 fingerprint。
9. **记录结论，不记录噪声**：记录影响判断、源码目标、门禁或下一动作的事实；不为每个微小成功步骤、命令拼写错误或重复 PASS 建立独立流程事件。

## 2. 启动与精简执行包

每个 plan 在独立 branch 上启动，记录：

- plan ID、branch point 和 `plans/workflow.md` 所在 commit；
- 当前 tracked 代码、配置、脚本、入口、测试和文档事实；
- 明确保留、删除和不处理的范围；
- formal source scopes；
- 最小测试阶梯和资源预算。

Plan ID 取 plan 文件名去掉 `.md`，须在 `plans/DOING/**` 和 `plans/DONE/**` 中全局唯一，并只使用 `[A-Za-z0-9._-]`。

报告目录只要求：

```text
reports/DOING/<plan-id>/
├── execution.md
├── requirements.csv
├── progress.md
├── failures.md                 # 出现需保留的失败时再创建
└── artifacts/

reports/DOING/code_review/<plan-id>/<review-id>/
└── review packet、报告、job summary 与 finding disposition
```

`execution.md` 是实施时逐步完善的精简执行包，包含：

1. 当前状态和删除边界；
2. formal source scopes；
3. 从静态到正式验收的测试阶梯、拓扑和预算；
4. 只对本 plan 实际涉及的高风险边界记录 identity/authority、fault oracle、performance 方法或 cleanup owner。

不要为不适用的 concern 建空表，也不要把 inventory、identity、test ladder 和 artifact schema 机械拆成多份文件。

`requirements.csv` 每行对应一个用户可观察结果或关键不变量，最小字段为：

```text
requirement_id,outcome,verification,evidence,status
```

同一结果涉及的多个 finding、文件或测试应归并到一行。不要为 helper、单个 review finding、每个测试或每个 phase 建 requirement。多数 plan 应能控制在约 5–20 行；超出时先检查是否把实现过程误写成了完成条件。

启动材料只冻结已经观察到的事实。执行中发现新事实时更新 `execution.md` 和 requirement，而不是维护与代码脱节的原计划细节。

## 3. 实施阶段

### 3.1 INIT：盘点和边界冻结

1. 读取 plan 和适用的 `AGENTS.md`。
2. 检查当前 worktree、branch point、已有实现和未提交用户改动。
3. 创建精简执行包和 requirement matrix。
4. 运行无需项目 runtime 的静态自检。
5. 把启动事实追加到 `progress.md`。

### 3.2 IMPLEMENT：按结果实现

- 以能独立验证的最小行为单元推进，不以预先写死的文件顺序推进。
- 优先删除 obsolete 实现，再收敛剩余唯一路径；不得为了旧测试恢复兼容接口。
- 行为缺陷应先有 RED、characterization、mutation probe 或独立 oracle，证明测试确实能识别缺陷。
- 每个单元先运行最相关的静态/focused 验证，通过后再扩大测试范围。
- 只有当变更影响对外结果、关键不变量或下一门禁时，才向 `progress.md` 追加一个里程碑。

### 3.3 VERIFY：候选验证

按任务需要选择最短充分阶梯：

```text
静态检查
→ focused tests
→ 完整相关测试或 full suite
→ tiny/integration/fault scenario
→ 正式多节点、soak、boundedness 或 performance
```

高阶 gate 只在低阶 gate 已覆盖其 harness、参数、identity projection 和失败分类后运行。Plan 没有要求的拓扑或实验不因 workflow 自动增加。

### 3.4 PREFORMAL：昂贵实验前的完整审查

当实现、测试设计和便宜/中等成本验证已经稳定时：

1. 创建 clean candidate commit；
2. 对全部 current source 做一次完整审查，而不是只看最后 diff；
3. 检查唯一实现路径、dead/legacy surface、authority、identity、持久化、错误处理、harness oracle、配置、PBS、Checker 和文档一致性；
4. 修复 blocking finding 并重跑受影响的候选测试；
5. 冻结唯一 `FINAL_COMMON_TARGET`。

不得先运行正式 9-node、长 soak 或性能比较，再做第一次 current-state 全量审查。

### 3.5 FORMAL：正式验收

- 所有正式 gate 绑定 `FINAL_COMMON_TARGET` 的 commit、scopes 和 fingerprint。
- 多个 gate 共同组成结论时，创建一个简洁的 formal manifest，列出 gate、拓扑、workload、PASS 公式、artifact 和 cleanup owner。
- 只运行 plan/requirement 明确要求的正式 gate。
- 若正式 source scope 发生变化，旧 target 的正式证据失效；重新审查、冻结，并只在新共同目标上形成最终证据集。

### 3.6 FINAL：证据审查与归档

最终审查只确认：

- requirement 已全部绑定同一正式目标的有效证据；
- 实验命令、config、workload、拓扑和 durable oracle 支持结论；
- 文档与当前实现和已验证结果一致；
- 没有 open blocking finding、self-proof、wrong-source 或未跟踪的唯一证据；
- cleanup 没有删除唯一证据或仍可恢复的状态。

如果最终审查发现需要修改 source、test、config、PBS、launcher 或 Checker 逻辑，返回 `PREFORMAL`，不要在旧 evidence 上补丁式宣布完成。

## 4. 审查策略

### 4.1 必做审查

Codex/GPT coordinator 必须在以下位置保存明确结论：

1. `PREFORMAL` 的 current-state 全量审查；
2. `FINAL` 的 evidence 审查；
3. 同一验证域连续三次有效失败后的 failure review。

普通 work unit 和非最终 phase 不要求重复的全量审查。若 plan 有多个自然 phase，可在 `progress.md` 记录 phase milestone，但不为每个 phase 强制建立一套 review target、matrix 和 completed Checker。

### 4.2 何时使用外部多智能体 reviewer

仅在以下情况 materially beneficial 时使用：

- 用户或 plan 明确要求；
- 变更涉及 authority、并发、持久化格式、恢复、安全边界或高风险 fault oracle；
- 即将运行成本较高的多节点故障、soak 或性能验收；
- 三次有效失败后需要不同解释；
- coordinator 对 blocking finding 的处置缺乏足够反证。

通常把 test-design 和 current-state 问题合并到一次 preformal review。修缮后只有关键不变量再次变化时才做增量复审；不得因为 reviewer 不可用而重复提交相同 review。

外部 reviewer 是 best effort：不可用不等于 APPROVE，也不自动阻断；任何有效 finding 都必须处置。Codex/GPT 必须先保存自己的结论，再读取本轮外部报告。

所有外部 reviewer 必须通过 `scripts/miyabi/agent/run_multi_agent_review.pbs` 在 compute node 上运行。Runner 当前支持的 `REVIEW_KIND` 为：

```text
test-design | failure | phase | preformal-plan-complete |
final-evidence | critical-incremental
```

Review packet 保持紧凑，只包含 review kind、plan/requirement、冻结 snapshot 中需要审查的精确相对文件路径、关键不变量、相关证据路径、明确问题和期望 verdict。Reviewer 的工作接口是文件路径和 current-state 内容。需要增量复审时，由 coordinator 直接列出本轮必须检查的当前文件以及受影响的 caller、test、config、launcher、Checker 和文档文件。若有与 exact target 匹配且最新的 CodeGraph 索引，优先用 `impact`/`affected` 补全这些路径；索引身份不匹配时不得把其结果当作审查范围证据。

Runner 仍使用唯一 `TARGET_COMMIT` 和 tree hash 创建不可变 snapshot，并把它们写入 request/job summary 作为证据身份，但不把 commit ID 当作 reviewer 的导航或范围定义。提交时必须提供：

```text
TARGET_COMMIT
REVIEW_PROMPT_FILE + REVIEW_PROMPT_SHA256
REVIEW_PATHS_FILE + REVIEW_PATHS_SHA256
```

Runner 只通过 OpenCode 调用固定模型 `opencode-go/deepseek-v4-flash`，不接受模型选择参数，也不调用 Claude Code 或其他 OpenCode 模型。Reviewer 使用固定 ID `opencode-deepseek-v4-flash`；request/job summary 同时记录 reviewer ID 与完整 requested model ID。

`REVIEW_PATHS_FILE` 每行必须是 `TARGET_COMMIT` 中一个精确、tracked、repository-relative 文件路径；不接受目录、glob、重复项、绝对路径或 `..`。PREFORMAL 应列出全部 current source，并按需要列出测试、配置、PBS、Checker 和文档；FINAL 只列 evidence review 实际需要的文件。Runner 将这份列表复制为 snapshot 内的 `.review-scope/paths.txt`，reviewer 可以读取直接耦合文件来确认契约和影响，但 finding 的主范围必须由路径列表定义。

Review 前必须验证：

- target 是完整 commit ID，来自 clean candidate，且 tree identity 已记录；
- prompt 和 review-path list 位于规定 review 目录；
- 每个 review path 都是 target 中的精确 tracked 文件，且列表完整覆盖本轮声明的审查范围；
- reviewer snapshot 只读，结果记录 requested/actual model、PBS job、状态和路径；
- reviewer 不修改仓库、不提交 job、不读取其他 reviewer 输出。

Finding 处置只使用：

```text
fixed | rejected-with-evidence | deferred-with-justification
```

Critical/High 必须修复或以强反证拒绝。Medium 在当前 scope 内修复，或写清影响和延期理由。Low 只在有后续价值时保留。

## 5. Miyabi/PBS 与测试路由

涉及 Miyabi runtime、PBS、GPU、distributed、CUDA/NCCL、训练或推理验证时，显式使用 `$miyabi-development` skill；纯文档、静态分析和文件编辑不使用该 skill。

- login/control plane：只做源码编辑、静态 shell 检查、Git/PBS 控制和结果读取；不运行 pytest、Torch、项目 runtime 或 reviewer CLI。
- compute node：运行项目 import、测试、训练、runtime 和 reviewer。
- 单节点 edit-test 循环优先复用仍有充足 walltime 的同一 allocation；没有工作时及时退出。
- 多节点和正式实验使用独立、预注册的 batch job。

提交任何 PBS script 前必须：

1. 运行 `bash -n scripts/miyabi/agent/*.pbs`；
2. 确认每个 `#PBS -W group_list=` 是有效 literal group ID；
3. 根据 workload 和既有证据申请最短但有安全余量的 walltime，至少 10 分钟；
4. 明确成功证据，不能只以 PBS exit code 判定 PASS。

测试失败时先判断任务是否真正执行、环境是否有效以及 harness 是否到达目标 oracle。不得通过增加 timeout、放宽状态集合、恢复 alias 或弱化 Checker 使结果变绿。

Performance gate 还必须在运行前冻结 baseline/candidate、fresh run-root、环境、warmup、repeat/顺序、timer anchor、timeout、随机 seed、signed statistic、CI、门槛和 incomparable 条件，并以 terminal authority 的实际 workload 判断可比性。

## 6. 证据与记录

### 6.1 `progress.md`

每个有意义的里程碑记录：

- 时间、目标和 source commit；
- 实际命令或实验入口；
- 环境、PBS job/节点和 workload（适用时）；
- 结果、关键指标和 evidence 路径；
- 尚未覆盖的风险或下一动作。

关联检查可以合并成一个里程碑。重复 PASS、纯机械格式化和不改变结论的短暂状态不单独成节。

### 6.2 `failures.md`

只保留会影响实现判断、门禁有效性、正式证据或资源使用的失败：

| 类别 | 含义 | 计入三连失败 |
|---|---|---|
| `expected-red` | 预注册缺陷或 mutation 精确失败 | 否 |
| `product-failure` | 有效环境和 harness 下产品不满足要求 | 是，按 product experiment |
| `harness-failure` | fixture、oracle、launcher 或 Checker 错误 | 是，按 harness experiment |
| `source-invalid` | wrong/dirty target，不能作为正式证据 | 否；先重新冻结 |
| `infra-invalid` | scheduler、节点、网络或环境使实验无效 | 否 |

命令拼写、路径输错或尚未进入目标 gate 的快速失败，直接修正；只有它消耗了显著资源、导致证据误判或改变下一动作时才写入 `failures.md`。

失败记录包含 experiment、source、命令/config、PBS/run、预期/实际、最小症状、证据、已知事实、根因假设和下一验证。

同一验证域连续三次有效 `product-failure` 或 `harness-failure` 后，停止第四次尝试，做完整 failure review：追踪输入、状态转换、持久化、恢复、进程/PBS 生命周期、oracle 和输出，提出不同解释或实现，并重写 RED 与通过条件。外部 reviewer 只在 §4.2 条件成立时调用。

### 6.3 Artifact

普通静态/focused/full test 可由 `progress.md` 中的命令、摘要和原始日志证明。以下场景才要求结构化 artifact：

- multi-process 或 fault scenario；
- 多节点、soak、boundedness 或 performance；
- 被 machine Checker/aggregate 消费的 gate；
- plan 的最终验收。

结构化 artifact 的最小公共字段为：

```text
status: PASS | FAIL | BLOCKED | REVIEW
gate / experiment_id / requirements
source: commit + dirty + scopes + fingerprint
environment: interpreter/packages + PBS job/nodes/topology（适用时）
config/workload identity（适用时）
metrics / errors / evidence_paths / cleanup
```

Artifact 必须原子、create-only 发布。Consumer 验证 schema、source identity、requirement ownership 和 evidence 存在性；输出不能把自身作为独立输入。成功实验只保留支撑结论的最小日志，失败实验保留根因分析所需的完整证据。

## 7. 完成条件与清理

Plan 只有在以下条件全部成立时完成：

1. 当前设计的实现、测试、配置、脚本和文档彼此一致；
2. obsolete/compatibility-only surface 已删除，仓库级搜索无当前引用；
3. requirements 全部由同一正式目标的有效 evidence 支持；
4. 必需测试和实验通过，blocking finding 已关闭；
5. 最终 evidence review 通过；
6. 没有 active/queued job、未归属临时状态或仍需保留的唯一证据被遗漏。

Cleanup 必须先解析 exact owner、terminal proof 和引用闭包。不得删除 live、queued、resumable run、authority、恢复所需 checkpoint、源码、报告、未解决失败证据、用户既有数据或所有权不确定路径。删除 material data 后记录目标、数量、大小、保留项和可恢复性。

完成后使用独立 archive/move commit 将 plan 和报告移动到：

```text
plans/DONE/<plan-id>/
reports/checked/<plan-id>/
```

移动时保留 Git 可追溯性和旧 evidence 路径映射，不顺带修改已冻结 artifact，也不清理其他 plan。

## 8. Workflow pin

Plan 启动时记录 workflow version 和 commit，执行期间保持不变。采用新版本时只从明确记录的 checkpoint 向前生效；不得把新增门禁倒推成历史 PASS，也不得重写已经产生的事实或 evidence。
