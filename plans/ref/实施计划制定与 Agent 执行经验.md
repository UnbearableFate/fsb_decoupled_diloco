# 实施计划制定与 Agent 执行经验

本文补充 [AGENTS.md](AGENTS.md) 的实施记录与失败升级规则，提炼 plan 01 在持久化、崩溃恢复、有界状态、PBS 验证和长作业交接中的经验。后续 plan 编写者和实施 agent 应优先把这些经验转化为明确的规格、检查点和证据要求，而不是依赖执行时临场补充。

## 1. 先定义正确性，再列改动文件

一份可实施的 plan 应首先回答“什么状态是权威的、什么结果算正确”，然后才描述代码改动。

### 1.1 明确权威关系

至少写清以下内容：

- 唯一权威状态是什么；
- 哪些文件或缓存可以重建；
- 哪个动作是提交点；
- 提交前后分别允许观察到什么；
- 缺失、冲突或损坏时是恢复、回退还是 fail closed；
- 谁是 writer，是否允许并发 writer；
- 进程、节点、调度作业和共享存储分别采用什么故障模型。

建议用一张很短的权威链表示，例如：

```text
不可变 payload
    ↓
事务性权威记录（唯一提交点）
    ↓
可重建的查询缓存
```

如果 plan 同时把数据库、指针文件和目录扫描结果都称为“事实源”，agent 很难实现无歧义的恢复语义，测试也无法判断冲突时谁应该获胜。

### 1.2 把不变量写成可检查条件

不要只写“支持恢复”“存储有界”“正常结束”。应改写为机器或人工都能判定的条件，例如：

- 最大 committed version 唯一且连续；
- DB、latest、checkpoint 和 summary 的版本一致；
- 一次 merge 中每个 learner 最多贡献一个 proposal；
- active proposal 数不超过 `2M`；
- current-only 模式只保留一份当前 weight/outer；
- 终态 proposal tensor 数为零；
- 删除 cache 后可由权威状态重建；
- 删除权威状态后不能仅凭 cache 恢复；
- terminal drain 不绕过 future/staleness 规则；
- `Exit_status=0` 之外还必须存在实际工作负载输出和状态变化。

每条核心不变量最好有稳定 ID，并在 plan 中建立以下映射：

```text
需求 ID → 实现位置 → 测试 ID → 检查点 → artifact
```

### 1.3 明确范围、兼容性和非目标

plan 必须显式区分：

- 本轮完整实现的路径；
- 只采用部分基础设施、但不实现完整语义的路径；
- 不兼容的旧运行方式；
- 是否提供迁移或自动回退；
- 本轮故意不做的恢复级别、并发模式或性能优化；
- 哪些行为留给下一份 plan。

plan 01 中“full 完整闭合、fragment 使用持久 DB 但 resume 另行实施”的边界很重要。没有这类边界，agent 容易把局部改造误报为全模式完成，或为了兼容旧状态引入未经设计的双重权威。

## 2. Plan 应覆盖完整生命周期

涉及持久状态时，不能只规划主循环的成功路径。至少要覆盖以下阶段：

1. 新 run 初始化；
2. proposal/payload 发布；
3. discovery 与幂等摄取；
4. eligibility、selection 与 quorum；
5. 状态变更和事务提交；
6. cache/latest 发布；
7. archive、DB pruning 与 artifact GC；
8. 正常停止和异常停止；
9. shutdown 期间的迟到输入；
10. resume、重复 resume 和跨节点 resume；
11. 离线分析与研究证据保留；
12. 长时间运行后的物理工作集。

### 2.1 状态转换要写全

对每种对象列出状态和转换条件。以 proposal 为例，应至少说明：

```text
pending → selected → applied
pending → dropped(superseded / too_stale / future / shutdown)
selected → pending（失败或 resume 回滚）
selected → dropped（正常输入闭合后的终态化）
terminal row → archive fsync → DB prune → payload GC
```

还要写清“谁负责转换”和“转换是否与版本提交处于同一事务”。否则实现容易出现 global version 已提交但 proposal 尚未 applied，或 proposal 已 applied 但版本行不存在的中间状态。

### 2.2 终止语义必须独立设计

正常达到目标后仍可能有以下并发事件：

- learner 已经开始写下一份 payload；
- learner 在看到 stop 前替换了最终 pointer；
- syncer 在 stop 后最后一次摄取到新 proposal；
- heartbeat 已停止更新，但 learner 没有明确声明 stopped；
- 输入已经关闭，但剩余 proposal 低于正常 quorum。

因此 plan 应明确：

- 什么证据可以证明输入关闭；
- terminal visibility grace 的起点和长度；
- grace 后是否再次摄取；
- 低于 quorum 时是否允许尾部 merge；
- 无 eligible proposal 时使用什么正常 stop reason；
- 未消费的 pending/selected 如何终态化；
- archive/GC 在 summary 前还是后执行；
- dead/stale 是否等价于 stopped。

“达到训练步数”或“心跳超时”通常不足以证明不会再发布输入；明确的 stopped 最终心跳更可靠。

### 2.3 GC 必须基于引用，而不是文件名或版本大小

有界状态计划应定义 live set，而不只定义“保留最近 N 份”。live set 至少要考虑：

- 当前 DB committed checkpoint；
- current fragment checkpoint；
- latest 引用的 materialized checkpoint；
- DB 中 pending/selected payload；
- 正在完成 pointer publication 的宽限期对象；
- 临时文件；
- 已归档终态对象。

高版本 orphan 不能挤掉较低但真正 committed 的 current checkpoint。终态 pointer 也不应无限保留已经归档的 tensor。

## 3. 在昂贵实验前规划可观测性

验收阈值所需的 telemetry 必须作为实现内容提前列入 plan。不要等到 9 节点作业结束后才发现无法计算 p95、维护开销或阶段耗时。

每个性能或可靠性门槛应提前定义：

- 指标字段名；
- 采样边界；
- wall-clock 还是 CPU time；
- 聚合方法（sum、mean、p95 等）；
- 分母是什么；
- 写入 CSV、JSONL、W&B 还是 DB；
- checker 如何读取；
- 缺失字段是否直接失败。

例如，“SQLite 开销低于 5%”必须拆为 `sqlite_commit_seconds`、`maintenance_seconds` 和完整训练时间，并说明使用逐 merge 求和后除以哪一个端到端时间。

## 4. 检查点应形成逐级验证阶梯

不要把“全部 pytest 通过”和“一次大作业成功”当成唯一两个检查点。推荐使用以下阶梯，并让较便宜的检查点先阻断明显错误。

### G0：指令、范围和基线

- 读取根目录与 scoped `AGENTS.md`；
- 按触发规则加载必要 skill；
- 确认 hostname、PBS 上下文和允许执行的动作；
- 检查 dirty worktree，区分用户改动与本轮改动；
- 记录现有配置、目录布局和关键不变量；
- 明确哪些旧 run 不属于兼容范围。

通过条件：实现范围和非目标无歧义，不需要靠猜测选择权威语义。

### G1：登录节点静态门禁

- `git diff --check`；
- lint/格式检查；
- 配置字段和禁用参数的静态搜索；
- `bash -n scripts/miyabi/*.pbs`；
- PBS group ID 必须是有效字面值；
- launcher 中不得残留已删除参数。

登录节点只做允许的静态检查和轻量只读分析，不运行 pytest、torch/model import 或训练。

### G2：聚焦单元与事务测试

按不变量分组运行，而不是一次只测一个函数：

- 配置拒绝和默认路径；
- PRAGMA、reopen 和 integrity；
- transaction 正常提交、rollback、重复版本、跳版本；
- selected 集合不完整、重复 learner、future/stale 边界；
- fixed pointer 重放、latest-wins 和 frontier；
- resume cache repair 与 fail-closed 反例；
- archive/GC 引用关系；
- terminal input closure 和 partial drain。

通过条件：同一工作单元的正例、反例和 rollback 断言共同通过。

### G3：崩溃矩阵

在每个有意义的 publication 边界注入确定性失败：

- 临时 payload 写入中；
- payload 完成后；
- outer/checkpoint 完成后；
- transaction 内；
- DB commit 后、cache 更新前；
- cache 更新后。

每个点应重复多次，并检查：

- 只能恢复到事务前或事务后；
- 不存在部分事务；
- selected 不重复应用；
- latest 可修复；
- orphan 可清理；
- integrity 正常；
- 恢复后还能继续提交下一版本。

### G4：真实小型 pipeline

单元测试不能替代真实 learner/syncer 收尾。tiny smoke 至少检查：

- 真实并发发布与摄取；
- stop/summary/latest/DB 一致；
- 最终 heartbeat；
- active row；
- checkpoint 文件集合；
- proposal tensor/meta；
- temp、WAL、dump；
- error/no-progress 事件。

必须同时检查数据库状态和最终目录，不能只看 stop reason。

### G5：长循环有界性

使用确定性 1000-cycle 或更长状态机，验证：

- active rows 的上界；
- discovery 面大小；
- current global/fragment row 数；
- archive identity 完整性；
- SQLite `page_count - freelist_count` 在 warm-up 后不线性增长；
- 文件数和 tensor 数不随历史线性增长。

逻辑行数有界不等于物理工作集有界，两者都要测。

### G6：跨节点验证

至少验证：

- 多节点同时访问共享 DB；
- transaction 压力下无 busy/IOERR/locked；
- 节点 A 提交、节点 B reopen 后看到相同版本；
- 节点 B 可只凭权威 DB 修复 cache；
- 遗留 selected 被回滚且最多应用一次；
- 恢复后继续产生新版本。

### G7：正式规模验收

正式作业应使用预先冻结的配置和验收口径。检查内容至少包括：

- 角色和节点数；
- committed merge 数；
- 每轮 distinct learner/selected count；
- DB/latest/stop/summary 一致；
- current-only 文件集合；
- active proposal 上界；
- 终态 payload 为零；
- integrity 和 failure-event 扫描；
- 预先定义的性能阈值。

### G8：长作业阶段性交接

长作业不一定需要 agent 等到自然结束。plan 可以定义阶段性交接点，例如：

- 作业已提交并真正开始执行；
- v0 后连续出现至少 N 次 commit；
- 每次 DB/latest/checkpoint 一致；
- 旧版本已 GC；
- learner adoption 持续前进；
- 无错误事件；
- 记录 job ID、run ID、shared root、节点、状态和日志路径；
- 达标后明确要求“不取消仍运行的作业”。

阶段验收应输出 `PASS_WITH_FOLLOWUPS`，并明确 follow-up 仅指尚未观察完整 terminal 结果，而不是当前已知失败。

### G9：独立 Checker 与文档同步

Checker 应从不变量反推遗漏，而不是重复主测试命令。至少检查：

- 权威/cache 关系；
- DB integrity 与 PRAGMA；
- version coherence；
- current-only checkpoint；
- fixed discovery surface；
- active row 上界；
- archive 唯一性或去重语义；
- terminal artifact；
- failure events；
- telemetry 阈值。

如果 plan 约束 Checker 输出只能是三值，stdout 只能打印：

- `PASS`
- `PASS_WITH_FOLLOWUPS`
- `BLOCKED`

详细数字写入单独 structured evidence，不要污染 Checker 的输出契约。对 live run 取样时要避免恰好落在 DB commit 与 cache 更新之间；应在稳定边界取样，或实现有限重试。

## 5. “通过”必须证明工作负载真的执行了

本轮出现过 PBS `Exit_status=0`、walltime 为零、输出文件为空，但 pytest 实际没有运行的情况。这说明调度器退出码不是充分证据。

每个 batch 检查点还应验证：

- 输出文件非空；
- 日志包含预期命令的完成标记，例如 `N passed`；
- walltime/cput 合理；
- run root 或目标 artifact 实际创建；
- DB/version/metrics 发生预期变化；
- job 没有只执行空 shell 或错误解析的 `bash -lc` 参数。

多行命令不要临时塞进 command-form `qsub -- ... bash -lc`。优先添加一个可静态检查、可重复使用的 PBS 脚本，先 `bash -n`，再提交字面脚本。

此外，PBS `job_state=R` 也不一定代表用户 workload 已开始。节点 provisioning/prologue 阶段可能表现为 `R`、substate 41、CPU/内存/输出仍为零。真正的启动门禁应至少要求以下一种证据：

- substate 进入执行态；
- CPU/walltime 开始增长；
- launcher 输出出现；
- run v0 已创建；
- heartbeat 或进程日志出现。

## 6. Agent 的推荐实施循环

### 6.1 开始前

1. 读取所有适用指令和计划全文；
2. 建立 requirement/checkpoint 清单；
3. 检查环境和工作树；
4. 识别不可逆动作、外部写入和昂贵作业；
5. 确认报告目录已经存在；
6. 记录初始未覆盖风险。

不要因为工作树已有未提交改动而整体回退或覆盖；只修改任务所需范围，并保留无法确认归属的用户改动。

### 6.2 每个工作单元

推荐按以下顺序执行：

```text
SPECIFY 不变量
→ 写正例/反例/rollback 检查
→ IMPLEMENT 最小完整状态转换
→ focused tests
→ 真实小型 pipeline
→ artifact/DB/log 人工复核
→ 记录 progress
→ 扩大规模
```

修改时优先解决权威边界和生命周期，不要先堆补偿性 cleanup。GC 不应掩盖仍为 pending 的错误状态；应先正确终态化，再让引用驱动 GC 删除对象。

### 6.3 失败后

严格遵守 [AGENTS.md](AGENTS.md)：先记录失败，再做针对性修改。失败记录应区分：

- 已观察事实；
- 尚未证实的假设；
- 已确认根因；
- 下一轮只准备改变什么；
- 用什么新检查证伪该修复。

不要把以下操作伪装成修复：

- 只延长 timeout；
- 只换随机种子；
- 只降低日志级别；
- 只忽略失败断言；
- 只清理现场后重跑；
- 在没有证据时把错误归因给共享文件系统。

同一实验三次失败后必须停止局部试错并升级全面审查。

### 6.4 提交集群作业前

- 再次运行 `bash -n scripts/miyabi/*.pbs`；
- 检查 literal group ID；
- 检查已删除参数和环境变量；
- 冻结并记录 config、run ID、shared root 和输出路径；
- 确认 cache/offline/W&B 策略；
- 确认节点数、GPU、walltime 和队列；
- 确认验收脚本能读取本次新增 telemetry；
- 确认失败日志路径不会覆盖 pass artifact。

作业启动后，不要修改该作业尚未 import 的运行时代码或配置。若必须改变，应该把该 run 标记为不再代表最终实现，并重新提交。文档和报告可以继续更新，但不要让 live job 在不同节点加载不同源码版本。

### 6.5 作业运行中

- 先判断 queue、prologue、execution、finish 的真实状态；
- 使用只读检查，不手工修改 DB/latest；
- 不用额外 reader 高频阻塞 rollback-journal writer；
- 在预定版本边界抓取 evidence；
- 区分 active payload、publication grace 对象和已终态对象；
- 不把合法的 in-flight 文件误报为 GC 泄漏；
- 除非 plan 或用户授权，不取消仍在运行的长作业。

### 6.6 完成时

- 重跑最终静态检查；
- 运行独立 Checker；
- 确认每个 requirement 都有证据；
- 确认报告引用的路径真实存在；
- 区分完整完成与阶段性交接；
- 记录仍运行的 job 和明确 follow-up；
- 不因 token/time 接近上限而把未完成目标标为完成。

## 7. 本轮失败带来的具体经验

### 7.1 普通 orphan grace 不能直接复用于输入闭合后的退出

运行中必须给“payload 已写、pointer 尚未替换”的对象保留 grace，否则 GC 可能删除合法 publication。但全部预期 learner 已明确 stopped 后，不会再有合法 publication 在途，此时继续使用普通 grace 会让正常结束目录残留无引用 tensor。

指导：plan 应把运行态 grace 和已证明 input-closed 的终态 grace 分开，并要求真实退出目录检查。

### 7.2 到达目标后仍要处理 shutdown 期间摄取的 proposal

syncer 达到目标并发布 stop 后，learner 可能在看到 stop 前再发布 proposal。最后一次 ingestion 会把它们放入 pending；如果不再 merge，也不终态化，引用驱动 GC 会正确地保留它们，造成最终状态泄漏。

指导：正常 shutdown 应在全部 learner stopped 和最终 ingestion 后，把不再消费的 pending/selected 明确终态化，再 archive/GC。

### 7.3 调度器成功不等于测试成功

错误的 command-form qsub 参数曾产生 `Exit_status=0`、空日志、零 walltime。若只看退出码，会把“什么都没执行”误记为通过。

指导：batch gate 必须验证工作负载特有的输出、状态变化和非空 artifact；复杂命令使用版本化 PBS 脚本。

### 7.4 逻辑有界和物理有界要分别验证

只检查 active row 数不能证明 SQLite 或目录的物理工作集稳定。删除行后 page reuse、freelist、archive 增长和目录扫描成本都需要独立观察。

指导：长循环检查应同时记录逻辑行数、used pages、文件数、live bytes 和 discovery 工作量。

### 7.5 Run 分析结果属于 reports，不属于系统 docs

系统文档应描述稳定接口、协议和操作方法；具体 job ID、run ID、时延数字和阶段结论会随实验变化，应集中写入 `reports/`。docs 只保留到报告的导航链接。

指导：plan 的文档同步应区分：

- 稳定设计/操作语义 → `docs/`；
- 实验方法、run 分析和结果 → `reports/`；
- 未来研究方向 → `plans/RESEARCH_PLAN.md`；
- 实施过程、失败和证据索引 → `reports/imp_plans/<plan-id>/`。

## 8. 推荐的 Plan 骨架

后续 implementation plan 建议至少包含以下章节：

1. **目标与完成定义**：用户可观察结果、最终状态、阶段性交接边界；
2. **适用指令与 skill**：需要读取的 scoped 指令、运行环境约束；
3. **权威关系与故障模型**：提交点、writer、cache、fail-closed/回退语义；
4. **范围与非目标**：模式矩阵、兼容性、迁移策略；
5. **状态机和文件生命周期**：初始化、运行、终止、恢复、归档、GC；
6. **接口与配置变更**：新增/删除字段、CLI、目录、schema、telemetry；
7. **Loop/工作单元**：每个 loop 的 SPECIFY、IMPLEMENT、HARDEN、PERSIST；
8. **测试矩阵**：稳定 ID、正例、反例、rollback、通过条件；
9. **验证阶梯**：login → 1-node → 2-node → 正式规模 → 长作业；
10. **性能与可靠性阈值**：字段、统计方法、门槛；
11. **报告与 artifact**：路径、命名、原始日志、structured evidence；
12. **Checker 契约**：输入、输出、completed/staged 模式；
13. **停止与升级规则**：失败三次、外部阻塞、何时需要用户授权；
14. **文档同步位置**：docs、reports、research plan 各自写什么。

## 9. Plan 发布前自检

### 正确性

- [ ] 是否只有一个明确权威状态？
- [ ] 是否定义了提交前、提交中、提交后的可见性？
- [ ] 是否覆盖初始化、正常停止、异常停止和 resume？
- [ ] 是否定义迟到输入和低于 quorum 的尾部行为？
- [ ] 是否定义每种 artifact 的 live set 和删除条件？
- [ ] 是否包含 fail-closed 反例？

### 可验证性

- [ ] 每个核心需求是否有稳定测试 ID？
- [ ] 通过条件是否是具体数值或具体状态集合？
- [ ] 是否同时检查 DB、cache、文件、archive 和日志？
- [ ] 是否包含 transaction rollback 和 crash matrix？
- [ ] 是否在大作业前实现了所需 telemetry？
- [ ] 是否定义独立 Checker？

### 集群执行

- [ ] 是否区分登录节点与计算节点允许的命令？
- [ ] 是否要求 PBS 静态检查和真实 group ID？
- [ ] 是否定义 job 真正启动的证据，而不只看 `R`？
- [ ] 是否记录 run ID、job ID、shared root 和日志路径？
- [ ] 长作业是否有明确阶段门禁和“不取消”规则？

### 证据与交接

- [ ] 失败是否要求先记录再修改？
- [ ] pass artifact 是否证明实际 workload 执行？
- [ ] live log 与阶段快照是否分开？
- [ ] docs 与 reports 是否分工明确？
- [ ] `PASS_WITH_FOLLOWUPS` 是否列出唯一、具体的 follow-up？
- [ ] 完成声明是否与实际覆盖范围一致？
