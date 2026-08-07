# 07 运行与运维

> 术语约定见 [00-glossary.md](00-glossary.md)。本节所有命令均以仓库主工作树为基准。

## 1. 环境

- 项目虚拟环境:`.venv/`(PBS 脚本默认 `PYTHON_BIN=$PROJECT_ROOT/.venv/bin/python`);
- 依赖:torch、transformers、datasets、safetensors、pyyaml、nvidia-ml-py(GPU 利用率采样)、wandb(可选)、lm-eval(评测用,可选);
- Miyabi 上**登录节点只做静态检查**,运行必须在 PBS 计算/调试节点:

在 PBS 计算节点中根据 `.python-version` 和 `pyproject.toml` 创建或同步完整开发/评测环境:

```bash
uv sync --all-extras
source .venv/bin/activate
```

项目通过 setuptools 以 editable 模式安装,`fs-diloco-*` 命令与 `python -m fs_diloco.*` 入口均指向当前工作区源码。

登录节点上的静态检查:

```bash
bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh
python -m compileall -q fs_diloco
```

## 2. 手动启动(任意机器)

仓库内配置和训练/评测启动脚本默认把 run 与独立 launcher 日志集中写到主工作树
`/work/xg24i002/x10041/fsb_decoupled_diloco/{runs,logs}`。从其他 worktree
启动时仍使用该绝对根目录;如确需隔离,可显式传入 `--shared-root`、`SHARED_ROOT`
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
- 中途停止:向 run 目录原子写合法 `control/stop.json` 可让 learner 退出;syncer 不把该文件当自己的直接停止开关,但在全部 learner 写出 stopped 心跳后会经末端排空以 `input_exhausted` 收束。手工写半截 JSON 不安全,应使用临时文件 + rename。

恢复时使用同一个 run id/shared root,并把配置中的 `init.resume` 设为 `true`。恢复不会信任 `latest.json`:持久 DB 必须存在且通过 integrity/identity/checkpoint 一致性校验,否则 syncer fail closed。不要移动或清理 `control/syncer_metadata.sqlite3`。启动 resume syncer 后再启动新 learner;resume 事务会把旧代 learner 行重置为 `unknown`,并隔离切代时心跳指针的完整内容,旧 `stopped` 不会触发本代 `input_exhausted`。检查 `run_resumed` 后应先看到新代 `learner_liveness_updated(active>0)`,再看到严格更大版本的 `outer_step_applied/global_published`。

任何可读的非 error `control/stop.json.reason` 或 `summary.json.stop_reason` 都会令 resume fail closed,
不要求两文件成对一致;不得手工删除终态文件重开。`error` 终态可由 resume 原子归档后继续。可用 Checker 的
`--require-resume-progress --resume-artifact <path>` 同时验证 DB/latest/checkpoint、旧心跳
隔离栅栏、新代 active 与下一次 commit;stdout 仍只有三值结果。

### 2.1 HA full static:初始化、独立作业与人工接管

本节是 full + static 成员路径。先在同一 source tree 捕获身份并运行唯一 initializer;run root 必须不存在:

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

从 `control/run_descriptor.json` 读取 `descriptor_sha256`,然后分别提交角色。下面的 walltime 只是 tiny 示例;正式提交应根据 inner/global steps、已测单步和发布时间重新估算,并使用能覆盖启动波动、预期运行和完整收尾的最短实用值。缩短请求是为了更快取得配额,前提是保留足够余量让测试可靠跑完:

```bash
vars='FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256=<sha>,PROJECT_ROOT=/absolute/project'
qsub -l walltime=00:02:00 -v "$vars" scripts/miyabi/run_syncer_candidate.pbs
qsub -l walltime=00:02:00 -r y -J 0-7 -v "$vars" scripts/miyabi/run_static_learner.pbs
```

syncer 异常退出后,只需用相同 descriptor 变量重新 `qsub` 一个 `run_syncer_candidate.pbs`。不要删除 fixed cache、DB 或旧 epoch 目录;后继者从 DB 恢复并修复权威控制面。异常 leader 可能留下权威 `error` 世代,它是诊断记录而不是最终 stop,learner 会继续等待/对齐,后继者可将它推进为更高世代的正常终态。若旧进程暂停在写事务内并持续持有 SQLite lock,先通过 operator 确认并终止那个旧 job,再提交/等待后继者;系统不会自动 `qdel`。`coordination.recovery_submission.enabled` 默认 false,开启后 learner 只能去重并提交 candidate,candidate 仍必须竞争租约。

已完成的 run 先在 compute node 生成与该 run descriptor 绑定的 matched 性能产物,再用只读 Checker 验收。两次 PBS 提交都应根据最近实测显式覆盖成尽可能短、但为启动波动、预期运行和完整收尾保留充分余量的 walltime:

```bash
matched_job=$(qsub -l walltime=00:01:00 \
  -v "FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,OUTPUT=/absolute/report/path/phase1-matched-performance_pass.json" \
  scripts/miyabi/run_plan02_phase1_matched_performance.pbs)

qsub -l walltime=00:00:15 -W "depend=afterok:${matched_job}" \
  -v "FS_DILOCO_SHARED_ROOT=/shared/runs/my_ha_run,MATCHED_PERFORMANCE_ARTIFACT=/absolute/report/path/phase1-matched-performance_pass.json,OUTPUT=/absolute/report/path/phase1-completed-checker_pass.json" \
  scripts/miyabi/run_plan02_phase1_checker.pbs
```

matched 产物在同一 shared filesystem 上用细粒度 AB/BA 配对块交错比较健康 leader 只读 candidate observer 与静默 baseline 的隔离事务 p99,并用 SQLite trace 核验 candidate 没有尝试 writer transaction;checkpoint 门禁从目标配置构建同一 model/seed/tensor,交替比较 HA publication 与 Plan 01 legacy baseline。completed Checker 会复算冻结阈值、核对每块采样/观察证据,并要求产物的 run/descriptor/source/config identity 全部与被验收 run 一致;缺失、错配或性能回归都返回 `BLOCKED`。

### 2.2 HA full dynamic:引导、扩容与关闭

dynamic run 同样先捕获 source identity 并执行唯一 initializer,但配置必须同时满足 `membership.mode=dynamic`、Syncer HA 开启、fragment 关闭。每个 bootstrap learner 必须取得唯一 slot 并由独立 PBS job 启动;每次进程启动自行生成 `learner_li_<uuid4>`,不要传 `--learner-id` 或 `--num-learners`。

PBS 正式启动还必须把每个已接受的 bootstrap qsub job ID 立刻写入 identity-bound `control/bootstrap_scheduler_jobs.json`。这是准入与调度对账的权威输入,不能先批量 qsub、最后才补 manifest,也不能只保留 shell 变量。learner 可能在 manifest 更新前已写注册,因此 leader 会把带调度器的请求持久化为 pending、保留其文件,并仅在 launch 行出现精确 job 回执绑定后重试准入;错误绑定会被拒绝。仓库的 Phase 2 launcher 实现了「每次 qsub 后原子持久化回执与 manifest、失败保留 partial 产物且不自动 qdel」的完整顺序;正式动态验收应直接使用:

```bash
# 提交前:bash -n scripts/miyabi/*.pbs,确认 literal group ID,并根据相邻实测覆盖 walltime。
qsub -l walltime=00:00:30 \
  -v PHASE2_ACCEPTANCE_KIND=g9,RUN_ID=my_dynamic_run \
  scripts/miyabi/run_plan02_phase2_acceptance_launcher.pbs
```

对自建 launcher,角色命令必须保持与正式实现相同的身份和授权契约:syncer 使用 `run_syncer_candidate.pbs`;bootstrap learner 使用 `run_dynamic_learner.pbs` 并传 `BOOTSTRAP_SLOT=0...N-1`;scale replacement 只能传 leader 创建的 `FS_DILOCO_LAUNCH_REQUEST_ID`。`scaling.learner_walltime` 在启用自动扩容时必填,`scaling.learner_queue` 可显式选择目标队列;两者都在运行前冻结进 descriptor。queued/running job 即使超过 request TTL 仍占预留容量,不要手工删除 DB/outbox 记录。

manual close 必须使用 identity-bound 工具,不能手写半截 drain/stop JSON:

```bash
python -m fs_diloco.tools.request_dynamic_close \
  --shared-root /shared/runs/my_dynamic_run \
  --expected-descriptor-sha256 '<descriptor_sha256>'
```

leader 接收后在事务中关闭准入并冻结终止上限,发布权威排空世代。健康 learner 会在周期边界提交最终指针和排空确认;未响应实例只会在超时后经成员隔离撤销。operator 应等待权威终态和 Phase 2 completed Checker,不要通过删除 registration/pointer 或伪造 heartbeat 来催促闭合。

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

Python `clean_run` 是已完成实验的首选收口工具:只接受 project `runs/` 下一个精确 run 目录、同 run 且无 error 并绑定当前 terminal final version 的 `PASS` completion 产物、匹配的 terminal stop/summary 和已停止的全部 learner;authority SQLite sidecar 存在时拒绝。默认只输出精确清单。`--delete` 还要求在 `reports/` 内创建新的 manifest,保留 authority DB、current weight/outer、control/config、所有 fsync-before-prune history(包括 `metrics/update_history.jsonl`)、syncer/candidate 日志和一份代表性 learner 日志,只删除重复 learner 日志、terminal heartbeat/pointer/payload、offline W&B cache、learner metrics/update manifest 等可重建遥测及临时文件;候选在清单后变化会停止删除并把 manifest 标为 `failed`。旧实现的 `20260807-0150_phase2-g9-cleanup.json` 已从主 G9 run 不可恢复地删除 2,563,263-byte `update_history.jsonl`;cleanup 前的 completed 产物仍冻结 PASS 结论,未清理的 detached coherent run 保留可复查原始 history,但主 run 不能再完整重跑 completed Checker。

legacy `clean_run.sh` 只扫描项目 `runs/` 内的目标目录;默认 dry-run,必须传 `--delete` 才删除。它的实际 glob 是递归 `*.safetensors` 和 `*.db`:当前权威库名为 `syncer_metadata.sqlite3`,**不会**被该 glob 删除,但 global/outer/update/fragment tensor 会被删掉,run 仍无法恢复。`--keep-latest-global` 只在每个目录保留编号最大的 `global_v<version>.safetensors`,不会保留匹配的 outer/fragment/update tensor,也不会把残留 DB+weight 变成可恢复证据。它没有 Python 工具的 completion-evidence 和 manifest 门禁,不应替代正式实验清理。

对应配置 `configs/fs_diloco_tiny_local.yaml` / `fs_diloco_tiny_fragment_local.yaml`。

## 4. Miyabi PBS 批作业

任何 PBS 提交前必须先运行 `bash -n scripts/miyabi/*.pbs`,并逐个确认 `#PBS -W group_list=...` 是当前账户可用的**字面 group ID**,不能保留 `<group_id>` 占位符。还要按工作量、已有实测、启动/运行波动和收尾预算估算尽可能短但有充分余量的 walltime;目标是更快取得配额并可靠完成,而不是把时间压到容易超时。若脚本默认明显过长,在命令行用 `qsub -l walltime=HH:MM:SS` 覆盖。三项未完成时不要 `qsub`。

| 脚本 | 规模 | 用途 |
|---|---|---|
| `run_1node_debug.pbs` | 1 节点 | debug 队列,1 syncer + 1 learner 同卡 |
| `run_1node_fragment_debug.pbs` | 1 节点 | fragment 冒烟 |
| `run_plan01_regression.pbs` | 1 节点 | 持久状态完整 pytest + tiny full telemetry 回归 |
| `run_2node_debug.pbs` / `run_2node_fragment_debug.pbs` | 2 节点 | 双节点验证 |
| `run_2node_resume_regression.pbs` | 2 节点 | 构造旧代 stopped DB/heartbeat,受控终止 phase-A syncer,再跨节点原地 resume 并运行扩展 Checker |
| `run_9node_gpt2_wikitext2.pbs` | 9 节点 | 8 learner + 1 syncer 短跑 |
| `run_9node_gpt2_wikitext2_5000steps.pbs` | 9 节点 | 名义 5000 本地步、global step 50 终止的正式实验;learner 超过 5000 后继续到 stop |
| `run_8node_colocated_gpt2_wikitext2_5000steps.pbs` | 8 节点 | 实验性部署:rank0 CPU syncer + GPU learner_000,其余七节点各一 learner;双进程 fail-fast 监督。GPT-2 124M 三 seed 中位数劣化 1.83%,通过 ≤10% 门禁,但有一个 +41.4% 离群 seed,故不替代 9 节点默认 |
| `run_9node_fragment_gpt2_wikitext2_5000steps.pbs` | 9 节点 | **当前脚本名与默认配置不一致**:默认指向 `...5000steps_predict_bf16all_cuda.yaml`(full predict,`fragments.enabled=false`),结尾却执行 fragment 断言;不覆盖 `CONFIG` 会失败。要跑其名义 fragment 实验必须显式设 `CONFIG=.../fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml` |
| `run_9node_fragment_gpt2_wikitext2_50x4.pbs` | 9 节点 | 4 个 fragment merge event 的 50-inner-step 实验 |
| `run_9node_fragment_gpt2_wikitext2_50x10.pbs` / `run_9node_no_fragment_gpt2_wikitext2_50x10.pbs` | 9 节点 | 8 learner、inner steps=50、outer steps=10 的 fragment/no-fragment 对照实验 |
| `run_1node_lm_eval.pbs` | 1 节点 | checkpoint 导出 + lm-eval |
| `run_1node_validation_eval.pbs` | 1 节点 | 使用 run resolved config 的专用 validation loss/ppl;校验非空 token、有限指标、checkpoint/source identity 并原子附加 summary |
| `run_syncer_candidate.pbs` / `run_static_learner.pbs` | 各 1 节点独立 job | HA full 候选和 static learner array;两者在 runtime import 前校验 descriptor/source identity,提交时应覆盖成 workload 所需且留有充分完成余量的短 walltime |
| `run_plan02_phase1_{tests,smoke,faults,lock,acceptance_launcher,matched_performance,checker}.pbs` | 1 或 2 节点 | Phase 1 关联测试、故障矩阵、SQLite lock 边界、独立 1+8 launcher、matched 性能门禁和只读 completed Checker;验证脚本使用由相邻实测估算、留有充分完成余量的秒/分钟级短 walltime |
| `run_dynamic_learner.pbs` | 1 节点独立 job | dynamic bootstrap 或 scale learner;pre-import 校验 descriptor/source,要求唯一 `BOOTSTRAP_SLOT` 或 `FS_DILOCO_LAUNCH_REQUEST_ID` |
| `run_plan02_phase2_{tests,evidence_tests,acceptance_launcher,chaos_checker,matched_launcher,matched_checker,completed_checker}.pbs` | 1 至 9 个并发节点 | Phase 2 focused/full 回归、G8/G9 crash/churn/duplicate/drain 验收、static/dynamic v120 matched 门禁和只读 completed Checker;launcher 逐 job 持久化回执与 bootstrap manifest |

2026-08-06 的最终 Phase 1 正式验收绑定 clean commit `36762854bfcbbc23b71ab838913023d64cf37b5e`,在 Miyabi 上以 1 个 syncer job 和 8 个独立 learner array element 运行:epoch 1 syncer 在 v0 DB 提交后的 failpoint 被 `SIGKILL`,依赖 job 取得 epoch 2 并恢复,随后连续提交 v1–v10;8 个 learner 分别位于独立 GPU 节点并正常停止。最终 terminal generation 为 2、5120 seen tokens、120 次租约续约和 457 次业务事务均无失败,stale epoch commit 与权威采纳错误均为 0,completed Checker 返回 `PASS` 且无 runtime failure event。matched 门禁另以 400+400 个业务样本和 100+100 个 checkpoint 样本通过两项 p99 阈值,并验证健康 candidate writer transaction attempt 为 0。该 workload 每个 learner 约执行 200 以上 local steps 且完成 10 个 global merge,超过 50-local-step × 10-global-step 文档同步基线;证据为 PBS `2499329/2499331/2499332/2499333[]/2499345/2499349` 及 `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-1624_phase1-completed-checker_pass.json`。这是恢复、协议和控制面性能验证,不是训练质量结论。

2026-08-07 的 Phase 2 审查整改后最终验收绑定 clean commit `61f571bbe4460b257abe8452c2ea63df79515b29` 和 source fingerprint `sha256:cdf8f01bdb6f4bfd62dbe9a1103bca0a14f8b029ef3eaf12d8c77221aa94d0c0`。G7/兼容回归 PBS `2501753/2501752` 与 G8 launcher/checker 链 `2501754/2501758` 均返回 `PASS`。G9 launcher `2501765` 及其独立 crash/successor/bootstrap `2501767–2501776`、duplicate `2501777` 和 checker `2501778` jobs 在最多 9 个并发节点内完成 v120:8 个 bootstrap slot 恰好 admit 一次,永久终止一个成员后两个唯一 low observation 只创建一个 scale request,replacement 恢复 8 个 current 成员并复用 stream epoch 1,duplicate physical job 被拒绝,暂停成员恢复,最后 dynamic drain/ack 闭合;总计 1,516,128 tokens,每 cycle 51 local steps。matched launcher/checker `2501807/2501826` 的同 source/config/model/data/seed/v120 static 为 101.949 秒、dynamic 为 47.348 秒,冻结 `max(0,dynamic-static)/static` 为 0,小于 5% 门槛。completed Checker `2501846` 返回 `PASS`,确认 MEM-01 至 MEM-20、每个 v1–v120 恰有一个对应 `merge:<version>` observation、starvation generation 连续、schema v3 integrity、64 条 active/59 条 archived observation、有界 launch/instance/stream 状态及零 blocking failure event;产物分别为 `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260807-014345_phase2-completed_pass.json` 和 `20260807-014003_phase2-matched-performance_pass.json`。该 51×120 workload 超过 50×10 文档同步基线,但仍只验证恢复、成员、调度和控制面性能,不形成训练质量结论。

提交与自定义(以 9 节点为例):

```bash
qsub scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
# 环境变量可覆盖:CONFIG、RUN_ID、SHARED_ROOT、WANDB_MODE、
# HF_DATASETS_OFFLINE、HF_HUB_OFFLINE、FS_DILOCO_HF_WIKITEXT_REPO、
# SYNCER/LEARNER_CUDA_VISIBLE_DEVICES 等(见脚本头部)
```

该 launcher 还支持 `TRAINING_SEED`、`SYNC_SCAN_INTERVAL_SECONDS`、`INGEST_DURING_PUBLISH`、`SYNCER_PUBLISH_DTYPE`、`STALENESS_LAMBDA`、`MAX_STALENESS_VERSIONS`、`GLOBAL_ADOPTION_STRATEGY`、`CAPTURE_TERMINAL_PREDECESSOR_FOR_EVAL` 和 `COMPLETION_MODE` 的显式实验覆盖,并把同一值传播给 syncer 与全部 learner。例如:

```bash
qsub -v TRAINING_SEED=2027,SYNC_SCAN_INTERVAL_SECONDS=0.2,INGEST_DURING_PUBLISH=false \
  scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs
```

正式作业在启动 rank 前捕获 source fingerprint;实际覆盖值也会写入 `run_config.resolved.yaml`,不能只依赖 qsub 命令历史。

50×10 专用消融 launcher 也保留窄覆盖:full/no-fragment 脚本接受 `TRAINING_SEED` 与 `PARALLEL_CHECKPOINT_WRITES`;fragment 脚本接受 `TRAINING_SEED` 与 `MATERIALIZE_FULL_EVERY_EVENTS`。这些参数用于 E1/E3 的同 fingerprint 单变量对照,不改变长期默认配置。

脚本约定:第 1 台节点跑 syncer,其余 8 台各跑一个 learner;所有角色共享 `<SHARED_ROOT>/control/syncer_metadata.sqlite3`;节点本地 `$TMPDIR` 只承载非权威 runtime/cache。HF 缓存与 W&B 目录也在脚本里统一设置。WikiText 默认映射到 `Salesforce/wikitext`;已有完整缓存时可在 `qsub -v` 中设置 `HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1`,避免运行期的 Hub HEAD 请求影响启动。

辅助脚本:

```bash
scripts/miyabi/inspect_run.sh <run_root>    # 快速查看 run 状态
python scripts/miyabi/sqlite_shared_fs_probe.py --help  # 共享 SQLite stress/contention/clock/kill-reopen probe
python scripts/miyabi/publication_crash_probe.py --help # full publication crash matrix
python scripts/miyabi/check_plan01_invariants.py --help # current-only/一致性/时延三值 Checker
python scripts/miyabi/measure_pointer_polling.py --help # 固定指针轮询成本测量
python scripts/miyabi/benchmark_syncer_device.py --help # syncer device/dtype 微基准
python scripts/miyabi/capture_source_identity.py --help # git/source fingerprint 捕获
```

`scripts/local/prune_runs_without_5000.sh` 是面向特定历史清理任务的破坏性脚本:只看目标目录的**直接子目录**,名称不含字面 substring `5000` 的 run 都是候选;默认 dry-run,`--delete` 才递归删除。目标必须是项目 `runs/` 的严格后代。它不属于通用 runtime maintenance。`publication_crash_probe.py`、`sqlite_shared_fs_probe.py` 和 benchmark/measurement 脚本是独立诊断工具,不会由训练主循环自动运行。

Run 分析方法和实验结果统一维护在 [reports/checked/run_analysis.md](../reports/checked/run_analysis.md)。

正式训练可使用 `submit_train_with_validation.sh` 创建 train job 与
`depend=afterok:<train_job_id>` 的独立一节点 validation job。评估不占用 9 节点训练
allocation 的尾部时间;训练失败时不会生成伪成功结果。结构化主结果位于
`<run_root>/metrics/validation_eval.json`,协议见
[`reports/checked/quality_fix-Q/validation_protocol.md`](../reports/checked/quality_fix-Q/validation_protocol.md)。

递归遍历一个或多个 root,以 `control/stop.json` 作为训练结束标志,把所有已结束
run 中用于实验对比的核心指标抽取到 CSV:

```bash
# 扫描 runs/fs_diloco;默认新建或追加到 reports/run_metrics.csv
python -m fs_diloco.tools.run_metrics_csv \
  runs/fs_diloco

# 可指定多个扫描 root;--overwrite 原子覆盖
python -m fs_diloco.tools.run_metrics_csv \
  runs/fs_diloco reports/checked -o reports/my_metrics.csv --overwrite

# 安装 editable package 后也可使用
fs-diloco-export-run-metrics runs/fs_diloco -o reports/my_metrics.csv
```

默认追加模式会读取已有 CSV,以 `run_id` 或规范化后的 `run_path` 去重,只写入新发现
的 run;重复传入扫描范围或重复执行命令都不会产生重复行。只有 `--overwrite` 会重建
完整输出文件,既有表头不兼容时追加模式会拒绝写入。

每个 run 输出一行,包括 produced/applied/dropped、proposal 利用率、drop reason、local steps、完整训练时间、selected 分布、applied staleness 0/1/2、produced/applied tokens、loss first-10/last-10/全量均值及关键配置。新 run 优先使用 `update_history.jsonl + SQLite` 的终态;旧 run 缺少 history 时,从 committed syncer merge 指标和 `updates_selected` 日志重建 applied/staleness。未完成 run 的未知 proposal 记入 `pending_or_unclassified_updates`,不会误计为 dropped。

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

fragment run 评测用物化出的 `weights/global_v{event}.safetensors`。当前 `eval_lm_harness.resolve_checkpoint` 不会自动把 fragment latest 的 `materialized_weight_path` 当成 `weight_path`,因此需同时显式传 `--checkpoint <materialized_weight_path> --run-root <run_root>`。

## 6. 常见问题排查

| 现象 | 排查 |
|---|---|
| learner 卡在启动 | 是否在等 `param_index.json`/`latest.json`(syncer 没起来或 shared_root 不一致);`wait_for_json` 默认 1800s 超时 |
| syncer 一直 `quorum_wait` | learner 是否在替换 `updates/latest/learner_XXX.json`;run_id 是否一致;陈旧度窗口是否太紧;最终会 `no_progress_timeout` 停机 |
| 大量 `dropped(missing_file)` | payload 是否被外部清理/移动,或共享文件系统是否出现可见性/读取错误;learner 不再自行清理 payload |
| 大量 `dropped(too_stale)` | learner 太慢或 `max_staleness_versions` 太小;`stale` 是旧文档/历史 run 的兼容称呼 |
| resume 后重复合并了旧更新? | DB commit 是权威边界;resume 重置 selected、重建 latest,proposal 摄取水位/唯一约束阻止重放。先检查 analysis 的 integrity 与 committed version |
| SQLite 报锁/IO 错误 | 保留现场并运行共享 FS probe;确认 DB 使用 `journal_mode=delete`,`synchronous=2(FULL)`,且没有额外 writer/WAL 文件 |
| W&B 初始化失败 | import/init 会降级为不上报;离线产物在 `logs/wandb/`,事后可 `wandb sync`。初始化后的 SDK `log/summary/finish` 异常并非全部局部捕获,遇到 runtime error 仍需查 syncer traceback |
