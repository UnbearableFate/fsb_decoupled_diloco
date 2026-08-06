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

仓库内配置和训练/评测启动脚本默认把 run 与独立 launcher 日志集中写到主工作树
`/work/xg24i002/x10041/fsb_decoupled_diloco/{runs,logs}`。从其他 worktree
启动时仍使用该绝对根目录；如确需隔离，可显式传入 `--shared-root`、`SHARED_ROOT`
或 `PRIMARY_WORKTREE_ROOT` 覆盖。

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
- 中途停止:向 run 目录原子写合法 `control/stop.json` 可让 learner 退出；syncer 不把该文件当自己的直接停止开关，但在全部 learner 写出 stopped 心跳后会经 terminal drain 以 `input_exhausted` 收束。手工写半截 JSON 不安全，应使用临时文件 + rename。

恢复时使用同一个 run id/shared root,并把配置中的 `init.resume` 设为 `true`。恢复不会信任 `latest.json`:持久 DB 必须存在且通过 integrity/identity/checkpoint 一致性校验,否则 syncer fail closed。不要移动或清理 `control/syncer_metadata.sqlite3`。启动 resume syncer 后再启动新 learner；resume 事务会把旧代 learner 行重置为 `unknown`，并 fence 切代时 heartbeat pointer 的完整内容，旧 `stopped` 不会触发本代 `input_exhausted`。检查 `run_resumed` 后应先看到新代 `learner_liveness_updated(active>0)`，再看到严格更大版本的 `outer_step_applied/global_published`。

任何可读的非 error `control/stop.json.reason` 或 `summary.json.stop_reason` 都会令 resume fail closed，
不要求两文件成对一致；不得手工删除终态文件重开。`error` 终态可由 resume 原子归档后继续。可用 Checker 的
`--require-resume-progress --resume-artifact <path>` 同时验证 DB/latest/checkpoint、旧 heartbeat
fence、新代 active 与下一次 commit；stdout 仍只有三值结果。

### 2.1 HA full：初始化、独立作业与人工接管

HA 配置只支持 full + static membership。先在同一 source tree捕获 identity并运行唯一 initializer；run root必须不存在：

```bash
python scripts/miyabi/capture_source_identity.py \
  --project-root "$PWD" --output-json /tmp/fsdiloco-source.json \
  --output-env /tmp/fsdiloco-source.env
source /tmp/fsdiloco-source.env
python -m fs_diloco.tools.init_run \
  --config configs/fs_diloco_tiny_ha_static.yaml \
  --run-id my_ha_run --shared-root /shared/runs/my_ha_run \
  --project-root "$PWD"
```

从 `control/run_descriptor.json` 读取 `descriptor_sha256`，然后分别提交角色。下面的 walltime只是 tiny示例；正式提交应根据 inner/global steps、已测单步和发布时间重新估算，并使用能覆盖预期运行和短收尾的最短值：

```bash
vars='FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256=<sha>,PROJECT_ROOT=/absolute/project'
qsub -l walltime=00:02:00 -v "$vars" scripts/miyabi/run_syncer_candidate.pbs
qsub -l walltime=00:02:00 -r y -J 0-7 -v "$vars" scripts/miyabi/run_static_learner.pbs
```

syncer异常退出后，只需用相同 descriptor变量重新 `qsub` 一个 `run_syncer_candidate.pbs`。不要删除 fixed cache、DB或旧 epoch目录；successor从 DB恢复并修复 canonical control。异常 leader可能留下 canonical `error` generation，它是诊断记录而不是最终 stop，learner会继续等待/reconcile，successor可将它推进为更高 generation的正常终态。若旧 process暂停在 write transaction内并持续持有 SQLite lock，先通过 operator确认并终止那个旧 job，再提交/等待 successor；系统不会自动 `qdel`。`coordination.recovery_submission.enabled` 默认false，开启后 learner只能去重并提交 candidate，candidate仍必须竞争 lease。

completed run用只读 Checker验收：

```bash
python scripts/miyabi/check_plan02_phase1.py \
  --run-root /shared/runs/my_ha_run --mode phase1-completed \
  --output /absolute/report/path/phase1-completed-checker_pass.json
```

## 3. 本地冒烟(无 GPU、无外网)

```bash
scripts/local/run_tiny_2proc_smoke.sh      # synthetic-tiny 模型 + 合成数据,CPU 上 1 syncer + 2 learner
scripts/local/clean_run.sh runs/fs_diloco  # 递归预览将清理的 safetensors 和 .db
scripts/local/clean_run.sh --delete runs/fs_diloco
scripts/local/clean_run.sh --delete --keep-latest-global runs/fs_diloco
```

`clean_run.sh` 只扫描项目 `runs/` 内的目标目录；默认 dry-run，必须传 `--delete` 才删除。它的实际 glob 是递归 `*.safetensors` 和 `*.db`：当前权威库名为 `syncer_metadata.sqlite3`，**不会**被该 glob 删除，但 global/outer/update/fragment tensor 会被删掉，run 仍无法恢复。`--keep-latest-global` 只在每个目录保留编号最大的 `global_v<version>.safetensors`，不会保留匹配的 outer/fragment/update tensor，也不会把残留 DB+weight 变成可恢复证据。

对应配置 `configs/fs_diloco_tiny_local.yaml` / `fs_diloco_tiny_fragment_local.yaml`。

## 4. Miyabi PBS 批作业

任何 PBS 提交前必须先运行 `bash -n scripts/miyabi/*.pbs`，并逐个确认 `#PBS -W group_list=...` 是当前账户可用的**字面 group ID**，不能保留 `<group_id>` placeholder。还要按工作量、已有实测和收尾预算估算尽可能短的 walltime；若脚本默认明显过长，在命令行用 `qsub -l walltime=HH:MM:SS`覆盖。三项未完成时不要 `qsub`。

| 脚本 | 规模 | 用途 |
|---|---|---|
| `run_1node_debug.pbs` | 1 节点 | debug 队列,1 syncer + 1 learner 同卡 |
| `run_1node_fragment_debug.pbs` | 1 节点 | fragment 冒烟 |
| `run_plan01_regression.pbs` | 1 节点 | 持久状态完整 pytest + tiny full telemetry 回归 |
| `run_2node_debug.pbs` / `run_2node_fragment_debug.pbs` | 2 节点 | 双节点验证 |
| `run_2node_resume_regression.pbs` | 2 节点 | 构造旧代 stopped DB/heartbeat，受控终止 phase-A syncer，再跨节点原地 resume 并运行扩展 Checker |
| `run_9node_gpt2_wikitext2.pbs` | 9 节点 | 8 learner + 1 syncer 短跑 |
| `run_9node_gpt2_wikitext2_5000steps.pbs` | 9 节点 | 名义 5000 本地步、global step 50 终止的正式实验;learner 超过 5000 后继续到 stop |
| `run_8node_colocated_gpt2_wikitext2_5000steps.pbs` | 8 节点 | 实验性部署：rank0 CPU syncer + GPU learner_000，其余七节点各一 learner；双进程 fail-fast 监督。GPT-2 124M 三 seed 中位数劣化 1.83%，通过 ≤10% 门禁，但有一个 +41.4% 离群 seed，故不替代 9 节点默认 |
| `run_9node_fragment_gpt2_wikitext2_5000steps.pbs` | 9 节点 | **当前脚本名与默认配置不一致**：默认指向 `...5000steps_predict_bf16all_cuda.yaml`（full predict，`fragments.enabled=false`），结尾却执行 fragment 断言；不覆盖 `CONFIG` 会失败。要跑其名义 fragment 实验必须显式设 `CONFIG=.../fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml` |
| `run_9node_fragment_gpt2_wikitext2_50x4.pbs` | 9 节点 | 4 个 fragment merge event 的 50-inner-step 实验 |
| `run_9node_fragment_gpt2_wikitext2_50x10.pbs` / `run_9node_no_fragment_gpt2_wikitext2_50x10.pbs` | 9 节点 | 8 learner、inner steps=50、outer steps=10 的 fragment/no-fragment 对照实验 |
| `run_1node_lm_eval.pbs` | 1 节点 | checkpoint 导出 + lm-eval |
| `run_1node_validation_eval.pbs` | 1 节点 | 使用 run resolved config 的专用 validation loss/ppl；校验非空 token、有限指标、checkpoint/source identity 并原子附加 summary |
| `run_syncer_candidate.pbs` / `run_static_learner.pbs` | 各 1 节点独立 job | HA full候选和 static learner array；两者在 runtime import前校验 descriptor/source identity，提交时应覆盖成 workload所需的短 walltime |
| `run_plan02_phase1_{tests,smoke,faults,lock,acceptance_launcher,checker}.pbs` | 1或2节点 | Phase 1关联测试、故障矩阵、SQLite lock边界、独立 1+8 launcher和只读 completed Checker；验证脚本使用分钟级 walltime |

2026-08-06 的 Phase 1 正式验收在 Miyabi 上以 1 个 syncer job、8 个独立 learner array element运行：epoch 1 syncer在 v0提交后的 failpoint被 `SIGKILL`，依赖 job取得 epoch 2并从 DB恢复，随后连续提交 v1–v10；8个 learner分别位于独立GPU节点并正常停止。最终 terminal generation为2，completed Checker返回`PASS`，无runtime failure event。该 workload每个 learner约执行200以上local steps且完成10个global merge，超过50-local-step × 10-global-step文档同步基线；证据为 PBS `2498481/2498482/2498483/2498484[]/2498521`及 `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-130015_phase1-completed-checker_pass.json`。这是恢复/协议验证，不是训练质量结论。

提交与自定义(以 9 节点为例):

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
# 环境变量可覆盖:CONFIG、RUN_ID、SHARED_ROOT、WANDB_MODE、
# HF_DATASETS_OFFLINE、HF_HUB_OFFLINE、FS_DILOCO_HF_WIKITEXT_REPO、
# SYNCER/LEARNER_CUDA_VISIBLE_DEVICES 等(见脚本头部)
```

该 launcher 还支持 `TRAINING_SEED`、`SYNC_SCAN_INTERVAL_SECONDS`、`INGEST_DURING_PUBLISH`、`SYNCER_PUBLISH_DTYPE`、`STALENESS_LAMBDA`、`MAX_STALENESS_VERSIONS`、`GLOBAL_ADOPTION_STRATEGY`、`CAPTURE_TERMINAL_PREDECESSOR_FOR_EVAL` 和 `COMPLETION_MODE` 的显式实验覆盖，并把同一值传播给 syncer 与全部 learner。例如：

```bash
qsub -v TRAINING_SEED=2027,SYNC_SCAN_INTERVAL_SECONDS=0.2,INGEST_DURING_PUBLISH=false \
  scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
```

正式作业在启动 rank 前捕获 source fingerprint；实际覆盖值也会写入 `run_config.resolved.yaml`，不能只依赖 qsub 命令历史。

50×10 专用消融 launcher 也保留窄覆盖：full/no-fragment 脚本接受 `TRAINING_SEED` 与 `PARALLEL_CHECKPOINT_WRITES`；fragment 脚本接受 `TRAINING_SEED` 与 `MATERIALIZE_FULL_EVERY_EVENTS`。这些参数用于 E1/E3 的同 fingerprint 单变量对照，不改变长期默认配置。

脚本约定:第 1 台节点跑 syncer,其余 8 台各跑一个 learner;所有角色共享 `<SHARED_ROOT>/control/syncer_metadata.sqlite3`;节点本地 `$TMPDIR` 只承载非权威 runtime/cache。HF 缓存与 W&B 目录也在脚本里统一设置。WikiText 默认映射到 `Salesforce/wikitext`;已有完整缓存时可在 `qsub -v` 中设置 `HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1`,避免运行期的 Hub HEAD 请求影响启动。

辅助脚本:

```bash
scripts/miyabi/inspect_run.sh <run_root>    # 快速查看 run 状态
python scripts/miyabi/sqlite_shared_fs_probe.py --help  # 共享 SQLite stress/contention/clock/kill-reopen probe
python scripts/miyabi/publication_crash_probe.py --help # full publication crash matrix
python scripts/miyabi/check_plan01_invariants.py --help # current-only/一致性/时延三值 Checker
python scripts/miyabi/measure_pointer_polling.py --help # 固定 pointer 轮询成本测量
python scripts/miyabi/benchmark_syncer_device.py --help # syncer device/dtype microbenchmark
python scripts/miyabi/capture_source_identity.py --help # git/source fingerprint 捕获
```

`scripts/local/prune_runs_without_5000.sh` 是面向特定历史清理任务的破坏性脚本：只看目标目录的**直接子目录**，名称不含字面 substring `5000` 的 run 都是候选；默认 dry-run，`--delete` 才递归删除。目标必须是项目 `runs/` 的严格后代。它不属于通用 runtime maintenance。`publication_crash_probe.py`、`sqlite_shared_fs_probe.py` 和 benchmark/measurement 脚本是独立诊断工具，不会由训练主循环自动运行。

Run 分析方法和实验结果统一维护在 [reports/run_analysis.md](../reports/run_analysis.md)。

正式训练可使用 `submit_train_with_validation.sh` 创建 train job 与
`depend=afterok:<train_job_id>` 的独立一节点 validation job。评估不占用 9 节点训练
allocation 的尾部时间；训练失败时不会生成伪成功结果。结构化主结果位于
`<run_root>/metrics/validation_eval.json`，协议见
[`reports/checked/quality_fix-Q/validation_protocol.md`](../reports/checked/quality_fix-Q/validation_protocol.md)。

递归遍历一个或多个 root，以 `control/stop.json` 作为训练结束标志，把所有已结束
run 中用于实验对比的核心指标抽取到 CSV：

```bash
# 扫描 runs/fs_diloco；默认新建或追加到 reports/run_metrics.csv
python -m fs_diloco.tools.run_metrics_csv \
  runs/fs_diloco

# 可指定多个扫描 root；--overwrite 原子覆盖
python -m fs_diloco.tools.run_metrics_csv \
  runs/fs_diloco reports/checked -o reports/my_metrics.csv --overwrite

# 安装 editable package 后也可使用
fs-diloco-export-run-metrics runs/fs_diloco -o reports/my_metrics.csv
```

默认追加模式会读取已有 CSV，以 `run_id` 或规范化后的 `run_path` 去重，只写入新发现
的 run；重复传入扫描范围或重复执行命令都不会产生重复行。只有 `--overwrite` 会重建
完整输出文件，既有表头不兼容时追加模式会拒绝写入。

每个 run 输出一行，包括 produced/applied/dropped、proposal 利用率、drop reason、local steps、完整训练时间、selected 分布、applied staleness 0/1/2、produced/applied tokens、loss first-10/last-10/全量均值及关键配置。新 run 优先使用 `update_history.jsonl + SQLite` 的终态；旧 run 缺少 history 时，从 committed syncer merge 指标和 `updates_selected` 日志重建 applied/staleness。未完成 run 的未知 proposal 记入 `pending_or_unclassified_updates`，不会误计为 dropped。

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

fragment run 评测用 materialize 出的 `weights/global_v{event}.safetensors`。当前 `eval_lm_harness.resolve_checkpoint` 不会自动把 fragment latest 的 `materialized_weight_path` 当成 `weight_path`，因此需同时显式传 `--checkpoint <materialized_weight_path> --run-root <run_root>`。

## 6. 常见问题排查

| 现象 | 排查 |
|---|---|
| learner 卡在启动 | 是否在等 `param_index.json`/`latest.json`(syncer 没起来或 shared_root 不一致);`wait_for_json` 默认 1800s 超时 |
| syncer 一直 `quorum_wait` | learner 是否在替换 `updates/latest/learner_XXX.json`;run_id 是否一致;staleness 窗口是否太紧;最终会 `no_progress_timeout` 停机 |
| 大量 `dropped(missing_file)` | payload 是否被外部清理/移动,或共享文件系统是否出现可见性/读取错误;learner 不再自行清理 payload |
| 大量 `dropped(too_stale)` | learner 太慢或 `max_staleness_versions` 太小；`stale` 是旧文档/历史 run 的兼容称呼 |
| resume 后重复合并了旧更新? | DB commit 是权威边界;resume 重置 selected、重建 latest,proposal frontier/唯一约束阻止重放。先检查 analysis 的 integrity 与 committed version |
| SQLite 报锁/IO 错误 | 保留现场并运行共享 FS probe;确认 DB 使用 `journal_mode=delete`,`synchronous=2(FULL)`,且没有额外 writer/WAL 文件 |
| W&B 初始化失败 | import/init 会降级为不上报；离线产物在 `logs/wandb/`，事后可 `wandb sync`。初始化后的 SDK `log/summary/finish` 异常并非全部局部捕获，遇到 runtime error 仍需查 syncer traceback |
