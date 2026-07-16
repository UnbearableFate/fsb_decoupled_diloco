# 存储布局与元数据 / Storage Layout and Metadata

[返回总览 / Back to index](00-README.zh-en.md)

中文：说明 run 目录、原子发布、param index、tensor 文件和 SQLite 数据模型。

English: Run layout, atomic publication, param index, tensor files, and SQLite data model.

---

# 中文

## 目录和文件格式

典型 run layout：

```text
runs/fs_diloco/<RUN_ID>/
  control/
    latest.json
    stop.json
    run_config.resolved.yaml
    param_index.json
  weights/
    global_v000000.safetensors
    global_v000001.safetensors
  optim/
    outer_v000000.safetensors
    outer_v000001.safetensors
  updates/
    pending/learner_000/update_<uuid>.params.safetensors
    pending/learner_000/update_<uuid>.meta.json
  heartbeats/
    learner_000.json
  db_dumps/
    metadata_<timestamp>_v000001.db
  logs/
    syncer.jsonl
    learner_000.jsonl
  metrics/
    syncer_metrics.csv
    learner_metrics.csv
    update_manifest.csv
```

### Atomic publication

所有关键 JSON 和 tensor 写入都走临时文件 + `os.replace()`。同目录 rename 是原子操作。重要语义：

- `.params.safetensors` 只是大张量；
- `.meta.json` 是 commit marker；
- syncer 忽略没有最终 `.meta.json` 的 tensor；
- learner 只轮询 `control/latest.json`，不扫描 `weights/`。

### `param_index.json`

`param_index.json` 定义 flatten 顺序和 shape：

```json
{
  "format_version": 1,
  "model_name_or_path": "gpt2",
  "trainable_only": true,
  "total_numel": 124439808,
  "params": [
    {
      "name": "transformer.wte.weight",
      "shape": [50257, 768],
      "dtype": "torch.float32",
      "numel": 38597376,
      "offset": 0
    }
  ]
}
```

这保证 learner 和 syncer 对 flat vector 有相同解释。

### Update tensor

```text
update_<uuid>.params.safetensors:
  local_params: flat tensor
```

dtype 由 `io.tensor_dtype` 控制，默认 `float32`。

### Global weight tensor

Global weight file 保存 named tensors，尽量兼容 `model.load_state_dict(..., strict=False)` 的命名形式。加载到 learner 时会按 `param_index` 转成 flat，再写回模型参数。

## SQLite 数据模型

Schema 位于 `fs_diloco/schema.sql`。

关键表：

- `run_state`：保存 resolved config 等 run-level KV。
- `global_versions`：每个 committed global version 的 weight path、optim path、update 数、token 数和 optimizer 名称。
- `learners`：learner heartbeat 解析后的状态。
- `updates`：update metadata ingest 后的权威记录。
- `events`：syncer 事件日志索引。
- `db_dumps`：SQLite backup 记录。

Update 状态：

```text
pending -> selected -> applied
pending -> dropped
selected -> dropped
```

重要约束：

```sql
UNIQUE(learner_id, local_step_end, base_global_version)
```

用于避免相同 learner 在同一 local step / base global version 下重复提交。

---

# English

## File Layout and Atomicity

Typical run directory:

```text
runs/fs_diloco/<RUN_ID>/
  control/latest.json
  control/stop.json
  control/run_config.resolved.yaml
  control/param_index.json
  weights/global_v000000.safetensors
  optim/outer_v000000.safetensors
  updates/pending/learner_000/update_<uuid>.params.safetensors
  updates/pending/learner_000/update_<uuid>.meta.json
  heartbeats/learner_000.json
  db_dumps/metadata_<timestamp>_v000001.db
  logs/syncer.jsonl
  logs/learner_000.jsonl
  metrics/syncer_metrics.csv
  metrics/learner_metrics.csv
  metrics/update_manifest.csv
```

Key rules:

- Writes use temp-file plus `os.replace()` publication.
- Update metadata JSON is the commit marker.
- Tensor files without final metadata are ignored.
- Learners only poll `control/latest.json`.
- SQLite is local to the syncer and only backed up to shared storage.

`param_index.json` records the deterministic flat-vector interpretation:

```json
{
  "format_version": 1,
  "model_name_or_path": "gpt2",
  "trainable_only": true,
  "total_numel": 124439808,
  "params": [
    {
      "name": "transformer.wte.weight",
      "shape": [50257, 768],
      "dtype": "torch.float32",
      "numel": 38597376,
      "offset": 0
    }
  ]
}
```

Update tensor files contain:

```text
local_params: flat tensor
```

Global weight files store named tensors in a form compatible with `model.load_state_dict(..., strict=False)` where possible.

## SQLite Data Model

Schema file: `fs_diloco/schema.sql`.

Important tables:

- `run_state`: run-level key/value records such as resolved config.
- `global_versions`: committed global versions with weight path, optimizer path, selected update count, selected token count, and optimizer name.
- `learners`: heartbeat-derived learner status.
- `updates`: authoritative records for ingested learner metadata.
- `events`: syncer event index.
- `db_dumps`: SQLite backup records.

Update status transitions:

```text
pending -> selected -> applied
pending -> dropped
selected -> dropped
```

The updates table has a uniqueness constraint on `(learner_id, local_step_end, base_global_version)` to prevent duplicate submissions from the same learner interval.
