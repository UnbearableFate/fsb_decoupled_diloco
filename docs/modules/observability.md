# 模块参考：observability

- `logging_utils.py`：`ActorTelemetryWriter` 通过 per-kind/actor/attempt create-no-replace claim 保证单写者 JSONL；payload 不能覆盖冻结 actor identity。
- `resource_monitor.py`：进程/设备资源采样和 step-duration summary。
- `wandb_logging.py`：可选 W&B mirror；不是 authority。
- `phase1_performance.py`：历史/性能 evidence helper。

runtime 不再向共享 CSV 多写者 append。telemetry、W&B 和兼容 CSV 分析结果可以缺失或清理；token balance、selection、publication 和 terminal 只能来自 authority/audit。稳定 docs 不记录具体 run 性能数字，正式结果位于 `reports/`。
