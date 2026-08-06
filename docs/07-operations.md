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

### 2.1 HA full static：初始化、独立作业与人工接管

本节是full + static membership路径。先在同一 source tree捕获 identity并运行唯一 initializer；run root必须不存在：

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

从 `control/run_descriptor.json` 读取 `descriptor_sha256`，然后分别提交角色。下面的 walltime只是 tiny示例；正式提交应根据 inner/global steps、已测单步和发布时间重新估算，并使用能覆盖启动波动、预期运行和完整收尾的最短实用值。缩短请求是为了更快取得配额，前提是保留足够余量让测试可靠跑完：

```bash
vars='FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256=<sha>,PROJECT_ROOT=/absolute/project'
qsub -l walltime=00:02:00 -v "$vars" scripts/miyabi/run_syncer_candidate.pbs
qsub -l walltime=00:02:00 -r y -J 0-7 -v "$vars" scripts/miyabi/run_static_learner.pbs
```

syncer异常退出后，只需用相同 descriptor变量重新 `qsub` 一个 `run_syncer_candidate.pbs`。不要删除 fixed cache、DB或旧 epoch目录；successor从 DB恢复并修复 canonical control。异常 leader可能留下 canonical `error` generation，它是诊断记录而不是最终 stop，learner会继续等待/reconcile，successor可将它推进为更高 generation的正常终态。若旧 process暂停在 write transaction内并持续持有 SQLite lock，先通过 operator确认并终止那个旧 job，再提交/等待 successor；系统不会自动 `qdel`。`coordination.recovery_submission.enabled` 默认false，开启后 learner只能去重并提交 candidate，candidate仍必须竞争 lease。

completed run先在compute node生成与该run descriptor绑定的matched性能artifact，再用只读 Checker验收。两次PBS提交都应根据最近实测显式覆盖成尽可能短、但为启动波动、预期运行和完整收尾保留充分余量的walltime：

```bash
matched_job=$(qsub -l walltime=00:01:00 \
  -v "FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,OUTPUT=/absolute/report/path/phase1-matched-performance_pass.json" \
  scripts/miyabi/run_plan02_phase1_matched_performance.pbs)

qsub -l walltime=00:00:15 -W "depend=afterok:${matched_job}" \
  -v "FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,MATCHED_PERFORMANCE_ARTIFACT=/absolute/report/path/phase1-matched-performance_pass.json,OUTPUT=/absolute/report/path/phase1-completed-checker_pass.json" \
  scripts/miyabi/run_plan02_phase1_checker.pbs
```

matched artifact在同一shared filesystem上用细粒度AB/BA配对块交错比较健康leader只读candidate observer与静默baseline的fenced transaction p99，并用SQLite trace核验candidate没有尝试writer transaction；checkpoint门禁从目标配置构建同一model/seed/tensor，交替比较HA publication与Plan 01 legacy baseline。completed Checker会复算冻结阈值、核对每块采样/观察证据，并要求artifact的run/descriptor/source/config identity全部与被验收run一致；缺失、错配或性能回归都返回`BLOCKED`。

### 2.2 HA full dynamic：bootstrap、扩容与关闭

dynamic run同样先捕获source identity并执行唯一initializer，但配置必须同时满足`membership.mode=dynamic`、Syncer HA开启、fragment关闭。每个bootstrap learner必须取得唯一slot并由独立PBS job启动；每次进程启动自行生成`learner_li_<uuid4>`，不要传`--learner-id`或`--num-learners`。

PBS正式启动还必须把每个已接受的bootstrap qsub job ID立刻写入identity-bound `control/bootstrap_scheduler_jobs.json`。这是admission与scheduler reconciliation的权威输入，不能先批量qsub、最后才补manifest，也不能只保留shell变量。learner可能在manifest更新前已写registration，因此leader会把scheduler-bearing request持久化为pending、保留其文件，并仅在launch row出现精确job receipt绑定后重试admission；错误绑定会被拒绝。仓库的Phase 2 launcher实现了“每次qsub后原子持久化receipt与manifest、失败保留partial artifact且不自动qdel”的完整顺序；正式动态验收应直接使用：

```bash
# 提交前：bash -n scripts/miyabi/*.pbs，确认literal group ID，并根据相邻实测覆盖walltime。
qsub -l walltime=00:00:30 \
  -v PHASE2_ACCEPTANCE_KIND=g9,RUN_ID=my_dynamic_run \
  scripts/miyabi/run_plan02_phase2_acceptance_launcher.pbs
```

对自建launcher，角色命令必须保持与正式实现相同的identity和授权契约：syncer使用`run_syncer_candidate.pbs`；bootstrap learner使用`run_dynamic_learner.pbs`并传`BOOTSTRAP_SLOT=0...N-1`；scale replacement只能传leader创建的`FS_DILOCO_LAUNCH_REQUEST_ID`。`scaling.learner_walltime`在启用自动扩容时必填，`scaling.learner_queue`可显式选择目标队列；两者都在运行前冻结进descriptor。queued/running job即使超过request TTL仍占reserved capacity，不要手工删除DB/outbox记录。

manual close必须使用identity-bound工具，不能手写半截drain/stop JSON：

```bash
python -m fs_diloco.tools.request_dynamic_close \
  --shared-root /shared/runs/my_dynamic_run \
  --expected-descriptor-sha256 '<descriptor_sha256>'
```

leader接收后在transaction中关闭admission并冻结terminal上限，发布canonical drain generation。健康learner会在cycle边界提交final pointer和drain ack；未响应实例只会在timeout后经membership fence撤销。operator应等待canonical terminal和Phase 2 completed Checker，不要通过删除registration/pointer或伪造heartbeat来催促闭合。

## 3. 本地冒烟(无 GPU、无外网)

```bash
scripts/local/run_tiny_2proc_smoke.sh      # synthetic-tiny 模型 + 合成数据,CPU 上 1 syncer + 2 learner
python -m fs_diloco.tools.clean_run runs/fs_diloco/<EXACT_RUN_ID> \
  --evidence reports/DOING/<PLAN_ID>/artifacts/<COMPLETED_PASS>.json
python -m fs_diloco.tools.clean_run runs/fs_diloco/<EXACT_RUN_ID> \
  --evidence reports/DOING/<PLAN_ID>/artifacts/<COMPLETED_PASS>.json \
  --delete --manifest-output reports/DOING/<PLAN_ID>/artifacts/<CLEANUP>.json
scripts/local/clean_run.sh runs/fs_diloco  # 递归预览将清理的 safetensors 和 .db
scripts/local/clean_run.sh --delete runs/fs_diloco
scripts/local/clean_run.sh --delete --keep-latest-global runs/fs_diloco
```

Python `clean_run`是已完成实验的首选收口工具：只接受project `runs/`下一个精确run目录、同run且无error的`PASS` completion artifact、匹配的terminal stop/summary和已停止的全部learner；authority SQLite sidecar存在时拒绝。默认只输出精确inventory。`--delete`还要求在`reports/`内创建新的manifest，保留authority DB、current weight/outer、control/config、syncer/candidate日志和一份代表性learner日志，只删除已由completion artifact覆盖的重复learner日志、terminal heartbeat/pointer、offline W&B cache和raw update telemetry；候选在inventory后变化会停止删除并把manifest标为`failed`。

legacy `clean_run.sh`只扫描项目`runs/`内的目标目录；默认dry-run，必须传`--delete`才删除。它的实际glob是递归`*.safetensors`和`*.db`：当前权威库名为`syncer_metadata.sqlite3`，**不会**被该glob删除，但global/outer/update/fragment tensor会被删掉，run仍无法恢复。`--keep-latest-global`只在每个目录保留编号最大的`global_v<version>.safetensors`，不会保留匹配的outer/fragment/update tensor，也不会把残留DB+weight变成可恢复证据。它没有Python工具的completion-evidence和manifest门禁，不应替代正式实验清理。

对应配置 `configs/fs_diloco_tiny_local.yaml` / `fs_diloco_tiny_fragment_local.yaml`。

## 4. Miyabi PBS 批作业

任何 PBS 提交前必须先运行 `bash -n scripts/miyabi/*.pbs`，并逐个确认 `#PBS -W group_list=...` 是当前账户可用的**字面 group ID**，不能保留 `<group_id>` placeholder。还要按工作量、已有实测、启动/运行波动和收尾预算估算尽可能短但有充分余量的 walltime；目标是更快取得配额并可靠完成，而不是把时间压到容易超时。若脚本默认明显过长，在命令行用 `qsub -l walltime=HH:MM:SS`覆盖。三项未完成时不要 `qsub`。

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
| `run_syncer_candidate.pbs` / `run_static_learner.pbs` | 各 1 节点独立 job | HA full候选和 static learner array；两者在 runtime import前校验 descriptor/source identity，提交时应覆盖成 workload所需且留有充分完成余量的短 walltime |
| `run_plan02_phase1_{tests,smoke,faults,lock,acceptance_launcher,matched_performance,checker}.pbs` | 1或2节点 | Phase 1关联测试、故障矩阵、SQLite lock边界、独立 1+8 launcher、matched性能门禁和只读 completed Checker；验证脚本使用由相邻实测估算、留有充分完成余量的秒/分钟级短walltime |
| `run_dynamic_learner.pbs` | 1节点独立job | dynamic bootstrap或scale learner；pre-import校验descriptor/source，要求唯一`BOOTSTRAP_SLOT`或`FS_DILOCO_LAUNCH_REQUEST_ID` |
| `run_plan02_phase2_{tests,evidence_tests,acceptance_launcher,chaos_checker,matched_launcher,matched_checker,completed_checker}.pbs` | 1至9个并发节点 | Phase 2 focused/full回归、G8/G9 crash/churn/duplicate/drain验收、static/dynamic v120 matched门禁和只读completed Checker；launcher逐job持久化receipt与bootstrap manifest |

2026-08-06 的最终 Phase 1 正式验收绑定clean commit `36762854bfcbbc23b71ab838913023d64cf37b5e`，在 Miyabi 上以1个syncer job和8个独立learner array element运行：epoch 1 syncer在v0 DB提交后的failpoint被`SIGKILL`，依赖job取得epoch 2并恢复，随后连续提交v1–v10；8个learner分别位于独立GPU节点并正常停止。最终terminal generation为2、5120 seen tokens、120次lease renew和457次business transaction均无失败，stale epoch commit与canonical adoption错误均为0，completed Checker返回`PASS`且无runtime failure event。matched门禁另以400+400个business样本和100+100个checkpoint样本通过两项p99阈值，并验证健康candidate writer transaction attempt为0。该workload每个learner约执行200以上local steps且完成10个global merge，超过50-local-step × 10-global-step文档同步基线；证据为PBS `2499329/2499331/2499332/2499333[]/2499345/2499349`及`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1624_phase1-completed-checker_pass.json`。这是恢复、协议和控制面性能验证，不是训练质量结论。

2026-08-07 的Phase 2审查整改后最终验收绑定clean commit `61f571bbe4460b257abe8452c2ea63df79515b29`和source fingerprint `sha256:cdf8f01bdb6f4bfd62dbe9a1103bca0a14f8b029ef3eaf12d8c77221aa94d0c0`。G7/兼容回归PBS `2501753/2501752`与G8 launcher/checker链`2501754/2501758`均返回`PASS`。G9 launcher `2501765`及其独立crash/successor/bootstrap `2501767–2501776`、duplicate `2501777`和checker `2501778` jobs在最多9个并发节点内完成v120：8个bootstrap slot恰好admit一次，永久终止一个成员后两个唯一low observation只创建一个scale request，replacement恢复8个current成员并复用stream epoch 1，duplicate physical job被拒绝，暂停成员恢复，最后dynamic drain/ack闭合；总计1,516,128 tokens，每cycle 51 local steps。matched launcher/checker `2501807/2501826`的同source/config/model/data/seed/v120 static为101.949秒、dynamic为47.348秒，冻结`max(0,dynamic-static)/static`为0，小于5%门槛。completed Checker `2501846`返回`PASS`，确认MEM-01至MEM-20、每个v1–v120恰有一个对应`merge:<version>` observation、starvation generation连续、schema v3 integrity、64条active/59条archived observation、有界launch/instance/stream状态及零blocking failure event；artifact分别为`reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260807-014345_phase2-completed_pass.json`和`20260807-014003_phase2-matched-performance_pass.json`。该51×120 workload超过50×10文档同步基线，但仍只验证恢复、成员、调度和控制面性能，不形成训练质量结论。

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
