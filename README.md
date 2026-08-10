# Filesystem Decoupled DiLoCo

这是一个面向 Miyabi 共享文件系统的 Decoupled DiLoCo 原型。仓库只有一种可运行协议：**Full Protocol**。learner 独立训练并发布不可变的完整参数 proposal；syncer 通过 SQLite leader lease 获得唯一写权限，在事务内完成摄取、选择、合并、发布、token 记账和终态收敛。

当前实现同时支持两种 membership：

- `static`：固定 learner 身份；替换活跃进程必须先发布精确匹配旧 fence 和新 attempt 的 operator authorization。
- `dynamic`：固定虚拟 stream pool；leader 持久化 admission、capacity observation、launch reservation 和 scheduler reconciliation。

配置只有一个严格入口 `fs_diloco.core.config.Config`，并由 `load_config`、`resolve_config`、`write_resolved_config` 处理。配置结构、类型和语义均在启动前严格验证。

快速检查配置和初始化命令：

```bash
python -m fs_diloco.tools.launch_independent_run \
  --config configs/full_protocol_static.yaml \
  --run-id example \
  --shared-root /path/to/runs/example \
  --project-root "$PWD"
```

加上 `--submit --actor-queue QUEUE --syncer-walltime HH:MM:SS --learner-walltime HH:MM:SS --log-root /absolute/log/root` 才会提交独立 PBS actor；queue 和两个 walltime 都必须显式给出，walltime 至少十分钟。static 模式为每个 learner 提交一条 scalar PBS job，不使用 job array。正式 9-node 配置是 `configs/full_protocol_static.yaml`，工作量固定为每轮 50 个 local optimizer steps、10 个 committed global steps。

当前 authority schema revision 11 删除了从未写入的 epoch 终态，并继续由 contributor progress 直接持有恢复所需的最后 update 身份。revision 11 已由 PBS `2521638.opbs` 在 9 个独立节点上完成 51-local-step × 11-global-step 超基线验证：结构化 Checker 返回 `PASS`，88 个 proposal 共应用 71,808 direct tokens，全部 processed token 都有 durable fate。正式验收仍使用唯一的 50 × 10 配置。

运行根目录中的权威状态是 `control/syncer_metadata.sqlite3`。配置、descriptor、source manifest、artifact policy 和 bootstrap marker 都在初始化时不可变发布；weights、outer optimizer state、receipt、proposal、control publication 和 audit history 都通过内容哈希或 authority row 绑定。

详细文档：

- [设计与数据流](docs/design.md)
- [配置](docs/configuration.md)
- [Miyabi 运维](docs/operations.md)
- [测试与证据](docs/testing.md)
