# fs_diloco 系统文档

fs_diloco(Filesystem-backed Decoupled DiLoCo)是一个面向 Miyabi-G 超算的分布式训练研究原型:多个互相独立的 learner 进程本地训练 GPT 风格因果语言模型（有 CUDA 时每进程用一张卡，本地 synthetic 可回退 CPU）,一个 syncer 进程通过**共享文件系统**(而非任何网络集合通信)收集参数更新、执行 token/staleness 加权合并与外层优化器步进,并发布新的全局权重。

本目录是一组 wiki 式文档,按"先总览、后细节"组织。

## 文档索引

### 设计与流程

| 文档 | 内容 |
|---|---|
| [01-overview.md](01-overview.md) | 系统总览:是什么、设计目标与非目标、核心术语 |
| [02-architecture.md](02-architecture.md) | 详细系统设计:进程角色、通信契约、合并算法、全量/分片两种模式、容错设计 |
| [03-runtime-flow.md](03-runtime-flow.md) | 系统运行流程:初始化、learner 主循环、syncer 主循环、停机与恢复 |
| [04-data-flow.md](04-data-flow.md) | 数据流:共享目录布局、每类文件的格式与生命周期、update 状态机、SQLite schema |
| [05-code-structure.md](05-code-structure.md) | 代码结构:目录树、模块职责、模块间依赖 |
| [06-configuration.md](06-configuration.md) | 配置参考:YAML 全部配置节与字段 |
| [07-operations.md](07-operations.md) | 运行与运维:启动命令、PBS 脚本、checkpoint 评测与故障排查 |

Run 分析方法和实验结果不放在系统文档中,统一维护在 [reports/run_analysis.md](../reports/run_analysis.md)。

### 模块级参考(以函数为单位)

| 文档 | 模块 |
|---|---|
| [modules/core.md](modules/core.md) | `fs_diloco/core/` — 配置加载与共享常量 |
| [modules/storage.md](modules/storage.md) | `fs_diloco/storage/` — 原子 I/O、safetensors 编解码、路径、持久 SQLite、归档与引用驱动 GC |
| [modules/protocol.md](modules/protocol.md) | `fs_diloco/protocol/` — 合并选择、liveness、fragment 索引/编解码/调度 |
| [modules/modeling.md](modules/modeling.md) | `fs_diloco/modeling/` — 模型、数据、参数索引、外层优化器 |
| [modules/observability.md](modules/observability.md) | `fs_diloco/observability/` — JSONL 日志、CSV 指标、W&B |
| [modules/runtime-learner.md](modules/runtime-learner.md) | `fs_diloco/runtime/learner.py` + `failure_sim.py` — learner 进程 |
| [modules/runtime-syncer.md](modules/runtime-syncer.md) | `fs_diloco/runtime/syncer.py` — syncer 进程 |
| [modules/tools.md](modules/tools.md) | `fs_diloco/tools/` + `cli.py` — run 检查与 LM Eval Harness 工具 |
| [modules/scripts.md](modules/scripts.md) | `scripts/` — 本地 launcher、Miyabi PBS launcher 与独立诊断脚本 |

## 快速定位

- 想知道**两个进程之间到底传了什么文件** → [04-data-flow.md](04-data-flow.md)
- 想知道**syncer 怎么决定合并哪些 update** → [02-architecture.md](02-architecture.md) 的"合并协议"一节
- 想知道**某个 YAML 字段是干什么的** → [06-configuration.md](06-configuration.md)
- 想知道**某个函数是干什么的** → `modules/` 下对应模块的参考页
- 想**跑起来** → [07-operations.md](07-operations.md)

## 文档对应的代码版本

本组文档已按 2026-07-22 的 `master` 代码重新核对。协议事实以 `runtime/`、`storage/schema.sql` 和 `core/config.py` 为最终依据；实验结论只引用仓库中已经保留的报告证据。模块参考同时覆盖公开入口和会影响协议行为的私有 helper。
