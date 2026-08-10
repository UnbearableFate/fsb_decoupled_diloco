# 配置

## 唯一入口

完整配置类型是 `fs_diloco.core.config.Config`。公共入口只有：

- `load_config(path)`：解析并完整验证当前 schema。
- `resolve_config(path, run_id=..., shared_root=..., project_root=...)`：在完整验证前解析 run identity 和路径。
- `write_resolved_config(config, path)`：写出 canonical resolved snapshot。

unknown key、错误类型、非有限数、非法枚举、可移动 Hub revision 和不满足跨 section 约束的值都会失败。Hub model/dataset 必须用 40 位小写 commit SHA；synthetic model/data 是测试时的显式例外。local path 没有 descriptor-bound content manifest，因此统一拒绝。

## Sections

- `run`：run name、resolved ID/root 和 source identity。
- `model` / `data`：冻结的输入身份、dtype、block size 和 synthetic 尺寸。
- `sync` / `syncer`：learner 数、quorum、staleness、轮询、停止条件和 merge device/dtype。
- `membership` / `scaling`：static/dynamic scope、admission 时限、capacity window 和 PBS launch 资源。
- `terminal`：关闭策略、visibility grace、drain ack 和 terminal merge 上限。
- `training`：inner steps、micro batch、gradient accumulation、completion mode 和 seed。
- `inner_optimizer` / `outer_optimizer`：本地 optimizer/scheduler 和全局 outer optimizer。
- `io`：proposal tensor dtype。
- `learner`：global adoption 策略和等待参数。
- `leader` / `maintenance`：lease、SQLite timeout、audit retention 和 orphan grace。

## 仓库配置

- `configs/full_protocol_static.yaml`：8 learners，full quorum，50 local steps，10 global steps；9-node 正式实验。
- `configs/full_protocol_functional.yaml`：4 learners，full quorum，20 local steps，4 global steps；5-node normal/replacement/takeover harness。
- `configs/full_protocol_dynamic.yaml`：4 个 bootstrap instances 的 dynamic membership 示例，使用 `scripts/miyabi/run_learner.pbs` 扩容或替换。

static mode 不允许 scaling，也不重复声明 `stream_pool_size` 或
`bootstrap_instances`；两者在内部唯一地由 `sync.num_learners` 推导。
dynamic mode 要求
`quorum_min <= desired_contributors <= quorum_max <= stream_pool_size`，启用
scaling 时必须显式设置至少十分钟的 learner walltime。
