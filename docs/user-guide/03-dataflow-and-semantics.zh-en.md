# 数据流与优化语义 / Dataflow and Semantics

[返回总览 / Back to index](00-README.zh-en.md)

中文：覆盖初始化、learner upload、syncer 聚合、global adoption、merge 权重和 outer optimizer。

English: Initialization, learner uploads, syncer aggregation, global adoption, merge weights, and outer optimizer.

---

# 中文

## 完整数据流

### 初始化数据流

1. Syncer 解析 YAML 和 CLI override。
2. Syncer 创建目录结构：

```text
control/
weights/
optim/
updates/pending/<learner_id>/
updates/processed/<learner_id>/
updates/dropped/<learner_id>/
heartbeats/
db_dumps/
logs/
metrics/
```

3. Syncer 打开本地 SQLite DB，例如：

```text
${TMPDIR:-/tmp}/fs_diloco/<RUN_ID>/syncer_metadata.sqlite3
```

4. Syncer 加载模型并构造 deterministic `param_index.json`。
5. Syncer flatten 模型参数得到 `theta_0`。
6. Syncer 初始化 outer optimizer state。
7. Syncer 发布：

```text
control/param_index.json
weights/global_v000000.safetensors
optim/outer_v000000.safetensors
control/latest.json
control/run_config.resolved.yaml
```

### Learner 本地训练数据流

1. Learner 等待 `control/param_index.json`。
2. Learner 等待 `control/latest.json`。
3. Learner 读取 `latest.weight_path` 并加载 `global_vNNNNNN.safetensors`。
4. Learner 创建 inner optimizer。
5. Learner 从数据分片中取 batch。
6. Learner 执行 forward/backward/optimizer step。
7. Learner 在 interval 结束后 flatten 当前模型参数。
8. Learner 写 tensor 文件：

```text
updates/pending/learner_003/update_<uuid>.params.safetensors
```

9. Tensor 写完并 rename 后，learner 写 metadata commit marker：

```text
updates/pending/learner_003/update_<uuid>.meta.json
```

10. Learner 写 heartbeat：

```text
heartbeats/learner_003.json
```

### Syncer 聚合数据流

1. Syncer 扫描 `heartbeats/*.json`，更新 `learners` 表。
2. Syncer 扫描 `updates/pending/*/*.meta.json`。
3. Syncer 只 ingest 同时满足以下条件的 metadata：

- `format_version` 匹配；
- `run_id` 匹配；
- `learner_id` 在合法范围内；
- `file_path` 指向的 tensor 文件存在；
- metadata JSON 解析成功。

4. SQLite 中 status 为 `pending` 的 update 才能被选择。
5. Syncer 过滤超过 `sync.max_staleness_versions` 的 update。
6. 如果 eligible learner 数少于 `sync.quorum_min`，syncer sleep `sync.scan_interval_seconds` 后继续。
7. 达到 quorum 后，syncer 打开 grace window，继续收集 update，直到：

- 达到 `sync.quorum_max`；
- 或 `sync.grace_window.fixed_seconds` / `max_seconds` 到期。

8. Syncer 对每个 learner 至多选择一个 update，默认选最新：

```yaml
sync.selection_policy: most_recent_per_learner
```

9. Syncer 加载选中 update 的 `local_params`。
10. Syncer 计算权重并应用 outer optimizer。
11. Syncer 发布新版本 `global_vNNNNNN` 和 `outer_vNNNNNN`。
12. Syncer 更新 SQLite：

- selected -> applied；
- 同 learner 更旧 pending update -> dropped；
- 过 stale bound 的 pending update -> dropped。

13. Syncer 写 metrics 和 DB dump。

### Learner 采用新全局版本

Learner 默认在每次 upload 后检查 `latest.json`：

```yaml
learner:
  adopt_global_after_upload: true
  poll_latest_during_inner_steps: false
```

如果发现 `latest.version > last_loaded_global_version`：

1. 读取新 global weight file；
2. 用 flat vector 覆盖模型完整 trainable parameters；
3. 重建 inner optimizer 和 scheduler；
4. 将 `tokens_since_global_load` 置零；
5. 记录 `global_adopted` 和 `inner_optimizer_reset`。

## Merge 和优化语义

Milestone 1 默认 upload mode 为 `params`，即 learner 上传完整参数向量 `p_i`。

设 syncer 当前全局参数为 `theta_t`，选中 update 集合为 `i = 1..K`，每个 update 的 token 数为 `n_i`，base global version 为 `b_i`。当前版本为 `v_t`。

Staleness：

```text
s_i = v_t - b_i
```

Raw weight：

```text
raw_weight_i = n_i / (1 + staleness_lambda * s_i)
```

归一化：

```text
alpha_i = raw_weight_i / sum_j raw_weight_j
```

加权本地参数：

```text
p_bar = sum_i alpha_i * p_i
```

Outer pseudo-gradient：

```text
grad = theta_t - p_bar
```

然后 outer optimizer 使用 gradient descent 语义更新：

```text
theta_{t+1} = OuterOpt(theta_t, grad)
```

这个符号约定表示：如果 learner 本地参数 `p_i` 相对全局参数发生了有益移动，`theta_t - p_bar` 会把全局参数朝 `p_bar` 方向推进。

## Outer Optimizer

实现位置：`fs_diloco/outer_optim.py`。

支持：

- `sgd`：无 momentum 的 SGD；
- `momentum`：SGD with momentum；
- `nesterov`：Nesterov momentum；
- `adamw`：AdamW-style flat-vector optimizer。

State 存储在 `optim/outer_vNNNNNN.safetensors`：

- `theta`：全局 flat parameter vector；
- `momentum`：SGD momentum / Nesterov 时存在；
- `exp_avg`、`exp_avg_sq`：AdamW 时存在；
- `step`：int64 step tensor。

### Nesterov

```text
momentum_buffer = momentum * momentum_buffer + grad
update = grad + momentum * momentum_buffer
theta = theta - lr * update
```

### AdamW-style

```text
theta = theta * (1 - lr * weight_decay)
exp_avg = beta1 * exp_avg + (1 - beta1) * grad
exp_avg_sq = beta2 * exp_avg_sq + (1 - beta2) * grad^2
m_hat = exp_avg / (1 - beta1^step)
v_hat = exp_avg_sq / (1 - beta2^step)
theta = theta - lr * m_hat / (sqrt(v_hat) + eps)
```

---

# English

## End-to-End Dataflow

### Initialization

1. The syncer resolves YAML config and CLI overrides.
2. It creates the run layout under `runs/fs_diloco/<RUN_ID>/`.
3. It opens syncer-local SQLite at `${TMPDIR:-/tmp}/fs_diloco/<RUN_ID>/syncer_metadata.sqlite3`.
4. It loads the model and builds a deterministic `param_index.json`.
5. It flattens trainable parameters into `theta_0`.
6. It initializes the outer optimizer state.
7. It publishes initial weights, optimizer state, param index, resolved config, and `latest.json`.

### Learner upload

1. The learner waits for `param_index.json` and `latest.json`.
2. It loads the current global weights.
3. It builds the inner optimizer.
4. It trains locally for the configured interval.
5. It flattens model parameters.
6. It writes `update_<uuid>.params.safetensors`.
7. It writes `update_<uuid>.meta.json` after the tensor write completes.
8. It writes a heartbeat.

### Syncer aggregation

1. The syncer ingests heartbeats.
2. It ingests metadata JSON files whose tensor files exist.
3. It filters pending updates by staleness.
4. It waits until `quorum_min` is reached.
5. It collects additional updates during the grace window.
6. It selects at most one update per learner.
7. It loads selected tensors.
8. It computes normalized token/staleness weights.
9. It computes `p_bar`, then `grad = theta - p_bar`.
10. It applies the outer optimizer.
11. It publishes the next global version.
12. It marks updates as applied or dropped and writes metrics/DB dumps.

### Learner adoption

By default, learners poll `latest.json` after each upload. If the version is newer, they load the full global weight file, overwrite all trainable parameters, rebuild the inner optimizer/scheduler, reset `tokens_since_global_load`, and log `global_adopted` plus `inner_optimizer_reset`.

## Merge Semantics

Milestone 1 uses parameter-vector uploads. For each selected local vector `p_i`, token count `n_i`, base global version `b_i`, and current version `v_t`:

```text
s_i = v_t - b_i
raw_weight_i = n_i / (1 + staleness_lambda * s_i)
alpha_i = raw_weight_i / sum_j raw_weight_j
p_bar = sum_i alpha_i * p_i
grad = theta_t - p_bar
theta_{t+1} = OuterOpt(theta_t, grad)
```

This sign convention moves the global vector toward the weighted learner parameters when the outer optimizer subtracts the pseudo-gradient.

## Outer Optimizer

Implementation: `fs_diloco/outer_optim.py`.

Supported optimizers:

- `sgd`: plain SGD over the flat global vector.
- `momentum`: SGD with a momentum buffer.
- `nesterov`: Nesterov-style momentum.
- `adamw`: AdamW-style flat-vector optimizer.

Outer optimizer state is written to `optim/outer_vNNNNNN.safetensors`:

- `theta`: current global flat parameter vector.
- `momentum`: present for momentum/Nesterov.
- `exp_avg` and `exp_avg_sq`: present for AdamW-style updates.
- `step`: int64 step counter.

Nesterov update:

```text
momentum_buffer = momentum * momentum_buffer + grad
update = grad + momentum * momentum_buffer
theta = theta - lr * update
```

AdamW-style update:

```text
theta = theta * (1 - lr * weight_decay)
exp_avg = beta1 * exp_avg + (1 - beta1) * grad
exp_avg_sq = beta2 * exp_avg_sq + (1 - beta2) * grad^2
m_hat = exp_avg / (1 - beta1^step)
v_hat = exp_avg_sq / (1 - beta2^step)
theta = theta - lr * m_hat / (sqrt(v_hat) + eps)
```
