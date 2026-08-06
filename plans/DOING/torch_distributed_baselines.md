# Torch Distributed Baselines

## 目标

在独立分支 `codex/torch_ddp_baselines` 上实现两个不依赖 filesystem
learner/syncer 的 PyTorch 分布式基线：标准 DDP，以及每 100 个 optimizer
steps 进行一次 BF16 参数平均、但保留各 rank AdamW/scheduler 状态的 periodic
average。两个正式实验共享 GPT-2、WikiText-2 和其余训练超参数，各使用 8 个节点、
每节点 1 GPU，运行 5000 optimizer steps。

## phase: implementation-and-formal-validation

- 新增 `fs_diloco.baselines.train` 与 console entry point。
- 复用模型、数据、AdamW/cosine scheduler 和 source identity。
- 记录 per-rank metrics/logs/heartbeats、rank-0 synchronization metrics、manifest、
  resolved config、source identity、summary 和 5000-step final checkpoint。
- 新增只读健康检查器，验收 8 rank/host、NCCL/CUDA、有限且下降的 loss、DDP
  step 同步或 periodic steps 100/200 参数平均，以及 PBS job 存活状态。
- 新增共享正式配置与两个 8-node PBS launcher。
- 依次完成静态、单元、1-node、2-node、双模型审查和正式 8-node 验证；正式作业
  达到 step 200 并 PASS 后继续运行至 step 5000，不主动终止。

## 正式验收

两个作业均须在所有 8 rank 达到至少 step 200 后通过健康检查；DDP 有 200 次
optimizer-step gradient synchronization，periodic average 在 steps 100/200 完成两次
全 rank 参数平均。steps 151–200 的 8-rank mean loss 必须严格低于 steps 1–50，
且作业仍正常运行，或已完成全部 5000 steps 并以 0 退出。
