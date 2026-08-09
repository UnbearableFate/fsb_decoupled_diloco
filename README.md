# Filesystem Decoupled DiLoCo

这是一个基于共享文件系统的 Decoupled DiLoCo 原型。当前生产路径只有 **Full Protocol v4**：learner 独立训练并发布不可变的完整参数 proposal，syncer candidate 通过 SQLite leader lease 竞争唯一写权限，在事务内完成摄取、选择、合并、发布和终态记账。

系统支持两种成员拓扑：

- static：descriptor 冻结固定 learner ID；每个新进程 attempt 都需通过 admission fence。
- dynamic：descriptor 冻结 stream pool；每个进程使用新的 instance ID，通过 bootstrap slot 或明确的 launch request 取得 stream fence。

两种拓扑都使用同一 v4 proposal、token ledger、publication 和 terminal authority。多个 syncer candidate 可以同时排队或运行，但任一时刻只有持有当前 `(epoch, owner)` lease 的 candidate 能提交业务事务。

## 目录

- `fs_diloco/core/`：严格 v4 配置、版本和不可变 run descriptor。
- `fs_diloco/protocol/`：不做 I/O 的 typed proposal、receipt、fence、selection 和 accounting 对象。
- `fs_diloco/storage/`：SQLite authority、leader lease、文件发布、admission/control adapter 和 initializer。
- `fs_diloco/runtime/`：learner/syncer v4 composition 与训练循环。
- `fs_diloco/modeling/`：模型、数据、参数索引和优化器帮助函数。
- `fs_diloco/observability/`：每 actor/attempt 单写者 JSONL telemetry 与资源观测。
- `fs_diloco/legacy/`：已完成 v1-v3/full 与 Fragment V0 run 的 query-only reader/decoder。
- `fs_diloco/baselines/`：独立 torch DDP/periodic-average baseline；不依赖 runtime learner。
- `fs_diloco/tools/`：初始化、启动、分析、评估、迁移、清理和 operator 工具。

## 快速入口

正式运行先由 initializer 创建 run，再独立提交 syncer candidate 和 learner：

```bash
python -m fs_diloco.tools.launch_independent_run \
  --config configs/fs_diloco_tiny_ha_static.yaml \
  --syncer-walltime 00:10:00 \
  --learner-walltime 00:10:00
```

Miyabi 上通常从 PBS 脚本调用相同入口；提交前按仓库 `AGENTS.md` 运行 `bash -n`、确认字面 group ID，并根据 workload 选择最短实用 walltime。不要在 login node 运行 torch/pytest/GPU workload。

旧的 classic full writer、Fragment V0 writer、旧 schema/bootstrap 和动态 proxy mutator 已从当前分支删除。它们只保存在不可变归档 tag：

- `archive/classic-full-v1-final`
- `archive/fragment-v0-final`
- 两个 tag 的完整 commit：`a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`

旧 run 只能分析/导出/评估，不能在当前代码上恢复训练。详见 [兼容与迁移](docs/08-compatibility-and-migration.md)。

## 文档

- [文档索引](docs/README.md)
- [总体设计](docs/01-overview.md)
- [架构与故障模型](docs/02-architecture.md)
- [运行流程](docs/03-runtime-flow.md)
- [数据与持久化](docs/04-data-flow.md)
- [配置](docs/06-configuration.md)
- [运维](docs/07-operations.md)
