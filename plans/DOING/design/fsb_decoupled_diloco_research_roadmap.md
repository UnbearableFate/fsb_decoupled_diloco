# FS-Based Decoupled DiLoCo 下一步研究路线建议

对象：`UnbearableFate/fsb_decoupled_diloco` 分支 `codex/fsb_decoupled_diloco_plan_02`  
形成日期：2026-08-07（Asia/Tokyo）  
定位：基于当前代码、现有研究计划、Miyabi/PBS/Lustre 场景，以及截至 2026-08-07 的相关研究进展

## 1. 核心判断

该研究下一步不应继续把主要精力放在“再增加一种 dynamic policy”或“继续证明文件可以交换参数”。更合适的研究主线是：

> **面向批调度 HPC 的 durable storage-native decoupled training：在没有长驻 RPC 服务、没有跨作业 collective、角色可独立启动和失败的条件下，使用共享存储构建有界、可恢复、可审计的训练 data/control plane。**

推荐的论文方向名称：

- **DuraLoCo: Durable Storage-Native Decoupled Training for Batch-Scheduled HPC**
- **A Crash-Consistent Shared-Storage Control Plane for Decoupled LLM Training**
- **Beyond Checkpointing: Shared Storage as a Durable Data Plane for Decoupled Training**

不建议继续把“独立 learner + central syncer + minimum quorum + adaptive grace + token weighting + fragment”作为主要新颖性。2026 年 4 月公开的 **Decoupled DiLoCo for Resilient Distributed Pre-training** 已经系统提出这些机制，并进一步给出了 Pathways 驱动的 message-passing 架构、vector clock、consistent distributed snapshot、event tape、deterministic replay、scavenging、heterogeneous hardware 和大规模质量实验。[1]

当前项目仍有明确的新颖空间，但需要把差异说准：

1. **通信和权威状态以共享存储为中心，而不是长驻 FIFO message channel。**
2. **运行单位是彼此独立的 PBS 作业，不是由统一 runtime 长期管理的 worker graph。**
3. **syncer 本身可以失败并由 epoch/lease fencing 接管，而不是假定稳定 CPU syncer。**
4. **系统追求 DB-first crash consistency、bounded discovery、reference-driven retention 和可审计调度。**
5. **目标场景是“只有共享存储是稳定公共基础设施”的 HPC/batch environment。**

这应成为下一阶段所有工程和实验选择的中心。

## 2. 当前项目已经具备的研究资产

### 2.1 系统资产

- learner 和 syncer 可作为独立 PBS 作业运行；
- communication 不依赖 NCCL/RPC/Ray/DeepSpeed；
- proposal 通过 immutable-style tensor object + atomic pointer 发布；
- SQLite 作为权威 commit record，固定 JSON 作为可重建 view；
- full mode 已有 resume/crash-matrix 基础；
- syncer HA 已实现 lease、epoch fencing 和 epoch-unique controls/checkpoints；
- dynamic membership 已区分 instance、placement、stream 和 admission generation；
- scheduler launch outbox、capacity observation 和 close-and-drain 已进入协议；
- fixed pointer surface、DB archive 和 current-only GC 已形成 bounded-state 设计；
- 仓库保留了系统测试、PBS 证据、失败记录和修复记录。

### 2.2 经验信号

仓库已有实验给出两个重要方向性结论：

1. full 模式的 SQLite commit/maintenance 不是当前主要瓶颈；
2. fragment 虽然把 payload 从约 249 MB 降到约 63 MB，但端到端仍比 full 慢约 32%，等待 latest/quorum 比序列化和磁盘读写更重要。

因此，下一步不应继续只优化 tensor file 编码。真正的问题是：

- 如何减少 blocking wait；
- 如何把 fragment publication/merge/adoption 与训练重叠；
- 如何在不牺牲恢复语义的前提下允许多个 fragment in flight；
- 如何精确定义有效计算和数据连续性。

### 2.3 当前最影响研究可信度的技术缺口

在开始新实验矩阵前，必须先解决独立代码审查报告中的 P0 问题，尤其是：

- replacement 后旧 proposal 可造成 dynamic livelock；
- mid-cycle `replace` 的 token 统计和 merge weight 不代表有效计算；
- proposal unique conflict 可丢失旧 pending 并推进 frontier；
- contributor selection 存在确定性 ID 饥饿；
- transient shared-FS read error 被永久化；
- `total_seen_tokens` 的语义不足以作为论文分母；
- matched performance gate 会把异常巨大差异截断成 0 overhead。

在这些问题修复前继续扩展算法，会使后续结果难以解释。

## 3. 与最新相关工作的关系

## 3.1 Decoupled DiLoCo 已覆盖的部分

2026 年的 Decoupled DiLoCo 已包括：

- 独立 asynchronous learners；
- central syncer；
- parameter fragments；
- minimum quorum；
- adaptive grace window；
- per-learner/per-fragment token counters 和动态权重；
- failure/straggler isolation；
- heterogeneous/scavenged compute；
- balanced tensor fragmentation；
- deterministic event tapes；
- vector-clock coordination；
- Chandy–Lamport-style distributed snapshot；
- dense/MoE、text/vision 和大规模模拟/训练验证。[1]

其主要实验还采用 `P=H=24`、`τ=2`，使每一步都有一个 fragment 发送/接收、两个 fragment in flight；其核心不是“轮询一个 fragment 后等待完成”，而是把同步放进稳定的流水线中。[1]

这意味着以下表述不再足以形成论文贡献：

- “我们首次解耦 learner 和 syncer”；
- “我们允许 minimum quorum”；
- “我们按 token 给异步 learner 加权”；
- “我们使用 balanced tensor fragment”；
- “我们允许 learner 失败后继续训练”；
- “我们提供 event log/replay”——除非项目给出与现有 event tape 明确不同的存储原生机制和证据。

## 3.2 Streaming DiLoCo 对 fragment 路线的启示

Streaming DiLoCo 已证明三件事可以共同工作：

1. 分批同步参数子集；
2. 同步与训练重叠；
3. 低精度传输。[2]

当前项目 fragment 慢于 full 的结果并不否定 fragment，而是说明当前协议没有实现其主要系统收益：**降低 blocking critical path**。因此下一步 fragment 研究应围绕 overlap 和 in-flight protocol，而不是继续压缩单文件写入时间。

## 3.3 异步 Local-SGD 对 outer optimizer 的启示

Asynchronous Local-SGD 的研究指出，naive asynchronous local training 的关键问题之一是 stale worker update 与 global momentum 的相互作用；delayed Nesterov 和根据 worker 速度调节 local steps 可以改善质量/时间。[3]

当前项目拥有显式 outer optimizer state，因而非常适合做下列对照：

- ordinary Nesterov；
- delayed Nesterov；
- staleness-aware momentum；
- speed-adaptive local horizon；
- direct displacement average；
- base-relative pseudo-gradient；
- RDA。

但这些应在 token/accounting 和 transport baseline 先闭合后进行。

## 3.4 HALoS、DreamDDP 和 Factored Gossip DiLoCo 的启示

- HALoS 表明 hierarchical asynchronous local SGD 和 hierarchical momentum 是扩展跨域训练的重要方向。[4]
- DreamDDP 表明 layer-wise partial synchronization 的收益依赖精细的 communication-computation scheduling，而不只是“少同步一些参数”。[5]
- Factored Gossip DiLoCo 在 2026 年被 ICML 接收，其核心是把 exact synchronization 分解为 non-blocking mixing 和较少的 blocking agreement，从而在延迟/失败下优雅退化。[6]

对本项目的含义是：

- **短期不要立即改成 gossip。** 当前最独特的资产是 durable shared-storage authority，而不是去中心化拓扑。
- **中期可把 gossip 作为 comparator 或 extension。** 例如 storage 中持久化 mixing state，而不是把 central syncer 全部取消。
- **层次化 syncer 值得晚些研究。** 当单 syncer 或 SQLite/metadata plane 真正成为瓶颈后，再引入 learner-local/micro-syncer 和 global authority。

## 4. 推荐的研究定位

## 4.1 首选：系统论文主线

### 研究问题

> 在 HPC 批调度环境中，如果多个训练作业之间唯一稳定的共享基础设施是并行文件系统，能否构建一个不依赖长驻服务的 decoupled training runtime，并同时实现低控制面开销、故障后继续前进、有界权威状态和可复现审计？

### 预期贡献

1. **Storage-native training plane**  
   用 immutable object publication、atomic pointer、SQLite authority 和 bounded inbox 实现训练更新与控制状态。

2. **Crash-consistent and fenced control plane**  
   syncer lease/epoch、事务内 fencing、epoch-unique publication、DB-first recovery、stale leader containment。

3. **Batch-scheduler-native elasticity**  
   独立 PBS jobs、durable launch outbox、incarnation/placement/stream separation、scheduler ambiguity handling、close-and-drain。

4. **Bounded state and auditable replayability**  
   固定 proposal surface、reference-driven retention、authority/audit/telemetry separation、event lineage。

5. **Real-system evaluation**  
   在 Lustre/PBS 上与 message-passing 和 naive FS baselines 做相同算法、相同工作量的对照，并量化 failure recovery、storage cost、goodput 和 quality。

### 为什么比“再做一个 DiLoCo 变体”更强

因为这条主线回答的是一个现有 Decoupled DiLoCo 没有直接回答的问题：

> 当 central runtime、稳定 FIFO channel 和统一 worker orchestration 不存在时，能否把共享存储从 checkpoint backend 提升为训练协议的权威数据面？

## 4.2 次选：算法—系统协同论文

在系统主线稳定后，可加入一个算法贡献：

- effective-compute-aware merge；
- delayed/staleness-aware outer momentum；
- storage-latency-aware fragment schedule；
- version-vector-aware radial-directional aggregation；
- churn-aware data-continuity weighting。

风险是研究变量过多。若同时改变 transport、HA、membership、fragment schedule、merge formula 和 optimizer，很难证明收益来自哪里。因此更合适的策略是：系统论文先固定算法；算法扩展作为第二篇或附加章节。

## 4.3 不建议作为主线的方向

- 继续增加大量启发式 stale weighting，但没有 matched effective-token baseline；
- 只在 GPT-2/WikiText-2 上报告 loss 降低；
- 把 PBS job replacement 本身作为唯一贡献；
- 把 current fragment payload 缩小当作性能贡献，而端到端更慢；
- 在 central syncer 尚未成为瓶颈前直接做复杂 micro-syncer/gossip；
- 同时引入 dynamic learner、fragment HA、object store、second-order outer optimizer 和多模型支持。

## 5. 需要明确回答的研究问题与假设

## RQ-1 共享存储 data plane 的真实代价是多少？

**假设 H1：** 在低频同步、固定模型和相同 merge algorithm 下，storage-native runtime 相对 message-passing decoupled baseline 的 learner goodput 损失可以保持在可接受范围，同时避免长驻服务依赖。

必须报告：

- signed wall-clock delta；
- quality vs unique tokens；
- quality vs GPU-hours；
- bytes read/write；
- metadata operations；
- syncer duty cycle；
- quorum wait；
- learner publication pause。

## RQ-2 durable authority 是否真正改善恢复？

**假设 H2：** 对已经 committed 的 global version，syncer crash/restart 可以达到 `RPO=0 committed versions`，且 recovery time 不随历史 update 数线性增长。

需要区分：

- process crash；
- SIGSTOP；
- node loss；
- scheduler rerun；
- DB writer lock stall；
- transient shared-FS read failure；
- corrupt/torn artifact；
- stale leader continuation。

## RQ-3 bounded-state 是否在长跑和规模扩展中成立？

**假设 H3：** 在固定 learner/fragment cardinality 下，活跃文件数、DB active rows、discovery work 和 recovery work 在 warm-up 后保持有界，与累计 global steps 解耦。

不仅测磁盘容量，还要测：

- 每次 scan 的 stat/open/read 次数；
- SQLite pages/WAL或rollback journal行为；
- GC latency；
- archive throughput；
- restart scan time。

## RQ-4 fragment 何时优于 full？

**假设 H4：** 只有当 fragment publication、merge 和 adoption 与 inner compute 重叠，并允许多个 fragment in flight 时，payload 减少才会转化为端到端收益。

研究变量：

- `P`：fragment count；
- `H`：每 fragment 同步周期；
- `τ`：in-flight delay；
- fragment assignment；
- upload dtype/quantization；
- quorum/grace；
- storage bandwidth/metadata latency；
- model size。

## RQ-5 dynamic membership 是否提升“有效”goodput？

**假设 H5：** 在可重放的 failure schedule 下，dynamic replacement 能提高 effective tokens/GPU-hour 和达到目标质量的时间，而不是仅提高 processed tokens。

前提：replacement 必须保持 stream data cursor/RNG continuity，或明确计算 replayed/duplicated data。

## RQ-6 stale update 应如何与 outer optimizer 结合？

**假设 H6：** base-relative update、delayed Nesterov 或 RDA 比简单参数平均 + token/staleness scalar 更稳定，尤其在 learner 速度差异和 churn 下。

比较必须在相同：

- unique-token budget；
- processed compute budget；
- failure tape；
- transport；
- local optimizer；
- outer update count。

## 6. 分阶段路线

## Stage 0：Correctness and measurement freeze

目标：把当前 full mode 变成可信实验平台。

### 工作

1. 修复代码审查报告 P0。
2. 建立 strict proposal/control schemas。
3. 建立 authoritative token/compute ledger。
4. 把 timeout 全部区分 monotonic time 与 wall time。
5. 实现 dynamic stream cursor/RNG continuity。
6. 建 deterministic state-machine simulator 和 fault tape。
7. 建 workload equivalence checker。
8. 冻结 source、environment、dataset、tokenizer identity。

### 验收门禁

- dynamic old-incarnation/replacement regression 通过；
- proposal ingest conflict 不丢 pending；
- processed/effective/discarded token 对账为零差；
- 在固定 virtual tape 下两次执行得到相同 selection/event lineage；
- full/static/HA 和 full/dynamic/HA 的 guarantee matrix 完成；
- 8+1 Miyabi fault regression 通过；
- 不以 best-effort CSV 作为任何 PASS denominator。

## Stage 1：建立可证伪的 transport baselines

目标：把“共享存储是否值得”变成可回答问题。

### 最低 baseline

1. **Single learner/local AdamW**：质量和单机吞吐参考。
2. **Synchronous data parallel 或同步 DiLoCo**：紧耦合参考。
3. **Message-passing decoupled baseline**：与 FS 版本使用相同 learner/syncer/selection/outer optimizer，只替换 transport/authority adapter。
4. **Naive filesystem exchange**：目录扫描、全 checkpoint、无 bounded pointer/DB fencing。
5. **FS-DiLoCo full/static/HA**。
6. **FS-DiLoCo full/dynamic/HA**。

不必复刻 Pathways。关键是实现一个足够窄的 TCP/gRPC/ZeroMQ/PyTorch-Gloo baseline，让算法和工作量相同，只改变通信/权威方式。

### 实验原则

- 相同初始化 checkpoint；
- 相同 dataset revision 和 shard map；
- 相同 unique-token budget；
- 相同 `H/K/grace/outer optimizer`；
- 相同 terminal anchor；
- 系统 microbenchmark 至少多次重复；
- quality 结论至少 3 seeds；
- 报告 signed delta 和置信区间。

### 关键输出

- storage plane overhead breakdown；
- service-free deployment cost/benefit；
- model size、M、H 与 break-even bandwidth/latency；
- naive FS 与 protocolized FS 的差异。

## Stage 2：把 durable storage protocol 做成论文级贡献

目标：系统性证明 safety、liveness、boundedness 和 recoverability。

### 需要形式化或半形式化的 invariant

1. committed version 单调且 predecessor 唯一；
2. stale leader 不能提交 business mutation；
3. current member fence 在 ingest 和 commit 两处成立；
4. current quorum 持续存在且存储最终可用时，系统最终前进；
5. authority cache 可重建；
6. active-state cardinality 有界；
7. GC 不删除任何 committed/current reference；
8. scheduler request 至多 admission 一个 incarnation；
9. terminal close 后 admission/merge bound 不被突破。

### 方法

- Python/Hypothesis state machine；
- 可选 TLA+/PlusCal 对 leader、membership、publication、terminal 进行小模型验证；
- crash point 自动枚举；
- fault tape 重放；
- Lustre 实机故障注入。

### 实验

- syncer 在每个 publication point crash；
- dual syncer + stale leader resume；
- SQLite writer SIGSTOP；
- proposal pointer/payload visibility delay；
- PBS qstat/qsub ambiguity；
- learner permanent loss/replacement；
- terminal drain 中的失败；
- 1,000/10,000 cycle boundedness。

### 论文指标

- RPO/RTO；
- availability；
- progress gap；
- orphan bytes；
- recovery scan time；
- state cardinality；
- false replacement/duplicate admission count；
- operator intervention frequency。

## Stage 3：重构 fragment 为真正的 streaming protocol

目标：让 fragment 减少 critical-path blocking，而不是只减小 payload。

### 当前设计需要改变的核心

当前 round-robin 模式近似：

```text
train H steps → publish one fragment → wait/poll → adopt → next cycle
```

目标应是：

```text
inner step t
  ├── snapshot/encode fragment p(t) asynchronously
  ├── publish p(t)
  ├── continue training
  ├── syncer aggregates prior fragment p(t-τ)
  └── learner adopts returned fragment at controlled boundary
```

### 必要机制

1. per-fragment version vector；
2. per-fragment base lineage；
3. multiple in-flight publications；
4. background CPU encode/write queue；
5. commit watermark；
6. adoption queue 和 deterministic ordering；
7. fragment crash-consistent resume；
8. backpressure，避免无限 pending；
9. tensor lifetime 和 GPU→CPU overlap；
10. per-fragment effective token counters。

### 需要比较的 fragment strategies

- balanced tensor；
- layer-aligned；
- tensor round-robin；
- profile-guided balanced；
- variance/curvature-aware grouping；
- embedding/lm_head special handling。

### 需要比较的传输形式

- FP32、BF16；
- 8-bit/4-bit outer update quantization；
- full parameter fragment；
- base-relative delta；
- sparse/top-k delta（作为后续 extension）。

### Go/No-Go

只有同时满足以下条件，fragment 才进入主论文贡献：

- 端到端时间优于 full，而不只是 bytes 更少；
- quality-vs-unique-token 不显著退化；
- syncer/learner wait 比例下降；
- resume 和 GC 保证不弱于 full 的声明范围；
- 结果在至少两个模型规模或两个带宽区间成立。

否则 fragment 保留为负面结果/设计边界，不应强行包装为性能胜利。

## Stage 4：有效计算和异步优化算法

目标：在稳定系统上研究“哪些异步计算值得吸收”。

### 先做 discard decomposition

每个 proposal/segment 标记最终命运：

- applied fresh；
- applied stale；
- superseded by newer proposal；
- invalid membership fence；
- replaced locally before publication；
- rejected by quorum cap；
- file visibility failure；
- terminal cutoff；
- data replay after replacement。

### 聚合方法矩阵

1. direct parameter averaging；
2. base-relative displacement average；
3. Nesterov outer optimizer；
4. delayed Nesterov；
5. staleness-decayed displacement；
6. RDA；
7. speed-adaptive local steps；
8. prediction/rebase strategy。

### 主要研究问题

- staleness 应按 global version、wall time、effective local steps 还是 model drift 衡量？
- token quantity 与 update quality 是否应相乘？
- replacement 后 partial segment 如何计权？
- fast learner 是否会主导数据分布？
- outer momentum 如何避免重复放大 stale direction？

### 实验控制

固定 failure tape 和 unique-token sequence，通过 deterministic replay 只替换 merge/optimizer policy。这样才能把算法差异从系统 timing 中分离。

## Stage 5：dynamic membership 的训练质量闭环

目标：从“控制面能替换作业”推进到“替换不破坏训练解释”。

### 必做

- 持久 stream cursor/sample index；
- RNG/shuffle state continuity；
- stream epoch 与数据 lineage 绑定；
- replacement replay token 计数；
- duplicate physical job 不得重复消费同一 authoritative cursor；
- admission 时确定 resume point；
- terminal drain 时 final cursor 持久化。

### churn matrix

- permanent learner loss；
- transient pause；
- slow learner；
- queue-delayed replacement；
- scheduler rerun same PBS job ID；
- simultaneous multiple losses；
- replacement 再失败；
- heterogeneous GPU speed。

### 结果必须同时报告

- processed tokens/s；
- unique tokens/s；
- effective applied tokens/s；
- GPU-hours to target loss；
- replay ratio；
- selection fairness；
- quality under same unique-token budget；
- recovery lag。

## Stage 6：层次化和更大规模扩展

仅在实验显示 central syncer、SQLite writer 或 Lustre metadata 成为瓶颈后进入。

### 可选方向

1. **micro-syncer / local aggregator**  
   learner CPU 先聚合一组 fragments，global authority 只接收较粗更新。

2. **hierarchical authority**  
   local shard DB/ledger + global commit coordinator；必须避免重新引入不可恢复的跨 DB transaction。

3. **sharded outer optimizer**  
   per-fragment owner，global event 只提交 version-vector watermark。

4. **gossip comparator**  
   把 Factored Gossip DiLoCo 作为非中心化 baseline，研究 durable storage 是否可以保存 mixing/checkpoint lineage。

5. **object-store backend**  
   当 POSIX/Lustre 结论稳定后，再验证 conditional PUT、ETag 和 manifest commit 能否推广到 S3-compatible storage。

### 不应过早进行的原因

在 8 learner + 1 syncer、SQLite commit 占比很低的阶段，分片控制面会增加复杂度而缺少证据。研究扩展应由测得的 bottleneck 驱动。

## Stage 7：论文和 artifact 冻结

### 论文需要的最小证据包

- immutable source commit；
- dependency/container lock；
- dataset/tokenizer revisions；
- all configs and seed map；
- fault tapes；
- raw event/audit logs；
- authority DB snapshot；
- result extraction scripts；
- workload equivalence report；
- negative results；
- one-command local simulator；
- Miyabi/PBS reproduction instructions；
- cleanup manifest 和 retained artifact inventory。

### artifact 不能依赖的内容

- 仅存在于 W&B 的指标；
- cleanup 后不可恢复的 CSV；
- 没有 source fingerprint 的 checkpoint；
- 手工复制但没有 hash 的结果 JSON；
- 只在 README 中声明、没有 checker 的 invariant。

## 7. 推荐实验矩阵

## 7.1 Workload tiers

### Tier A：协议正确性

- tiny synthetic model/data；
- CPU 或单 GPU；
- 高密度 crash/reorder/fault injection；
- 目标是状态空间覆盖，不用于质量结论。

### Tier B：系统性能

- GPT-style 100M–400M 规模；
- 2/4/8 learners；
- 固定训练 token 和 outer steps；
- full vs fragment vs message baseline；
- 目标是测 transport、storage、waiting、recovery。

### Tier C：训练质量

- 至少一个更接近真实预训练的 dataset subset；
- 至少两个模型规模，理想为数亿参数和约 1B 参数；
- 3+ seeds 或足够长训练配合 bootstrap CI；
- validation perplexity + 少量固定 downstream eval；
- 目标是证明 asynchronous/storage mechanisms 没有掩盖质量代价。

WikiText-2 可继续用于 smoke，但不应承担论文主要质量结论。

## 7.2 变量

| 维度 | 建议值/范围 |
|---|---|
| learners M | 2, 4, 8；资源允许时 16 |
| quorum K | 1, 2, M/2, M |
| local horizon H | 25, 50, 100 或按模型 step time 校准 |
| fragment count P | 1, 4, 8, 16/24 |
| in-flight delay τ | 0, 1, 2, 4 |
| staleness bound | 0, 1, 2, 4 |
| failure rate | none, low, medium, high；用固定 tape |
| heterogeneity | 1.0×, 0.75×, 0.5× learner speed |
| transport | message, naive FS, durable FS |
| dtype | FP32, BF16, optional INT8/4-bit |
| outer optimizer | SGD, Nesterov, delayed Nesterov, RDA variant |

不要一次笛卡尔积全部变量。采用分层消融：先 transport，后 fragment，后 optimizer，最后 churn。

## 7.3 Baseline fairness checklist

每组比较前自动检查：

- source fingerprint 一致；
- environment fingerprint 一致；
- model init hash 一致；
- tokenizer/dataset revision 一致；
- shard/cursor plan 一致；
- target unique tokens 一致；
- local batch/sequence length 一致；
- inner/outer optimizer 一致；
- eval checkpoint anchor 一致；
- failure tape 一致；
- timer start/end anchor 一致；
- actual GPU allocation/device type 一致；
- selected/applied effective token 在允许误差内一致。

任何关键项不一致，结果状态应为 `INCOMPARABLE`，而不是 PASS/FAIL。

## 8. 指标体系

## 8.1 质量指标

- validation loss/perplexity vs unique tokens；
- validation loss/perplexity vs GPU-hours；
- downstream average vs unique tokens；
- seed variance；
- post-failure recovery quality gap；
- learner divergence/fragment drift。

## 8.2 计算利用指标

- learner step goodput；
- effective tokens/GPU-second；
- processed/effective token ratio；
- discarded compute ratio，按原因分解；
- system uptime；
- accelerator idle due to sync/adoption/storage；
- slowest/fastest learner contribution ratio。

## 8.3 通信与存储指标

- bytes written/read per effective token；
- metadata ops per global update；
- file visibility latency p50/p95/p99；
- proposal publication pause；
- syncer payload read time；
- DB transaction latency；
- GC/archival cost；
- active file/row/page count；
- recovery scan bytes/ops。

## 8.4 协调指标

- quorum wait；
- grace used/slack；
- selected contributor count；
- selection fairness/Jain index；
- max stream wait versions；
- stale distribution；
- fence rejection count；
- scheduler uncertain duration；
- replacement admission delay；
- terminal drain duration。

## 8.5 故障恢复指标

- RPO：丢失 committed versions/token；
- RTO：故障到新 committed version；
- time to detect；
- time to elect/admit；
- orphan artifacts；
- duplicate admission attempts；
- manual intervention required；
- post-recovery data replay ratio。

## 9. 建议的 discrete-event simulator 和 event lineage

现有论文已使用 event tape 进行 deterministic replay，因此本项目不能只说“我们也记录日志”。应形成 storage-native 的差异：

### 9.1 Event lineage 的目标

从 authority DB 和 immutable artifacts 中恢复：

- 谁在何时、基于哪个 version/epoch 发布 proposal；
- 哪些 proposal 被观测、选择、拒绝、应用；
- 每次 merge 的 contributor set、weight 和 base lineage；
- membership/scheduler 的全部 state transition；
- control publication 与 checkpoint hash；
- data cursor/unique-token lineage。

### 9.2 推荐模型

- authority transaction 产生 append-only `event_id`；
- 每个 event 记录 predecessor/causal parents；
- SQLite current tables 是 materialized view；
- JSONL/Parquet event archive 可重放 current view；
- tensor object 由 content hash 或 immutable object ID 引用；
- simulator 读取 event/fault tape，重建 selection timing。

### 9.3 不要过早使用“event-sourced”表述

只有在以下条件成立后，才建议把项目称为 event-sourced：

- 所有权威 mutation 都有不可丢失事件；
- current DB 可由 event log 重建；
- event schema 有版本；
- event order/causality 明确；
- compaction 不破坏可验证 lineage。

当前更稳妥的表述是 **durable DB-first authority with append-only audit history**。

## 10. 算法研究的具体建议

## 10.1 优先研究 base-relative update，而不是直接 stale parameter average

对于 proposal `θ_m` 和其 base `Θ_b`，保存：

```text
Δ_m = θ_m - Θ_b
```

syncer 在 current `Θ_t` 上处理 `Δ_m`，能更清楚地区分：

- 本地训练方向；
- base staleness；
- global drift；
- outer momentum。

这比直接平均来自不同 base 的参数更容易分析，也更适合 hash/lineage 验证。

## 10.2 比较 delayed Nesterov 和 RDA

- delayed Nesterov 针对 stale global momentum；
- RDA 把方向和范数分开聚合，在 learner 数增加时可能更稳定；
- 两者可以分别作为 outer optimizer 和 merge operator，不要同时首次启用。

推荐消融顺序：

1. direct displacement + SGD；
2. direct displacement + Nesterov；
3. direct displacement + delayed Nesterov；
4. RDA + Nesterov；
5. RDA + delayed Nesterov。

## 10.3 local horizon 应按速度和存储 slack 自适应

可研究：

```text
H_m = clamp(target_sync_period_seconds × learner_steps_per_second_m,
            H_min, H_max)
```

但目标应是让每个 contributor 的 update age/compute 接近，而不是让最快 learner 无限产生 proposal。需与 fairness scheduler 配合。

## 10.4 stale metric 不应只看 global version

候选指标：

- version staleness；
- wall-clock age；
- local effective steps；
- norm of `Θ_t - Θ_b`；
- cosine between stale delta and recent global direction；
- fragment-specific drift。

先做 correlation study，判断哪种 staleness 与 update utility/quality 最相关，再设计 weighting。

## 10.5 second-order outer optimizer 暂不优先

Shampoo/K-FAC 类 outer preconditioner有潜力，但当前系统问题主要是活性、等待、accounting 和 transport。过早引入二阶状态会：

- 放大 checkpoint/HA state；
- 增加 fragment state coupling；
- 使恢复和对照复杂；
- 掩盖 storage protocol 的主要贡献。

更合适的顺序是：先完成 reliable first-order baseline，再把 second-order outer state 作为独立扩展。

## 11. 论文结构建议

## 11.1 可能的标题

**DuraLoCo: Durable Storage-Native Decoupled Training for Batch-Scheduled HPC**

## 11.2 主论点

传统 decoupled training 假定存在稳定的 runtime/message channel 和 syncer。批调度 HPC 中，作业可能独立排队、退出和重启，而共享并行文件系统往往是唯一跨作业稳定可见的基础设施。DuraLoCo 把共享存储提升为训练 data/control plane，通过 DB-first authority、immutable publication、epoch fencing 和 bounded discovery 实现无长驻服务的 resilient training。

## 11.3 章节

1. **Introduction**  
   batch-scheduled HPC 的运行约束；为什么 checkpoint-only storage 不够；与 message-based Decoupled DiLoCo 的差异。

2. **Background and Problem Model**  
   DiLoCo/Streaming/Decoupled；PBS/Lustre；故障模型；保证与非目标。

3. **Storage-Native Protocol**  
   proposal publication、authority DB、version/epoch、merge commit、learner view。

4. **Fault Tolerance and Elasticity**  
   syncer HA、membership fence、launch outbox、close-and-drain、recovery。

5. **Bounded State and Auditability**  
   fixed pointers、retention、event lineage、replay simulator。

6. **Implementation**  
   PyTorch/HF、safetensors、SQLite rollback/full sync、PBS integration。

7. **Evaluation**  
   transport overhead、fault recovery、boundedness、dynamic churn、quality；fragment 可作为独立 subsection。

8. **Limitations**  
   shared-FS availability、SQLite writer behavior、malicious learner 非目标、fragment resume status、规模边界。

## 11.4 论文 claim 表

在写作前建立表格，每个 claim 绑定证据：

| Claim | 所需 evidence |
|---|---|
| no long-lived communication service | architecture + launch trace |
| stale syncer cannot commit | invariant + dual-syncer fault matrix |
| committed state crash-consistent | crash points + RPO result |
| active state bounded | long-run files/rows/pages/scan ops |
| storage overhead acceptable | matched message baseline + CI |
| dynamic membership improves goodput | replayable churn + effective token/GPU-hour |
| fragment improves wall time | overlapped protocol + quality equivalence |

没有绑定 evidence 的 claim 不进入摘要。

## 12. 决策建议

### 立即继续

- full/static/HA；
- full/dynamic/HA，但先修复 liveness 和 data cursor；
- DB-first recovery；
- bounded proposal surface；
- storage vs message transport baseline；
- event lineage/state-machine simulator。

### 重构后继续

- fragment overlap/version vector；
- adaptive grace；
- dynamic token weighting；
- prediction/rebase；
- scheduler auto scaling。

### 暂缓

- second-order outer optimizer；
- micro-syncer/fully decentralized topology；
- object-store generalization；
- 多模型、多数据集、多调度器同时扩展；
- fragment dynamic membership HA 全组合。

### 明确停止的表述

- 不再把基本 Decoupled DiLoCo 机制作为首创；
- 不再使用 `total_seen_tokens` 指代全部训练计算；
- 不再用 clipped single-run ratio 证明 overhead；
- 不再把 payload 更小直接等同于系统更快；
- 不在缺少 data cursor continuity 时声称 replacement 完全保持训练语义。

## 13. 最优的近期工作顺序

1. **修复 P0 correctness/accounting。**
2. **提取 transport/authority port，做 message baseline。**
3. **完成 workload equivalence checker 和 authoritative metrics。**
4. **跑 full/static/HA 的无故障 matched baseline。**
5. **跑可重放 fault matrix，证明 recovery/availability。**
6. **加入 dynamic data cursor，跑 churn quality。**
7. **重构 fragment 为多 in-flight overlap，再判断是否进入主论文。**
8. **最后才做 delayed Nesterov/RDA 等算法扩展。**

这个顺序能最大化每一步的可解释性：先让系统结果可信，再证明 storage-native 的独特价值，然后才扩大算法贡献。

## 14. 参考文献

[1] Arthur Douillard et al. **Decoupled DiLoCo for Resilient Distributed Pre-training.** arXiv:2604.21428, 2026.  
<https://arxiv.org/abs/2604.21428>

[2] Arthur Douillard et al. **Streaming DiLoCo with Overlapping Communication: Towards a Distributed Free Lunch.** arXiv:2501.18512, 2025.  
<https://arxiv.org/abs/2501.18512>

[3] Bo Liu et al. **Asynchronous Local-SGD Training for Language Modeling.** arXiv:2401.09135, 2024.  
<https://arxiv.org/abs/2401.09135>

[4] Geon-Woo Kim et al. **HALoS: Hierarchical Asynchronous Local SGD over Slow Networks for Geo-Distributed Large Language Model Training.** ICML 2025 / arXiv:2506.04531.  
<https://arxiv.org/abs/2506.04531>

[5] Zhenheng Tang et al. **DreamDDP: Accelerating Data Parallel Distributed LLM Training with Layer-wise Scheduled Partial Synchronization.** arXiv:2502.11058, 2025.  
<https://arxiv.org/abs/2502.11058>

[6] Chamin Hewa Koneputugodage et al. **Factored Gossip DiLoCo: Reducing Blocking Communication in DiLoCo.** ICML 2026 / arXiv:2606.22768.  
<https://arxiv.org/abs/2606.22768>
