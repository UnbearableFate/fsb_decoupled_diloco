# 系统概览 / System Overview

[返回总览 / Back to index](00-README.zh-en.md)

中文：介绍项目定位、核心角色、共享目录和当前里程碑边界。

English: Project scope, roles, shared-root behavior, and milestone boundaries.

---

# 中文

## 项目定位

本项目实现一个面向 Miyabi-G 的文件系统版 Decoupled DiLoCo 研究原型。目标是在不依赖 RPC、NCCL collectives、`torch.distributed`、Ray、DeepSpeed、FSDP 或 PCCL 的情况下，验证异步低通信训练的控制流、可观测性和容错语义。

Milestone 1 的核心约束如下：

- 模型类型：GPT 风格 causal language model，默认 `gpt2`。
- 数据集：默认 WikiText-2，配置为 `wikitext` / `wikitext-2-raw-v1`。
- 节点形态：8 个 learner 节点，每个 learner 单进程、单 GPU；1 个 syncer 节点，使用本地 GPU 做聚合、pseudo-gradient 和 outer optimizer step。
- 通信模拟：大张量通过共享文件系统上的 `safetensors` 文件传输；控制状态通过 syncer 本地 SQLite 管理；learner 用 JSON metadata 文件作为提交标记；syncer 通过 `control/latest.json` 发布最新全局版本。
- 上传粒度：Milestone 1 使用完整可训练参数向量作为单个逻辑 fragment，即 `fragment_id = 0`。
- learner 采用新全局版本时必须覆盖完整模型参数，并重置 inner optimizer 和 scheduler。

这个系统不是高性能参数服务器实现。它的价值在于把 Decoupled DiLoCo 的异步训练协议拆成可检查、可复现、可调试的文件系统步骤，为后续 fragment 化、RPC 或真正通信后端打基础。

## 核心概念

### Learner

Learner 是实际训练进程。每个 learner：

1. 加载模型、tokenizer 和数据分片。
2. 等待 syncer 发布 `param_index.json` 和初始 `latest.json`。
3. 加载当前全局权重。
4. 初始化 inner optimizer。
5. 执行若干 `training.inner_steps` 本地训练 step。
6. 将完整 trainable parameter vector 写为 `update_*.params.safetensors`。
7. 再写 `update_*.meta.json` 作为提交标记。
8. 轮询 `control/latest.json`。
9. 如果发现新全局版本，覆盖完整模型参数并重置 inner optimizer。
10. 看到 `control/stop.json` 或达到 `training.max_local_steps` 后停止。

### Syncer

Syncer 是中心同步进程。它通常运行在 rank 0 节点，并通过 `CUDA_VISIBLE_DEVICES=${SYNCER_CUDA_VISIBLE_DEVICES:-0}` 使用本地 GPU。Syncer：

1. 初始化共享 run 目录。
2. 建立 syncer-local SQLite DB。
3. 初始化全局模型向量 `theta` 和 outer optimizer state。
4. 发布 `weights/global_v000000.safetensors`、`optim/outer_v000000.safetensors` 和 `control/latest.json`。
5. 读取 learner heartbeat 与 update metadata。
6. 将合法 metadata ingest 到 SQLite。
7. 按 quorum、grace window、staleness 和 one-update-per-learner 策略选择更新。
8. 从 `safetensors` 加载 learner 参数向量。
9. 计算 token/staleness 加权平均。
10. 生成 outer pseudo-gradient 并执行 outer optimizer step。
11. 发布新的全局权重和 optimizer state。
12. 将已选 update 标为 applied，过期或被替代的 update 标为 dropped。
13. 周期性将 SQLite 备份到共享文件系统。
14. 将 selected update loss、token、staleness 和耗时指标写入自动命名的 W&B run。
15. 满足停止条件后写 `control/stop.json`。

### Shared Root

每次运行都有一个共享根目录，默认：

```text
runs/fs_diloco/<RUN_ID>/
```

所有跨进程可见的文件都在这里。SQLite 主库默认不在这里，而在 syncer 节点本地临时目录；只有一致性备份会复制到 `db_dumps/`。

---

# English

## Project Scope

This repository implements a filesystem-backed Decoupled DiLoCo research prototype for Miyabi-G. The goal is to validate asynchronous low-communication training semantics, control flow, observability, and recovery behavior without using RPC, NCCL collectives, `torch.distributed`, Ray, DeepSpeed, FSDP, or PCCL.

Milestone 1 has these constraints:

- Model family: GPT-style causal language modeling, default `gpt2`.
- Dataset: default WikiText-2, configured as `wikitext` / `wikitext-2-raw-v1`.
- Runtime shape: 8 learner nodes with one learner process and one GPU each, plus 1 syncer process that uses a local GPU for aggregation, pseudo-gradient computation, and outer optimizer updates.
- Communication simulation: large tensors are exchanged as `safetensors` files on the shared filesystem; control state is stored in syncer-local SQLite; learner metadata JSON files are commit markers; syncer publishes global versions through `control/latest.json`.
- Upload granularity: a full trainable parameter vector is treated as a single logical fragment, `fragment_id = 0`.
- When learners adopt a newer global version, they overwrite the full model and reset the inner optimizer.

This is not a high-performance parameter server. It is a debuggable protocol prototype that makes each Decoupled DiLoCo step inspectable before adding fragments, RPC, or a production communication backend.

## Main Concepts

### Learner

A learner is a training process. It:

1. Loads the model, tokenizer, and a dataset shard.
2. Waits for `param_index.json` and initial `latest.json`.
3. Loads the current global weights.
4. Builds the inner optimizer.
5. Runs `training.inner_steps` local optimization steps.
6. Writes the full trainable parameter vector to `update_*.params.safetensors`.
7. Writes `update_*.meta.json` as the commit marker.
8. Polls `control/latest.json`.
9. If a newer global version exists, overwrites the full model and resets the inner optimizer.
10. Stops when `control/stop.json` appears or `training.max_local_steps` is reached.

### Syncer

The syncer is the central coordination process. It usually runs on rank 0 with `CUDA_VISIBLE_DEVICES=${SYNCER_CUDA_VISIBLE_DEVICES:-0}`. It:

1. Creates the shared run layout.
2. Opens a syncer-local SQLite database.
3. Initializes the global parameter vector and outer optimizer state.
4. Publishes `global_v000000.safetensors`, `outer_v000000.safetensors`, and `latest.json`.
5. Reads learner heartbeats and update metadata.
6. Ingests valid metadata into SQLite.
7. Selects updates using quorum, grace-window, staleness, and one-update-per-learner rules.
8. Loads selected learner parameter vectors.
9. Computes a token/staleness-weighted average.
10. Applies an outer optimizer step.
11. Publishes the next global weights and optimizer state.
12. Marks selected updates as applied and obsolete updates as dropped.
13. Dumps SQLite backups to the shared filesystem.
14. Logs selected-update loss, token, staleness, and timing metrics to an automatically named W&B run.
15. Writes `control/stop.json` when a stop condition is met.

### Shared Root

Each run has one shared root directory, by default:

```text
runs/fs_diloco/<RUN_ID>/
```

All files that must be visible across processes are stored there. The primary SQLite database is syncer-local by default; only consistent backups are copied into `db_dumps/`.
