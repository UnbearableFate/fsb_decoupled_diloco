# scripts 参考

`scripts/` 包含 launcher 和独立验证工具,不会被 `fs_diloco` 运行时自动调用。PBS 资源布局、可覆盖变量和已知 launcher 错位见 [07-operations.md](../07-operations.md);本页记录脚本自身的实际实现。

## miyabi/benchmark_syncer_device.py

这是 8-vector 合并/发布微基准,不启动 learner/syncer 主循环。

- **`_percentile(values, quantile)`** — 排序后在 `(n-1)q` 位置做线性插值;输入为空会自然失败。
- **`_summary(values)`** — 返回 `count/min/mean/p50/p95/max`。
- **`_synchronize(device)`** — 只对 CUDA 调 `torch.cuda.synchronize`,用于计时边界。
- **`prepare_fixtures(root, config, update_count)`** — 使用 training seed 在 CPU 加载模型,建 `fixtures/param_index.json` 和 FP32 `base.safetensors`,再把 `theta + (i+1)×1e-4` 以 `io.tensor_dtype` 写成 N 份 update。只要 index/base/所有 update 已存在就直接复用,不再核对模型、dtype 或内容。
- **`benchmark(...)`** — 要求正数 update/repetition,CUDA 不可用时拒绝运行。每轮依次计时:读 N 个 vector、等权 `weighted_average_tensors`、`grad = theta - p_bar`、一次外层优化器步进、两个线程并发写 weight/outer。每轮继承更新后的 theta/state,统计发布 bytes 后立即删除该轮输出;返回原始 samples 和各阶段 summary。
- **`main()`** — 必需 `--config/--root/--device {cpu,cuda}`,默认 `--update-count 8 --repetitions 5`;`resolve_config` 用 root 覆盖 shared root,stdout 为排序缩进 JSON。

## miyabi/capture_source_identity.py

- **`SOURCE_SCOPES`** — 指纹范围固定为 `fs_diloco/`、`configs/`、`scripts/`、`pyproject.toml`、`uv.lock`、`.python-version`。
- **`_git(project_root, *args)`** — 在 project root 运行 check=True 的 git 子进程并返回 stdout bytes。
- **`_sha256_file(path)`** — 以 1 MiB 块计算文件 SHA256。
- **`_source_record(...)`** — 用 `lstat` 记录 missing;symlink 记 mode、target 和 target 字符串 hash,不跟随;regular file 记 mode、size、content hash;其他类型记 unsupported。
- **`capture(project_root)`** — 记 HEAD;`git_dirty` 来自整个 repo 的 porcelain status;指纹文件列表则仅由上述 scopes 中 Git tracked 和未忽略 untracked 文件组成。对 `fingerprint_format/source_scopes/source_files` 的规范 JSON 求 SHA256;commit、dirty 和 `captured_at` 不在该 hash payload 中。
- **`_atomic_write(path, content)`** — 同目录 `mkstemp`、flush + file `fsync`、`os.replace`,失败删 temp;不 fsync 父目录。
- **`main()`** — 必需 project root、JSON 输出和 env 输出。env 写入/导出 `FS_DILOCO_GIT_COMMIT`、`FS_DILOCO_GIT_DIRTY`、`FS_DILOCO_SOURCE_FINGERPRINT`、`FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1`;stdout 只打印前三个值的 JSON。

## Plan 03 P0 freeze/check 工具

- **`miyabi/check_plan03.py`** — 以 Git ref 重建 tracked source/tests/config/PBS/schema、fragment/baseline 迁移边界、42 个 bound mutator、archive tag target 和逐文件 SHA-256。无 `--expect` 时状态为 `INVENTORY`；`--expect <frozen.json>` 按 artifact 中的 source commit 逐项核对。再加 `--verify-boundaries` 时，会另外从当前 tracked worktree（或显式 `--source-ref`）重建fragment/baseline/historical/mutator边界及其hash，避免冻结commit自校验掩盖当前树漂移。任一差异输出 `BLOCKED`并非零退出，`--inventory-output`保存具体differences。YAML 由 `safe_load` 解析；不存在的historical control/recursive anchor直接阻塞。
- **`miyabi/plan03_fs_capability.py`** — 只在 `--shared-parent` 下精确临时目录探测 hard-link create-no-replace、directory `RENAME_NOREPLACE`、dir-fd/O_NOFOLLOW、parent fsync 和 SQLite DELETE writer contention。directory no-replace 不支持时验证 parent-sibling identity reservation、exclusive final mkdir、manifest SHA-256对象hard-link和last complete manifest的全部crash prefix、原staging inode retry、异staging并发拒绝、预存/异物final重复fail-closed及completed reservation显式repair。reservation与run同生命周期；reader还拒绝协议外条目。`--output-dir`在得知结果后生成`_pass.json`或`_fail.json`，记录timestamp/source identity；probe scratch在`finally`清理且应位于实际run parent。
- **`miyabi/plan03_p0_performance.py`** — 在 PBS allocation 内顺序运行 2-learner classic/static-HA fresh-root warmup 和 5 个 paired trial；共同 timer 在任何 arm-specific init 前开始、在最后一个actor clean exit时停止，读取process log不计入主指标；clock可注入且不patch全局stdlib。进程stdout写每actor临时文件避免PIPE阻塞，arm工作量不完全一致时在统计前失败。成功后只保留结构化 `--output` 和 process tail，删除 run/checkpoint/log scratch；artifact显式标记不是正式G10 gate。
- **`run_plan03_phase0_tests.pbs`** — 10 分钟单节点 P0 checker/focused/RED/full-suite 批处理。checker同时验证冻结snapshot和当前迁移边界并保存review JSON；FS probe在`runs/`下执行、证据另存reports。RED 用 `--runxfail` 要求精确 5 个失败并生成唯一 timestamp log；PBS stdout 使用默认 job-id 文件，不能覆盖既有冻结 artifact。
- **`run_plan03_p0_performance.pbs`** — 10 分钟单节点 P0 paired feasibility 批处理，输出 timestamp 结构化 artifact。提交任一脚本前仍须执行全体 `bash -n scripts/miyabi/*.pbs` 并确认 literal group。
- `pyproject.toml` 对 Plan 03 启用 strict markers、strict xfail、300 秒 test timeout 和 `plan03_red/state_machine/crash_matrix/integration/performance` markers；超长测试必须局部显式放宽 timeout。

## miyabi/check_plan01_invariants.py

- **`read_json` / `read_jsonl`** — 严格要求 object;JSONL 不容忍损坏/半行,文件不存在才返回空列表。
- **`percentile_95(values)`** — nearest-rank `ceil(.95n)-1`,不做插值。
- **`validate_resume_progress(...)`** — 取最后一个 `run_resumed`,核对 DB `resume_generation`、resume id 和 N 份 heartbeat fence;之后必须先有 current-generation active liveness,再有严格更大版本的 outer-step/global-published,中间不得出现 input-exhausted/stop/error。
- **`write_resume_artifact(path, payload)`** — path 为 null 时跳过,否则直接 `Path.write_text` 写缩进 JSON;此辅助产物不是 temp+rename 原子写。
- **`check(args)`** — 以 read-only URI 打开权威 DB,要求 integrity ok、DELETE journal/FULL sync、DB/latest/checkpoint 一致且 active checkpoint 严格 current-only。active proposal 上界是 `2×learners×max(1, fragments)`;complete 时 `gc_pending=0`。full 还要求固定指针数等于 learner 数;fragment 核对每片 weight/optim 和唯一物化完整权重。然后检查无 WAL/db_dumps,update/version archive identity 无重复且旧版本完整,syncer log 无 error/timeout/dump。`--require-complete` 还要求 stop/summary 版本、正好的历史和 metrics 行数、无 active/payload/temp,SQLite commit p95 < 2s,且 `(sum sqlite + sum maintenance)/complete_training_time < 5%`。
- **`main()`** — 必需 run root/expected learners/version。完整检查成功打印 `PASS`,未要求 complete 打印 `PASS_WITH_FOLLOWUPS`;任意异常被压成 `BLOCKED` 且 exit 1,前两者 exit 0。该三值 stdout 不传递原异常;只有开启 resume artifact 时错误详情会写文件。

## miyabi/measure_pointer_polling.py

- **`_proc_io()`** — 解析 Linux `/proc/self/io`;不存在时返回空 dict。
- **`_prepare_pointers(root, count)`** — 以本进程 PID temp 文件 + `os.replace` 建 N 个 fixed JSON pointer;未 flush/fsync,测试后也不删除。
- **`measure(...)`** — 要求正数 pointer/interval/duration,每轮对全部 pointer 做 `stat`。`next_poll` 按绝对节拍累加,避免把本轮开销叠加为漂移;返回 wall/CPU/stat 时间、次数、百分比和 `/proc` I/O delta。
- **`main()`** — 必需 root,默认 8 pointers、0.2s interval、60s duration,stdout 为 JSON。

## miyabi/publication_crash_probe.py

此工具只验证 **full publication**,使用 tiny-local、1 learner、`max_staleness_versions=0`、W&B off。

- **`FAILPOINTS`** — `weight_temp/after_weight/after_outer/sqlite_transaction/after_db_commit/after_latest`。
- **`config_for`** — resolve tiny config 并做上述固定覆盖。
- **`add_proposal` / `select`** — 写不可变 tensor + fixed pointer,再直接插入 DB;把指定 update 标为 selected 并反查状态。
- **`publish_next`** — 用 `theta+0.001`、唯一 selected 和权重 1 发布 predecessor+1;token 字段为 probe 固定值。
- **`child_publish`** — 要求 DB 在 v0,加载 v0 weight/outer 后触发配置 failpoint;若发布返回则报错。
- **`history_ids`** — 读 archive JSONL 中有 `update_id` 的行;不存在返回空。
- **`one_case(root, failpoint, iteration)`** — 建 v0 和 selected `crash-u0`,以 `FS_DILOCO_PUBLICATION_FAILPOINT=<name>` + `FS_DILOCO_FAILPOINT_ACTION=kill` 运行 child。crash 后要求 latest 仍可读且引用存在的 weight/outer;然后 DB-first resume,补发到 v2、跑 maintenance,验证 DB/latest v2、weight/outer current-only、无 proposal payload,`crash-u0` archive 恰好一次。`after_db_commit/after_latest` 预期 crash 后 DB v1,其他点预期 v0。
- **`cross_node_initialize` / `cross_node_resume`** — 前者发布 v1 并留一个 selected proposal;后者主动删除 latest,resume 必须把 carried selection 重置 pending,再发 v2,验证 exactly-once archive 和 active table 清空。两个 subcommand 可由不同节点顺序运行。
- **`parse_args/main`** — 内部 `_child`、`cross-init`、`cross-resume` 模式;默认 matrix 需 `--root`,每 failpoint 默认 10 轮,顺序运行 6×iterations 个 case 后打印总 JSON。

## miyabi/sqlite_shared_fs_probe.py

- **`connect(path, timeout_seconds, busy_timeout_ms)`** — 建父目录,使用至少 5 秒的启动期 busy timeout 安装 DELETE journal + FULL sync,再降到调用方的 measured busy timeout;幂等建 counter/event、lease 和 contention-event 表。
- **`open_existing` / `open_readonly`** — 前者只打开既有 DB、设置 PRAGMA 而不执行 DDL;后者使用 SQLite URI `mode=ro`,供跨节点可见性与 Checker 路径使用。
- **`verify(conn)`** — integrity 必须恰好 `ok`,counter 必须等于 event 行数,且 journal/synchronous 必须是 DELETE/FULL,否则 fail closed。
- **`stress(path, writer_id, count)`** — 每轮 `BEGIN IMMEDIATE`,`INSERT OR IGNORE` 成功才同事务 counter+1,随后 commit。记录 busy/locked 数,但捕获后仍 rollback 并重抛,不做 retry。
- **`contend(...)`** — 对既有 DB 做 acquire/renew 型 `BEGIN IMMEDIATE` 争抢;busy/locked 使用 seeded jitter 有界重试,逐 writer 记录 wait 分布、busy 次数、action 数和饥饿。若饥饿,先持久化 JSON 再以非零状态退出。
- **`clock_exchange(...)`** — 通过共享目录执行多轮 coordinator/responder request/response,计算非负单向延迟假设下的 offset interval 及交集,同时记录 wall/monotonic discontinuity。
- **`kill_once` / `kill_reopen`** — child 在 counter+event 事务 commit 前或后收 SIGKILL;parent 用 seeded RNG 选 phase,每轮重开并 verify,默认 100 cycles/seed 1337。
- **`parse_args/main`** — 支持 `stress`、`verify`、`verify-readonly`、`contend`、`clock-exchange` 和 `kill-reopen`;`_kill-once` 是内部 subcommand。结果为单行排序 JSON,`contend` 可另外原子写入每个 writer 的结构化结果;跨节点进程仍由 PBS/mpirun launcher 编排。

## shell launcher 与清理脚本

- **`local/run_tiny_2proc_smoke.sh`** — 默认 tiny full config、2 learners;先后台启 syncer,sleep 1s,再并发 learner。顺序 wait 各 learner 再 wait syncer,最后运行 analysis `--json` 并写/打印 launcher log root 中的 summary。它不设置退出 trap 来清理已启动的子进程;某个 wait 失败会由 `set -e` 直接终止脚本。
- **`local/clean_run.sh`** — legacy 广域 tensor 清理脚本;目标必须是 project `runs/` 本身或后代,递归匹配的只有 `*.safetensors`/`*.db`,默认 dry-run。`--keep-latest-global` 在**每个目录**以任意长十进制版本比较保留最大 `global_vN.safetensors`,同版本重名时保留路径字典序更大者。`--delete` 用 `rm -f`;它不匹配当前 `.sqlite3` DB,也没有 `tools.clean_run` 的 completion-evidence/manifest 门禁,因此正式实验优先使用 Python 工具。
- **`local/prune_runs_without_5000.sh`** — 目标必须是 project `runs/` 的严格后代;只看直接子目录,名称不含字面 `5000` 的全部列为候选。默认 dry-run,`--delete` 用 `rm -rf` 递归删除整个 run。
- **`miyabi/inspect_run.sh`** — 要求 shared root,可选 DB path,最后用 `exec` 替换自身运行 `python -m fs_diloco.analysis`;传 DB 时附 `--db`。
- **`miyabi/submit_train_with_validation.sh`** — 以 `qsub -v` 提交 train,再以 `-W depend=afterok:<train_job_id>` 提交 validation。拒绝不存在的脚本和 run/shared/project 中的逗号;可选传递 `CONFIG` 和固定的 9 个具名实验覆盖,不通配传递所有环境变量。取 qsub stdout 第一行为 job id,仅检查非空,最后打印 run root 和两个 job id。

## PBS 脚本边界

`scripts/miyabi/*.pbs` 是资源/进程编排层,不改变 Python 协议:它们通过 CLI/环境覆盖 resolve 配置,在共享 root 启动角色,收集退出码,再运行针对该实验的 analysis/checker。每个脚本的节点数、角色布局、用途和 5000-step fragment launcher 的当前默认配置错位,统一列在 [07-operations.md 的 Miyabi PBS 批作业章节](../07-operations.md#4-miyabi-pbs-批作业)。任何提交前必须先 `bash -n scripts/miyabi/*.pbs`,并把每个 `#PBS -W group_list=...` 换成当前账户可用的字面 group ID。

Plan 02 Phase 1 新增 `run_syncer_candidate.pbs` 与 `run_static_learner.pbs`:它们读取 run descriptor、比对本地 source identity 后才 import runtime;后者支持可重跑 PBS array。`run_plan02_phase1_acceptance_launcher.pbs` 完成唯一 initializer 后调用 `tools.launch_phase1_acceptance` 提交三个 child:故障候选结束后以 `afterany` 启动后继者,learner array 则以 `after:<successor-job>` 在后继者开始后放行,避免等待后继者结束形成调度死锁。launcher 在首次 qsub 前写 pending 产物,每次 qsub 后立即持久化回执;中途失败保留 `failed/partial` 证据、非零退出且不自动 qdel,三项都提交后才原子形成 `PASS` 产物。

`run_plan02_phase1_matched_performance.pbs` 在 compute node 生成与 clean run descriptor 绑定的 matched 性能产物;`run_plan02_phase1_checker.pbs`/`check_plan02_phase1.py --mode phase1-completed` 以只读方式核验 run,并把该产物作为 completed 门禁必需输入。其余 tests/smoke/lock/fault 脚本分别验证 pytest、端到端 tiny、双节点 writer-lock 和 60-case crash matrix。提交任何 PBS 脚本时都应按 workload 和相邻实测估算尽可能短、但足以覆盖启动波动、预期运行和完整收尾的 walltime,并以 `qsub -l walltime=...` 覆盖过长默认值;可靠跑完是首要目标。

Plan 02 Phase 2 新增 `run_dynamic_learner.pbs`:它只接受 `full_ha_dynamic` descriptor,在 import runtime 前复算 source identity,并要求 bootstrap slot 或 scale launch request 恰好一种授权。`run_plan02_phase2_acceptance_launcher.pbs` 初始化 G8/G9 run 后调用 `tools.launch_phase2_acceptance`;后者逐个提交独立 bootstrap job 并在每次成功 qsub 后立即更新 `bootstrap_scheduler_jobs.json`。即使 learner 先于 manifest 写 registration,leader 也会保持 pending 到精确回执绑定出现,不会在回执持久化之前 admit。G9 还提交同 slot duplicate,运行中 scale replacement 由 `LearnerLaunchOutbox` 提交,最大并发节点证据由 chaos checker 结合 receipts/DB 核验。

`run_plan02_phase2_matched_launcher.pbs` 先完整运行 static 1+8,再完整运行 dynamic 1+8,二者使用同 source/model/data/seed/v120,最后由 matched checker 计算冻结 5% 门禁。`run_plan02_phase2_completed_checker.pbs` 只读打开已完成 dynamic run,要求外部 G7/G8/G9/compatibility/matched 产物同 identity 并核验 MEM-01 至 MEM-20、schema v3、terminal generation、bounded active/physical state 与 failure scan。`tests/evidence_tests` 脚本分别运行 pytest 和聚合测试证据;chaos/matched/completed checker stdout 成功时只有 `PASS`。
