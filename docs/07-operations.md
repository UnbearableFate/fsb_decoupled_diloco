# 07 运行与运维

## 1. 环境

- 项目虚拟环境:`.venv/`(PBS 脚本默认 `PYTHON_BIN=$PROJECT_ROOT/.venv/bin/python`);
- 依赖:torch、transformers、datasets、safetensors、pyyaml、nvidia-ml-py(GPU 利用率采样)、wandb(可选)、lm-eval(评测用,可选);
- Miyabi 上**登录节点只做静态检查**,运行必须在 PBS 计算/调试节点:

在 PBS 计算节点中根据 `.python-version` 和 `pyproject.toml` 创建或同步完整开发/评测环境:

```bash
uv sync --all-extras
source .venv/bin/activate
```

项目通过 setuptools 以 editable 模式安装，`fs-diloco-*` 命令与 `python -m fs_diloco.*` 入口均指向当前工作区源码。

登录节点上的静态检查:

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m compileall -q fs_diloco
```

## 2. 手动启动(任意机器)

```bash
# 终端 1:syncer(先启动,它负责初始化 run 并发布 v0)
python -m fs_diloco.syncer \
  --config configs/fs_diloco_gpt2_wikitext2_8l.yaml \
  --run-id my_run --shared-root /shared/runs/my_run \
  --sqlite-local-dir /local_ssd/fs_diloco/my_run \
  --num-learners 2

# 终端 2..N+1:learners(可任意先后,会阻塞等待 syncer 发布)
python -m fs_diloco.learner \
  --config configs/fs_diloco_gpt2_wikitext2_8l.yaml \
  --run-id my_run --shared-root /shared/runs/my_run \
  --learner-id learner_000 --num-learners 2
```

要点:

- 所有进程的 `--run-id/--shared-root/--num-learners` 必须一致(元数据里的 run_id 不匹配会被 syncer 忽略);
- `learner-id` 必须是 `learner_000` 到 `learner_{N-1:03d}`;
- `--sqlite-local-dir` 必须是**节点本地盘**;
- 中途停止:向 run 目录写 `control/stop.json` 即可让 learner 退出(syncer 只认自己的停止条件)。

## 3. 本地冒烟(无 GPU、无外网)

```bash
scripts/local/run_tiny_2proc_smoke.sh      # synthetic-tiny 模型 + 合成数据,CPU 上 1 syncer + 2 learner
scripts/local/clean_run.sh runs/fs_diloco  # 递归预览将清理的 safetensors
scripts/local/clean_run.sh --delete runs/fs_diloco
scripts/local/clean_run.sh --delete --keep-latest-global runs/fs_diloco
```

`clean_run.sh` 只扫描项目 `runs/` 内的目标目录；默认是 dry-run，必须传入 `--delete` 才会删除。`--keep-latest-global` 会对递归扫描到的每个目录保留编号最大的 `global_v<version>.safetensors`，删除其余所有 safetensors。

对应配置 `configs/fs_diloco_tiny_local.yaml` / `fs_diloco_tiny_fragment_local.yaml`。

## 4. Miyabi PBS 批作业

| 脚本 | 规模 | 用途 |
|---|---|---|
| `run_1node_debug.pbs` | 1 节点 | debug 队列,1 syncer + 1 learner 同卡 |
| `run_1node_fragment_debug.pbs` | 1 节点 | fragment 冒烟 |
| `run_2node_debug.pbs` / `run_2node_fragment_debug.pbs` | 2 节点 | 双节点验证 |
| `run_9node_gpt2_wikitext2.pbs` | 9 节点 | 8 learner + 1 syncer 短跑 |
| `run_9node_gpt2_wikitext2_5000steps.pbs` | 9 节点 | 5000 本地步正式实验 |
| `run_9node_fragment_gpt2_wikitext2_5000steps.pbs` / `..._50x4.pbs` | 9 节点 | fragment 版实验 |
| `run_9node_fragment_gpt2_wikitext2_50x10.pbs` / `run_9node_no_fragment_gpt2_wikitext2_50x10.pbs` | 9 节点 | 8 learner、inner steps=50、outer steps=10 的 fragment/no-fragment 对照实验 |
| `run_1node_lm_eval.pbs` | 1 节点 | checkpoint 导出 + lm-eval |

提交与自定义(以 9 节点为例):

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
# 环境变量可覆盖:CONFIG、RUN_ID、SHARED_ROOT、SYNCER_DB_DIR、WANDB_MODE、
# HF_DATASETS_OFFLINE、HF_HUB_OFFLINE、FS_DILOCO_HF_WIKITEXT_REPO、
# SYNCER/LEARNER_CUDA_VISIBLE_DEVICES 等(见脚本头部)
```

脚本约定:第 1 台节点跑 syncer,其余 8 台各跑一个 learner;`SYNCER_DB_DIR` 默认 `$TMPDIR/fs_diloco/$RUN_ID`(节点本地);HF 缓存与 W&B 目录也在脚本里统一设置。WikiText 默认映射到 `Salesforce/wikitext`;已有完整缓存时可在 `qsub -v` 中设置 `HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1`,避免运行期的 Hub HEAD 请求影响启动。

辅助脚本:

```bash
scripts/miyabi/inspect_run.sh <run_root>    # 快速查看 run 状态
scripts/miyabi/dump_sqlite.sh               # 手动导出 syncer DB
```

## 5. run 分析

```bash
# 人类可读摘要(latest/stop/指标行数/DB 计数/心跳/loss 概览)
python -m fs_diloco.analysis summary runs/fs_diloco/<RUN_ID>
python -m fs_diloco.analysis summary runs/fs_diloco/<RUN_ID> --json   # 完整 JSON
# 兼容:python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID>

# fragment run 的自动断言(CI/验收用;失败列出所有不满足项并非零退出)
python -m fs_diloco.analysis assert-fragment-smoke \
  --run-root <run_root> --expected-learners 8 \
  --expected-global-merge-events 50 --expected-fragment-ids 0,1,2,3 \
  --min-selected-count 4
python -m fs_diloco.analysis assert-fragment-5000 ... --expected-local-steps 5000   # 额外要求本地步数达标
```

分析读取:共享目录的 `latest.json/stop.json/heartbeats/metrics/*.csv` + 最新 DB dump(可用 `--db` 指定),不需要 torch/GPU。

`control/summary.json` 和分析摘要中的 `complete_training_time_seconds` 从 syncer 启动计至 learner 全部 stopped;`learner_resources` 给出每个 learner 的全训练 CPU/GPU 峰值及跨 learner 聚合。若 W&B 未认证,用 `WANDB_MODE=offline qsub ...` 保留完整 run,认证后执行 `wandb sync <run_root>/logs/wandb/offline-run-*`。

也可以直接对 DB dump 跑 SQL:

```bash
sqlite3 runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v000047.db \
  "SELECT status, COUNT(*) FROM updates GROUP BY status"
```

### 5.1 BF16 upload + fragment 直接抽取的 50x10 复测(2026-07-16)

变更后两个 9 节点作业均为 8 learners、10 次 merge、每次 selected count=8,正常以 `stop_after_outer_steps` 结束,无 error/no-progress/未捕获异常。fragment 作业还通过了脚本内置的 `assert-fragment-smoke`。

| 模式 | run | 单份 update payload | learner 平均写 update | syncer 平均读 update | 完整训练时间 | loss(first-10 → last-10) |
|---|---|---:|---:|---:|---:|---:|
| fragment / FP32 基线 | `20260716_160259_fs_diloco_gpt2_wikitext2_8l_fragment_50x10` | 126.394 MB(四片加权平均) | 134.79 ms | 254.10 ms | 201.63 s | 3.9144 → 3.3156 |
| fragment / BF16 + 直接抽取 | `codex_bf16_fragment_50x10_20260716_1724` | 63.197 MB(-50.0%) | 63.88 ms(-52.6%) | 181.71 ms(-28.5%) | 198.90 s(-1.4%) | 3.9184 → 3.3237 |
| full / FP32 基线 | `20260716_160753_fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10` | 497.759 MB | 206.81 ms | 624.27 ms | 153.62 s | 3.8981 → 3.3621 |
| full / BF16 | `codex_bf16_full_50x10_20260716_1727` | 248.880 MB(-50.0%) | 140.18 ms(-32.2%) | 356.79 ms(-42.9%) | 150.44 s(-2.1%) | 3.8849 → 3.3716 |

结论:BF16 确实把共享文件系统上的 learner update payload 减半,并明显降低 learner 写入与 syncer 读取耗时。端到端时间只改善约 1–2%,说明当前 50x10 的主耗时仍是本地训练、quorum/轮询和 syncer 发布 FP32 全局权重;fragment 行的写入改善是“直接抽取 + BF16”的合并效果,不能仅凭这组对照拆分两项各自贡献。两种新 run 的 loss 都持续下降且分析器未判定发散。

## 6. checkpoint 评测(LM Evaluation Harness)

```bash
# 1. 解析要评测的 checkpoint(缺省自动找最新 run 的 latest)
python -m fs_diloco.eval_lm_harness resolve-checkpoint --project-root .

# 2. 导出为 HuggingFace 模型目录(把扁平权重灌回模型 + save_pretrained)
python -m fs_diloco.eval_lm_harness export-checkpoint \
  --eval-id my_eval --export-dir exports/my_eval --manifest-output exports/my_eval/manifest.json

# 3. 用 lm-eval 跑评测(外部工具),然后把结果 JSON 拍平成 CSV
lm_eval --model hf --model_args pretrained=exports/my_eval --tasks lambada_openai ... --output_path evals/my_eval
python -m fs_diloco.eval_lm_harness results-to-csv \
  --lm-eval-output evals/my_eval --output-csv evals/my_eval/metrics.csv --manifest exports/my_eval/manifest.json
```

fragment run 评测用 materialize 出的 `weights/global_v{event}.safetensors`。

## 7. 常见问题排查

| 现象 | 排查 |
|---|---|
| learner 卡在启动 | 是否在等 `param_index.json`/`latest.json`(syncer 没起来或 shared_root 不一致);`wait_for_json` 默认 1800s 超时 |
| syncer 一直 `quorum_wait` | learner 是否在提交(看 `updates/pending/`);run_id 是否一致;staleness 窗口是否太紧;最终会 `no_progress_timeout` 停机 |
| 大量 `dropped(missing_file)` | `io.keep_last_learner_update_versions` 太小,更新还没被消费就被 learner 清掉了 |
| 大量 `dropped(stale)` | learner 太慢或 `max_staleness_versions` 太小 |
| resume 后重复合并了旧更新? | 不会:`UNIQUE` 约束去重 + staleness/superseded 丢弃;但确认 dump 版本与 `latest.json` 匹配 |
| SQLite 报锁/IO 错误 | 检查 `sqlite_local_dir` 是否被误配到共享文件系统 |
| W&B 报错 | 只影响遥测,训练继续;离线模式产物在 `logs/wandb/`,事后 `wandb sync` |
