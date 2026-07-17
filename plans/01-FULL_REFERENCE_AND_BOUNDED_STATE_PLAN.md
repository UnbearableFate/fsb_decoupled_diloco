# Full Reference 与有界状态研究计划

## 1. 文档定位

本文详细展开项目近期的两条研究主线：建立可信的 full 模式参考路径，以及把有界状态从配置意图发展为协议结构。文档结合当前代码和已有实验现象给出研究方向、问题分解与实验组织方式，供后续设计讨论和人工审阅使用。

本文不是实现规格或验收清单，也不预设固定阈值、完成期限和唯一技术方案。各方向可以根据实验信号调整先后次序。这里所说的 full，是 learner 每个同步周期上传完整参数快照、syncer 聚合后发布完整全局模型的路径；fragment，则是每次只交换一个参数分片的路径。

## 2. 为什么先研究这两条主线

当前项目已经具备一条能够端到端运行的 full 路径，也有 payload-first、metadata-last、原子替换、SQLite 状态记录、外层优化器、停止通知、DB dump 和初步 resume 等基础。相比 fragment，full 只有一个全局版本和一组模型/外层优化器状态，更容易解释一次 outer update 的输入、提交点、失败恢复和最终结果。因此，full 适合作为协议语义、故障恢复和性能测量的参考实现。

近期长运行也暴露了一个有代表性的问题：learner 已经完成有限本地训练时，syncer 仍可能因为距离目标 outer step 只差一次、但不再有足够 proposal 而持续等待 quorum。当前 terminal drain 已能在部分情况下选择剩余 proposal，但“本地训练预算耗尽”“目标 outer step 未达到”“没有可消费 proposal”三种状态之间还没有形成完整、可解释的收尾语义。这个问题与 resume、独立退出和最终结果可信度直接相关，优先级高于继续扩展更多通信模式。

另一方面，当前运行目录会保存版本化模型、optimizer、learner update、metadata、SQLite 行和 DB dump。full 已有部分 retention，fragment 的历史权重、optimizer、applied/dropped update 及 metadata 仍会持续累积；syncer 还会通过 glob 扫描不断增长的 pending metadata 目录。只在运行结束后删除旧文件可以降低最终磁盘占用，却不能保证运行中的 discovery、查询和恢复成本不随历史增长。因而，“有界状态”不仅是空间优化，也是长期运行、恢复语义和可重复性能研究的共同基础。

## 3. 主线一：建立可信的 full reference

### 3.1 研究目标

full reference 希望提供一条语义简单、行为可解释、适合对照的存储通信路径。它可以回答四类基础问题：

- 一个 outer update 从 learner proposal 到新全局版本的权威提交点在哪里；
- 正常结束、learner 提前结束和 quorum 不再可达时，系统分别如何收尾；
- syncer 或 learner 在不同阶段退出后，哪些工作可以继续，哪些工作会被丢弃；
- 重启后如何从共享存储恢复，并避免重复消费、跳过已提交版本或组合不匹配的模型与 optimizer。

这条路径的价值不在于证明 full 永远优于 fragment，而在于给 fragment、stale、异步上传和独立作业拓扑提供一个稳定参照。未来任何复杂协议都可以与 full 使用相同的训练栈、模型、数据、outer optimizer 和观测口径比较，把协议收益与训练实现差异分开。

### 3.2 当前基础与主要缺口

当前 full 发布流程大致是：syncer 写入新模型 payload，写入对应 outer optimizer payload，在 SQLite 中登记 global version，最后原子替换 `latest.json`。聚合完成后，相关 learner update 再被标记为 applied，旧 proposal 被标记为 superseded 或 stale。正常退出时，syncer 写入 `stop.json`，等待 learner heartbeat 进入 stopped 状态，随后生成 summary 和 DB dump。

这条路径已经形成了清楚的基本轮廓，但其中仍有几个值得专门研究的窗口：

1. 模型或 optimizer 已写入、`latest.json` 尚未切换时，可能留下不再被引用的 payload；
2. SQLite 已登记新版本、`latest.json` 仍指向旧版本时，数据库和共享权威头可能短暂分歧；
3. `latest.json` 已指向新版本、selected update 尚未标记 applied 时，重启逻辑需要判断这些 update 是否已经产生过影响；
4. DB dump 可能落后于 `latest.json`，而节点本地 SQLite 是否仍存在又取决于部署和重启位置；
5. learner 到达 `max_local_steps` 后会自行退出，但 syncer 的 outer-step 目标可能仍未完成，现有逻辑可能进入无新 proposal 的 quorum wait；
6. 当前 full resume 可以恢复模型、outer state 和版本，但对 selected/pending/applied 的重建主要依赖现有 SQLite 或 dump，publication 中断后的自动对账还不完整。

这些缺口可以统一看作一个问题：模型权威状态、proposal 消费状态和进程生命周期目前分别记录，恢复时缺少一个能够把三者重新对齐的清晰边界。

### 3.3 建立统一的权威状态视图

full reference 可以逐渐围绕“不可变 payload + 小型 current record”组织。模型参数和 outer optimizer 继续作为不可变的大文件发布；一个小型 current record 表示当前权威版本，并同时描述：

- run identity、格式版本和参数布局 identity；
- 当前 global version 与累计 token 语义；
- 模型 payload 和 outer optimizer payload 的路径、大小与内容 identity；
- 产生该版本的 proposal 集合或等价的 consumed frontier；
- 与该提交相关的时间、发布序列和必要配置摘要。

这里的重点不是给 `latest.json` 不断增加字段，而是让恢复过程能够只依赖一个明确的 authority 来判断“哪个模型版本已经整体提交”。SQLite 可以继续承担高效查询、liveness、选择过程和研究记录，但它更适合作为可重建的运行索引，而不是与 current record 竞争最终权威。

模型和 optimizer 作为同一版本对待也很重要。二者可以分别存储，但 current record 只在两份 payload 都准备好后切换。这样，读取者不会从两个彼此独立的“最新文件”推断一个可能不匹配的组合。内容 identity 还可以用于识别路径复用、意外截断和错误 run 目录带来的混淆。

### 3.4 正常收尾与 terminal drain

有限训练包含两个不同的进度预算：learner 的本地 step/token 预算，以及 syncer 的 outer update/token 预算。二者不会天然同时结束。full reference 可以把收尾过程拆成几个可观察阶段：

1. **运行阶段**：learner 继续训练和发布，syncer 按正常 quorum/grace 规则产生全局版本；
2. **输入关闭阶段**：learner 达到本地预算后不再产生新 proposal，但会发布最终 heartbeat，并允许 syncer 识别“输入集合已经封闭”；
3. **terminal drain 阶段**：syncer 处理仍然可用的 proposal，是否允许低于正常 quorum 的最后一次 merge可以作为显式实验策略记录；
4. **不可继续阶段**：输入已经封闭、没有足够 proposal、outer 目标仍未达到，此时可以形成“有限输入耗尽”的正常研究结果，而不是无限等待；
5. **停止与汇总阶段**：权威版本固定后发布停止信息，learner 确认退出，syncer 再汇总最终状态和未消费 proposal。

这样组织后，“达到计划的 outer step”“输入耗尽后提前结束”“liveness timeout”“人为停止”和“进程错误”可以成为不同的 stop reason。研究结果因此能够解释最终版本为什么停在某处，也能区分协议失败与预算配置不匹配。

terminal drain 的策略本身可以保留为研究变量。严格语义可以不降低 quorum，接受更少 final updates 的语义则可以减少尾部浪费。无论选择哪一种，时间账本都适合单独记录 terminal wait、drained proposal 数、最终未消费数和 learner shutdown wait，避免把尾部时间混入稳定阶段的 update interval。

### 3.5 Syncer resume 与 publication reconciliation

syncer 重启可以按“先恢复权威模型，再重建运行索引”的思路展开。一个自然的恢复流程包括：

1. 读取 current record，验证 run、参数布局、模型和 optimizer 彼此匹配；
2. 将 current record 中的 global version 作为已提交边界；
3. 读取现有 SQLite 或最近 DB dump，把它视为恢复加速材料；
4. 对照 current record 修正数据库中落后、领先或停留在 selected 的行；
5. 从固定 proposal 可见面重新发现尚有资格参与未来版本的 proposal；
6. 把不再被 authority、恢复窗口或 proposal 引用的文件交给后续 GC；
7. 继续等待下一次正常聚合，或者进入已经明确的 terminal 状态。

对于一次 publication 中断，恢复结果通常可以归入两类：current record 尚未切换，则新 payload 只是 orphan candidate；current record 已切换，则该版本已经生效，即使 SQLite 的 applied 标记尚未完成，也可以根据提交记录补齐。这样的解释比重放全部历史 update 更简单，也更符合共享存储作为权威数据面的目标。

DB dump 仍然有价值，但角色会更清晰：它缩短重建时间、保存丰富的研究状态，却不单独决定当前模型版本。后续可以研究 dump 的版本命名、轮转和恢复耗时，以及“本地 SQLite 完整”“只有较旧 dump”“没有可用 dump”三种情况下的差异。

### 3.6 Learner 重启与独立生命周期

full learner 重启后可以重新加载 current 模型，并选择重新初始化 inner optimizer。研究重点是让 proposal identity 保持单调且不与旧进程混淆，同时把重启前后已经处理的 token、最后加载的 global version 和新 proposal 的 base 记录清楚。

进程 identity 可以由稳定 learner identity 与每次启动的 incarnation identity 共同表示。稳定 identity 便于 quorum 和资源归因；incarnation identity 便于区分同一 learner 的旧 proposal、迟到 heartbeat 和新进程输出。proposal 本身可以继续使用唯一 ID，同时保留单调 sequence 或等价排序信息，以便 latest-wins 和恢复对账。

当前 co-allocated MPI 作业适合快速、可重复的系统实验，但它把进程生死绑定在同一个 allocation 中。full reference 稳定后，可以逐步增加独立 PBS 作业场景：syncer 先启动或后启动、单 learner 延迟加入、单 learner 重启、syncer 在 learner 继续训练时重启。研究重点是共享存储协议是否足以承载这些生命周期差异，而不是立即替换现有 launcher。

### 3.7 Fresh-only 参考语义

现有实现可以按版本差和 token 数给 proposal 加权，但当前绝对参数平均还没有覆盖旧设计中完整的 base-relative stale displacement 语义。为了使 full reference 更容易解释，近期正式系统对照可以以 fresh-only 为主：proposal 的 base 与当前 global version 对齐，normal quorum 中不混入不同 base 的参数快照。

这不会取消 stale 研究。相反，它提供了一个数值和系统参考点：以后引入 stale proposal 时，可以明确看到 proposal 选择、参数重建、accepted-token efficiency 和训练质量分别改变了什么。短期故障恢复也会因此减少“这是恢复错误还是 stale 语义差异”的歧义。

### 3.8 观测与实验组织

full reference 的实验可以分成三层，每层复用相同的事件和状态词汇。

第一层是短运行状态机实验，覆盖初始化、正常 merge、输入关闭、terminal drain、停止和 resume。它们适合小模型与少量 update，主要观察版本、proposal status、current record 和 stop reason。

第二层是 publication 故障注入。注入点可以分布在模型写入后、optimizer 写入后、SQLite 登记后、current record 切换后、update 标记 applied 前后以及 DB dump 前后。报告重点是恢复后选定的权威版本、orphan 数、对账动作、重复消费情况和恢复时间，而不是只记录进程是否重新启动。

第三层是较长的 full 运行，用于观察稳定阶段 goodput、update interval、目录规模、DB 规模和收尾时间。近期已经出现的 5000-step 尾部等待现象可以作为第一批案例，先形成完整事件时间线，再用修订后的停止语义重复对照。

建议持续保留以下观测面：

- learner 训练、snapshot、写入、poll、adoption 和退出时间；
- syncer discovery、quorum/grace、读取、聚合、outer step、发布和 shutdown wait；
- current version、pending/selected/applied/dropped proposal 数；
- stop reason、输入是否封闭、剩余 outer 目标和 terminal drain 决策；
- resume source、恢复版本、数据库对账数量、orphan 数和恢复到再次前进的时间。

### 3.9 预期研究产出

这条主线最终可以形成一份简洁的 full 协议图、一张正常结束状态图、一组 publication crash window 图，以及 full 模式的长期时间账本。它们共同回答“共享存储上什么时刻算一次 update 已提交”“进程独立失败后如何继续”和“有限训练为什么在某一版本结束”。这些产出也会成为 fragment resume 和独立作业实验的直接参照。

## 4. 主线二：实现真正的有界状态

### 4.1 有界状态的研究含义

有界状态不等于给运行目录设置一个总容量，也不等于训练结束后集中清理。它关注的是：在 learner 数、fragment 数、staleness window 和恢复窗口固定时，运行协议为继续前进所需的活跃对象数量、可发现 proposal 数、权威 payload 数和数据库活跃行数不会随历史 update 总数一起增长。

历史 metrics、日志和研究归档可以增长，因为它们不参与每一轮 discovery、选择、发布或恢复。关键是把“运行时需要反复读取的 live state”和“只供离线分析的 history”分开。由此可以同时讨论三个边界：

- **空间边界**：共享存储中被运行协议保留的模型、optimizer、proposal 和临时对象规模；
- **发现边界**：一次 poll/scan 需要检查的目录项、metadata 和数据库活跃行；
- **单次操作边界**：第 N 次 update 的发现、选择、提交和恢复成本不因为 N 本身增加。

### 4.2 当前实现中的增长来源

当前路径已经有一些天然有界的对象，例如单个 `latest.json`、单个 `stop.json`、每 learner 一个 heartbeat，以及配置和参数索引。full 模式也能按版本清理全局模型与 outer optimizer，并在 learner 侧按最近 update 数做 retention。

仍需要重点研究的增长来源包括：

- syncer 每轮 glob `updates/pending/learner_*/update_*.meta.json`，目录中的历史 metadata 越多，扫描面越大；
- SQLite 的 updates、fragment_updates、global_versions 和 fragment_versions 以追加历史为主，即使状态已变为 applied 或 dropped，行仍留在主运行库；
- fragment learner 为避免误删仍可能被消费的分片，没有启用基于 local step 的简单 retention；
- fragment 权重和 per-fragment outer optimizer 按版本保存，但没有形成与 current/base window 配套的回收策略；
- applied、dropped、missing 和 superseded proposal 的 tensor/meta 主要改变数据库状态，物理 artifact 不一定随之迁移或删除；
- DB dump、materialized full checkpoint、日志和 metrics 也会累计，虽然其中部分不直接影响 discovery；
- 写入中断可能留下临时文件或只有 tensor、没有有效 metadata 的孤儿对象。

这些来源的风险不同。历史日志增长主要影响归档空间；pending 目录和活跃 SQLite 表增长会直接影响稳态延迟；错误删除仍被 current、base 或 selected proposal 引用的 payload，则会破坏恢复和正确性。因此，有界化适合从状态分类和引用关系开始，而不是从统一的“删除旧文件”开始。

### 4.3 建议的状态分类

运行目录可以按用途理解为六类状态：

| 状态类别 | 作用 | 生命周期方向 |
| --- | --- | --- |
| Current authority | 当前模型、outer state、版本和 consumed frontier | 被新 current 原子替换，旧 current 进入恢复窗口或回收候选 |
| Base window | stale 或恢复仍可能引用的旧模型状态 | 随 current 前进滑动 |
| Proposal surface | 每 learner 或每 learner/fragment 当前可见 proposal | latest-wins 覆盖旧引用，payload 在安全后回收 |
| In-flight objects | 正在写入的临时 payload、临时 metadata 和未完成 publication | 提交后转为 live，超时且无引用时成为孤儿候选 |
| Active index | quorum、liveness、选择和恢复当前需要的 SQLite 状态 | 保留活跃集和短恢复窗口 |
| Research history | metrics、事件、已完成 update 记录、DB 历史快照 | 轮转、压缩或移入离线归档，不参与运行 discovery |

这套分类可以同时用于 full 和 fragment。两者的主要差别是 authority 和 base window 的粒度：full 使用一个 global version；fragment 使用 per-fragment version vector，并可能额外保留一个较低频的 materialized full checkpoint。

### 4.4 固定大小的 proposal 可见面

当前每次生成 proposal 都创建新的 tensor 和 metadata 文件，syncer 再扫描所有 metadata。更适合长期运行的方向是给每个生产者建立固定 discovery slot：

- full 模式可以为每个 learner 提供一个 latest proposal reference；
- fragment 模式可以为每个 learner/fragment pair 提供一个 latest proposal reference；
- proposal payload 仍可以使用不可变唯一文件，reference 在 payload 完整写入后原子切换；
- syncer 只读取固定数量的 reference，再通过 proposal ID 去重并查询 active index。

这样，full 的发现面主要随 learner 数变化，fragment 的发现面主要随 learner 数和 fragment 数变化，而不随历史 update 数变化。latest-wins 也会从 SQLite 中的事后 superseded 标记，前移为协议的可见面结构。

不可变 payload 与固定 reference 之间仍有一个生命周期问题：learner 切换到新 reference 后，syncer 可能正在读取旧 proposal。可以研究几种实现方向，例如短期保留被替换 payload、由 syncer 记录 selected lease、使用少量轮换 slot，或通过 reference snapshot 和延迟 GC 保护读者。这里不需要过早确定唯一方案，重点是让“谁仍可能引用某个 payload”能够被计算和观测。

### 4.5 权威模型和 outer state 的成对 retention

full 模式的模型与 outer optimizer 以相同 version 命名，但当前清理主要根据目录中出现的版本统一保留最近若干项。更完整的 retention 可以从 current record 的引用集合出发：当前版本始终属于 live state；恢复窗口和 stale base window 中的版本属于 pinned state；其余完整版本才进入回收候选。

fragment 模式需要对每个 fragment 单独计算引用集合。每片的 current parameters、current outer state、允许的 base window，以及正在被 proposal 或恢复流程引用的版本共同构成 pinned set。这样可以避免用 global merge event 粗略删除某个仍较旧但尚属该 fragment 当前状态的文件。

materialized full checkpoint 可以被视为另一种稀疏快照。它方便 learner 首次加载、评估和人工检查，但不一定参与每个 fragment 的权威提交。只要 `latest.json` 仍引用某一 materialized checkpoint，它就属于 pinned 对象；产生更新的 materialized checkpoint 后，旧快照可以按独立的恢复/评估窗口轮转。将它与 per-fragment authority 分开，有助于避免为了保留一次全模型快照而无限保留所有历史 fragment 版本。

### 4.6 Proposal artifact 的状态迁移与回收

proposal 在逻辑上会经历 pending、selected、applied、dropped 或 superseded 等状态，但文件系统 artifact 当前未完整跟随这些状态迁移。后续可以把生命周期设计为两层：

1. metadata/reference 决定 proposal 是否仍处于运行可见面；
2. tensor 和详细 metadata 在不再被 current commit、selected transaction、恢复窗口或可见 reference 引用后进入回收队列。

applied 与 dropped 记录对研究分析很有价值，但不一定需要让完整 tensor 永久在线。较小的结构化摘要可以归档 proposal ID、learner、base、token、选择结果、drop reason、effective weight 和时间；大 tensor 通常只在短期故障分析或可重复聚合需要时保留。

回收动作适合保持幂等：重复执行得到相同 live set；遇到已删除文件只记录事实；遇到仍被引用对象则跳过。删除前先根据 authority、proposal reference、active selection 和恢复窗口生成 live-reference snapshot，也比单纯按文件 mtime 清理更容易解释。若希望保留人工恢复空间，可以先把 artifact 移入隔离区，再由较低频任务最终删除。

### 4.7 SQLite 活跃状态与研究历史分离

SQLite 目前同时承担运行状态机和长期实验记录。随着 update 数增长，按 status 查询虽然有索引，数据库文件、dump 时间和恢复扫描仍可能增长。一个自然方向是把数据库分为逻辑上的 active set 与 archive：

- active set 保存当前 learner/liveness、current 或短窗口 versions、pending/selected proposal、近期 applied/dropped 摘要和 GC 引用；
- archive 保存完整 update 历史、资源指标、drop reason、版本统计和用于论文分析的事件；
- 已离开恢复窗口的终态行可以批量写入按时间或版本轮转的归档，再从 active 表压缩；
- 在线查询围绕 status、learner、fragment 和 base version 维持固定工作集，离线分析读取归档而不影响 syncer。

归档形式可以是只读 SQLite 分片、CSV/JSONL、Parquet 或现有 metrics/event 文件的增强版本。选择重点在于可追溯性和低运行干扰，而不是统一所有数据格式。DB dump 也可以区分“用于快速恢复的近期 active dump”和“用于研究保存的历史快照”，分别采用不同轮转节奏。

### 4.8 临时文件、孤儿和 GC 的崩溃语义

payload-first、metadata-last 已经让 reader 不会仅因看到未完成 tensor 就把 proposal 当作 committed。剩余问题是写进程崩溃后如何识别和清理这些对象。临时对象可以携带创建时间、writer/incarnation identity 和目标对象信息；GC 在确认没有 live reference、writer lease 已过期并且对象不属于正在恢复的 publication 后，把它列为 orphan candidate。

syncer publication 的孤儿与 learner proposal 的孤儿可以使用同一思路处理，但保护集合不同：前者由 current/base/recovery window 决定，后者由 proposal reference/selected state 决定。GC 自身如果中断，下一轮可以重新计算，而不需要把 GC 进度变成新的复杂权威。

对共享文件系统而言，大量小文件删除和目录重命名也有元数据成本。GC 可以批量、低频执行，并记录扫描、候选计算、删除和失败重试的时间。这样既能控制 live state，也能研究清理工作是否反过来影响训练 I/O。

### 4.9 Full 与 fragment 的推进顺序

有界状态可以先在 full 路径上形成完整闭环。full 只有一组 authority，固定 proposal surface 也只需要每 learner 一个 slot，更适合验证 reference 切换、payload pinning、DB active/archive 分离和 GC 幂等性。

之后再推广到 fragment：

1. 把 proposal surface 扩展为 learner/fragment 维度；
2. 把单一 current version 扩展为 per-fragment current vector；
3. 为每片维护独立 base/recovery window 和参数/optimizer pinned set；
4. 让 materialized full checkpoint 作为被显式引用的稀疏快照管理；
5. 将 fragment resume 建立在同一套 reference reconciliation 与 GC 规则上。

这种顺序能够让 fragment 主要增加“多 authority 分片”的复杂度，而不同时重新发明 proposal 生命周期、DB 归档和故障回收。

### 4.10 有界性实验与观测

有界状态研究适合把“随 update 序号的变化”作为主要横轴，而不是只比较运行结束后的目录大小。可持续记录：

- live protocol bytes、archive bytes 和总 bytes；
- current/base/proposal/in-flight/orphan 各类文件数；
- pending、selected、近期终态和归档 SQLite 行数；
- proposal discovery 扫描项数、读取 metadata 数和 discovery latency；
- SQLite page count、活跃库大小、dump 时间和 restore 时间；
- GC backlog、每轮候选数、删除数、失败数和 GC I/O 时间；
- 第 N 次 update 的 read、selection、publish 和完整 interval；
- fragment 模式中每片版本跨度、pinned version 数和 materialized checkpoint 数。

实验可以从合成的小 payload、多 update 状态机运行开始，快速暴露目录项和 DB 行的增长；再进入真实模型中长跑，观察 GC、checkpoint 写入与训练 I/O 的相互影响。还可以人为暂停 GC 或减慢存储，研究 backlog 如何形成和恢复，并区分暂时积压与结构性无界增长。

有界性结果适合用时间序列和状态构成图表达。若 live state 保持平稳而 archive 持续增长，说明协议运行面与研究历史已经分离；若 discovery latency 随 archive 增长仍保持稳定，则能进一步说明离线证据保存没有进入热路径。

### 4.11 设计取舍

固定 proposal slot 会强化 latest-wins，因此可能更快丢弃 learner 连续产生但尚未被 syncer 观察的中间 update。这是存储边界与 accepted-token efficiency 之间的取舍，适合显式报告 replacement rate，而不是把覆盖视为纯工程细节。

更长的 base/recovery window 提高 stale 和人工回滚空间，也增加权威 payload 与 optimizer 状态。窗口长度可以作为研究参数，与恢复需求和 stale 策略共同讨论。

更频繁的 GC 降低 live bytes，却增加 metadata 操作和与训练 I/O 竞争的机会；更低频的 GC 会形成暂时 backlog。研究目标可以是找到可解释的稳态关系，而不是追求每时每刻零孤儿。

active/archive 分离会让在线恢复更轻，但离线复现实验可能需要组合多个归档文件。可以通过稳定 proposal ID、版本 identity 和归档 manifest 保留可追溯性。

## 5. 两条主线的衔接

full reference 和有界状态并不是两个独立工程包。current record 既定义 full update 的提交点，也是 retention 计算 pinned set 的根；proposal consumed frontier 既解决 resume 对账，也决定哪些 proposal tensor 可以回收；terminal drain 固定最终 authority 后，GC 才能解释尾部未消费 artifact；DB active/archive 分离则让长期 full 运行和故障恢复成本都不再依赖全部历史。

近期工作可以按下列研究节奏自然推进：

1. 先用现有长运行日志还原 full 尾部等待的状态时间线，明确输入关闭、outer 目标和 pending proposal 的关系；
2. 描述 full current record 与 publication reconciliation，使模型、outer state 和 consumed proposal 共享一个提交边界；
3. 将停止原因、terminal drain 决策和 resume 对账纳入统一事件记录；
4. 以 full 为起点引入固定 proposal reference 和引用驱动的模型/proposal retention；
5. 分离 SQLite active state 与研究 archive，并补充 live bytes、scan surface 和 GC 指标；
6. 通过短故障注入和中长运行共同观察恢复正确性、收尾时间和有界性；
7. 当 full 路径的状态词汇和生命周期稳定后，把同一框架推广到 fragment version vector 与 fragment resume。

这一顺序不会阻止 fragment 性能分析继续进行。现有 fragment timing 可以继续解释 quorum wait 和 learner wait-latest，但较大规模的 fragment 协调重构可以建立在更清楚的停止、恢复和 artifact 生命周期之上，从而减少同时改变多个协议层带来的归因困难。

## 6. 面向审阅的阶段性成果

为了让研究过程易于讨论，两条主线可以逐步形成以下可独立审阅的材料：

- full 正常运行、terminal drain、停止和 resume 的状态图；
- publication 各阶段崩溃后的 authority/SQLite/artifact 对账表；
- current、base、proposal、in-flight、active index 和 archive 的状态生命周期图；
- full 与 fragment 在固定 M/F/窗口下的 live-state 构成与 discovery 复杂度说明；
- 长运行中 live bytes、文件数、DB active size、GC backlog 和 update latency 的联合曲线；
- co-allocated 与独立进程生命周期下的恢复事件时间线；
- 从 full 单一版本推广到 fragment version vector 的设计差异说明。

这些材料的共同目标，是把项目从“能够通过共享存储完成训练”推进到“能够解释长期运行中什么是当前状态、为什么能够恢复、为什么成本不会随历史自然增长”。在此基础上，fragment、stale 和更复杂的异步 pipeline 才能获得稳定、可比较的研究底座。
