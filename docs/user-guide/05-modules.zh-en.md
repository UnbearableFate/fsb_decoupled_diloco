# 模块设计 / Module Design

[返回总览 / Back to index](00-README.zh-en.md)

中文：按模块列出 Python package 中每个组件的职责边界。

English: Responsibilities and boundaries of each Python package module.

---

# 中文

## 模块设计

| 模块 | 职责 |
| --- | --- |
| `fs_diloco/config.py` | YAML 配置 dataclass、默认值、CLI override 合并、resolved config 写出。 |
| `fs_diloco/paths.py` | 所有 run directory 路径约定和目录创建。 |
| `fs_diloco/atomic_io.py` | 原子 JSON/text/tensor 文件发布辅助函数。 |
| `fs_diloco/param_index.py` | deterministic 参数索引、flatten、load flat into model、named tensor/flat 转换。 |
| `fs_diloco/tensor_codec.py` | `safetensors` 读写、update/global/outer state 编码。 |
| `fs_diloco/outer_optim.py` | flat-vector SGD/momentum/Nesterov/AdamW-style outer optimizer。 |
| `fs_diloco/merge.py` | staleness 权重、one-update-per-learner 选择、weighted tensor average。 |
| `fs_diloco/sqlite_store.py` | SQLite 连接、schema 初始化、update/learner/global version 状态转换、DB backup。 |
| `fs_diloco/liveness.py` | heartbeat 验证、learner active/stale/dead/stopped 状态转换、no-progress timeout 判断。 |
| `fs_diloco/hf_model.py` | Hugging Face causal LM 加载和 synthetic tiny model。 |
| `fs_diloco/hf_data.py` | WikiText-2 或 synthetic data batch iterator，learner shard。 |
| `fs_diloco/learner.py` | learner CLI 与训练循环。 |
| `fs_diloco/syncer.py` | syncer CLI、初始化/resume、metadata ingest、merge、publish、stop。 |
| `fs_diloco/metrics.py` | metrics CSV 写入和字段定义。 |
| `fs_diloco/failure_sim.py` | jitter、skip upload、intentional crash 等 failure simulation。 |
| `fs_diloco/analysis.py` | run inspection CLI，读取 latest/stop/metrics/DB dump。 |

---

# English

## Module Design

| Module | Responsibility |
| --- | --- |
| `config.py` | YAML config dataclasses, defaults, CLI overrides. |
| `paths.py` | Run directory paths and layout creation. |
| `atomic_io.py` | Atomic JSON/text/tensor publication helpers. |
| `param_index.py` | Deterministic parameter index and flat-vector conversion. |
| `tensor_codec.py` | `safetensors` encoding for updates, globals, and outer state. |
| `outer_optim.py` | Explicit flat-vector SGD, momentum, Nesterov, and AdamW-style optimizers. |
| `merge.py` | Staleness weights, selection, and weighted tensor averaging. |
| `sqlite_store.py` | SQLite schema, status transitions, and DB backups. |
| `liveness.py` | Heartbeat validation and active/stale/dead/stopped transitions. |
| `hf_model.py` | HF causal LM loading and synthetic tiny model. |
| `hf_data.py` | WikiText-2 and synthetic batch iterators. |
| `learner.py` | Learner CLI and training loop. |
| `syncer.py` | Syncer CLI, run init/resume, ingest, merge, publish, and stop. |
| `metrics.py` | CSV metrics helpers. |
| `failure_sim.py` | Jitter, skipped uploads, and intentional crashes. |
| `analysis.py` | Run inspection CLI. |
