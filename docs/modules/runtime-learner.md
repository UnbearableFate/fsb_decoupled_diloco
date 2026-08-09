# 模块参考：learner runtime

## `learner_entrypoint.py`

入口在 torch/CUDA 前完成：

1. immutable descriptor/config/source validation；
2. static logical launch/attempt 或 dynamic instance/stream request publication；
3. highest live epoch、current pointer、request digest、response和 contributor fence validation；
4. admission 的立即重验证。

等待/拒绝路径不会 import torch。static active replacement 需要 operator authorization；dynamic process 每次使用 fresh instance ID。

## `learner_v4.py`

admitted learner 恢复 authority 提供的 `ContributorResumeState`，按 deterministic indexed cursor 读取数据。`TrainingSegmentAccumulator` 在 replace/rebase/stop 边界结算有效、discarded 和 carried work。cycle 结束发布 immutable tensor、typed proposal（若有）和 mandatory receipt。

learner 随后等待 exact receipt acknowledgement、drain 或 terminal，防止 authority ingest 无限落后。global adoption 只接受 current epoch latest chain；learner 不读 SQLite。current latest 达到配置的 global target 后，learner 进入 target-aware await-close：停止消费数据和训练，但保持进程存活，直到 leader 发布 drain，再提交 exact final cycle/update ack。hard crash 由 authority 记上界而不是伪造 stopped heartbeat。

## `adoption.py`

保留三种 full-model策略：`replace`、`rebase_post_publish_delta`、`predict_post_publish_global`。rebase/predict 保留未发布局部工作并与 token segment accounting 对齐；predict 的 outer optimizer/timeout 约束由 config validation 强制。

没有 fragment learner、fixed latest/stop branch 或 runtime failure injection。
