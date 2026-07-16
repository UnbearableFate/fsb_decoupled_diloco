# 07 运行与运维

## 1. 环境

- 项目虚拟环境:`.venv/`(PBS 脚本默认 `PYTHON_BIN=$PROJECT_ROOT/.venv/bin/python`);
- 依赖:torch、transformers、datasets、safetensors、pyyaml、wandb(可选)、lm-eval(评测用,可选);
- Miyabi 上**登录节点只做静态检查**,运行必须在 PBS 计算/调试节点:

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
scripts/local/clean_run.sh                 # 清理冒烟产物
```

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
| `run_1node_lm_eval.pbs` | 1 节点 | checkpoint 导出 + lm-eval |

提交与自定义(以 9 节点为例):

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
# 环境变量可覆盖:CONFIG、RUN_ID、SHARED_ROOT、SYNCER_DB_DIR、WANDB_MODE、
# SYNCER/LEARNER_CUDA_VISIBLE_DEVICES 等(见脚本头部)
```

脚本约定:第 1 台节点跑 syncer,其余 8 台各跑一个 learner;`SYNCER_DB_DIR` 默认 `$TMPDIR/fs_diloco/$RUN_ID`(节点本地);HF 缓存与 W&B 目录也在脚本里统一设置。

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

也可以直接对 DB dump 跑 SQL:

```bash
sqlite3 runs/fs_diloco/<RUN_ID>/db_dumps/metadata_*_v000047.db \
  "SELECT status, COUNT(*) FROM updates GROUP BY status"
```

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
