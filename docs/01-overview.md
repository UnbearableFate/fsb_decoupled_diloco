# 01 系统总览

## fs_diloco 是什么

fs_diloco 实现了一种 **Decoupled DiLoCo**(解耦的 DiLoCo)训练协议:

- **DiLoCo**(Distributed Low-Communication training)的基本思想:每个 worker 在本地用内层优化器(AdamW)训练 H 步(`inner_steps`),然后把参数上传给协调者;协调者把"全局参数 − worker 参数均值"当作**外层伪梯度**,用外层优化器(默认 Nesterov 动量 SGD)更新全局参数,再分发回去。通信频率从每步一次降为每 H 步一次。
- **Decoupled**(解耦):learner 和 syncer 完全异步。learner 不等待彼此,也不等待 syncer;syncer 用 quorum(法定人数)+ 宽限窗口 + staleness(陈旧度)加权来容忍快慢不一、掉线和陈旧更新。
- **Filesystem-backed**(文件系统承载):进程间**唯一**的通信媒介是共享文件系统(Miyabi 的 Lustre)上的文件。没有 `torch.distributed`、NCCL、RPC、Ray、DeepSpeed、FSDP、PCCL。所有共享状态的可见性只依赖一种操作:**写临时文件 + rename 的原子发布**。

一次训练 run 的参与者:

- **N 个 learner 进程**(`fs_diloco.runtime.learner`),每个占一张 GPU,通常每节点一个;
- **1 个 syncer 进程**(`fs_diloco.runtime.syncer`),按 `syncer.device` 在 GPU 或 CPU 上做合并和外层优化器步进。

## 设计目标

1. **零网络通信依赖**:只要各节点能挂载同一个共享目录就能跑,天然适配抢占式/机会式算力。
2. **异步容错**:任何 learner 崩溃、变慢、暂停都不阻塞全局进度;syncer 通过心跳判定 learner 存活状态,通过 staleness 窗口丢弃过期更新。
3. **可审计**:每个 update 从产生到被应用/丢弃的完整生命周期都有记录(SQLite + JSONL 日志 + CSV 指标),可离线复盘。
4. **崩溃一致性**:所有共享文件用原子写发布;全量 learner 先写不可变 payload,再原子替换自己的固定 proposal pointer;syncer 以共享目录中的持久 SQLite 提交记录为恢复权威,`latest.json` 只是可重建缓存。
5. **有界运行面**:长期 run 的活跃 DB、proposal 可见面、checkpoint 和单轮 discovery 工作量不随历史版本数增长;终态记录先 fsync 到 JSONL 历史再从活跃 DB 剪枝。

## 非目标(Milestone 1 明确不做)

- 不做梯度/参数压缩(上传的是 float32/可配 dtype 的完整参数向量或分片)。
- 不做 learner 间点对点通信。
- 不做多 GPU learner(每个 learner 单卡)。
- fragment 模式暂不支持 resume(`run_fragment_syncer` 中显式 `NotImplementedError`)。

## 两种运行模式

| | 全量模式(默认) | fragment 分片模式(`fragments.enabled: true`) |
|---|---|---|
| learner 上传 | 完整可训练参数扁平向量 | 参数向量的一个分片(按 round-robin 轮转选片) |
| syncer 每次合并 | 整个参数向量,产生新的 `global version` | 单个 fragment,产生该片的新 `fragment version`;全局计数器叫 `global_merge_event` |
| 分片方式 | —— | `full`(单片)或 `balanced_tensor`(按张量贪心装箱均衡) |
| 完整 checkpoint | 每个 version 都是完整权重 | 周期性 materialize(`materialize_full_every_events`) |
| resume | 支持 | 未实现 |

分片模式的动机:把每次合并的 I/O 和计算量从"整个模型"降到"1/K 个模型",提高合并事件频率、摊薄共享文件系统带宽。

## 核心术语

| 术语 | 含义 |
|---|---|
| **inner step / local step** | learner 本地的一次优化器步进(含梯度累积)。 |
| **update** | learner 每完成 `inner_steps` 个本地步后上传的一份不可变参数 payload 与描述它的 proposal pointer/metadata。 |
| **global version** | 全量模式下 syncer 每次外层步进后的全局权重版本号,从 0 开始递增。 |
| **global merge event** | fragment 模式下的全局合并事件计数(每次合并任一 fragment 都 +1)。 |
| **fragment version** | 某个 fragment 自己的版本号(只在该片被合并时 +1)。 |
| **base version** | 某个 update 出发时 learner 加载的全局版本(或 fragment 版本),用于计算 staleness。 |
| **staleness** | `当前版本 − base 版本`,更新的陈旧度。超过 `max_staleness_versions` 的 pending 更新被丢弃。 |
| **quorum** | 一次合并需要的 update 数下限 `quorum_min` / 上限 `quorum_max`(每个 learner 至多贡献 1 份)。 |
| **grace window(宽限窗口)** | 达到 `quorum_min` 后,syncer 再等待一小段时间以收集更多 learner 的更新。`fixed` 使用固定时长;`adaptive_fastest_upload_eta` 从 `initial_seconds` 开始,并以已选 learner 中最快下一次上传的 ETA 为界只缩短、不延长;凑满 `quorum_max` 也会提前结束。 |
| **terminal drain(末端排空)** | 所有预期 learner 都明确写出 `stopped` 最终心跳后,syncer 等一个 grace/reingest 周期,在严格 future/staleness 准入下放宽 quorum、按最旧优先合并剩余 proposal;合法输入耗尽时以 `input_exhausted` 停止。 |
| **latest.json** | learner 轮询的**唯一**全局指针文件,指向最新已提交的全局权重(或各 fragment 版本)。 |
| **proposal pointer** | 全量模式中每 learner 固定的一份 `updates/latest/learner_XXX.json`;新 proposal 以原子替换覆盖旧可见面,SQLite 负责 latest-wins 摄取和生命周期。 |
| **heartbeat** | learner 周期性写入的存活信号 JSON,syncer 据此把 learner 分类为 active/stale/dead/stopped。 |
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
learner_* <──轮询 control/latest.json,发现新版本则整体加载、重置内层优化器 <──────────────┘
```

更完整的架构与流程见 [02-architecture.md](02-architecture.md) 与 [03-runtime-flow.md](03-runtime-flow.md)。
