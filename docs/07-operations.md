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
- syncer 的权威 DB 固定为 `<shared-root>/control/syncer_metadata.sqlite3`;所有节点必须看到同一 shared root;
- 中途停止:向 run 目录写 `control/stop.json` 即可让 learner 退出(syncer 只认自己的停止条件)。

恢复时使用同一个 run id/shared root,并把配置中的 `init.resume` 设为 `true`。恢复不会信任 `latest.json`:持久 DB 必须存在且通过 integrity/identity/checkpoint 一致性校验,否则 syncer fail closed。不要移动或清理 `control/syncer_metadata.sqlite3`。

## 3. 本地冒烟(无 GPU、无外网)

```bash
scripts/local/run_tiny_2proc_smoke.sh      # synthetic-tiny 模型 + 合成数据,CPU 上 1 syncer + 2 learner
scripts/local/clean_run.sh runs/fs_diloco  # 递归预览将清理的 safetensors 和 .db
scripts/local/clean_run.sh --delete runs/fs_diloco
scripts/local/clean_run.sh --delete --keep-latest-global runs/fs_diloco
```

`clean_run.sh` 只扫描项目 `runs/` 内的目标目录；默认是 dry-run，必须传入 `--delete` 才会删除。脚本会递归删除 `*.safetensors` 和 `*.db`;这会删除持久恢复权威,只应对确定废弃的 run 使用。`--keep-latest-global` 只保留编号最大的 `global_v<version>.safetensors`,不会保留恢复所需的 outer state/DB。

对应配置 `configs/fs_diloco_tiny_local.yaml` / `fs_diloco_tiny_fragment_local.yaml`。

## 4. Miyabi PBS 批作业

| 脚本 | 规模 | 用途 |
|---|---|---|
| `run_1node_debug.pbs` | 1 节点 | debug 队列,1 syncer + 1 learner 同卡 |
| `run_1node_fragment_debug.pbs` | 1 节点 | fragment 冒烟 |
| `run_plan01_regression.pbs` | 1 节点 | 持久状态完整 pytest + tiny full telemetry 回归 |
| `run_2node_debug.pbs` / `run_2node_fragment_debug.pbs` | 2 节点 | 双节点验证 |
| `run_9node_gpt2_wikitext2.pbs` | 9 节点 | 8 learner + 1 syncer 短跑 |
| `run_9node_gpt2_wikitext2_5000steps.pbs` | 9 节点 | 5000 本地步正式实验 |
| `run_9node_fragment_gpt2_wikitext2_5000steps.pbs` / `..._50x4.pbs` | 9 节点 | fragment 版实验 |
| `run_9node_fragment_gpt2_wikitext2_50x10.pbs` / `run_9node_no_fragment_gpt2_wikitext2_50x10.pbs` | 9 节点 | 8 learner、inner steps=50、outer steps=10 的 fragment/no-fragment 对照实验 |
| `run_1node_lm_eval.pbs` | 1 节点 | checkpoint 导出 + lm-eval |

提交与自定义(以 9 节点为例):

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
# 环境变量可覆盖:CONFIG、RUN_ID、SHARED_ROOT、WANDB_MODE、
# HF_DATASETS_OFFLINE、HF_HUB_OFFLINE、FS_DILOCO_HF_WIKITEXT_REPO、
# SYNCER/LEARNER_CUDA_VISIBLE_DEVICES 等(见脚本头部)
```

脚本约定:第 1 台节点跑 syncer,其余 8 台各跑一个 learner;所有角色共享 `<SHARED_ROOT>/control/syncer_metadata.sqlite3`;节点本地 `$TMPDIR` 只承载非权威 runtime/cache。HF 缓存与 W&B 目录也在脚本里统一设置。WikiText 默认映射到 `Salesforce/wikitext`;已有完整缓存时可在 `qsub -v` 中设置 `HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1`,避免运行期的 Hub HEAD 请求影响启动。

辅助脚本:

```bash
scripts/miyabi/inspect_run.sh <run_root>    # 快速查看 run 状态
python scripts/miyabi/sqlite_shared_fs_probe.py --help  # 共享 SQLite stress/kill-reopen probe
python scripts/miyabi/publication_crash_probe.py --help # full publication crash matrix
python scripts/miyabi/check_plan01_invariants.py --help # current-only/一致性/时延三值 Checker
```

Run 分析方法和实验结果统一维护在 [reports/run_analysis.md](../reports/run_analysis.md)。

## 5. checkpoint 评测(LM Evaluation Harness)

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

## 6. 常见问题排查

| 现象 | 排查 |
|---|---|
| learner 卡在启动 | 是否在等 `param_index.json`/`latest.json`(syncer 没起来或 shared_root 不一致);`wait_for_json` 默认 1800s 超时 |
| syncer 一直 `quorum_wait` | learner 是否在替换 `updates/latest/learner_XXX.json`;run_id 是否一致;staleness 窗口是否太紧;最终会 `no_progress_timeout` 停机 |
| 大量 `dropped(missing_file)` | payload 是否被外部清理/移动,或共享文件系统是否出现可见性/读取错误;learner 不再自行清理 payload |
| 大量 `dropped(stale)` | learner 太慢或 `max_staleness_versions` 太小 |
| resume 后重复合并了旧更新? | DB commit 是权威边界;resume 重置 selected、重建 latest,proposal frontier/唯一约束阻止重放。先检查 analysis 的 integrity 与 committed version |
| SQLite 报锁/IO 错误 | 保留现场并运行共享 FS probe;确认 DB 使用 `journal_mode=delete`,`synchronous=2(FULL)`,且没有额外 writer/WAL 文件 |
| W&B 报错 | 只影响遥测,训练继续;离线模式产物在 `logs/wandb/`,事后 `wandb sync` |
