# Filesystem Decoupled DiLoCo

这是一个面向 Miyabi 共享文件系统的 Decoupled DiLoCo 原型。仓库只有一种可运行协议：**Full Protocol**。Learner 独立训练并发布不可变的完整参数 proposal；Syncer 通过 SQLite leader lease 获得唯一写权限，在事务内完成摄取、选择、合并、发布、token 记账和终态处理。

成员协议使用固定 stream pool。每个 Learner 进程必须先绑定 bootstrap slot 或已授权的 launch request，再取得包含 instance、placement、stream 和 admission generation 的 `ContributorFence`。`scaling.enabled=false` 保持固定容量；`scaling.enabled=true` 启用同一成员协议上的自动 replacement 和 scale-out。两种容量策略不会改变身份、admission 或 proposal 格式。

配置只有一个严格入口 `fs_diloco.core.config.Config`，并由 `load_config`、`resolve_config` 和 `write_resolved_config` 处理。启动前会严格验证配置结构、类型和跨字段约束。

初始化命令如下：

```bash
python -m fs_diloco.tools.launch_independent_run \
  --config configs/full_protocol.yaml \
  --run-id example \
  --shared-root /path/to/runs/example \
  --project-root "$PWD"
```

只有添加 `--submit --actor-queue QUEUE --syncer-walltime HH:MM:SS --learner-walltime HH:MM:SS --log-root /absolute/log/root` 才会提交独立 PBS actor。队列和两个 walltime 必须显式提供，walltime 不得少于 10 分钟。Launcher 为每个 bootstrap slot 提交一条 scalar PBS job，不使用 job array。`configs/full_protocol.yaml` 定义 8 个 Learner、每轮 50 个 local optimizer step 和 10 个 committed global step。

运行根目录中的权威状态位于 `control/syncer_metadata.sqlite3`。配置、descriptor、source manifest、artifact policy 和 bootstrap marker 在初始化时不可变发布。Weights、outer optimizer state、receipt、proposal、control publication 和 audit history 通过内容哈希或 authority row 绑定。

网页文档源文件位于 [website](website/README.md)，包含 Overview、Getting Started、Concepts、User Guide、Architecture、Reference，并为 Experiments 保留独立章节。
