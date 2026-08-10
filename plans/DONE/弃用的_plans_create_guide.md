# Plan 构建指南

## 1. 目的与适用范围

本文指导如何基于仓库当前状态构建新的实施 plan、design 和 requirement matrix。目标不是写一份愿望清单，而是形成一套可以被后续执行者按顺序实施、证伪、审查、复现和关闭的工程合同。

新建或实质修订 `plans/DOING/**` 下的 plan 前，必须同时读取：

- 仓库根目录 `AGENTS.md`；
- `plans/AGENTS.md`；
- 本指南；
- `plans/workflow.md`；
- 与目标代码最近的现有 design、历史 plan、review 和实验报告。

本指南回答“plan 应怎样构建”；`plans/workflow.md` 回答“plan 应怎样实施”。Plan 可以增加更严格条件，但不得复制一份稍有差异的 workflow 或降低通用门禁。

本版方法特别吸收了 [`plans/ref/实施计划制定与 Agent 执行经验.md`](ref/%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92%E5%88%B6%E5%AE%9A%E4%B8%8E%20Agent%20%E6%89%A7%E8%A1%8C%E7%BB%8F%E9%AA%8C.md) 以及 [Plan 03 实施复盘](../reports/checked/fsb_decoupled_diloco_plan_03_unified_ha/implementation-retrospective.md) 中的问题模式。它们是经验来源，不是新 plan 的默认事实；每份 plan 仍须重新核验当前代码。

## 2. Plan 的输出集合

推荐使用以下结构：

```text
plans/DOING/design/<plan-id>-design.md              # 复杂架构变更时需要
plans/DOING/plans/<plan-id>.md                      # 必需
plans/DOING/plans/<plan-id>-requirement-matrix.csv  # 必需
```

`<plan-id>` 必须：

- 满足 `[A-Za-z0-9._-]+`；
- 在 `plans/DOING/**` 和 `plans/DONE/**` 中全局唯一；
- 不依赖目录名表达身份；
- 创建后保持稳定，报告、review、artifact 和 Checker 都使用同一 ID。

Plan 正文只写目标、设计约束、工作单元、测试设计和完成条件。实施中的 job ID、运行结果、finding disposition 和临时路径写入 `reports/DOING/<plan-id>/`，不得反向污染 plan 正文。

## 3. 先调查当前状态，再写解决方案

### 3.1 必做 inventory

至少检查并记录：

1. 当前 branch/commit、dirty/untracked 状态和相关历史提交；
2. production entrypoint、launcher、PBS、config 和环境 lock；
3. public API、typed protocol、SQLite schema/DDL、filesystem layout 和 maintenance/cleanup；
4. static/dynamic、current/legacy、baseline/production 的真实调用图；
5. tests、fixtures、xfail/skip、Checker、requirement matrix 和现有 structured artifacts；
6. 相关 run 的 terminal evidence、失败报告和性能/资源数据；
7. 已存在的 compatibility、migration、query-only 和删除边界；
8. 外部 model/dataset/source identity 如何从配置到达实际 loader。

优先使用 `rg`、`rg --files`、Git 和只读解析。不要仅根据文件名、旧 plan 或单个测试推断当前行为。

### 3.2 将调查结果分类

Plan 中的背景必须区分：

| 类别 | 写法 |
|---|---|
| 已验证事实 | 给出代码、测试、schema、历史 artifact 或复现路径 |
| 待验证假设 | 明确写成假设，并安排最早的 falsification gate |
| 已知缺陷 | 给出影响、不变量和预期 RED，不先假定修法 |
| 设计选择 | 记录替代方案、选择理由和被拒绝方案 |
| 非目标 | 说明不做什么，以及为什么不会破坏完成定义 |
| follow-up | 说明为何非阻塞、owner/后续 plan 和触发条件 |

不得把推断写成事实，也不得把“已有测试通过”直接等同于“设计正确”。

## 4. 先定义不变量、authority 和身份

跨进程、跨节点或持久化计划必须先回答“谁有权改变状态”和“同一个对象在各层叫什么”。

### 4.1 Authority table

建议在 design 或 plan 中加入：

| 状态/动作 | 唯一 mutation authority | transaction/fence | 可读 cache | recovery owner | 禁止路径 |
|---|---|---|---|---|---|
| 示例：membership admission | SQLite authority command | leader epoch + contributor fence | current pointer | current leader replay | actor 直写 DB、cache 授权 |

至少覆盖：

- run initialization；
- membership/admission/replacement；
- receipt/proposal ingest；
- selection/commit/publication；
- token accounting；
- terminal/drain；
- scheduler reconciliation；
- audit/GC/cleanup；
- legacy read/migration；
- external model/data/source identity。

### 4.2 Identity table

建议加入：

| Identity | 原始形式 | canonical form | authority owner | 持久化位置 | 允许比较对象 |
|---|---|---|---|---|---|
| PBS job | qsub receipt / `*.opbs` | 明确 canonicalizer 后的 job ID | scheduler receipt/history | launch + instance row | 同类型 canonical PBS ID |
| Git target | ref/tag object/commit | peeled full commit | Git object DB | workflow/review artifact | commit，不与 tag object 混比 |
| contributor | actor instance + stable stream + fence | typed identity | membership authority | SQLite + immutable controls | 相同 identity type |

不要用一个通用字符串字段混装 request ID、command ID、actor ID、stream ID、generation、epoch、PBS ID 或 commit ID。若确实需要投影，Plan 必须指定单一 canonicalizer 和反例测试。

### 4.3 External input identity

涉及 model、tokenizer、dataset、checkpoint、代码快照或本地文件输入时，Plan 必须说明：

- immutable Hub commit、content digest 或 descriptor manifest 是什么；
- config validator、run descriptor 和实际 producer 是否消费同一 identity；
- relative path、symlink、environment override、fallback 和 actor cwd 如何处理；
- local mutable input 没有 content manifest 时是否 fail closed；
- synthetic、baseline、legacy query-only 是否使用独立 validation profile。

只在 config-time 检查字符串通常不够，必须追到实际 I/O 调用点。

### 4.4 异步交接、流控与计数语义

只要 producer 与 authority 之间存在异步交接，Plan 就必须声明：

- publication 与 durable ingest 各自的 commit point；
- ack 绑定的 exact identity、fence、sequence 和 content digest；
- producer 在 ack、drain、terminal 或 timeout 前能否继续，以及积压上界；
- successor 如何幂等 replay publication/ack，stale ack 如何被拒绝；
- terminal close 如何联合 authority 的已 ingest frontier 而不是猜测进程进度。

不得用“global version 变了”、“看到日志”或“等待一段时间”代替 exact ingestion ack。公平性和 accounting 也应直接记录被声明的量，例如 committed service count、processed token/cursor 或 terminal gap；只有 durable commit 可以消耗 credit，不要用 version、selection 或时间作为不完整代理。

## 5. Requirement 的写法

### 5.1 每条 requirement 必须可证伪

一条合格 requirement 至少包含：

1. 稳定且唯一的 ID；
2. 行为或不变量，而不是文件改动描述；
3. authority/fence/identity 前提；
4. 正例、负例、replay/rollback 或 counterexample；
5. 机器可判定的 PASS 公式；
6. implementation owner 和 test owner；
7. artifact producer 和 Checker consumer；
8. owner phase 与最终状态。

较差写法：

> 改进动态恢复并增加测试。

较好写法：

> 在 scheduler historical FINISH 证明 exact instance allocation 终止前，不得替换该 incarnation；FINISH 后 replacement 必须复用 stable stream、推进 stream epoch，旧 fence 在 replacement boundary 后成功 commit 数为 0。

### 5.2 Requirement matrix 最小列

建议 CSV 至少包含：

```text
requirement_id
phase_id
source_finding
behavior
authority_and_identity
implementation_owner
test_owner
test_level
pass_formula
checker_requirement
evidence_schema_or_path
status
```

Matrix 的作用是建立可执行追踪，不是重复 plan 正文。所有 `complete` 行必须由 tracked、独立、source-bound artifact 支持；Checker 输出不能自证当前 Checker requirement。

### 5.3 删除 requirement

删除旧代码时，matrix 同时证明：

- production writer/entrypoint/config/DDL/fallback 不存在；
- shared invariant 已迁移到 current tests；
- 需要保留的 legacy query-only 行为仍可用；
- old incomplete run 明确拒绝 resume；
- 删除测试逐项归入 `migrate-to-current`、`retain-query-only` 或 `delete-obsolete`。

不要以“文件已删除”代替行为和兼容性证明。

## 6. Phase 与 work unit 的设计

### 6.1 Phase 原则

Phase 应按风险和依赖切分，而不是按文件夹切分。每个 phase 必须有：

- 输入 commit/前置 requirement；
- 本 phase 建立的不变量；
- 有界实现范围；
- focused、完整相关和高层 gate；
- artifact/Checker owner；
- review target 与 phase-final 条件；
- 明确非目标。

典型顺序可以是：

1. current-state inventory、oracle 和方法冻结；
2. typed/config/schema foundation；
3. correctness 与 measurement authority；
4. operational/recovery hardening；
5. mandatory runtime cutover；
6. compatibility、migration 与 old writer 删除；
7. preformal current-state review、formal acceptance 和 evidence closure。

具体 plan 不必机械复制这些 phase，但任何后续 work unit 都不能依赖尚未建立的 authority 或 schema。

### 6.2 Work unit 原则

每个 work unit 应足够小，使执行者能清楚回答：

- 这一步只改变哪个不变量？
- pre-fix RED 是什么？
- 哪些 API/schema/config 会改变？
- 失败时能否判断 product、harness、invocation、source freeze 或 infra？
- 哪组测试共同证明完成？
- 是否会使已有 evidence 失效？

不要把大量 unrelated refactor、format、docs、migration 和 runtime behavior 放在同一 review target 中。

## 7. 测试与 fault scenario 设计

### 7.1 Test ladder

Plan 必须给出从便宜到昂贵的阶梯，并说明哪些层适用：

```text
static diff/compile/lint/format/PBS syntax
→ focused pure/unit tests
→ 1-node affected/full suite
→ generated state machine/crash matrix
→ tiny real pipeline
→ 2-node shared-FS/SQLite/distributed
→ formal multi-node
→ soak/boundedness/performance
```

高层 gate 不能替代低层 gate；低层 PASS 也不能替代真实 topology。Static 与 compute 必须使用同一显式文件集合，包括新增未跟踪但未 ignored 的目标文件。

### 7.2 Durable oracle

Fault test 的成功条件应来自 durable authority，例如：

- exact fence 后成功 commit 数；
- current membership/stream epoch；
- immutable request/receipt digest；
- scheduler live + historical evidence；
- token fate/ledger balance；
- terminal fences、summary 和 SQLite integrity；
- hot+archive 的联合历史。

PID、Unix exit、日志末行、固定 sleep、mtime 或临时文件数可以用于诊断，但通常不能单独定义 PASS。

### 7.3 Fault layer 必须真实

Plan 必须为每个故障场景注明注入层：

| Fault | 正确注入层示例 |
|---|---|
| permanent learner loss | 独立 PBS allocation + scheduler historical FINISH |
| stale writer takeover | SQLite transaction 外 SIGSTOP；transaction 内场景必须等待 lock 并显式终止 old writer |
| filesystem visibility | immutable publication boundary/create-no-replace/manifest marker |
| duplicate actor | admission 前的 registration path，不先加载 Torch/GPU |
| candidate crash | 明确在 transaction 前/内/后哪一个边界 |

如果 production 条件依赖 scheduler 或 filesystem 第二因子，test 不得通过直接写 SQLite、伪造 FINISH 或杀共享 allocation 内的 PID 绕过它。

### 7.4 RED 与 failure class

Plan 应预先声明 RED artifact 的期望失败集合，避免把预期 RED 与 production regression 混淆。使用 workflow 中的分类：

```text
expected-red
product-valid-failure
test-harness-failure
invocation-invalid
source-blocked
infra-invalid-run
```

RED 必须“精确失败”：只失败目标断言，且反向兼容 counterexample 继续通过。

## 8. Harness、Checker 与 evidence 的设计

### 8.1 Harness 先单独验证

首次 qsub 前，Plan 必须要求 runner/harness 具有纯静态或纯单元测试，至少覆盖：

- 参数解析和 mutually exclusive CLI；
- Git ref peeling 和 package origin；
- full/normalized PBS job ID；
- artifact JSON schema 与字段投影；
- terminal status/reason；
- timeout/cleanup finally；
- failure classification；
- output path 和 source identity。

不要用正式 PBS allocation 调试 `set & dict`、遗漏 import、错误 JSON 字段或 shell quoting。

### 8.2 Structured artifact

Plan 中应冻结 artifact schema，最小字段遵循 workflow。进一步建议：

```json
{
  "artifact_version": 1,
  "status": "PASS",
  "gate": "<gate-id>",
  "requirements_covered": ["<requirement-id>"],
  "source_identity": {
    "git_commit": "<full-commit>",
    "git_dirty": false,
    "scopes": ["..."],
    "fingerprint": "sha256:..."
  },
  "environment": {},
  "workload_identity": {},
  "metrics": {},
  "errors": [],
  "evidence_paths": []
}
```

不要在不同 producer 中交替使用顶层 `source_commit`、嵌套 `source.commit`、`source_identity.git_commit` 而不提供统一 parser/schema。

### 8.3 Checker 拓扑

Checker 应按有向无环证据流设计：

```text
source/static contract
→ runtime/test artifact
→ requirement matrix binding
→ staged requirement Checker
→ tracked-evidence completed Checker
```

禁止：

- 当前 Checker 输出作为其自身 input；
- 在 runtime artifact 产生前要求 matrix 已绑定该 target 的 PASS artifact；
- wrong-source evidence 通过 basename 或 status 混入；
- silent `pop(..., None)`、模糊 projection 或多个相互重叠的迁移 mini-language。

## 9. Formal source identity 与最终证据

Plan 必须声明 formal source scopes。通常至少考虑：

```text
fs_diloco/
tests/
configs/
scripts/
docs/
main.py
pyproject.toml
uv.lock
.python-version
```

具体 scope 可调整，但必须说明排除项和理由。Untracked/ignored 的 executable input 也必须被 source capture 发现或显式拒绝。

正式 acceptance 前执行 preformal current-state review；修缮完毕后冻结唯一 `FINAL_COMMON_TARGET`。Formal-ladder manifest 为每个 gate 声明：

- source commit/fingerprint；
- upstream artifact；
- resolved config/schema/workload identity；
- topology、queue、walltime；
- PASS 公式；
- output 和 cleanup policy。

同一 aggregate 中所有 gate 必须共享 formal source identity。任何 formal scope 变更都会使旧 artifact 失效；report-only/evidence-only commit 只有在 source-equivalence Checker 证明后才可承接旧 runtime evidence。

## 10. Performance、boundedness 与质量研究

### 10.1 Performance

在看到结果前预注册：

- baseline/candidate 与独立 worktree/venv/run root；
- package/model/data/config/seed/resource identity；
- fresh-root timer anchor、warmup、repeat、AB/BA 顺序和 timeout；
- actual processed tokens/cursor/steps、selected/applied diagnostics；
- signed metric、CI、margin、incomparable 和 no-clipping 条件；
- cleanup 前必须投影的 terminal/event evidence。

Timer 不能决定 data cursor，selected work 不能代替实际 processed work。负 overhead 仍须通过 workload equivalence 和异常幅度审计，不能自动视为 PASS。

### 10.2 Boundedness

区分：

- recovery hot set：必须有界，启动/发现不能随历史线性扫描；
- immutable audit/history：可按事件增长，但需 batch/hash/partition/compact policy，且不进入 hot recovery path。

不要用“磁盘文件总数永远不增长”作为错误门禁。Slope、CI、warm-up、sample interval 和 retention 必须预注册。

### 10.3 质量研究

Correctness、performance 和统计质量研究分域。若多 seed 质量研究不属于 blocking completion definition，必须明确写为 nonblocking、NOT_RUN 可接受且不得声称结论；反之则提前冻结功效、seed、unique-token budget 和统计方法。

## 11. Miyabi/PBS 资源设计

Plan 对每个 PBS gate 至少给出：

- 节点数和 topology；
- queue 和 placement；
- dtype/CPU/GPU/memory 预算；
- workload 估算和最短安全 walltime，至少 10 分钟；
- parent/child allocation 上限；
- job 创建者与可清理 job ID 集合；
- timeout、terminal wait 和 orderly teardown 余量。

登录节点只做控制面工作。不要先安排大量 9-node 作业再补 harness review；先运行 test-design review、1-node full suite 和最小 topology probe。

## 12. Compatibility、migration 与文档

Plan 必须明确区分：

- 新 run strict current behavior；
- old completed run query-only behavior；
- old incomplete run resume policy；
- baseline/synthetic/legacy validation profile；
- config/schema migration 是否创建新 root，是否允许原地写。

Migration 需要 exact input/output projection 和 unknown-key fail-closed。不要让 shared lossy projection 承担只有 full envelope 才能判断的语义。

文档同步应指定目标章节、symbol/link scan 和实验数字归属。稳定 docs 描述验证后的行为与限制；job ID、临时时间和逐次结果保留在 reports。

## 13. Review 位置

Plan 至少安排：

1. expensive test 前的 test-design review；
2. 每个非最终 phase 的 phase review；
3. 正式昂贵 acceptance 前的 preformal current-state full review；
4. formal ladder 后的 final evidence review；
5. critical boundary remediation 后的连续增量复审。

这样可以在昂贵作业前发现 external identity、dead surface、oracle 和 harness 问题，同时仍对最终实验有效性做独立审计。

## 14. Cleanup 与归档设计

Plan 在实验开始前定义 cleanup policy，而不是事后凭目录名删除。至少说明：

- terminal/resumable/live 判定；
- exact run ownership；
- evidence SHA/policy SHA；
- inode/device/mtime/size revalidation；
- DB/checkpoint/audit/GC/current authority 保留；
- 删除数量、字节数、可恢复性和复现路径；
- cleanup 前 summary/event/workload projection。

Plan-final 和 completed Checker 通过后，才安排单独 archive/move commit；不得在移动 Plan 03 时顺便删除 Plan 02 报告，或让历史 evidence path 无映射失效。

## 15. 推荐 Plan 正文模板

```markdown
# <Plan title>

## 0. Metadata
- Plan ID
- Workflow version/commit
- Branch point
- Related design/reviews/plans

## 1. Objective and non-goals

## 2. Verified current-state inventory
- Facts
- Assumptions to falsify
- Existing behavior/oracles

## 3. Findings and dispositions to implement

## 4. Invariants

## 5. Authority table

## 6. Identity table

## 7. Target architecture and alternatives

## 8. Compatibility/migration/deletion boundaries

## 9. Requirements and matrix contract

## 10. Phase/work-unit sequence
- Inputs
- Changes
- RED/counterexamples
- Focused/full/high-level gates
- Review/final conditions

## 11. Test ladder and fault scenarios

## 12. Formal source/artifact/Checker design

## 13. Miyabi resource and walltime plan

## 14. Performance/boundedness/quality design

## 15. Documentation and cleanup

## 16. Risks, rollback and follow-ups

## 17. Completion definition
```

## 16. Plan 发布前自审清单

只有以下问题都能明确回答，plan 才应进入 `plans/DOING/`：

- [ ] Plan ID 全局唯一，workflow version/commit 已冻结。
- [ ] 当前代码、测试、config、PBS、schema、entrypoint 和旧报告已实际检查。
- [ ] 事实、假设、设计选择、非目标和 follow-up 已分开。
- [ ] 每个关键状态有唯一 mutation authority。
- [ ] 跨层 identity 有 type、canonicalizer、owner 和反例测试。
- [ ] 每条 requirement 可证伪，并有 implementation/test/artifact/Checker owner。
- [ ] 每个行为缺陷有 RED 或等价 mutation/characterization 证据。
- [ ] Fault scenario 在正确的 process/allocation/scheduler/transaction/filesystem 层注入。
- [ ] PASS 基于 durable authority，不只基于 exit code、sleep 或日志。
- [ ] Harness、artifact schema 和 identity normalization 在 qsub 前可独立测试。
- [ ] Static 与 compute 使用同一显式文件范围，包含新增文件。
- [ ] Formal source scopes、fingerprint 和 evidence invalidation 规则明确。
- [ ] Performance 使用实际 processed workload，并预注册统计方法。
- [ ] Hot recovery 与 audit growth 分域。
- [ ] Legacy query-only 与 current strict runtime 边界清楚。
- [ ] Preformal current-state review 位于昂贵 acceptance 之前。
- [ ] Resource topology、walltime、child allocation 和 cleanup owner 有估算。
- [ ] Cleanup 有 dry-run、identity revalidation、保留集和不可恢复说明。
- [ ] Completion definition 可以由 requirement matrix 和 completed Checker 机器判定。

## 17. 应避免的反模式

- 先写实现文件清单，再倒推行为目标。
- 用 timeout、重试次数或更宽状态集合掩盖未知 race。
- 用进程退出代替 stale-commit/ledger/fence 证明。
- 用 selected update 代替实际 processed workload。
- 在 actor loader 之外只验证 config 字符串。
- 对全仓运行 formatter，触碰冻结或无关范围。
- 用正式 PBS 作业调试 harness 的字段名、CLI 或 fixture。
- 让 Checker 消费自身输出或尚未产生的 evidence。
- 在 plan-complete review 之前先跑全部昂贵 formal ladder。
- 为兼容方便恢复已删除 writer、alias 或 runtime fallback。
- 把未运行的质量研究写成 PASS，或用它阻塞无关 correctness gate。
- 清理时按 glob/目录直觉删除，没有 exact owner 和引用闭包。
