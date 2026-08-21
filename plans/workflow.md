# Plan 实施工作流

本文件只规定实施 plan 时必须遵守的最小流程。Plan 负责说明“做什么、达到什么结果”；实施细节必须从当前仓库事实逐步形成，不在 plan 中预先复制架构说明、文件清单、测试数量或完整状态机。

开始前先读取 project root 下的 `AGENTS.md`。所有工作必须遵守其中的指示，尤其是 `## 最简设计&实现原则` 一节。

## 1. 核心原则

1. **只实现当前设计**：旧 API、旧配置、旧 schema、旧路径、旧行为和兼容层不属于当前设计时直接删除，不为历史调用增加 alias、fallback 或 migration wrapper。
2. **目标先于细节**：初始 plan 只冻结目标、非目标和验收结果。模块划分、函数名、文件落点和测试数量在检查当前代码后决定。
3. **单一实现 writer**：Codex/GPT 主实例协调并修改主 worktree。只读审查角色不得修改实现、Git、scheduler 或 run 数据；commit/tree 只用于 coordinator 绑定证据身份。
4. **证据与风险成比例**：普通静态检查和单元测试不需要各自产生复杂 artifact；跨进程、故障恢复、多节点、soak、性能和最终验收必须保存可核验的结构化证据（见 §6.3）。
5. **从便宜到昂贵**：先做静态、focused 和 harness 验证，再运行完整测试、真实 pipeline、多节点、soak 或性能实验（见 §3.3）。
6. **昂贵实验前审查 current state**：current-state 全量审查必须发生在正式多节点、长跑和性能 gate 之前，避免晚发现 obsolete surface、identity 或 harness 问题后重跑整套证据（见 §3.4）。
7. **验证 durable effect**：故障测试优先以 authority row、fence、immutable object、scheduler history、ledger 或 terminal state 为 oracle。只有 plan 明确要求进程生命周期结果时，exit code 才能作为对应通过条件；exit code、PID 和日志不能代替 durable product effect。
8. **精确身份、按影响复用证据**：每项正式证据记录实际 commit、source scopes 和 fingerprint。源码身份不同不等于训练结果失效；是否重跑由变更是否影响该结果的因果输入决定（见 §3.3 和 §3.5）。
9. **验收与诊断分离**：实验状态只由预先登记的有效性条件和通过公式决定。诊断检查不得覆盖 PASS/FAIL，也不得在运行后升级为新的通过条件。
10. **记录结论，不记录噪声**：记录影响判断、源码目标、gate 或下一动作的事实；不为每个微小成功步骤、命令拼写错误或重复 PASS 建立独立流程事件。

## 2. 启动与精简执行包

每个 plan 在独立 branch 上启动。Plan ID 取 plan 文件名去掉 `.md`，须在 `plans/DOING/**` 和 `plans/DONE/**` 中全局唯一，并只使用 `[A-Za-z0-9._-]`。

报告目录只要求：

```text
reports/DOING/<plan-id>/
├── execution.md
├── requirements.csv
├── progress.md
├── failures.md                 # 出现需保留的失败时再创建
└── artifacts/
```

`execution.md` 是实施时逐步完善的精简执行包，记录：

1. plan ID、branch point 和 `plans/workflow.md` 所在 commit；
2. 当前状态：tracked 代码、配置、脚本、入口、测试和文档事实，以及明确保留、删除和不处理的范围；
3. formal source scopes，以及正式证据的复用和失效边界；
4. 从静态检查到正式验收的最小充分测试阶梯、拓扑和资源预算；
5. 正式 gate 的有效性条件、通过公式和诊断项；
6. 只对本 plan 实际涉及的高风险边界记录 identity/authority、fault oracle、performance 方法或 cleanup owner。

不要为不适用的检查项建空表，也不要把盘点、identity、测试阶梯和 artifact schema 机械拆成多份文件。

`requirements.csv` 每行对应一个用户可观察结果或关键不变量，最小字段为：

```text
requirement_id,outcome,verification,evidence,status
```

同一结果涉及的多个 finding、文件或测试归并到一行。不要为 helper、单个 review finding、每个测试或每个 phase 建 requirement。观察项、排障信息和证据完整性检查只有在 plan 明确将其定义为验收结果时，才能成为 requirement。多数 plan 应能控制在约 5–20 行；超出时先检查是否把实现过程或诊断项误写成了完成条件。

启动材料只冻结已经观察到的事实。执行中发现新事实时更新 `execution.md` 和 requirement，而不是维护与代码脱节的原计划细节。

## 3. 实施阶段

阶段顺序为 `INIT → IMPLEMENT → VERIFY → PREFORMAL → FORMAL → FINAL`。`FINAL` 发现实现或验证仍需改动时返回 `PREFORMAL`（见 §3.6）。

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
- 只有当变更影响对外结果、关键不变量或下一 gate 时，才向 `progress.md` 追加一个里程碑。

### 3.3 VERIFY：候选验证

按任务需要选择最短充分阶梯：

```text
静态检查
→ focused tests
→ 完整相关测试或 full suite
→ tiny/integration/fault scenario
→ 正式多节点、soak、boundedness 或 performance
```

高阶 gate 只在低阶 gate 已覆盖其 harness、参数、identity projection 和失败分类后运行。Plan 没有要求的拓扑或实验不因本工作流自动增加。

每次变更后，先根据实际执行路径和数据依赖判断影响类型。不得只根据文件路径、diff 大小、commit 变化或完整 source fingerprint 选择重跑范围。

| 影响类型 | 典型变化 | 必须重跑的范围 | 已完成实验 |
|---|---|---|---|
| `product-functional` | 训练、协议、模型、数据、优化器、状态转换、持久化语义、运行时配置或 workload | 受影响的 focused/integration/full tests，以及受影响的正式实验 | 对应结果失效 |
| `experiment-execution` | launcher、PBS 资源、故障注入时机、拓扑或会改变实际运行路径的环境设置 | 受影响的 harness 和正式场景 | 只使受影响场景失效 |
| `evidence-evaluation` | Checker、oracle、summary parser、测试、报告或仅消费既有 artifact 的工具 | 变更工具的 focused tests；共享验证契约变化时再运行相关完整测试 | 原始运行结果保留；使用已有 durable evidence 重新判定 |
| `non-functional-operation` | queue、wall-time、日志位置、文档或不改变 workload/运行语义的操作参数 | 对应静态、配置或提交检查 | 不使已完成结果失效 |

一项变更可能属于多个类型。此时取各类型影响范围的并集。若 `evidence-evaluation` 变更需要的原始字段没有保存，无法可靠重新判定的实验标记为 `BLOCKED`，并只重跑这些实验。证据复用不表示新旧 fingerprint 相同；manifest 必须保留每项证据的真实身份和影响分析。

测试也按影响扩大范围。每个变更先运行直接 owning tests。只有共享依赖、跨模块契约或最终 `product-functional` 候选发生变化时，才扩大到完整相关测试或 full suite。文档、报告、Checker 或测试本身的局部修改，不自动要求重新运行与其无依赖关系的完整测试，更不自动要求重跑训练实验。

### 3.4 PREFORMAL：昂贵实验前的完整审查

当实现、测试设计和便宜/中等成本验证已经稳定时：

1. 创建 clean candidate commit；
2. 对当前全部源码做一次 current-state 全量审查，参照规则 `plans/review_prompts/review_prompt.md`；
3. 检查唯一实现路径、obsolete surface、authority、identity、持久化、错误处理、harness oracle、配置、PBS、Checker 和文档一致性；
4. 修复 blocking finding，并按 §3.3 重跑受影响的测试或实验；
5. 冻结 `FINAL_IMPLEMENTATION_TARGET`，并记录可复用正式证据的 source lineage 和影响分析。

### 3.5 FORMAL：正式验收

正式运行前，在 formal manifest 中冻结以下三类内容：

1. `validity_conditions`：证明实验确实执行了目标 workload、必要拓扑和预定输入动作，并且测量来源可信。有效性条件必须保持最小，只能包含缺失后无法解释实验结论的前置事实；产品响应、理想终态和辅助完整性检查不能伪装成有效性条件。未满足时状态为 `BLOCKED`，不能解释为产品失败。
2. `pass_formula`：只包含 plan 或用户明确要求的最终结果。有效实验不满足该公式时，状态为 `FAIL`。
3. `diagnostics`：用于解释行为、定位风险或支持后续改进。诊断异常必须保留，但不得改变 PASS/FAIL。

Checker 必须分别输出三类结果，不得把所有检查合并到一个会覆盖最终状态的 `errors` 列表。运行后发现新的风险时，将其记录为诊断或后续 requirement；除非用户明确修改 plan，不得追溯增加本次实验的通过条件。

- 每项正式 gate 必须绑定实际运行时的 commit、scopes、fingerprint、config、workload 和环境，不要求所有 gate 具有相同 fingerprint。
- 多个 gate 共同组成结论时，formal manifest 记录每项 gate 的 source identity、artifact、状态和与 `FINAL_IMPLEMENTATION_TARGET` 的影响关系。
- 只运行 plan/requirement 明确要求，或按 §3.3 已被功能变更影响的正式 gate。
- `evidence-evaluation` 变更优先重新消费不可变 artifact，并发布引用原始证据的新 adjudication artifact；不得改写旧 artifact。只有证据缺失、身份不可验证或原运行没有到达 validity boundary 时，才重跑受影响实验。
- Baseline 可在模型、数据、优化器、workload、运行语义和必要环境保持可比时复用。普通 source fingerprint、Checker、queue、wall-time、测试或文档变化本身不使 baseline 失效。
- Performance gate 在运行前还须冻结 baseline/candidate、fresh run-root、环境、warmup、repeat 与顺序、timer anchor、timeout、随机 seed、signed statistic、CI、门槛和 incomparable 条件，并以 terminal authority 的实际 workload 判断可比性。性能改善不得因绝对差值被判为回退；只有 plan 明确要求双向等价时，才使用双侧阈值。

### 3.6 FINAL：证据审查与归档

最终审查只确认：

- requirement 已全部绑定有效证据，且跨 source 复用均有 §3.3 要求的影响分析；
- 实验命令、config、workload、拓扑和 durable oracle 支持结论；
- formal manifest 已分离 validity、acceptance 和 diagnostics，且状态只由预先登记的公式产生；
- 文档与当前实现和已验证结果一致；
- 没有 open blocking finding、self-proof、wrong-source 或未跟踪的唯一证据；
- cleanup 没有删除唯一证据或仍可恢复的状态。

如果最终审查发现需要修改产品、test、config、PBS、launcher 或 Checker，返回 `PREFORMAL` 完成影响分析和相应验证。返回 `PREFORMAL` 不等于废弃全部正式证据；只按 §3.3 使受影响的测试或实验失效。

## 4. 必做审查

Codex/GPT coordinator 必须在以下位置保存明确结论：

1. `PREFORMAL` 的 current-state 全量审查（见 §3.4）；
2. `FINAL` 的证据审查（见 §3.6）；
3. 同一验证域连续三次有效失败后的 failure review（见 §6.2）。

普通 work unit 和非最终 phase 不要求重复的全量审查。若 plan 有多个自然 phase，可在 `progress.md` 记录 phase milestone，但不为每个 phase 强制建立一套 review target、matrix 和 completed Checker。

## 5. Miyabi/PBS 与测试路由

涉及 Miyabi runtime、PBS、GPU、distributed、CUDA/NCCL、训练或推理验证时，显式使用 `miyabi-development` skill；纯文档、静态分析和文件编辑不使用该 skill。

- login/control plane：只做源码编辑、静态 shell 检查、Git/PBS 控制和结果读取；不运行 pytest、Torch 或项目 runtime。
- compute node：运行项目 import、测试、训练和 runtime。
- 单节点 edit-test 应当申请并进入一个interactive computation node, 优先复用仍有充足 walltime 的同一 allocation；没有工作时及时退出。
- 多节点和正式实验使用独立、预注册的 batch job。

提交任何 PBS script 前必须：

1. 运行 `bash -n scripts/miyabi/agent/*.pbs`；
2. 确认每个 `#PBS -W group_list=` 是有效 literal group ID；
3. 根据 workload 和既有证据申请最短但有安全余量的 walltime，至少 10 分钟；
4. 明确成功证据，不能只以 PBS exit code 判定 PASS。

## 6. 证据与记录

### 6.1 `progress.md`

每个有意义的里程碑记录：

- 时间、目标和 source commit；
- 实际命令或实验入口；
- 环境、PBS job/节点和 workload（适用时）；
- 结果、关键指标和证据路径；
- 尚未覆盖的风险或下一动作。

关联检查可以合并成一个里程碑。重复 PASS、纯机械格式化和不改变结论的短暂状态不单独成节。

### 6.2 `failures.md`

测试失败时先判断任务是否真正执行、环境是否有效、harness 是否到达 validity boundary，以及失败的是 pass formula 还是诊断检查。不得在没有 plan、产品契约或 durable evidence 支持的情况下增加 timeout、放宽状态集合、恢复 alias 或弱化 Checker。若 Checker 与明确的验收公式或产品契约不一致，必须修正 Checker；修正时记录依据，并增加能够识别原错误的 regression test、mutation probe 或独立 oracle。

`failures.md` 只保留会影响实现判断、gate 有效性、正式证据或资源使用的失败：

| 类别 | 含义 | 计入三次失败计数 |
|---|---|---|
| `expected-red` | 预注册缺陷或 mutation 精确失败 | 否 |
| `product-failure` | 实验有效，但产品不满足预先登记的 pass formula | 是，按 product experiment |
| `harness-failure` | fixture、oracle、launcher 或 Checker 错误；不能据此证明产品失败 | 是，按 harness experiment |
| `source-invalid` | wrong/dirty target，不能作为正式证据 | 否；先重新冻结 |
| `infra-invalid` | scheduler、节点、网络或环境使实验无效 | 否 |

命令拼写、路径输错或尚未进入目标 gate 的快速失败，直接修正；只有它消耗了显著资源、导致证据误判或改变下一动作时才写入 `failures.md`。

失败记录包含 experiment、source、命令/config、PBS/run、预期/实际、最小症状、证据、已知事实、根因假设和下一验证。

同一验证域连续三次有效 `product-failure` 或 `harness-failure` 后，停止第四次尝试，做完整 failure review：追踪输入、状态转换、持久化、恢复、进程/PBS 生命周期、oracle 和输出，提出不同解释或实现，并重写 RED 与通过条件。

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
validity_conditions / pass_formula / diagnostics（正式实验适用）
metrics / errors / evidence_paths / cleanup
```

Artifact 必须原子、create-only 发布。Consumer 验证 schema、source identity、requirement ownership 和证据存在性；输出不能把自身作为独立输入。`errors` 只保存导致 `FAIL` 或 `BLOCKED` 的条件，诊断异常单独写入 `diagnostics`。成功实验只保留支撑结论的最小日志，失败实验保留根因分析所需的完整证据。

## 7. 完成条件与清理

Plan 只有在以下条件全部成立时完成：

1. 当前设计的实现、测试、配置、脚本和文档彼此一致；
2. obsolete surface 和仅为兼容保留的代码已删除，仓库级搜索无当前引用；
3. plan 和 requirement 要求的测试与实验全部通过；诊断异常不阻止完成，因有效性条件未满足而 `BLOCKED` 的实验不得充当通过证据；
4. §3.6 的证据审查通过：requirement 全部绑定有效证据，跨 source 复用具有明确影响分析，且没有 open blocking finding；
5. 没有 active/queued job 和未归属的临时状态，也没有遗漏仍需保留的唯一证据。

Cleanup 必须先解析 exact owner、terminal proof 和引用闭包。不得删除 live、queued、resumable run、authority、恢复所需 checkpoint、源码、报告、未解决失败证据、用户既有数据或所有权不确定的路径。删除 material data 后记录目标、数量、大小、保留项和可恢复性。

完成后使用独立 archive/move commit 将 plan 和报告移动到：

```text
plans/DONE/<plan-id>/
reports/checked/<plan-id>/
```

移动时保留 Git 可追溯性和旧证据路径映射，不顺带修改已冻结 artifact，也不清理其他 plan。

## 8. Workflow pin

Plan 启动时记录 `plans/workflow.md` 所在 commit，执行期间保持不变。采用新版本时只从明确记录的 checkpoint 向前生效；不得把新增 gate 倒推成历史 PASS，也不得重写已经产生的事实或证据。
