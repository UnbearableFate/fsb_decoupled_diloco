# Plan 多智能体实施工作流

Workflow version：`3`

本文件定义实施 `plans/DOING/**` 下 plan 的统一工作流。Plan 是“做什么”的来源：它定义功能、实现落点、测试、实验、Checker requirement 和完成条件；本文件是“怎么做”的来源：它定义职责、状态转换、证据、失败升级、审查、修缮、Miyabi/PBS 路由和完成门禁。

## 1. 基本原则

1. **单一实现 writer**：Codex/GPT 主实例负责协调、修改主工作树、执行测试、处置 finding、提交 Git 和维护记录。外部 reviewer 不直接修改主工作树。
2. **冻结后审查**：reviewer 只审查可由完整 commit ID 标识的冻结目标。未提交工作树不属于审查目标。
3. **独立形成结论**：Codex/GPT 必做审查必须在读取本轮外部 reviewer 的实质结论前保存；外部 reviewer 之间不得读取彼此本轮报告。
4. **可用性 best effort，finding 不打折**：外部 reviewer 因额度、余额、认证、工具、网络、scheduler 或超时不可用时不阻断后续流程；但成功产出的有效 finding 必须逐条处置。
5. **证据优先于投票**：不按 APPROVE 数量投票。任何一个有代码路径、反例、测试或实验依据的 Critical/High finding 都必须修复或以可核验反证拒绝。
6. **实验后置**：先审查测试设计并运行便宜门禁，再执行完整测试、真实 pipeline、多节点、soak 或性能实验。
7. **事实、推断和计划分开**：报告必须区分观察事实、根因假设、处置决定和下一步验证。
8. **追加历史，更新状态**：Markdown 记录和 review artifact 只追加、不覆盖。`workflow_state.yaml` 是当前状态快照，允许原子更新；每次状态变化的摘要仍须追加到 `progress.md` 或 `failures.md`。
9. **身份和 authority 先行**：跨 config、descriptor、SQLite、filesystem、scheduler、actor、Git 和 artifact 的身份必须声明类型、canonical form 与 authority owner；不得用相似字符串、进程退出或日志推断 durable state。
10. **harness 也是产品代码**：fixture、launcher、Checker、artifact producer 和 cleanup planner 必须有静态/单元验证；能够在 qsub 前发现的问题不得靠正式 allocation 试错。
11. **单一正式源码目标**：正式 acceptance ladder 必须绑定同一 clean source commit、source scopes 和 fingerprint。任何正式 scope 的后续修改都会使依赖旧 fingerprint 的 evidence 失效。
12. **先证明语义，再比较指标**：fault test 先证明 durable effect；performance test 先证明实际 processed workload、终态、环境和 timer anchor 等价。
13. **异步交接必须有精确流控**：producer 不得用相关但不等价的 version、日志或时间推断 authority 已 ingest；需要保护终态 gap 或有界积压时，ack 必须绑定 exact identity/fence/digest，且可幂等 replay。

## 2. 角色与审查分工

### 2.1 必做角色

| 角色 | 职责 | 是否阻断 |
|---|---|---|
| Codex/GPT coordinator | 读取 plan、拆分 work unit、维护状态、冻结 target、调度 reviewer、汇总意见和决定下一动作 | 是 |
| Codex/GPT implementer | 修改功能代码和测试、运行验证、保存证据、修缮 finding | 是 |
| Codex/GPT mandatory review pass | 在 failure、phase 和 plan-complete 门禁中，对冻结目标形成必做报告；同一主实例执行时也必须先封存报告再读取外部意见 | 是 |

只有在使用单独 GPT session、独立上下文和只读冻结目标时，才能把这一 pass 称为 independent review；不能把同一实现对话中的即时自我确认伪装成独立 reviewer。

### 2.2 外部 reviewer 群

默认 reviewer 及主要 lane：

| Reviewer | 默认模型标识 | 主要 lane | 可用性 |
|---|---|---|---|
| Claude Code | `claude-opus-5` | 协议正确性、跨模块控制流、架构与并发/持久化不变量 | best effort |
| OpenCode Go | `opencode-go/glm-5.2` | requirement 到测试断言的映射、oracle、覆盖和测试可维护性 | best effort |
| OpenCode Go | `opencode-go/deepseek-v4-flash` | 错误路径、race、crash/restart、边界条件和反例 | best effort |
| OpenCode Go | `opencode-go/kimi-3` | 集成一致性、实验过程、artifact/Checker 证据和文档漂移 | best effort |

模型名是 runner 的默认值；若 CLI 中实际标识变化，可通过 `qsub -v` 覆盖对应 `*_MODEL`，但必须在 invocation metadata 和报告中同时记录 requested/actual identity。无法核验实际模型时标为 `invalid-model-identity`，不伪造该 reviewer 报告。

外部 reviewer 只能：

- 读取 runner 从 target commit 导出的 node-local、相互隔离、只读 snapshot；
- 执行只读搜索、diff、静态分析和范围受控的轻量检查；
- 把 Markdown 结论返回 stdout，由 runner 保存。

外部 reviewer 不得：

- 修改主仓库、临时 snapshot 中的实现、现有报告或 Git 历史；
- 执行 `qsub`、`qdel`、commit、push、merge 或创建 PR；
- 删除或改变 run、checkpoint、数据库、scheduler job 或外部服务；
- 读取其他 reviewer 的本轮输出；
- 在报告中写入 secret、token、凭据或完整环境变量。

## 3. Plan 身份、workflow pin 与迁移

### 3.1 Plan 身份

对任意：

```text
plans/DOING/**/<plan-id>.md
```

其稳定报告根目录为：

```text
reports/DOING/<plan-id>/
```

`<plan-id>` 取计划文件名去掉 `.md`，不包含中间目录名，且必须在 `plans/DOING/**` 与 `plans/DONE/**` 中全局唯一并满足 `[A-Za-z0-9._-]+`。Requirement matrix、review 和 artifact 都使用同一个 plan ID。新 plan 的构建和命名遵循 [`plans_create_guide.md`](plans_create_guide.md)。

### 3.2 启动冻结

每个新 plan 必须：

1. 在独立 Git branch 上实施；
2. 记录 branch point 完整 commit ID；
3. 记录使用的 workflow version 和包含 `plans/workflow.md` 的 commit ID；
4. 保存当前代码/配置/PBS/entrypoint/runtime surface inventory，区分事实、待验证推断和非目标；
5. 冻结 requirement matrix、预期 phase ID、测试阶梯、formal source scopes、artifact schema、资源预算和计划内非目标；
6. 对关键对象建立 identity/authority table，对每个 fault scenario 声明注入层与 durable oracle；
7. 初始化 `workflow_state.yaml`，并运行不需要项目 runtime 的 plan/matrix/schema 自检。

Plan 执行期间修改 workflow 不自动改变该 plan 的规则。采用新版本时必须在 `progress.md` 记录 migration checkpoint、旧/新版本、开始生效的 commit/work unit、保留的历史结论和需要新增的后续门禁。不得倒推重做已完成 phase，除非用户明确要求或已有证据表明历史结论无效。

## 4. 报告、状态和 artifact

### 4.1 目录

```text
reports/DOING/<plan-id>/
├── workflow_state.yaml
├── progress.md
├── failures.md
├── code_review.md
├── plan-complete-remediation.md
└── artifacts/

reports/DOING/code_review/<plan-id>/<review-id>/
├── prompt-source_<target>.md
├── review-request_<target>_<pbs-job>.md
├── codex-gpt_<target>.md
├── claude-opus-5_<target>_<pbs-job>.md
├── glm-5.2_<target>_<pbs-job>.md
├── deepseek-v4-flash_<target>_<pbs-job>.md
├── kimi-3_<target>_<pbs-job>.md
├── reviewer-job_<target>_<pbs-job>.json
└── finding-dispositions_<target>.md
```

`<review-id>` 必须稳定、路径安全并表达门禁，例如：

- `test-P3-operational-robustness-C17`；
- `failure-P6-G5-round1`；
- `P4-mandatory-fenced-runtime`；
- `plan-complete`。

`REVIEW_KIND` 使用稳定枚举：`test-design`、`failure`、`phase`、`preformal-plan-complete`、`final-evidence` 或 `critical-incremental`。不要把自由文本放入该字段；具体范围写入 prompt。

重试报告不得覆盖旧文件；使用新的 PBS job ID，必要时附加 `retryN`。

### 4.2 `workflow_state.yaml` 最小字段

```yaml
workflow_version: 3
plan_id: <plan-id>
branch_point: <full-commit>
workflow_commit: <full-commit>
phase_id: <phase-id>
work_unit_id: <work-unit-id>
state: <state-name>
base_commit: <full-commit>
target_commit: <full-commit-or-null>
formal_source_commit: <full-commit-or-null>
formal_source_fingerprint: <sha256-or-null>
experiment_id: <experiment-id-or-null>
experiment_domain: <product|harness|source-freeze|infra|null>
failure_counters:
  "<product-or-harness>:<experiment-id>": 0
required_tests: []
required_evidence: []
invalidated_evidence: []
external_review:
  pbs_job_id: null
  status: not-started
open_blocking_findings: []
allowed_next_action: <one-action>
```

状态更新必须使用安全的原子替换。不得通过改状态文件掩盖旧失败；相同变化还要追加写入相应 Markdown 记录。

### 4.3 Artifact 命名和保留

Artifact 使用：

```text
YYYYMMDD-HHMMSS_<experiment-id>_<pass|fail|blocked|red|review>.<ext>
```

体积大的训练产物、模型和重复日志保留在原 run 目录时，报告只记录绝对路径、run ID、PBS job ID、source identity、摘要和必要 hash。报告和 prompt 不得含 secret。

每个结构化 gate artifact 至少包含：

```text
artifact_version
status: PASS | FAIL | BLOCKED | REVIEW
gate / experiment_id / requirements_covered
source_identity: commit + dirty + scopes + fingerprint
config_schema_identity / protocol_schema_identity（适用时）
environment: interpreter/package provenance + PBS job/node/topology
workload_identity（适用时）
metrics
errors
evidence_paths
```

Producer 必须原子发布 artifact；consumer 必须验证 schema、status、source identity、requirement ownership 和路径存在性。Checker 结果不能作为产生它自己的独立 evidence，当前输出路径也不能被输入扫描自证。

## 5. 冻结 review target 和 review packet

### 5.1 Commit 连续性

1. Phase 首次审查的 base 是上一 phase-final commit；第一 phase 使用 plan branch point。
2. 后续增量审查的 base 是上一次已审查 target，target 是最新冻结 commit。
3. 同一 target 的重试保持原 base/target。
4. 审查前必须验证 base 是 target 的 ancestor。
5. 禁止跳过、重叠、倒退或把未提交工作树混入审查范围。
6. Plan-complete 报告仍记录 base/target，但审查范围是 target 的完整 current state，而不是只看 diff。

### 5.2 Review packet

Codex/GPT 在提交 reviewer job 前生成统一 prompt。至少包含：

- review kind、plan ID、phase/work unit/experiment ID；
- 完整 base/target commit ID 和审查范围；
- 对应 plan 段落、requirement matrix 行和关键不变量；
- 相关实现、测试、配置、PBS、launcher、Checker 和文档落点；
- 实际测试命令、resolved config、环境、run/PBS ID；
- 结构化结果、关键原始日志和 artifact 路径；
- formal source scopes/fingerprint、identity/authority table 和本轮 source-diff scope；
- fault injection 所在层、durable oracle、预期 failure class 和 cleanup ownership；
- 已知非目标和待判断问题；
- 统一 finding 格式，并要求最后一个非空行严格为 `Verdict: APPROVE` 或 `Verdict: CHANGES_REQUIRED`。

Prompt 只传递路径、commit、要求和必要短文本，不把大段源码或日志塞进 PBS 环境。Prompt 中引用的报告/artifact 必须已 tracked 在冻结 target；runner 只额外提供 diff/stat/changed-file packet，不会复制任意主工作树文件。不得引用 reviewer snapshot 中不可见的 untracked/dirty 文件。

默认将 prompt 保存为 `reports/DOING/code_review/<plan-id>/<review-id>/prompt-source_<target>.md`，通过绝对路径和 SHA-256 传给 PBS。只有短 prompt 才允许使用 Base64 环境变量 fallback。`qsub -v` 值和 scheduler metadata 中绝不能包含 token、凭据、secret 或敏感环境变量。

## 6. 总体状态机

```text
PLAN_INIT
  -> IMPLEMENT_AND_DRAFT_TESTS
  -> TEST_REVIEW_TARGET
  -> EXTERNAL_TEST_REVIEW
  -> TEST_REMEDIATION
  -> STAGED_TEST_EXECUTION
       -> failure 1/2: RECORD_FAILURE -> TARGETED_REMEDIATION
       -> valid failure 3: FAILURE_REVIEW -> LOGIC_REWRITE
  -> PHASE_REVIEW_TARGET
  -> PHASE_CODE_AND_EVIDENCE_REVIEW
  -> PHASE_REMEDIATION_AND_RETEST
  -> PHASE_FINAL
```

最后一个 phase 使用：

```text
FINAL_PHASE_CANDIDATE_TESTS_PASS
  -> PREFORMAL_PLAN_CURRENT_STATE_REVIEW
  -> PLAN_REMEDIATION
  -> FINAL_COMMON_TARGET_FREEZE
  -> FINAL_TEST_LADDER
  -> FINAL_EVIDENCE_REVIEW
  -> optional CRITICAL_BOUNDARY_INCREMENTAL_REREVIEW
  -> PLAN_FINAL
```

`FINAL_PHASE_CANDIDATE_TESTS_PASS` 只要求足以审查实现和测试设计的便宜/中等成本证据，不要求先完成正式 9-node、长 soak 或 20-pair performance。这样 current-state review 发现代码、identity 或 dead-surface 问题时，不会先浪费整套 evidence。`FINAL_EVIDENCE_REVIEW` 主要审查最终实验、matrix、Checker、docs 和 cleanup 的有效性；若仍发现正式 source defect，修复后必须重新冻结 target 并重跑所有被 fingerprint 失效的 gate。

任何状态只能执行 `workflow_state.yaml` 的 `allowed_next_action`。前一门禁未闭合，不得进入下一 phase 或宣布完成。

## 7. 实现和测试设计审查

### 7.1 Codex/GPT 实现

Codex/GPT 按 plan 的 work unit 顺序实现功能代码和测试代码。每个 work unit 尽量小，并明确：

- 要建立或保持的不变量；
- 行为成功和失败的可观察结果；
- 对应 requirement；
- focused test 和更高层验证；
- 风险边界与非目标。

对于行为缺陷，优先保存修复前 RED 证据。若测试与实现同时产生而无法自然保留 RED commit，必须用 characterization、fault injection、mutation probe 或独立 oracle 证明测试能检测目标缺陷，不能只展示最终 GREEN。

每个 work unit 在写 runtime test 前必须先声明：

| 项目 | 要求 |
|---|---|
| identity | 原始值、canonical form、authority owner、持久化位置 |
| mutation authority | 哪个 fenced command/transaction 可以改变状态 |
| fault layer | process、PBS allocation、scheduler、SQLite transaction 内/外或 filesystem visibility |
| durable success oracle | authority row、immutable object、scheduler history、ledger/fence，而不是单一 exit code |
| replay/recovery | 幂等 key、successor owner、允许的事务前/后状态 |
| flow control | producer 可以继续之前所需的 exact ack、积压上界、drain/terminal 优先级与 replay 行为 |
| cleanup owner | terminal proof、引用闭包、可删除范围和不可恢复影响 |

### 7.2 测试审查门禁

在首次执行 runtime/PBS 实验前，冻结 test-review target，并让外部 reviewer 群审查测试设计。审查不能只看测试文件，至少检查：

- requirement 是否映射到具体断言和命令；
- oracle 是否独立于生产实现，是否重复了同一个错误算法；
- 测试是否可能 false pass、false fail 或依赖非确定性时序；
- static/dynamic、正常/失败、crash/restart、takeover、重试和边界反例是否覆盖；
- fixture、clock、lease、PBS identity、path、hash 和状态前置条件是否真实；
- 是否把 normalized/full PBS ID、request/command ID、actor/stable stream、Git ref/commit 或 selected/processed workload 混为同一身份；
- 测试失败时能否区分 production defect、test defect 和 infrastructure failure；
- 是否验证 structured Checker result，而不是只看 PBS exit code；
- 测试成本、walltime、fail-fast 和 artifact 保留是否合理；
- plan 明确要求的项目是否存在无测试或只靠文档声明。

外部 reviewer 全部不可用时，记录其 terminal status 后，由 Codex/GPT 完成同一清单并继续；不得伪造外部 APPROVE。任何已成功产生的测试 finding 都必须先处置，再执行对应实验。

在首次 runtime/PBS 实验前，harness 本身至少通过参数解析、artifact schema、result projection、Git/PBS identity normalization、package provenance、cleanup dry-run 和 expected failure classification 的纯静态或纯单元验证。纯静态验证在 control plane 完成；需要执行项目 runtime 的纯单元验证必须按 7.3 节进入交互式 allocation。Static preflight 与 compute gate 必须使用同一显式文件集合，并包含新增未跟踪但未 ignored 的目标文件；不得对冻结范围外文件执行 bulk formatter。

### 7.3 Codex/GPT 主实例的单节点交互式测试

进入测试执行阶段、且即将运行首个非静态测试时，Codex/GPT 主实例应该显式使用 `$miyabi-development` 技能，在 Miyabi login 节点直接申请并持有一个 1 节点、`walltime=01:00:00` 的 `interact-g` 交互式 PBS job。进入 compute 节点后核验 hostname、`PBS_JOBID`、`PBS_NODEFILE`、项目根目录和 module 状态，然后才能运行项目代码。

在 allocation 存活期间，主实例必须留在该 compute-node session 中，直接修改共享工作树并执行所有只需单节点的 runtime 验证，包括 focused test、单元测试、单节点相关测试组、完整 suite 和短时 smoke test。后续 edit-test-debug 循环必须复用该主实例持有的同一 session，不得回到 login 节点运行 runtime 测试，在qstat命令检测到剩余时间小于10min时可以暂时退回login节点并重新申请一个新的`interact-g` 交互式 PBS job.

所有单节点测试完成后，主实例必须先保存命令、结果、日志、artifact 和 PBS identity，停止其启动的后台进程，正常 `exit` 交互式 shell，确认 allocation 已终止并返回 Miyabi login 节点，然后再进入多节点或批处理测试阶段。不得在单节点测试完成后无任务地保留该交互式 allocation。

## 8. Miyabi reviewer job

### 8.1 节点路由

- 非 Miyabi 主机：在本地完成文件编辑和安全静态检查；需要运行 reviewer 时，通过 Git feature branch 把冻结 commit 同步到 Miyabi。
- Miyabi login 节点：只准备 prompt 文件/hash（或短 Base64 fallback）、检查 commit、运行 `bash -n`、提交 `qsub`、使用 `qstat` 跟踪和读取报告。
- Miyabi compute node：由 `scripts/miyabi/run_multi_agent_review.pbs` 启动四个外部 reviewer。默认在同一个节点并行执行，不为每个 reviewer 单独申请节点。

不得在 login 节点直接运行 `claude -p`、`opencode run` 或 reviewer 内的测试/项目导入。

### 8.2 提交前检查

从 Miyabi 仓库根目录执行：

```bash
bash -n scripts/miyabi/*.pbs
rg -n '^#PBS -W group_list=xg24i002$' scripts/miyabi/run_multi_agent_review.pbs
! rg -n 'group_list=<group_id>' scripts/miyabi
git merge-base --is-ancestor "$BASE_COMMIT" "$TARGET_COMMIT"
```

所有 PBS script 必须使用有效 literal group ID。Reviewer job 默认 1 节点、4 个并发 agent、30 分钟；若已有证据说明更短 walltime 足够，可以在 `qsub -l walltime=...` 中缩短，但不得少于 10 分钟，也不得把余量压到无法完成汇总和清理。

### 8.3 使用路径和 hash 传递 prompt

Prompt 默认先写入 review 目录，使用路径和 SHA-256 避免 PBS `-v` 长度、逗号、换行与 scheduler metadata 泄漏问题：

```bash
PROMPT_FILE="$PWD/reports/DOING/code_review/$PLAN_ID/$REVIEW_ID/prompt-source_${TARGET_COMMIT}.md"
PROMPT_SHA256="$(sha256sum "$PROMPT_FILE" | awk '{print $1}')"

qsub \
  -v "PROJECT_ROOT=$PWD,PLAN_ID=$PLAN_ID,REVIEW_ID=$REVIEW_ID,REVIEW_KIND=$REVIEW_KIND,BASE_COMMIT=$BASE_COMMIT,TARGET_COMMIT=$TARGET_COMMIT,REVIEW_PROMPT_FILE=$PROMPT_FILE,REVIEW_PROMPT_SHA256=$PROMPT_SHA256" \
  scripts/miyabi/run_multi_agent_review.pbs
```

Prompt 文件必须是 `PROJECT_ROOT/reports/DOING/code_review/` 下的普通文件，不能是 symlink，且 hash 必须匹配。只有路径无法共享且 prompt 足够短时，才可改用 `REVIEW_PROMPT_B64`；runner 仍限制解码后大小并记录 hash。

所有 `-v` 值不得包含逗号；当前项目路径、prompt 路径和稳定 ID 应在提交前验证。脚本内已有 literal `group_list=xg24i002`，不要在 qsub 时用动态 group 覆盖。不得使用 `qsub -V` 批量传播环境，因为它可能把无关凭据带入 job。Agent CLI 应使用 compute node 上既有的受控登录状态；不得把 API key/token 放进 `-v`。

可选覆盖变量：

```text
CLAUDE_BIN
OPENCODE_BIN
CLAUDE_MODEL
OPENCODE_GLM_MODEL
OPENCODE_DEEPSEEK_MODEL
OPENCODE_KIMI_MODEL
REVIEW_TIMEOUT_SECONDS
```

### 8.4 Job 结束条件

Runner 必须：

- 确认实际 hostname 是 `mg<number>` 且存在 `PBS_JOBID`；
- 在 node-local 临时目录为每个 reviewer 从 target commit 导出相互隔离的 target snapshot；
- snapshot 必须只包含 target tracked tree 和 runner packet，设为只读，并在 reviewer 前后比较完整 tree digest；发生写入时该 reviewer 标为 invalid；
- 为受限 reviewer 预生成 base/target diff、stat 和 changed-file list；OpenCode 使用只读 `plan` agent，不通过自动批准放开 edit/bash 权限；
- 并行运行四个 reviewer，并对每个实例设置小于 job walltime 的 timeout；
- 分别保存 stdout/report、stderr、raw metadata、requested model、exit code 和 status；
- 即使部分或全部 reviewer 不可用，也生成 `reviewer-job_*.json` 汇总并正常结束；
- 只有 target/base 无效、无法创建安全 snapshot、prompt 无法解码或无法写入汇总等 orchestration failure 才让整个 job 失败；
- 删除 node-local 临时 snapshot，不删除 shared reports 或任何 run 数据。

外部 reviewer 每轮默认只调用一次。额度/余额/限流不自动重试；明确的 transient CLI failure 最多在用户或 workflow state 明确允许时重试一次，并使用新文件名。

## 9. 分层测试执行

测试按 plan 中实际需要逐级扩大：

1. 本地或 login 节点允许的纯静态检查；
2. Miyabi 1-node focused tests；
3. 1-node 完整相关测试组和完整 suite；
4. tiny real pipeline、crash matrix 或生成式状态机；
5. 2-node shared-FS/SQLite/distributed 验证；
6. 8+1 或 plan 指定的正式多节点实验；
7. soak、boundedness 和预注册性能比较。

不得在 Miyabi login 节点运行 pytest、Torch、项目 runtime、Claude/OpenCode reviewer 或重型 Python import。每一级必须通过并保存结构化证据后才扩大资源；plan 明确要求直接使用更高阶拓扑的情况除外，但仍须完成适用的静态门禁。

正式多节点、soak、boundedness 和 performance 只能在 `FINAL_COMMON_TARGET_FREEZE` 后运行。提交前生成 formal-ladder manifest，逐 gate 声明 producer、依赖 artifact、source commit/fingerprint、config/workload identity、节点数、walltime、PASS 公式和 cleanup policy。Aggregate 必须验证所有依赖来自同一正式 source identity；不得仅检查每个 artifact 各自为 PASS。

性能 gate 还必须预注册 baseline/candidate、fresh run-root 规则、environment/package provenance、warmup、固定 repeat 与 AB/BA 顺序、主 timer anchor、timeout、随机 seed、signed statistic、CI、non-inferiority margin 和 incomparable 条件。Workload identity 使用 terminal authority 的实际 processed tokens/cursor/steps；selected/applied work 只能作为另名诊断字段。Cleanup 必须发生在 trial、事件 tape、终态和 workload identity 投影成功之后。

PBS job 提交前必须：

1. 运行 `bash -n scripts/miyabi/*.pbs`；
2. 确认每个 `#PBS -W group_list=` 是有效 literal group；
3. 根据工作量和既有证据申请最短但有安全余量的 walltime，至少 10 分钟；
4. 预注册 success evidence，不能只以 exit code 判定 PASS。

## 10. 测试记录和失败分类

### 10.1 通过记录

一组围绕同一改动或不变量的关联测试全部通过后，在进入下一 work unit 前向 `progress.md` 追加：

- 时间、work unit/experiment ID；
- 目标和范围；
- 新增或修改内容；
- 完整命令、resolved config、环境和 source identity；
- run ID、PBS job ID、节点/拓扑；
- 结果、关键指标、Checker verdict 和 artifact 路径；
- 未覆盖风险、非目标和后续工作。

单个偶然通过不能代替关联测试组结论。代码已修改但尚未验证时不得记为完成。

### 10.2 失败分类

每次失败都写入 `failures.md`，但连续失败计数按验证域分别维护：

| 类别 | 含义 | 是否计入三连失败 |
|---|---|---|
| `expected-red` | 预注册的 pre-fix RED/mutation probe 精确失败，证明测试灵敏度 | 不计入；进入已声明修复步骤 |
| `product-valid-failure` | 环境和 harness 有效，production 未满足接受条件 | 计入 production experiment |
| `test-harness-failure` | fixture、oracle、launcher、Checker 或测试前置条件错误 | 计入对应 test/harness experiment |
| `invocation-invalid` | CLI 参数、路径、shell quoting、patch 或 artifact 字段读取错误，目标 gate 未执行 | 不计入；必须先修命令/preflight |
| `source-blocked` | 行为通过或未否定，但 source dirty、wrong target 或 fingerprint 不一致，不能成为 formal evidence | 不计入 production；禁止重复 compute，先完成 freeze |
| `infra-invalid-run` | 节点、scheduler、quota、网络或环境使结果无效 | 不计入 production/test 连续失败 |
| `reviewer-unavailable` | reviewer 未产生有效报告 | 不计入测试失败，也不阻断 |

失败记录至少包括时间、experiment ID、类别、是否 valid attempt、该域连续失败次数、命令、配置、run/PBS ID、环境、预期/实际、最小症状、原始证据、事实/假设、下一修改和用于证伪的新测试。

“同一 experiment”要求验证目标、核心配置和目标不变量相同。只改 seed、timeout、日志级别或无关参数不产生新 experiment。真正改变核心假设、fault layer、authority oracle 或验证目标时，必须先记录边界变化和理由，再创建新 experiment ID。不得通过把 product failure 重标为新 experiment、harness failure 或 infra failure 清零计数。

## 11. 连续三次有效失败后的升级

同一验证域连续三次有效失败后：

1. 立即停止局部试错；
2. 在 `code_review.md` 记录 escalation；
3. 冻结包含三次失败证据的 review target；
4. Codex/GPT 在读取外部意见前完成必做 failure review；
5. 通过一个 compute-node reviewer job 调用外部 reviewer 群；
6. 汇总完整数据流、控制流、测试 oracle 和至少一个不同实现/解释；
7. 重写根因判断、实现逻辑、RED test 和明确通过条件；
8. 完成修缮前不得运行同一 experiment 的第四次有效尝试。

Failure review 至少覆盖：

- 三次失败的共同模式、差异和证据；
- 输入、状态转换、持久化、恢复和输出全链路；
- SQLite transaction、文件发布、GC 引用、进程和 scheduler 生命周期；
- 实现、测试、配置、launcher 和 Checker，而不是只看最后修改函数；
- 测试是否验证正确不变量、是否有错误假设或漏掉反例；
- 与原思路不同的候选解释或实现；
- 新方案影响范围、RED test、通过条件和避免前三次失败的机制。

外部 reviewer 全部不可用也不阻断第四次尝试，但必须先有 Codex/GPT failure review、不可用证据、finding disposition 和完成后的逻辑重写。

## 12. Finding 汇总、修缮和复测

Codex/GPT 在所有外部实例 terminal 或 reviewer job 达到已记录截止条件后读取报告，去重并处理冲突。每个有效 finding 必须标为：

- `fixed`；
- `rejected-with-evidence`；
- `deferred-with-justification`。

严重级别：

- Critical/High：阻止 phase/plan 完成，必须修复或以强反证拒绝；
- Medium：修复，或写明证据、影响、后续负责人/plan 后延期；
- Low：可记录为 follow-up，但不得丢失；
- 外部 reviewer 缺席：不是 APPROVE，不生成虚假报告，也不阻止 Codex/GPT 门禁。

对接受的行为缺陷，必须新增或修正一个修复前会失败的测试，或保存等价 RED/mutation 证据。修复后运行覆盖所有改动的 focused tests；触及 phase 关键不变量时重跑该 phase 完整关联测试组。

修复不会无限递归触发全量 review。但若改变公共 API、持久化格式、并发协议、安全边界、authority、恢复语义或其他关键不变量，必须冻结新 target，对连续增量执行 Codex/GPT + 外部 reviewer 复审。

## 13. Phase 完成审查

非最终 phase 的所有实现、测试、Checker 和文档候选通过后：

1. 创建 phase review-target commit；
2. Codex/GPT 对 `base..target` 增量独立审查并先保存报告；
3. 外部 reviewer 群通过 compute-node job 审查同一冻结范围；
4. 审查范围包括增量代码、测试、配置、PBS、launcher、Checker、文档以及该 phase 的实验过程和结果；
5. 验证实验命令、resolved config、source identity、run/PBS ID、结构化结果、原始日志和 requirement matrix 可追溯；
6. 检查结果是否真正支持 PASS，而不是只证明进程退出；
7. Codex/GPT 汇总 finding、修缮并重跑受影响验证；
8. 所有 blocking finding 关闭且测试通过后创建 phase-final commit。

Phase-final 前不得开始下一 phase。连续三次失败后的 `code_review.md` 是失败诊断，不能替代 phase 完成审查；phase 完成报告也不能替代失败记录。

## 14. Plan-complete 预正式全量审查与最终 evidence 审查

最后一个 phase 不再执行重复的 phase 小审查。其代码、测试设计和便宜/中等成本 candidate gate 通过后，先冻结 preformal plan-complete target；全量审查必须显式包含最后 phase 的全部增量，不能因此形成审查空洞。正式 9-node、长 soak 和 performance 在该审查及修缮完成后运行。

Plan-complete 审查对象是 target 中项目全部 tracked current state，而不是仅审查最后 diff。至少覆盖：

- `fs_diloco/` 全部 tracked 实现和公共 API；
- tests、configs、PBS、launcher、Checker 和 maintenance/cleanup tools；
- 完整 requirement matrix、已有 phase evidence、formal-ladder manifest 和最终实验设计；
- authority、并发、持久化、恢复、兼容性、安全和性能边界；
- 分层、依赖方向、抽象/API 边界、重复实现、错误处理、可测试性、死代码和文档漂移；
- harness 是否使用正确 identity/authority/oracle，设计是否足以在最终结果出现后支持或拒绝 PASS。

Codex/GPT 必须先完成 current-state 全量报告，再读取外部 reviewer 结果。随后在 `plan-complete-remediation.md` 建立按优先级和依赖排序的处置计划。

架构 finding 分成：

1. **架构正确性缺陷**：错误 authority、危险依赖、重复 writer、不安全边界、恢复/兼容性错误；必须在当前 plan 修复；
2. **必要维护性修缮**：与本 plan 变更直接相关且风险可控；当前 plan 修复；
3. **可选架构改进**：不影响正确性且会显著扩大范围；记录到下一 plan/follow-up，不得在最终阶段无界重构。

修缮后运行受影响的 candidate tests，并完成必要增量复审；随后冻结唯一 final common target，才开始 formal ladder。

Formal ladder 完成后执行 `FINAL_EVIDENCE_REVIEW`，审查：

- 每个 gate 的命令、resolved config、source/config/workload identity、PBS 拓扑与 walltime；
- durable authority 结果是否支持 PASS，而不是只证明退出码或进程表象；
- performance workload equivalence、signed statistics、CI 和预注册门槛；
- requirement matrix 是否 100% 绑定 tracked、独立、同 target evidence；
- documentation synchronization、artifact retention 和 evidence-bound cleanup 是否准确；
- completed Checker 是否排除 self-proof、wrong source 和未跟踪 evidence。

Evidence-only/report-only finding 可直接修正并重跑 Checker。若该审查发现 source、test、config、PBS、launcher 或 Checker 逻辑需要修改，必须回到 `PLAN_REMEDIATION`，冻结新 common target，并重跑所有被 source fingerprint 或测试语义失效的正式 gate。预正式全量审查、finding 处置、formal ladder、最终 evidence review 和 completed Checker 全部闭合前，不得宣布 plan 完成或进入下一 plan。

## 15. Reviewer 状态和审计字段

每个 reviewer invocation 至少记录：

- reviewer、CLI、invocation/session ID、requested model、可核验 actual model；
- review kind、plan/review ID、base/target；
- PBS job ID、compute hostname、开始/结束时间；
- prompt SHA-256；
- exit code、status、报告/raw/stderr 路径；
- 是否产生有效 finding/verdict。

允许的外部状态包括：

```text
completed
skipped-capacity
skipped-tool-unavailable
skipped-scheduler-unavailable
timed-out
invalid-output
invalid-model-identity
invalid-snapshot-mutation
failed-command
```

除 `completed` 外都不是 APPROVE。它们对外部 reviewer 本身非阻断，但 orchestration 必须有可核验证据，且 Codex/GPT 必做审查仍然阻断。

Runner 产生的 `completed` 只表示 CLI 成功返回了满足最小长度，且最后一个非空行是规定 verdict 的结构有效报告。Codex/GPT 仍须根据 raw metadata 和报告头核验实际模型与范围；核验失败时把该实例重新处置为 `invalid-model-identity` 或 `invalid-output`。

## 16. Test artifact retention and cleanup

每个测试或实验达到 terminal state 后，在下一 work unit 前：

1. 先保存完整命令、resolved config、source identity、run/PBS ID、Checker result、summary metrics 和审计路径；
2. 成功测试只保留证明不变量所需的最小代表日志和 artifact；
3. 失败测试保留完整错误日志、最小复现和仍用于根因分析的 artifact；
4. 盘点并解析精确 cleanup target，只清理该已完成测试的已知 run 目录；
5. 不删除 live、queued、resumable run，恢复所需 DB/checkpoint，源码、配置、报告、未解决失败证据、预先存在的用户数据或所有权不确定路径；
6. 删除仅由已完成测试产生且信息已被摘要/manifest 覆盖的重复 checkpoint、staging、cache、重复成功日志和 orphan payload；
7. 使用并扩展 `fs_diloco/tools/clean_run.py`，在 `progress.md` 或 `failures.md` 记录删除范围、数量、大小和可恢复性。

Plan-final 提交和 completed Checker 均通过后，才可用独立 archive/move commit 把 plan 与报告迁入 `plans/DONE/<plan-id>/` 和 `reports/checked/<plan-id>/`。移动必须保留 Git rename 可追溯性，并生成旧 `reports/DOING/...` evidence 路径到新位置的映射或稳定索引；不得在移动时顺便删除其他 plan 的报告或 artifact。
