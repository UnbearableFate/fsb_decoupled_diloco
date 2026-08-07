# 01 系统总览

> 本文是入门读物。遇到不认识的术语请查 [00-glossary.md](00-glossary.md) 术语表。

## fs_diloco 是什么

fs_diloco 实现了一种 **Decoupled DiLoCo**(解耦的 DiLoCo)训练协议。DiLoCo 全称 Distributed Low-Communication training(分布式低通信训练),核心思想是:

- 每个 worker(本项目称为 **learner**)在本地用内层优化器(AdamW)连续训练 H 步(`inner_steps`),再把参数上传给协调者;
- 协调者(**syncer**)把「全局参数 − worker 参数均值」当作**外层伪梯度**,用外层优化器(默认 Nesterov 动量 SGD)更新全局参数,再分发回去;
- 通信频率从每步一次降为每 H 步一次。

本实现在此基础上做了两个关键选择:

- **Decoupled(解耦)**:learner 与 syncer 完全异步。learner 不等待彼此,也不等待 syncer;syncer 用 quorum(法定人数,见下)、宽限窗口和 staleness(陈旧度)加权来容忍快慢不一、掉线和陈旧更新。
- **Filesystem-backed(文件系统承载)**:进程之间**唯一**的通信媒介是共享文件系统(Miyabi 的 Lustre)上的文件。没有 `torch.distributed`、NCCL、RPC、Ray、DeepSpeed、FSDP、PCCL。具体做法是:JSON 控制面和 safetensors 用「同目录临时文件 + fsync + `os.replace`」发布;SQLite 依赖事务;JSONL/CSV 是追加式遥测。每个 actor 的 JSONL、syncer 的 CSV/历史文件是单写者,但多个 learner 会无锁共享追加两张 learner CSV,因此 CSV 不是权威提交介质。

一次训练 run 的参与者:

- **N 个 learner 进程**(`fs_diloco.runtime.learner`),每个占一张 GPU,通常每节点一个;
- **1 个 syncer 进程**(`fs_diloco.runtime.syncer`),按 `syncer.device` 在 GPU 或 CPU 上做合并和外层优化器步进。

除上述全量(full)模式外,还有两种正交的模式维度(详见 [02-architecture.md](02-architecture.md)):

- **分片(fragment)模式**:把参数向量切成 K 片,每次只合并一片,把单次合并的 I/O 和计算量降到 1/K;
- **Syncer HA 与动态成员**:HA 让 syncer 崩溃后可接管恢复;`static` 成员保留配置冻结的 `learner_000...` 集合,`dynamic` 则让每次进程启动生成新的 `learner_li_<uuid4>` 化身,经带隔离(leader-fenced)的注册/准入获得部署位置纪元、固定数据流及数据流纪元。扩容由持久化的容量观测和 PBS 启动发件箱驱动,终止由按世代闭合的排空/确认闭环完成。dynamic 不支持 fragment。

## 设计目标

1. **训练协调零网络通信依赖**:角色间协议只要求各节点挂载同一个共享目录,天然适配抢占式/机会式算力。真实 HF 模型/数据首次获取和可选 W&B 在线上报仍可能访问外网;离线运行还需预先准备依赖与缓存。
2. **异步容错**:learner 之间不直接等待;变慢、暂停或崩溃不会占住其他 learner。只要剩余贡献者仍能满足 `quorum_min`,syncer 就可以继续进展;static/fragment 最终可走 `no_progress_timeout` 停机,dynamic 则把该条件持久化为关闭原因并先完成排空/输入闭合闭环。syncer 通过心跳分类存活状态,通过陈旧度窗口丢弃过期更新。
3. **可审计**:每份被 syncer 摄取的更新从待处理(pending)到已应用/已丢弃(applied/dropped)都有 SQLite + 归档记录;learner 的 JSONL/CSV 提供产生侧证据。若同一固定指针在 syncer 首次读取前已被下一份提议覆盖,旧载荷从未入库,只会在孤儿宽限期后回收,不能声称它有数据库生命周期;共享 learner CSV 也只是无锁的尽力而为(best-effort)遥测。
4. **崩溃一致性**:指针和张量快照用原子替换发布;全量 learner 先写不可变载荷,再原子替换自己的固定提议指针;syncer 以共享目录中的持久 SQLite 提交记录为恢复权威,`latest.json` 只是可重建缓存。原子替换保证读者不会看到半文件;helper 并不 fsync 父目录,因此不宣称断电后的目录项持久性。
5. **有界运行面**:长期 run 的活跃数据库、提议可见面、checkpoint 和单轮发现工作量不随历史版本数增长;终态记录先 fsync 到 JSONL 历史再从活跃数据库剪枝。

## 张量模式与成员模式

### 张量模式:全量 vs 分片

| | 全量模式(默认) | 分片模式(`fragments.enabled: true`) |
|---|---|---|
| learner 上传 | 完整可训练参数扁平向量 | 参数向量的一个分片(按轮转 round-robin 选片) |
| syncer 每次合并 | 整个参数向量,产生新的 `global version`(全局版本号) | 单个分片,产生该片新的 `fragment version`(分片版本号);全局计数器叫 `global_merge_event`(全局合并事件号) |
| 分片方式 | —— | `full`(单片)或 `balanced_tensor`(按张量贪心装箱均衡) |
| 完整 checkpoint | 每个版本都是完整权重 | 周期性物化(`materialize_full_every_events`) |
| 恢复(resume) | 支持 | 未实现 |

分片模式的动机:把每次合并的 I/O 和计算量从「整个模型」降到「1/K 个模型」,提高合并事件频率、摊薄共享文件系统带宽。

### 成员模式:static vs dynamic

成员模式与张量模式正交但有严格限制:legacy full/fragment 只支持 static;HA full 支持 static 或 dynamic;HA + fragment 和 dynamic + fragment 都失败即关闭(fail-closed)。dynamic 中 `sync.num_learners` 只保留合并/配置兼容字段,成员发现和数据分片权威分别来自成员数据库与不可变 `stream_pool_size`,不能由命令行在线改写。

## 核心术语

| 术语 | 含义 |
|---|---|
| **inner step / local step**(内层步/本地步) | learner 本地的一次优化器步进(含梯度累积)。 |
| **update**(更新) | learner 每完成一个本地区间后上传的一份不可变参数载荷与描述它的提议指针/元数据;常规区间最多 `inner_steps` 步,本地上限可让最后区间更短。 |
| **global version**(全局版本号) | 全量模式下 syncer 每次外层步进后的全局权重版本号,从 0 开始递增。 |
| **global merge event**(全局合并事件号) | 分片模式下的全局合并事件计数(每次合并任一碎片都 +1)。 |
| **fragment version**(分片版本号) | 某个分片自己的版本号(只在该片被合并时 +1)。 |
| **base version**(基准版本) | 某份更新出发时 learner 加载的全局版本(或分片版本),用于计算陈旧度。 |
| **staleness**(陈旧度) | `当前版本 − 基准版本`。超过 `max_staleness_versions` 的待处理更新被丢弃。 |
| **quorum**(法定人数) | 一次合并需要的更新数下限 `quorum_min` / 上限 `quorum_max`(每个 learner 至多贡献 1 份)。 |
| **grace window**(宽限窗口) | 达到 `quorum_min` 后,syncer 再等待一小段时间以收集更多 learner 的更新。`fixed` 使用固定时长;`adaptive_fastest_upload_eta` 从 `initial_seconds` 开始,并以已选 learner 中最快下一次上传的预计时间(ETA)为界只缩短、不延长;凑满 `quorum_max` 也会提前结束。 |
| **terminal drain**(末端排空) | 所有预期 learner 都明确写出 `stopped` 最终心跳后,syncer 等一个宽限/重新摄取周期,在严格 future/staleness 准入下放宽法定人数、按配置的选择策略合并剩余提议;合法输入耗尽时以 `input_exhausted` 停止。full/fragment 两条主循环都覆盖。 |
| **dynamic drain**(动态排空) | leader 关闭准入、冻结 `max_terminal_version` 并发布关闭世代;全局目标、token 目标、手动/截止时间/预算和 no-progress 都进入该持久状态机。当前健康实例在周期边界提交最终指针和确认(ack),死亡实例经超时撤销(revoke)。只有请求/注册可见性和全部确认/撤销条件同时满足,dynamic 输入才闭合,正常终态才可发布。 |
| **latest.json / canonical head**(全局指针文件/权威头部) | legacy learner 轮询固定 `latest.json`;HA learner 从最高合法 syncer epoch 读取权威头部并校验不可变指针 SHA。HA fixed cache 只是可修复的便利面。 |
| **proposal pointer**(提议指针) | 全量模式每 learner 一份 `updates/latest/learner_XXX.json`;分片模式每 `(learner, fragment)` 一份 `learner_XXX_fNNN.json`。新提议原子覆盖固定可见面,SQLite 摄取水位(frontier)负责重放抑制和生命周期。 |
| **heartbeat**(心跳) | learner 周期性写入的存活信号 JSON,syncer 据此把 learner 分类为 active/stale/dead/stopped。 |
| **instance / placement / stream**(实例/部署位置/数据流) | dynamic 的进程化身、物理 host+GPU 位置和固定数据/RNG 虚拟分片是三个独立身份;各自由实例 UUID、部署位置纪元和数据流纪元隔离。 |
| **param index**(参数索引) | 把 `model.named_parameters()` 中可训练参数按声明顺序映射到扁平向量的偏移区间,是所有「模型 ↔ 扁平向量」转换的契约。 |
| **fragment index**(分片索引) | 把扁平向量划分为若干不重叠、完全覆盖的分片,每片由若干参数切片组成。 |

## 一分钟看懂数据环路

下面的方框代表共享文件系统(一个 run 一个目录),箭头是文件写入方向:

```
        ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
        │                                                                                                                                         │
        │  learner_000 ──写──> updates/payloads/… 与 updates/latest/learner_000.json   │                                                        │
        │  learner_001 ──写──> updates/payloads/… 与 updates/latest/learner_001.json   │                                                        │
        │     ...      ──写──> heartbeats/learner_*.json                               │                                                        │
        │                                                                               │                                                        │
        │                                                                               │  syncer:读取固定指针 → SQLite 入库 → 选择/加权合并     │
        │                                                                               │  → 外层优化器步进 → 发布 weights/global_v*.safetensors │
        │                                                                               │  → 原子更新 control/latest.json                        │
        │                                                                               ▼                                                        │
          learner_* <──轮询 control/latest.json → 按策略采纳(替换/变基/预测/增量分片)─┘
        │                                                                               │                                                        │
        │                                                                                                                                         │
        └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

更完整的架构与流程见 [02-architecture.md](02-architecture.md) 与 [03-runtime-flow.md](03-runtime-flow.md)。
