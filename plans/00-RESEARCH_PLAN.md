# FS-Based Decoupled DiLoCo 后续研究计划

版本：2026-07-16 研究方向稿

## 1. 文档定位

本文给出 FS-Based Decoupled DiLoCo 项目下一阶段的研究方向。它综合了早期研究设想、已有仿真与文件系统实验记录，以及当前代码库和近期 Miyabi 实验所反映出的真实状态。

本文不是实现规格，也不是阶段验收表。文中的顺序表示研究上的自然推进关系，而不是强制门禁；具体工作可以根据实验信号、算力与工程复杂度交叉进行。计划的目的，是让研究者能够在一份文档中理解：项目试图回答什么问题、当前已经站在哪里、接下来哪些方向最值得投入，以及这些方向最终如何汇聚为论文贡献。

## 2. 研究愿景

本项目关注一种特定但现实的分布式训练环境：多个训练作业可以访问同一共享存储，却不适合依赖长驻参数服务器、RPC 服务或跨作业集合通信。HPC 批调度环境是主要场景，跨区域对象存储可以作为协议一般化的延伸场景。

核心研究判断是：DiLoCo 一类低频同步算法扩大了系统对通信延迟的容忍度，因此共享存储不必只承担 checkpoint 角色，也可能直接承担训练数据面。一个有研究价值的系统不只是“把参数写进文件”，而应展示以下整体性质：

- learner 可以独立训练，通过共享存储发布参数更新；
- update 和全局状态具有清晰、原子的可见性边界；
- 长时间运行时，存储量、发现成本和单次处理成本保持有界；
- syncer 或 learner 退出后，可以依靠存储中的权威状态继续运行；
- fragment 能够减少单次通信量，同时避免引入更大的协调与等待成本；
- 系统代价、适用边界和失败边界能够通过可重复实验被解释。

项目的主要创新空间在存储通信协议、状态管理、恢复语义和性能边界，而不在重新发明 DiLoCo 的基本优化算法。stale-aware 聚合仍有研究价值，但更适合作为机制研究和消融副线，而不是当前系统主线的唯一支点。

## 3. 已有研究信号

早期研究记录给出了几项重要背景信号。Miyabi Lustre 上曾观测到约 7.95 ms 的跨节点可见性 p99，十万次原子替换测试没有发现完整性违例，元数据处理能力显著高于原型所需的轮询速率，并发 fragment 写入时间也远小于当时估计的同步周期。这些结果说明，共享文件系统的基本可见性和原子发布能力不是当前最明显的障碍。

早期 stale 仿真则给出了相反方向的提醒：预注册配置下，`S_max=1` 的预测收益接近零，stale 接受率约为 5.31%，accepted-token 效率约为 33.48%。这表明“存在大量未吸收计算”与“允许一个版本的 stale 就能回收这些计算”并不是同一个命题。stale 研究值得保留，但更需要解释丢弃来自 latest-wins 覆盖、quorum 选择、base 过期还是调度节奏，而不是直接扩大多种子训练矩阵。

当前代码和近期实验又增加了几项更直接的证据：

- full 和 fragment 两条端到端路径都已经可以在 1、2、9 节点上运行；
- 参数通过 safetensors 发布，full learner 以固定 proposal pointer 作为提交点，`latest.json` 作为 learner 可见的全局缓存，syncer 使用共享 run 内的持久 SQLite 作为权威提交记录；
- 外层优化器以显式扁平向量实现，支持 full state 和 per-fragment state；
- fragment 已具备版本、round-robin 调度、增量采用和完整模型 materialization；
- learner 可以直接从目标参数切片构造 fragment，不再为上传单片而物化完整 CPU 扁平向量；
- BF16 upload 将 full 和 fragment 的文件 payload 基本减半，syncer 读取后仍以 FP32 聚合；
- full 模式已经实现 DB-first resume、单事务 global commit、crash-matrix 恢复与引用驱动的 current-only retention；fragment resume 仍未实现；
- 当前 fragment 采用 `balanced_tensor`，与旧设计中的 layer-aligned 连续分片不同；两者都可以保留为后续研究选项，不必立即把其中一种定义为唯一正确方案；
- 当前 9 节点部署由一个 co-allocated MPI 作业启动，数据面不使用 MPI，但它还没有完整复现“多个独立 PBS 作业只能通过共享文件系统协作”的动机场景。

近期 50×10 对照是当前最有价值的性能信号。BF16 fragment 的平均 update payload 约为 63.2 MB，full 约为 248.9 MB；fragment 的 learner 写入和 syncer 读取都明显快于 full。然而 fragment 完整时间约为 198.9 秒，full 约为 150.4 秒，fragment 仍慢约 32%。新增的一节点 timing breakdown 显示，fragment cycle 的主要时间落在等待 latest 和 syncer 等待 quorum，而不是参数抽取、写文件、聚合或外层优化。这意味着后续 fragment 优化的中心应从“继续压缩文件写入”转向“减少协议等待和同步化行为”。持久状态改造后的九节点 full 50×10 又验证了 DB/current-only 路径：十次 merge 均 selected=8，SQLite commit p95 为 6.2 ms，commit+maintenance 仅占完整训练时间约 0.36%，结束时只剩 v10 checkpoint、固定 pointers 与持久 DB。

较长的 full 训练已经能让八个 learner 各自运行到 5000 local steps，并完成数十次 outer update；旧路径曾暴露收尾阶段停在目标版本之前、syncer 持续等待而 learner 已全部退出的现象。持久 full 的两次九节点终态运行都已证明 terminal drain、DB/cache 一致性和 current-only GC 正常：FP32 fresh-only 运行应用 190/400 updates 后在 v25 `input_exhausted`，BF16/staleness=2 运行应用 372/400 updates 后在 v48 `input_exhausted`。后者把 proposal payload 减半并将 update 利用率提高到 93%，但端到端时间仅改善约 0.6%，且两次运行都没有达到配置的 v50。这说明“固定 5000 local steps”和“固定 50 outer merges”不是等价完成条件；outer version 还受 supersession、quorum batching 和 learner 尾部速度差异影响。

## 4. 当前研究问题

### 4.1 存储能否成为训练数据面

研究重点不再是证明“文件可以交换参数”，而是量化一个存储数据面相对本地无通信、网络聚合和天真 checkpoint 交换的真实代价。需要理解的包括 learner goodput、参数发布暂停、syncer duty cycle、共享文件系统流量、metadata 压力，以及这些量如何随模型大小、learner 数量和同步周期变化。

### 4.2 权威状态和运行成本能否保持有界

真正的有界存储意味着 update 数持续增长时，权威 payload、proposal 可见面、数据库活跃状态和单次 discovery 成本不会同步线性增长。full 模式具备每 learner 固定 proposal slot、latest-wins 摄取、活跃 DB/历史 JSONL 分离以及 current-only checkpoint/payload GC；1000-cycle 实验验证了活跃行、文件数和 DB page 使用在 warm-up 后有界。fragment 也已改为每 `(learner, fragment)` 固定 pointer、持久 frontier 和 signature 短路，发现面严格为 `N×K`；专门的 1000-cycle 实验在 M=4、K=2 下只保留 8 个 pointer/8 个 frontier，未变化 pointer 的重复 JSON 解析为 0。checkpoint、payload 与 pointer 继续由同一 reference-driven maintenance 回收。

这里需要区分两类数据：运行正确性依赖的权威状态应当小而有界；实验日志和离线分析数据可以增长，但应有独立归档策略，不能反过来成为 runtime discovery 的组成部分。

### 4.3 存储权威状态能否带来简单可靠的恢复

full resume 已把持久 SQLite 的 committed row 定义为权威边界：publication 先写 weight/outer，再用单事务提交版本与 update 状态，最后更新可重建的 `latest.json`。六个 publication 退出点的 crash matrix 已覆盖临时权重、weight 后、outer 后、事务内、DB commit 后和 latest 后；resume 会校验 identity/integrity/checkpoint theta、回滚遗留 selected 并重建 latest。后续恢复研究可以集中在 learner identity/重启、批作业级复现，以及把同一语义推广到 fragment version vector。

fragment 恢复比 full 更复杂，因为权威状态是 fragment version vector，而不是单一 global version。full 路径适合作为恢复语义的参考实现，之后可以把相同思想推广到 per-fragment current state、outer state、materialized checkpoint 和 global merge event。

恢复研究的范围可以保持务实：重点放在进程退出和批作业重启，不追求 exact replay、自动 failover 或完整 inner optimizer 复现。节点断电级持久性可以作为代价与限制讨论，而不是让主系统过早承担复杂事务协议。

### 4.4 Fragment 何时真正有收益

fragment 已经证明可以降低单份 payload 和读写耗时，但当前协议把一部分节省重新消耗在等待上。下一步需要回答的不是“fragment 文件是否更小”，而是：

- learner 为什么在上传后等待新的 fragment latest；
- 严格 round-robin 是否造成目标 fragment 的 head-of-line blocking；
- quorum、grace、扫描周期和 learner 更新节奏之间如何相互作用；
- fragment adoption 是否需要同步等待，能否只在安全边界非阻塞地采用最新状态；
- scatter、CPU/GPU copy、optimizer reset 和 materialization 在更大模型上占多少时间；
- `balanced_tensor` 与 layer-aligned fragment 在负载均衡、连续 I/O、采用成本和训练质量上各有什么优势；
- fragment 数量和同步周期怎样共同决定 bytes/token 与 update interval。

已有 timing instrumentation 为这一研究提供了入口。近期可以先形成完整的 9 节点时间账本，再逐步尝试非阻塞 poll/adopt、缩短固定等待、按 ready fragment 调度、降低 materialization 频率和后台 staging。是否引入更复杂的 asynchronous save pipeline，可以由更大模型和慢存储下的实测暂停占比来决定。

### 4.5 Fresh-only 与 stale-aware 的关系

当前实现允许按版本差和 token 数对参数快照加权，但它还不等同于旧设计中的完整 stale-aware 语义。旧设计强调 proposal 的真实 base identity、future-base 拒绝、有限 base history、fresh anchor，以及从旧 base 到 learner local 参数的 displacement；当前运行路径主要对绝对参数快照做加权平均。

2026-07-18 的同 fingerprint 三 seed 矩阵已经否决把 fresh-only 当成默认参考：它只应用 61.8–78.7% 的 proposal，validation loss 相对 λ=.25 的配对均值恶化 0.04305 nats，三个 seed 都超过预冻结 ε=.01。λ=1 与 λ=4 虽把有效 staleness 大幅压低，却没有在 validation 上显著优于 λ=.25。因此当前参考语义继续使用 `max_staleness_versions=2, staleness_lambda=.25`；fresh-only 仅保留为边界 control。

这组结果提高了 base-relative displacement 的优先级：绝对参数快照在混 base 下的回拉问题不能靠单纯增大 λ 解决。下一步应补齐 displacement 数值 oracle，比较 current-parameter averaging 与 displacement reconstruction，并观察真实异构下 stale proposal 为何被接受或拒绝。

如果 stale 最终仍只有很小收益，它依然可以形成有价值的负面结果：低频、fragmented、latest-wins 的系统中，主要浪费可能不是一个版本的 staleness 窗口能够回收的。若某些调度或异构区域显示稳定收益，再考虑 matched-token、from-scratch、多种子质量实验。

### 4.6 系统结果能否转化为训练质量结果

当前模型主要从 pretrained checkpoint 加载，WikiText-2 数据路径会在内存中构建 token blocks，适合功能与系统实验，不适合数十亿 token 的质量研究。后续可以逐渐补充 from-scratch 初始化、流式或预分词数据、held-out evaluation、固定 token 预算和多种子执行能力。

系统主张与质量主张可以分开组织：较大模型和较长运行用于观察 goodput、容量、恢复和有界性；较小模型的 from-scratch matched-token 实验用于研究 fragment/stale 是否改变优化质量。这样可以避免用昂贵系统长跑承担统计质量任务，也避免用小型质量实验推断大规模系统容量。

### 4.7 协议适用边界在哪里

共享存储方案的价值与运行环境高度相关。后续可以通过人为可见性延迟、限速和 metadata 压力，研究性能随“存储可见性延迟 / 实际同步周期”的变化。对象存储微基准可以帮助判断协议对原子 PUT 或条件写的可迁移性，但不需要一开始就建设完整跨区域训练平台。

单 syncer 和多 syncer 也应当以容量曲线来讨论。当前数据尚不足以说明多 syncer 是主要矛盾；先观察 fragment 字节数、M、Q、read/merge/publish 时间和 duty cycle，能够更自然地判断何时值得引入静态非重叠的多 syncer ownership。

当前 124M/8-vector 数据已经给出更直接的单 syncer 成本边界：既有 50-merge run 的 dedicated syncer duty cycle 为 5.362%，0.3279 node-hours 中估计 0.3103 GPU node-hours 非 active；同节点 CPU 基准的 `read+aggregation+outer` p95 为 0.2866 秒，低于预先冻结的 4 秒门槛。后续同 fingerprint 三 seed 共置实验的完整训练中位数只劣化 1.83%，通过 ≤10% 门禁；但 seed 4049 出现 +41.4% 离群。因此 8 节点“CPU syncer + GPU learner_000”是可用容量变体，低方差的 9 节点专用拓扑仍为默认。

## 5. 建议的研究主线

### 5.1 近期：建立可信的 full reference

full 模式可以承担系统语义的参考路径。有限训练正常收尾、terminal drain、syncer/learner 独立退出、resume 后幂等 metadata 摄取和 publication crash window 已形成实现与故障矩阵。正式性能配置不再收敛到 fresh-only：三 seed validation 已显示 fresh-only 明显损失质量；当前基线保持 staleness window 2、λ=.25，并把 fresh-only 作为边界 control。

这一阶段也适合逐步把进程启动从单个 MPI 作业扩展到独立 PBS 作业。co-allocated launcher 可以继续作为方便、可重复的工程测试方式；独立作业实验则用于验证项目最初的动机场景和不同启动时间、单 learner 重启、syncer 重启等行为。

### 5.2 近期至中期：实现真正的有界状态

full 和 fragment 共享一套状态分类：current authority、有限 base window、每 learner/fragment 的最新 proposal、写入中的临时对象，以及仅用于研究归档的历史记录。full 运行时发现固定 learner pointer；fragment 已改为固定 `(learner, fragment)` pointer 与持久 frontier，不再 glob payload metadata。1000-cycle 测试保持 M×K=8 个 pointer/frontier，三 seed 9 节点 fragment 消融进一步验证了跨节点路径。

长跑研究可以持续记录 live bytes、文件数、metadata scan 时间、DB 大小、GC backlog 和第 N 次 update 的各阶段延迟。目标不是单纯减少磁盘占用，而是展示运行成本不会因为历史变长而退化。

### 5.3 中期：重新设计 fragment 的协调路径

fragment 性能研究可以以当前 timing breakdown 为基础，先解释 50×10 中的等待，再进行小步重构。值得优先观察的方向包括：上传后不阻塞等待、poll 与训练重叠、learner 只采用已经可见的新片、syncer 按 ready fragment 而非机械全局 round-robin 推进，以及 optimizer reset 策略对速度和质量的影响。

fragment 优化应同时保留 full 作为对照。若某次优化只减少 update 文件时间，却增加 quorum wait、adoption lag 或 local idle，它对系统整体并没有形成收益。最终希望得到的不是 fragment 在所有配置下都更快，而是一张清晰的适用图：模型多大、同步多频繁、存储多慢、fragment 多大时，分片开始产生端到端收益。

### 5.4 中期：构建代表性性能基线

性能研究可以逐渐形成几条使用相同训练栈的对照：

- 关闭通信的 learner 本地训练，用于定义 goodput 分母；
- 当前 full FS 模式，作为最简单的存储数据面；
- 优化后的 fragment FS 模式；
- 每个同步周期交换完整 checkpoint 的 naive FS 模式；
- 在同一 allocation 内使用 gloo/TCP 的最小网络聚合模式。

实验维度可以覆盖模型规模、learner 数、quorum、inner steps、fragment 数、上传 dtype 和人为存储延迟。重点报告完整时间账本，而不只报告峰值带宽：训练时间、snapshot、写入、proposal discovery、quorum/grace wait、读取、聚合、outer step、发布、采用和 shutdown wait。

近期 BF16 结果已经说明 payload 优化不一定等价于端到端优化。publish-only BF16 将 checkpoint 字节减半，却使 publication 三 seed 均值慢 62.5%、完整时间慢 2.01%；质量门禁虽通过，仍不应成为性能默认。相反，并行 weight/outer 写把 publication 关键路径降低 44.5%，事务/crash 语义保持不变。后续图表应持续把 bytes/token、learner pause、syncer duty cycle、global/fragment interval 和 accepted-token efficiency 放在同一分析框架中。

fragment 物化也已完成单变量剥离：间隔 1→10 使物化字节减少 90%、物化时间降低 81.45%，完整训练三 seed 均值改善 2.21%。生产 profile 因而显式使用 10，终态仍强制物化；这把物化 I/O 与剩余协调等待分开了。

同 fingerprint 的三 seed 事件化 ingestion 对照已补齐这一时间账本：把 full sync scan 从 2 秒降到 0.2 秒，使完整时间均值从 1101.633 秒降到 1075.273 秒（-2.39%），quorum discovery+idle 均值下降 6.83%，而共享盘轮询只使用单核 0.0402% CPU。相邻开启 publish-wait metadata ingestion 后虽每 run 平均摄取 8 份 update，完整时间为 1076.155 秒，没有进一步收益。因此短 scan 可作为低成本实验参数；publish-wait ingestion 保持 opt-in，不作为默认性能主张。

### 5.5 中期至后期：形成恢复实验与长期运行证据

恢复实验已经从 full 的短小故障注入覆盖到 publication 六阶段、孤儿 payload、重复 pointer 和跨节点重开。下一步可以覆盖 learner 退出/重启与批作业重启，再把相同实验迁移到 fragment version vector。

长期运行可以用于同时研究三件事：有界存储、discovery 延迟是否稳定、恢复后是否继续前进。相比单次超大 token 目标，多次可解释的中长运行可能更适合早期定位状态泄漏和终止问题；当运行语义稳定后，再扩大到更有代表性的模型与 token 预算。

### 5.6 后期：质量、stale 与一般化实验

统一 validation evaluator、独立 `afterok` 一节点链路、checkpoint/source identity 和三 seed 协议已经落地。当前策略矩阵显示 rebase/prediction/replace 都在 ε=.01 内，replace 的均值最低，故保持默认；staleness 矩阵否决 fresh-only，但尚未回答 base-relative displacement。后续质量预算可转向 fragment 数/分片策略、optimizer moments 在 fragment adoption 时保留或重置，以及 from-scratch matched-token 一般化。

stale 研究可以先做小矩阵和机制归因，再决定是否扩大到多种子。对象存储、可见性延迟扫描和多 syncer 则可以根据论文叙事与单 syncer 容量结果逐步加入。它们更适合作为主系统边界的扩展，而不是同时改动当前所有协议层。

## 6. 实验组织建议

后续实验可以围绕以下维度组织，而不预先绑定单一配置：

| 维度 | 研究目的 | 可考虑的变化 |
|---|---|---|
| 通信模式 | 建立代价来源 | no-communication、full FS、fragment FS、naive checkpoint、network |
| 模型规模 | 观察 payload 与 syncer 容量 | 当前 GPT-2 规模、数亿参数规模、资源允许时更大模型 |
| 同步节奏 | 观察训练与通信重叠 | inner steps、poll 周期、grace、materialize 周期 |
| Fragment | 观察均衡与采用成本 | fragment 数、balanced-tensor、layer-aligned、不同片大小 |
| 存储 | 观察适用边界 | 正常 Lustre、限速、注入可见性延迟、对象存储微基准 |
| 运行拓扑 | 验证动机场景 | co-allocated MPI launcher、独立 PBS jobs、单角色重启 |
| 状态生命周期 | 观察有界性与恢复 | 引用集合、orphan grace、archive/GC backlog、故障注入时机 |
| 算法语义 | 区分系统与优化影响 | fresh-only、stale-aware、moment reset/retain、不同 merge |
| 训练质量 | 建立统计解释 | pretrained 系统跑、from-scratch matched-token、多种子 |

所有性能结果都适合同时记录 wall-clock 和实际工作量。同步周期应以真实秒数和 local steps 两种方式报告；吞吐对照尽量保持精度、telemetry 和数据处理设置一致；系统优化、调度变化和算法变化最好能够分别开关，以便解释因果来源。

## 7. 预期研究产出

如果上述方向逐步收敛，项目可以形成以下核心图表和论文材料：

1. 存储数据面协议图：payload-first、metadata/latest-last、单写者和 per-fragment authority。
2. Full 与 fragment 的端到端时间分解，展示 payload 收益与协调等待之间的关系。
3. Goodput、bytes/token 和 syncer duty cycle 随模型规模、fragment 大小、M/Q 变化的容量图。
4. 长运行中的 live storage、文件数和 discovery latency 曲线，用于说明有界状态。
5. Syncer/learner 在不同退出点的恢复时间线，以及重启前后版本和 update identity 的变化。
6. 存储可见性延迟与 goodput、adoption lag、discard rate 的关系图，用于给出适用边界。
7. Fresh-only 与 stale-aware 的丢弃原因分解和 accepted-token efficiency，对 stale 收益或负面结果作机制解释。
8. Fragment 策略、optimizer adoption 语义和 stale 设置的 matched-token loss 对照。

论文主线可以围绕三个相互支撑的贡献展开：共享存储可以成为低频去中心化训练的数据面；有界权威状态使长期运行和进程级恢复保持简单；fragment 的收益取决于 payload 节省与协调等待之间的平衡，并能够通过系统化测量给出适用区间。stale、对象存储和多 syncer 可以根据最终证据成为扩展贡献、消融或限制讨论。

## 8. 主要风险与应对方向

### Fragment 始终慢于 full

这不一定否定 fragment。可能的结论是：在当前模型和 Lustre 带宽下，full payload 尚未成为瓶颈，fragment 的协调成本反而更高。研究可以转向更大模型、更短同步周期或更慢存储，并给出 crossover point；若仍无收益，则把 full FS 作为主系统，把 fragment 作为负面系统结果和设计经验。

### Resume 比预期复杂

full 路径已经把持久 SQLite committed row 定义为唯一恢复权威，并用 checkpoint theta 一致性和 fail-closed 校验收紧 crash window。剩余复杂度主要在 fragment 多版本向量与 learner 自身恢复；研究主张仍可限定到进程/批作业级恢复，不必过早扩展到节点断电和自动 failover。

### 有界存储与研究可审计性冲突

runtime 需要删除历史，研究又希望保留证据。可以把两者分层：runtime 只保留 current/latest/base window；独立的低频快照、汇总 CSV 和实验 artifact 负责审计。这样不会为了研究记录而迫使在线协议扫描完整历史。

### Stale 收益继续接近零

这与已有仿真信号一致。项目仍可贡献一项清晰结论：在当前 fragmented latest-wins 协议中，计算丢弃的主要来源并不由小 staleness window 解决。系统论文主线可以继续成立，stale 作为机制分析或负面结果保留。

### 当前部署不足以支撑动机场景

单个 MPI 作业方便实验，却弱化了“跨作业只共享 FS”的论证。独立 PBS 作业、不同启动时间和角色级重启可以补足这一差距，同时保留 MPI launcher 作为开发工具。

### 训练质量基础设施投入过大

系统和质量两条实验线可以分开推进。系统线先用 pretrained 模型和可控数据研究运行行为；质量线使用较小模型、from-scratch 和流式数据建立统计结论。两条线最终在少量共享配置上交汇即可。

## 9. 近期可展开的工作

截至 2026-07-18，E/Q 第一轮计划已补齐 source identity、三 seed 单变量实验、统一 validation、固定 proposal surface、并行 publication、物化剥离、staleness/策略质量矩阵和 syncer 共置成本账本。terminal partial-merge 的三 seed predecessor/post 配对也已闭合：配对均值 -0.000330，所有 seed 都远低于 ε=.01，因此按条件计划不引入 outer-LR scaling。

下一阶段最自然的工作是 base-relative displacement 数值 oracle 与 matched-token 对照；同时明确“每 learner 5000 local steps AND 50 outer merges”的联合完成谓词。系统侧可在更大模型/更慢存储下寻找 fragment 与 8 节点共置的 crossover，并建立 no-communication learner 基线；恢复侧可从同 allocation 故障注入扩展到独立 PBS 角色启动与重启。

这些方向应继续复用现有 source fingerprint、resolved-config diff、三 seed、独立 validation 与时间账本纪律，避免重新依赖 local loss 或单次异步 walltime 作质量/性能判断。
