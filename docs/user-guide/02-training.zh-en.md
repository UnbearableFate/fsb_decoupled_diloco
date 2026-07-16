# 训练使用流程 / Training Workflows

[返回总览 / Back to index](00-README.zh-en.md)

中文：说明登录节点静态检查、1/2/9 节点 PBS 运行、synthetic smoke、手动启动和 resume。

English: Static checks, 1/2/9-node PBS runs, synthetic smoke, manual launch, and resume.

---

# 中文

## 快速开始

### 登录节点只做静态检查

在 Miyabi 登录节点上不要运行训练、模型加载、dataset preprocessing、`pytest`、`mpirun` 或任何 heavy import。可以运行：

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m py_compile fs_diloco/*.py
```

### 1 节点真实 GPT-2/WikiText-2 debug

提交：

```bash
qsub scripts/miyabi/run_1node_debug.pbs
```

这个脚本在一个 compute node 上启动两个后台进程：

- syncer：使用 `${SYNCER_CUDA_VISIBLE_DEVICES:-0}`，默认 GPU 0；
- `learner_000`：使用 `${LEARNER_CUDA_VISIBLE_DEVICES:-0}`，默认 GPU 0。

默认配置为 `configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml`，训练步数很小，目标是验证真实模型、真实数据、文件系统通信和 global publish 流程。

完成后检查：

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
```

典型通过条件：

- `latest_version: 1` 或更高；
- `stop_reason: stop_after_outer_steps`；
- `weights/global_v000001.safetensors` 存在；
- `db_dumps/metadata_*_v000001.db` 存在；
- learner metrics 中 loss 有限；
- syncer metrics 中 `selected_count` 大于 0。

### 2 节点 debug

提交：

```bash
qsub scripts/miyabi/run_2node_debug.pbs
```

脚本使用 MPI 作为 PBS 多节点进程启动器，但训练通信仍完全依赖共享文件系统：

- rank 0：syncer，运行在第一个节点，`CUDA_VISIBLE_DEVICES=${SYNCER_CUDA_VISIBLE_DEVICES:-0}`；
- rank 1：`learner_000`，运行在第二个节点，`CUDA_VISIBLE_DEVICES=${LEARNER_CUDA_VISIBLE_DEVICES:-0}`。

这个测试验证跨节点共享文件系统 update exchange、heartbeat、metadata ingest 和 global publish。

### 9 节点 acceptance / 8 learners

默认完整 8 learner 配置：

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

短 acceptance 配置：

```bash
qsub -q debug-g -l walltime=00:30:00 \
  -v CONFIG=$PWD/configs/fs_diloco_gpt2_wikitext2_8l_acceptance.yaml \
  scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

节点角色：

- rank 0：syncer；
- rank 1-8：`learner_000` 到 `learner_007`。

默认 quorum 策略为：

```yaml
sync:
  num_learners: 8
  quorum_min: 4
  quorum_max: 8
```

Acceptance 运行建议检查：

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
sqlite3 runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v000003.db \
  "SELECT applied_version, COUNT(*), COUNT(DISTINCT learner_id)
   FROM updates WHERE status='applied'
   GROUP BY applied_version;"
```

期望每个 outer step 至少有 `quorum_min` 个不同 learner；短 acceptance 配置通常会选满 8 个 learner。

### 本地 synthetic smoke

这个脚本使用 tiny synthetic model 和 synthetic data，不代表真实 GPT-2 训练性能，只用于快速验证协议：

```bash
scripts/local/run_tiny_2proc_smoke.sh
```

在 Miyabi 登录节点不要运行它，因为它会启动 Python runtime 和 project imports。可以在 compute/debug 节点或本地开发机上运行。

## 手动启动 syncer 和 learner

通常使用 PBS 脚本启动。调试时可以在 compute node 上手动运行。

Syncer：

```bash
RUN_ID=manual_debug
SHARED_ROOT=$PWD/runs/fs_diloco/$RUN_ID
SQLITE_DIR=${TMPDIR:-/tmp}/fs_diloco/$RUN_ID

CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m fs_diloco.syncer \
  --config configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --sqlite-local-dir "$SQLITE_DIR" \
  --num-learners 1
```

Learner：

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m fs_diloco.learner \
  --config configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --learner-id learner_000 \
  --num-learners 1
```

不要在 Miyabi 登录节点运行这些命令。

## Resume 工作流

恢复已有 run：

```yaml
init:
  resume: true
  resume_version: latest
  resume_db_dump: null
```

Syncer resume 会：

1. 读取 `control/latest.json` 或指定版本；
2. 读取 `control/param_index.json`；
3. 验证当前 model parameter index 与已保存 index 兼容；
4. 加载 global weight 和 outer optimizer state；
5. 如本地 SQLite 为空，尝试从 `init.resume_db_dump` 或 `db_dumps/metadata_*_vNNNNNN.db` 恢复；
6. 继续 ingest 文件系统中的 unapplied metadata；
7. 依靠 `update_id` primary key 和 uniqueness constraint 避免重复应用。

---

# English

## Training Workflows

### Login-node static checks

Miyabi login nodes are control-plane only. Do not run training, model loading, dataset preprocessing, `pytest`, `mpirun`, or heavy Python imports there.

Allowed checks:

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m py_compile fs_diloco/*.py
```

### 1-node real GPT-2/WikiText-2 debug run

```bash
qsub scripts/miyabi/run_1node_debug.pbs
```

The script starts a GPU-backed syncer and `learner_000` with GPU 0 on the same compute node. It uses `configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml`.

Inspect:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
```

Expected evidence:

- `latest_version` is at least 1;
- `stop_reason` is `stop_after_outer_steps`;
- `weights/global_v000001.safetensors` exists;
- a DB dump for version 1 exists;
- learner loss values are finite;
- syncer metrics show a selected update.

### 2-node debug run

```bash
qsub scripts/miyabi/run_2node_debug.pbs
```

Rank 0 runs the syncer. Rank 1 runs `learner_000`. MPI is only a PBS process launcher; all training communication still happens through the shared filesystem.

### 9-node acceptance / 8 learners

Full default:

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

Short acceptance:

```bash
qsub -q debug-g -l walltime=00:30:00 \
  -v CONFIG=$PWD/configs/fs_diloco_gpt2_wikitext2_8l_acceptance.yaml \
  scripts/miyabi/run_9node_gpt2_wikitext2.pbs
```

Rank 0 is the syncer; ranks 1-8 are `learner_000` through `learner_007`.

Validate with:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>
sqlite3 runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v000003.db \
  "SELECT applied_version, COUNT(*), COUNT(DISTINCT learner_id)
   FROM updates WHERE status='applied'
   GROUP BY applied_version;"
```

Each outer step should have at least `sync.quorum_min` distinct learners; the short acceptance config typically selects all eight.

### Synthetic smoke

```bash
scripts/local/run_tiny_2proc_smoke.sh
```

This uses a tiny synthetic model and synthetic data. It is useful for protocol testing, not performance. Do not run it on a Miyabi login node.

## Manual Process Launch

Use PBS scripts for normal runs. For debugging inside an already allocated compute node:

```bash
RUN_ID=manual_debug
SHARED_ROOT=$PWD/runs/fs_diloco/$RUN_ID
SQLITE_DIR=${TMPDIR:-/tmp}/fs_diloco/$RUN_ID

CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m fs_diloco.syncer \
  --config configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --sqlite-local-dir "$SQLITE_DIR" \
  --num-learners 1

CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m fs_diloco.learner \
  --config configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml \
  --run-id "$RUN_ID" \
  --shared-root "$SHARED_ROOT" \
  --learner-id learner_000 \
  --num-learners 1
```

Do not run these commands on Miyabi login nodes.

## Resume

To resume a run:

```yaml
init:
  resume: true
  resume_version: latest
  resume_db_dump: null
```

The syncer loads `latest.json`, validates the parameter index, loads global weights and outer optimizer state, restores a SQLite dump if needed, re-ingests unapplied metadata, and relies on primary keys and uniqueness constraints to avoid applying the same update twice.
