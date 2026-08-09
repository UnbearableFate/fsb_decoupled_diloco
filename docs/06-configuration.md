# 配置

## Strict v4 envelope

正式 full runtime 必须通过 `load_config_v4(..., profile=full_v4)`。配置包含共享 `Config` 字段，以及：

```yaml
config_schema_version: 1
coordination:
  leader: {}
maintenance: {}
sync:
  stop_after_outer_steps: 20
  # 或 stop_after_direct_weight_tokens_applied: <positive integer>
```

`training.completion_mode: global_only` 必须在完整 v4 envelope 中有 outer-step 或 direct-weight-token target。共享 `Config` projection 不独立猜测 envelope-only target。

## 关键字段

- `run`：name、run ID、shared root 模板、source/git identity。
- `model`：model/tokenizer revision、dtype、compile、synthetic shape。
- `data`：dataset/revision/split/block size/indexed shuffle。v4 full 拒绝 `streaming: true`，因为它不能精确恢复 cursor。
- `training`：inner steps、micro batch、gradient accumulation、precision、seed、local completion。
- `inner_optimizer` / `outer_optimizer`：local AdamW/scheduler 与 global outer step。
- `sync`：learner/quorum、staleness、scan、grace、stop target。
- `syncer`：device、compute/publish dtype 和 checkpoint write policy。
- `learner`：global adoption strategy 与 prediction timing。
- `membership`：`static|dynamic`、stream pool、bootstrap count、admission/heartbeat timeouts。
- `scaling`：dynamic scheduler policy/budgets。连续低容量、productive/startup grace、cooldown、pending/total budget、launch TTL、observation retention、reconcile/uncertainty timeout 和 learner PBS script/walltime/queue 都由 runtime service 使用；启用时 walltime 必须是显式合法且不少于 `00:10:00` 的 `HH:MM:SS`。
- `terminal`：`global_target_or_launch_budget|global_target|deadline|manual` close policy、deadline、pre-close registration visibility、drain ack/proposal visibility grace 和 bounded terminal merge。
- `liveness`：heartbeat/stale/dead/no-progress timeouts。
- `coordination.leader`：lease、renew、clock skew、busy timeout、candidate/recovery wait。
- `maintenance`：audit/visibility/quarantine retention 与 publication orphan grace。
- `torch_baseline`：独立 baseline profile；不能用 full_v4 profile 伪装。

所有 numeric identity/timeout 都拒绝 bool、NaN 和 infinity。leader lease 至少覆盖 renew/heartbeat/clock-skew 约束；publication orphan grace 至少覆盖 lease 加两倍 clock skew。

## 已删除字段

当前 loader fail closed 拒绝：

- `init`
- `fragments`
- `failure_sim`
- `coordination.syncer_ha`
- `coordination.recovery_submission`
- `sync.stop_after_global_tokens`
- `sync.capture_terminal_predecessor_for_eval`（旧 classic partial-terminal writer）
- `sync.upload_mode`
- `liveness.quorum_policy`
- `inner_optimizer.reset_on_global_update`
- `learner.prediction_reconcile_timeout_seconds`（新路径是 `learner.prediction.reconcile_timeout_seconds`）
- `syncer.parallel_checkpoint_writes`

旧 config 只能由 `legacy.load_query_config_snapshot` 在分析/导出/评估工具中投影；production loader 不会静默丢弃旧字段。本次配置收敛之前的 v4 resolved snapshot 中若仅多出已删除的 `syncer.parallel_checkpoint_writes`，query loader 会精确移除该字段以支持只读评估；production/resume loader 仍以“字段已移除”拒绝它。

## Migration

`tools.migrate_config_v3_to_v4` 默认 dry-run。输出路径 create-no-replace；repository-owned in-place migration 同时要求 `--in-place` 和原文件 `--expected-sha256`，publication 前再次核对 source identity。Fragment config 或语义不明确的旧 token stop 不自动迁移。

旧 in-progress run state 不迁移。迁移配置只用于创建 fresh v4 attempt；不要把 v4 resolved config 写回旧 run root。

authority schema 9 保留 schema 8 的 durable preclose cutoff、跨 successor terminal deadlines 和 terminal merge accounting，并增加 online dependency-closed audit batching/partition compaction、durable command receipts 和 identity-checked artifact/audit GC。contributor progress 不再以外键永久阻止已归档 receipt history 的精确 prune；controller 未 terminal 时其 current receipt 及依赖仍保留在 hot authority，terminal acknowledgement 完成并固化后才可归档，cursor/hash chain 始终由 progress row 持久化。当前没有 in-place v4 schema 6/7/8→9 migration；旧 completed v4 evidence 保持原提交只读，新的执行必须初始化 fresh schema 9 run。
