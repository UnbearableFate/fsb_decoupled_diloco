# TODO: 解耦 cosine scheduler 与 learner 停止上限

## 当前问题

`training.max_local_steps` 目前同时承担两种职责:

1. 在 `completion_mode=local_or_global` 时作为 learner 的本地硬停止上限;
2. 在 cosine scheduler 中作为进度分母:

   ```python
   progress = scheduler_step / max_local_steps
   ```

学习率仍然只在每个 inner/local optimizer step 后由 `scheduler.step()` 更新,但把
`max_local_steps` 调大以延迟 learner 退出也会改变 cosine 曲线。将其设为 `null` 时,
当前实现会在 warmup 后退化为恒定学习率,而不是 cosine。

此外,learner 每次采纳新 global version 都会重建 optimizer/scheduler,所以
`scheduler_step` 实际表示“自上次 global adoption 以来的本地步数”,并非累计
`local_step`。频繁采纳时 scheduler 会反复进入 warmup,且通常不会走完整个 cosine
周期。

## 后续改进方向

- 增加独立的 scheduler horizon,例如
  `inner_optimizer.scheduler_total_steps`,不再从 `training.max_local_steps` 推导。
- 明确定义 scheduler 的进度语义:累计 local step、当前 global adoption 之后的 step,
  或每个 DiLoCo inner cycle 内的 step。
- 明确定义 global adoption 时 optimizer 与 scheduler 是否分别重置;不要让
  `reset_on_global_update` 继续成为未消费配置。
- 保持停止策略由 `training.completion_mode` 与 syncer stop message 独立控制。
- 为 warmup、cosine 边界、global adoption 重置和 `max_local_steps=null` 增加单元测试。

## 验收标准

- 改变 learner 的停止上限不会隐式改变学习率曲线。
- `global_only` 与 `local_or_global` 在相同 scheduler 配置下产生相同的逐步学习率。
- resolved config、运行日志和文档能明确展示 scheduler horizon 与 reset 语义。
