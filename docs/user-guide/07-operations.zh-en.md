# 观测、排查与限制 / Operations, Troubleshooting, and Limits

[返回总览 / Back to index](00-README.zh-en.md)

中文：说明 analysis/SQLite 检查、常见问题、实验建议和已知限制。

English: Analysis and SQLite checks, troubleshooting, experiment suggestions, and known limitations.

---

# 中文

## 可观测性与排查

### Inspection CLI

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID> --json
```

输出包括：

- latest version；
- stop reason；
- metrics row counts；
- DB 中 global version 数；
- applied/pending/dropped update 数；
- applied contributors。

### 常用 SQLite 查询

```bash
DB=$(ls runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v*.db | sort | tail -n 1)

sqlite3 "$DB" \
  "SELECT version, num_updates, total_update_tokens, status
   FROM global_versions ORDER BY version;"

sqlite3 "$DB" \
  "SELECT applied_version, COUNT(*), COUNT(DISTINCT learner_id)
   FROM updates WHERE status='applied'
   GROUP BY applied_version ORDER BY applied_version;"

sqlite3 "$DB" \
  "SELECT status, COUNT(*) FROM updates GROUP BY status;"
```

### 常见问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| syncer 一直 `quorum_wait` | learner 未启动、metadata 未写、quorum 设置过高 | 检查 learner log、heartbeat、`sync.quorum_min`。 |
| update tensor 存在但未 ingest | 缺少 `.meta.json` commit marker 或 JSON malformed | 检查 `updates/pending/<learner>/`。 |
| learner 停在等待 latest | syncer 未初始化或失败 | 检查 `logs/syncer.jsonl` 和 PBS output。 |
| SQLite dump 缺失 | syncer 未完成一个 global version 或提前失败 | 检查 `db_dumps/` 和 syncer error。 |
| HF dataset 报 `wikitext` URI 问题 | 新版 HF hub 对 bare repo 名称更严格 | 代码会 fallback 到 `Salesforce/wikitext`；也可设置 `FS_DILOCO_HF_WIKITEXT_REPO`。 |
| 训练 loss NaN/Inf | 学习率、数据或 precision 问题 | 降低 inner/outer lr，开启 grad clipping，检查日志。 |

## 实验建议

正确性：

```text
1 learner, quorum=1, stop_after_outer_steps=3
2 learners, quorum=1
2 learners, quorum=2
8 learners, quorum=4
```

优化：

```text
outer_optimizer: nesterov, momentum, adamw
nesterov lr: 0.1, 0.3, 0.7, 1.0
inner_steps: 10, 50, 100
max_staleness_versions: 0, 1, 2, 4
```

系统指标：

```text
update_write_seconds
read_seconds
aggregation_seconds
outer_step_seconds
publish_seconds
tokens_per_sec
selected_count
stale_updates_dropped
```

## 已知限制

- Full-model vector upload 对 GPT-2 约为数百 MB，每个 interval 都会写入共享文件系统；这是 Milestone 1 的刻意简化。
- `upload_mode=delta` 尚未实现为主路径。
- SQLite 主库只应在 syncer-local storage 上使用，不应直接放在 Lustre 上作为高并发 DB。
- 当前 learner inner optimizer 只支持 AdamW。
- `quorum_policy=fixed` 不会因 learner dead 自动降低 quorum。
- 9-node 脚本会占用一个 GPU 节点给 syncer；syncer 会使用该节点本地 GPU 做聚合和 outer optimizer。

---

# English

## Observability

Inspect a run:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID> --json
```

Useful DB checks:

```bash
DB=$(ls runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v*.db | sort | tail -n 1)

sqlite3 "$DB" \
  "SELECT version, num_updates, total_update_tokens, status
   FROM global_versions ORDER BY version;"

sqlite3 "$DB" \
  "SELECT applied_version, COUNT(*), COUNT(DISTINCT learner_id)
   FROM updates WHERE status='applied'
   GROUP BY applied_version ORDER BY applied_version;"

sqlite3 "$DB" \
  "SELECT status, COUNT(*) FROM updates GROUP BY status;"
```

## Troubleshooting

| Symptom | Likely Cause | Action |
| --- | --- | --- |
| Syncer stays in `quorum_wait` | Learners did not start, metadata was not written, or quorum is too high | Check learner logs, heartbeats, and `sync.quorum_min`. |
| Tensor file exists but is not ingested | Missing `.meta.json` commit marker or malformed JSON | Inspect `updates/pending/<learner>/`. |
| Learner waits for `latest.json` | Syncer did not initialize or failed early | Inspect `logs/syncer.jsonl` and PBS output. |
| SQLite dump is missing | Syncer did not complete a global version or failed early | Inspect `db_dumps/` and syncer errors. |
| HF dataset reports a `wikitext` URI issue | Newer HF Hub behavior can be stricter for bare repo names | The code falls back to `Salesforce/wikitext`; `FS_DILOCO_HF_WIKITEXT_REPO` can override it. |
| Loss becomes NaN/Inf | Learning rate, data, or precision issue | Lower inner/outer LR, enable grad clipping, and inspect logs. |

## Experiment Suggestions

Correctness matrix:

```text
1 learner, quorum=1, stop_after_outer_steps=3
2 learners, quorum=1
2 learners, quorum=2
8 learners, quorum=4
```

Optimizer matrix:

```text
outer_optimizer: nesterov, momentum, adamw
nesterov lr: 0.1, 0.3, 0.7, 1.0
inner_steps: 10, 50, 100
max_staleness_versions: 0, 1, 2, 4
```

System metrics:

```text
update_write_seconds
read_seconds
aggregation_seconds
outer_step_seconds
publish_seconds
tokens_per_sec
selected_count
stale_updates_dropped
```

## Known Limitations

- Full GPT-2 parameter-vector uploads are large; this is intentional for Milestone 1.
- Delta upload mode is not the primary implemented path.
- The SQLite primary DB must remain syncer-local, not on Lustre as a shared concurrent DB.
- Learner inner optimizer support is currently AdamW.
- Fixed quorum does not automatically shrink when learners die.
- The 9-node script uses one GPU node for the syncer; the syncer uses that node's local GPU for aggregation and outer optimization.
