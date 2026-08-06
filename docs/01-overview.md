# 01 系统总览

## fs_diloco 是什么

fs_diloco 实现了一种 **Decoupled DiLoCo**(解耦的 DiLoCo)训练协议:

- **DiLoCo**(Distributed Low-Communication training)的基本思想:每个 worker 在本地用内层优化器(AdamW)训练 H 步(`inner_steps`),然后把参数上传给协调者;协调者把"全局参数 − worker 参数均值"当作**外层伪梯度**,用外层优化器(默认 Nesterov 动量 SGD)更新全局参数,再分发回去。通信频率从每步一次降为每 H 步一次。
- **Decoupled**(解耦):learner 和 syncer 完全异步。learner 不等待彼此,也不等待 syncer;syncer 用 quorum(法定人数)+ 宽限窗口 + staleness(陈旧度)加权来容忍快慢不一、掉线和陈旧更新。
- **Filesystem-backed**(文件系统承载):进程间**唯一**的通信媒介是共享文件系统(Miyabi 的 Lustre)上的文件。没有 `torch.distributed`、NCCL、RPC、Ray、DeepSpeed、FSDP、PCCL。JSON 控制面与 safetensors 用“同目录临时文件 + fsync + `os.replace`”发布；SQLite 依赖事务；JSONL/CSV 是追加遥测。每 actor JSONL、syncer CSV/历史为单写者，但多个 learner 会无锁共享追加两张 learner CSV，因此 CSV 不是权威提交介质。

一次训练 run 的参与者:

- **N 个 learner 进程**(`fs_diloco.runtime.learner`),每个占一张 GPU,通常每节点一个;
- **1 个 syncer 进程**(`fs_diloco.runtime.syncer`),按 `syncer.device` 在 GPU 或 CPU 上做合并和外层优化器步进。

full + Syncer HA 还有两种成员模式。`static` 保留配置冻结的 `learner_000...` 集合；`dynamic` 则让每次进程启动生成新的 `learner_li_<uuid4>` incarnation，经 leader-fenced registration/admission 获得 placement epoch、固定 virtual stream 及 stream epoch。扩容由持久 capacity observation 和 PBS launch outbox 驱动，终止由 generation-scoped drain/ack 闭环完成。dynamic 不支持 fragment。

## 设计目标

1. **训练协调零网络通信依赖**:角色间协议只要求各节点挂载同一个共享目录,天然适配抢占式/机会式算力。真实 HF 模型/数据首次获取和可选 W&B online 上报仍可能访问外网；离线运行还需预先准备依赖与缓存。
2. **异步容错**:learner 之间不直接等待；变慢、暂停或崩溃不会占住其他 learner。只要剩余贡献者仍能满足 `quorum_min`，syncer 就可继续进展；static/fragment最终可走`no_progress_timeout`，dynamic则把该条件持久化为close原因并先完成drain/input-closed闭环。syncer通过心跳分类存活状态，通过staleness窗口丢弃过期更新。
3. **可审计**:每个被 syncer 摄取的 update 从 pending 到 applied/dropped 都有 SQLite + archive 记录；learner JSONL/CSV 提供产生侧证据。若同一固定 pointer 在 syncer 首次读取前已被下一 proposal 覆盖，旧 payload 从未入库，只会在 orphan grace 后回收，不能声称它有 DB 生命周期；共享 learner CSV 也只是无锁 best-effort 遥测。
4. **崩溃一致性**:指针和张量快照用原子替换发布;全量 learner 先写不可变 payload,再原子替换自己的固定 proposal pointer;syncer 以共享目录中的持久 SQLite 提交记录为恢复权威,`latest.json` 只是可重建缓存。原子替换保证读者不会看到半文件；helper 并不 fsync 父目录，因此不宣称断电后的目录项持久性。
5. **有界运行面**:长期 run 的活跃 DB、proposal 可见面、checkpoint 和单轮 discovery 工作量不随历史版本数增长;终态记录先 fsync 到 JSONL 历史再从活跃 DB 剪枝。

## 张量模式与成员模式

| | 全量模式(默认) | fragment 分片模式(`fragments.enabled: true`) |
|---|---|---|
| learner 上传 | 完整可训练参数扁平向量 | 参数向量的一个分片(按 round-robin 轮转选片) |
| syncer 每次合并 | 整个参数向量,产生新的 `global version` | 单个 fragment,产生该片的新 `fragment version`;全局计数器叫 `global_merge_event` |
| 分片方式 | —— | `full`(单片)或 `balanced_tensor`(按张量贪心装箱均衡) |
| 完整 checkpoint | 每个 version 都是完整权重 | 周期性 materialize(`materialize_full_every_events`) |
| resume | 支持 | 未实现 |

分片模式的动机:把每次合并的 I/O 和计算量从"整个模型"降到"1/K 个模型",提高合并事件频率、摊薄共享文件系统带宽。

成员模式与上表正交但有严格限制：legacy full/fragment 只支持 static；HA full 支持 static 或 dynamic；HA + fragment 和 dynamic + fragment 都 fail closed。dynamic 中 `sync.num_learners` 只保留 merge/config 兼容字段，成员发现和数据分片权威分别来自 membership DB 与不可变 `stream_pool_size`，不能由 CLI 在线改写。

## 核心术语

| 术语 | 含义 |
|---|---|
| **inner step / local step** | learner 本地的一次优化器步进(含梯度累积)。 |
| **update** | learner 每完成一个本地区间后上传的一份不可变参数 payload 与描述它的 proposal pointer/metadata；常规区间最多 `inner_steps`，本地上限可让最后区间更短。 |
| **global version** | 全量模式下 syncer 每次外层步进后的全局权重版本号,从 0 开始递增。 |
| **global merge event** | fragment 模式下的全局合并事件计数(每次合并任一 fragment 都 +1)。 |
| **fragment version** | 某个 fragment 自己的版本号(只在该片被合并时 +1)。 |
| **base version** | 某个 update 出发时 learner 加载的全局版本(或 fragment 版本),用于计算 staleness。 |
| **staleness** | `当前版本 − base 版本`,更新的陈旧度。超过 `max_staleness_versions` 的 pending 更新被丢弃。 |
| **quorum** | 一次合并需要的 update 数下限 `quorum_min` / 上限 `quorum_max`(每个 learner 至多贡献 1 份)。 |
| **grace window(宽限窗口)** | 达到 `quorum_min` 后,syncer 再等待一小段时间以收集更多 learner 的更新。`fixed` 使用固定时长;`adaptive_fastest_upload_eta` 从 `initial_seconds` 开始,并以已选 learner 中最快下一次上传的 ETA 为界只缩短、不延长;凑满 `quorum_max` 也会提前结束。 |
| **terminal drain(末端排空)** | 所有预期 learner 都明确写出 `stopped` 最终心跳后,syncer 等一个 grace/reingest 周期,在严格 future/staleness 准入下放宽 quorum、按配置的选择策略合并剩余 proposal;合法输入耗尽时以 `input_exhausted` 停止。full/fragment 两条主循环都覆盖。 |
| **dynamic drain** | leader关闭admission、冻结`max_terminal_version`并发布close generation；global target、token target、manual/deadline/budget和no-progress都进入该持久状态机。current healthy instance在cycle边界提交final pointer和ack，dead instance经超时撤销。只有request/registration可见性和全部ack/revoke条件同时满足，dynamic input才闭合，正常terminal才可发布。 |
| **latest.json / canonical head** | legacy learner轮询 fixed `latest.json`；HA learner从最高合法 syncer epoch读取 canonical head并校验 immutable pointer SHA。HA fixed cache只是可修复便利面。 |
| **proposal pointer** | 全量模式每 learner 一份 `updates/latest/learner_XXX.json`；fragment 模式每 `(learner, fragment)` 一份 `learner_XXX_fNNN.json`。新 proposal 原子覆盖固定可见面，SQLite frontier 负责重放抑制和生命周期。 |
| **heartbeat** | learner 周期性写入的存活信号 JSON,syncer 据此把 learner 分类为 active/stale/dead/stopped。 |
| **instance / placement / stream** | dynamic进程 incarnation、物理 host+GPU位置和固定数据/RNG虚拟分片是三个独立身份；各自由 instance UUID、placement epoch和 stream epoch fencing。 |
| **param index** | 参数索引:把 `model.named_parameters()` 中可训练参数按声明顺序映射到扁平向量的偏移区间,是所有"模型 ↔ 扁平向量"转换的契约。 |
| **fragment index** | 分片索引:把扁平向量划分为若干不重叠、完全覆盖的分片,每片由若干参数切片组成。 |

## 一分钟看懂数据环路

```
        ┌──────────────────────── 共享文件系统(一个 run 一个目录)────────────────────────┐
        │                                                                                  │
learner_000 ──写──> updates/payloads/learner_000/*.safetensors → updates/latest/learner_000.json │
learner_001 ──写──> updates/payloads/learner_001/...       → updates/latest/learner_001.json │
   ...      ──写──> heartbeats/learner_*.json                                              │
        │                                              │                                   │
        │                                              ▼                                   │
        │                          syncer:读取固定 pointers → 共享 SQLite 入库 → 选择/加权合并 │
        │                                → 外层优化器步进 → 发布 weights/global_v*.safetensors │
        │                                → 原子更新 control/latest.json                    │
        │                                              │                                   │
learner_* <──轮询 control/latest.json,按 replace/rebase/predict 或增量 fragment 策略采纳 <────┘
```

更完整的架构与流程见 [02-architecture.md](02-architecture.md) 与 [03-runtime-flow.md](03-runtime-flow.md)。
