# Plan 03 失败记录

## 2026-08-08 22:20 JST — `p0-baseline-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute node `mg0004`，interactive PBS job `2508036.opbs`，source commit `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`。
- 命令：从仓库根目录依次执行 `module list`、`source .venv/bin/activate`、环境检查和 `pytest --collect-only -q`。
- 预期：激活项目 `.venv` 后完成 fresh collection。
- 实际：`module list` 在该 PTY 中启动 pager并消费后续输入，导致 `source` 命令首字符丢失；collection 落到系统 Python 3.9，最终为 `23 tests collected, 53 errors`，主要症状是 `ModuleNotFoundError: torch` 和 `ModuleNotFoundError: fs_diloco`。
- 原始证据：interactive job `2508036.opbs` 的终端 transcript（session chunk `e01bcc`）；没有生成 run 目录或训练产物。
- 已确认根因：这是环境激活命令没有执行，不是代码或测试 baseline 失败。Git tree 在运行前为 clean。
- 下一轮：设置 `MODULES_LMNOTES_OPTIONS=-l` 并单独执行无 pager 的 `module -t list`；使用绝对路径 `.venv/bin/python -m pytest`，先打印 `sys.executable`、Torch/Pytest version 再 collection。通过条件是无 collection error 并记录准确 collected 数。

## 2026-08-08 22:52 JST — `p0-shared-fs-capability-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute node `mg0004`，interactive PBS job `2508036.opbs`；临时目录由 `tempfile.mkdtemp` 精确创建在本仓库的 shared reports filesystem，脚本 `finally` 已只删除该临时目录。
- 命令：`.venv/bin/python scripts/miyabi/plan03_fs_capability.py --shared-parent /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts`。
- 预期：hard-link create-no-replace、directory `renameat2(RENAME_NOREPLACE)`、dir-fd/O_NOFOLLOW、parent fsync 和 SQLite DELETE lock 均通过。
- 实际：hard-link probe 已执行；第一次 directory `renameat2(RENAME_NOREPLACE)` 返回 `EINVAL`，脚本 fail closed，后续 probe 尚未执行。
- 原始证据：interactive job `2508036.opbs` transcript，traceback 指向 `plan03_fs_capability.py:_rename_noreplace`，精确临时路径 `.plan03-fs-capability-ukjqbssc` 已清理。
- 已确认事实：正式 shared filesystem 不支持计划原先依赖的 directory no-replace rename flag；不能把它当成本地实现 bug或静默退化为覆盖 `os.replace`。
- 下一轮：按计划 §5.1.11 在实现前冻结 fallback：exclusive `mkdir(final)` 作为 identity reservation，逐个 hard-link immutable staged object，fsync，最后以 hard-link create-no-replace 发布 `.complete` visibility marker；reader 只承认 marker 完整且 identity/hash匹配的目录，retry仅修复 identity-matched reservation，冲突 fail closed。probe 增加 crash-prefix枚举和 collision preservation 后重跑其余原语。

## 2026-08-08 23:01 JST — `p0-oracle-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute node `mg0004`，interactive PBS job `2508036.opbs`；Python 3.13.13；新 dev 依赖已安装。
- 命令：`.venv/bin/python -m pytest -q tests/test_plan03_support.py tests/reference/test_plan03_classic_static_oracle.py tests/test_plan03_p0_red.py`。
- 预期：support/oracle passing，5 个 accepted pre-fix behavior defects strict xfail。
- 实际：`3 passed, 5 xfailed, 1 failed in 4.36s`。唯一失败是 oracle fixture向现有 bootstrap 传入计划中的未来名称 `full_static`，而当前 schema bootstrap 的静态 mode literal 是 `full`，在任何语义比较前抛出 `ValueError: unsupported HA bootstrap mode: full_static`。
- 原始证据：interactive job `2508036.opbs` transcript，pytest failure node `tests/reference/test_plan03_classic_static_oracle.py::test_classic_and_static_ha_share_exact_semantic_projection`。
- 已确认根因：P0 oracle 必须刻画 frozen v3 classic/static HA当前行为，不能提前使用 P1/P4 才引入的 v4命名；这是 test fixture错误，不是 semantic mismatch。
- 下一轮：fixture identity mode改为当前合法 literal `full`，保持 projection和 production code不变；通过条件为 oracle exact/bitwise相同、support passing、RED仍严格 xfail。

## 2026-08-08 23:04 JST — `p0-oracle-attempt2`（连续失败 2 次）

- 环境/命令：同 `p0-oracle-attempt1`，PBS job `2508036.opbs`，只把 fixture mode改为当前合法 `full` 后重跑同一测试组。
- 预期：进入共同 semantic projection比较。
- 实际：`3 passed, 5 xfailed, 1 failed in 1.63s`。classic `SQLiteStore` 使用 base schema，不含 HA 后加的 `commit_epoch/publication_id` 列；共同 helper向classic传入 HA publication包装参数，导致 `sqlite3.OperationalError: table global_versions has no column named commit_epoch`。尚未产生 semantic mismatch。
- 原始证据：interactive transcript；同一 oracle node在 `_semantic_projection -> SQLiteStore.initialize_full_run` 失败。
- 已确认根因：计划明确不比较 epoch/path/timestamp/publication包装；oracle helper却把 HA-only包装强行用于 classic。这违反了 projection边界。
- 下一轮：helper显式区分 transport wrapper：classic v0/commit不传 `publication_id`/size，HA仍传必需包装；selected IDs/order、weights、merged tensor、theta、outer state和 predecessor保持共同且逐项比较。若第三次仍失败，按 scoped `plans/AGENTS.md` 在第四次前升级全面审查。

## 2026-08-08 23:06 JST — `p0-red-evidence-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute `mg0004`，PBS job `2508036.opbs`。
- 命令：`.venv/bin/python -m pytest -q --runxfail tests/test_plan03_p0_red.py`。
- 预期：5 个 test都因各自命名 finding assertion而真实失败。
- 实际：`5 failed in 0.33s`，但只有 H-05和H-07到达目标 assertion。H-01a/H-01b误把 keyword-only `pointer_path` 当位置参数，先触发 `TypeError`；H-06把 `write_registration_request` 返回的 payload dict误当 Path，先触发 `AttributeError`。因此该输出不作为 H-01/H-06 缺陷证据保留。
- 原始证据：interactive transcript chunk `765639`；没有生产数据或run目录。
- 已确认根因：RED fixture API使用错误；strict xfail本身不会保证失败发生在目标行为处，所以必须配合 `--runxfail`检查具体 traceback。
- 下一轮：heartbeat使用 `pointer_path="hb.json"`；registration path用 `authority.paths.registration_request_path(instance_id)`，并断言首次 transient后该文件是否仍存在。通过条件：H-01 traceback到 selection/commit disposition assertion，H-06到 request-preservation assertion，H-05/H-07保持原目标失败。

## 2026-08-08 23:20 JST — `p0-paired-tiny-feasibility-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute `mg0003`，interactive PBS job `2508070.opbs`，15分钟 allocation；临时 scratch由脚本精确创建在仓库 `runs/` 下并由 `finally` 清理。
- 命令：`.venv/bin/python scripts/miyabi/plan03_p0_performance.py --project-root /work/xg24i002/x10041/fsb_decoupled_diloco --shared-parent /work/xg24i002/x10041/fsb_decoupled_diloco/runs`。
- 预期：预热 classic/static-HA 后完成5 paired 2-learner tiny trials，且每臂 final version/workload相同。
- 实际：第一个 unmeasured classic warmup三个进程均成功退出、SQLite integrity `ok`，但 final version为1、tokens为128；runner硬编码期望version 2而fail closed，尚未运行HA或测量pair。
- 原始证据：interactive job `2508070.opbs` transcript；runner在 `_run_arm` workload check报告 `{'final_version': 1, 'total_seen_tokens': 128, 'integrity': ['ok']}`；scratch已清理。
- 已确认根因：两个arm使用了不同repository config（classic tiny的 `max_local_steps=8`，HA tiny为100），不符合P0“相同workload”设计；不能通过放宽 final-version断言继续比较。
- 下一轮：两臂都从 `fs_diloco_tiny_ha_static.yaml`生成同一临时配置，classic只将 `coordination.syncer_ha.enabled=false`，所有model/data/training/quorum/terminal字段保持一致；删除硬编码version 2，要求每个arm至少1 commit并在全体measured trials上精确核对 `(final_version,total_seen_tokens)`唯一。通过条件是12个arm成功退出、workload equivalent、signed 5-pair统计可计算。

## 2026-08-08 23:24 JST — `p0-paired-tiny-feasibility-attempt2`（连续失败 2 次）

- 环境/命令：同 attempt1，PBS job `2508070.opbs`；两臂已改用同源配置并完成 classic warmup。
- 预期：HA init读取可审计source identity后继续warmup。
- 实际：HA init在创建run root前fail closed：`ValueError: HA bootstrap requires run.git_commit`。runner没有设置正式launcher通常提供的 `FS_DILOCO_GIT_COMMIT`、`FS_DILOCO_SOURCE_FINGERPRINT`和dirty flag；未启动HA actor或任何measured pair。scratch已清理。
- 原始证据：interactive transcript；traceback到 `fs_diloco/tools/init_run.py:initialize_run` source identity precondition。
- 已确认根因：runner遗漏source identity环境，不是config/HA runtime失败。`--allow-dirty-snapshot`只允许已声明的dirty identity，不能补造缺失 identity。
- 下一轮：复用 `scripts/miyabi/capture_source_identity.py` 的 `capture(project_root)`，对init和全部actors传入同一 `FS_DILOCO_GIT_COMMIT`、`FS_DILOCO_SOURCE_FINGERPRINT`、`FS_DILOCO_GIT_DIRTY`及require flag；保存fingerprint但不记录secret。若第三次仍失败，按 scoped `plans/AGENTS.md` 在第四次前升级全面审查。

## 2026-08-09 00:02 JST — `p0-static-gate-wrapper-attempt1`（连续失败 1 次）

- 环境：Miyabi-G login node，仅执行静态校验；未运行训练、pytest或其他计算负载。
- 命令：串行执行 `git diff --check`、requirement matrix解析、JSON evidence解析、Ruff、PBS shell语法和group placeholder扫描。
- 预期：matrix解析脚本读取P0 traceability CSV，并在后续静态门禁均通过后退出0。
- 实际：wrapper把实际文件名 `fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv` 错写成不存在的 `_traceability.csv`，该子检查抛出 `FileNotFoundError`；wrapper未启用shell fail-fast，所以后续Ruff、format、PBS语法和placeholder检查仍全部通过。本次不作为matrix验证证据。
- 已确认根因：一次性校验命令的路径拼写错误，不是仓库文件缺失或实现错误。
- 下一轮：使用实际matrix路径并令Python脚本自身断言97个唯一requirement、10个P0条目、状态和evidence路径；单独执行以确保错误退出不会被掩盖。

## 2026-08-09 00:04 JST — `p0-static-gate-wrapper-attempt2`（连续失败 2 次）

- 环境/目标：同attempt1，使用实际matrix路径并启用shell fail-fast。
- 实际：CSV成功读取并打印了10个真实字段名，但临时解析器随后错误引用不存在的简写列 `id`；实际主键列名为 `invariant_id`，因此在任何行数、状态或evidence断言前抛出 `KeyError: 'id'`。后续mutator核对因fail-fast未执行。
- 已确认根因：一次性验证器第二处字段名假设错误；CSV header本身符合冻结的matrix schema。
- 下一轮：解析器仅使用实际header中的 `invariant_id`、`phase`、`status`、`evidence_path`，先断言精确header，再执行全部97行和P0证据核对。若第三次失败，按计划在第四次前升级全面审查。

## 2026-08-09 00:06 JST — `p0-static-gate-wrapper-attempt3`（连续失败 3 次，已触发全面审查）

- 环境/目标：同attempt2；matrix的97行、10字段、唯一主键、10个P0 completion-candidate、evidence存在性和全部JSON解析已通过。
- 实际：第二个独立核对器假设 `_BOUND_MUTATORS` 位于不存在的 `fs_diloco/coordination/ha_authority.py`，在读取源码时失败；尚未比较42项disposition。
- 全面审查：用 `rg --files` 和 `rg -n _BOUND_MUTATORS` 重新从仓库事实定位定义，唯一生产定义在 `fs_diloco/storage/fenced_store.py:3036`；同时检查CSV header为 `old_name,concern,disposition,new_command,reason`，不是验证器猜测的 `mutator`/`method`。前三次均是一次性校验器对已冻结文件/字段的无依据命名假设，且每次都发生在目标断言前；已通过读取真实CSV header和源码位置消除全部猜测。
- 下一轮：第四次只使用已观察到的 `fenced_store.py` 与 `old_name`，精确比较源码集合和CSV集合及合法disposition；不再扩展验证器职责。若仍失败，则暂停该门禁并审查AST取值方式或冻结artifact本身。

## 2026-08-09 00:10 JST — `p0-final-compute-validation-allocation-attempt1`（连续失败 1 次）

- 环境：Miyabi-G login node；尚未获得compute allocation、未执行pytest。
- 命令：`qsub -I -q rt_HG -l select=1 -l walltime=00:15:00 -W group_list=xg24i002`。
- 预期：取得单节点interactive compute allocation，重跑P0 focused和full suite。
- 实际：调度器立即返回 `qsub: Unknown queue`；没有创建job ID或任何运行产物。
- 已确认根因：沿用了不属于本Miyabi PBS环境的queue名称。仓库所有当前Miyabi脚本使用 `debug-g` 或 `regular-g`，其中P0测试耗时约33秒，适合最短允许的10分钟 `debug-g`。
- 下一轮：请求 literal group `xg24i002`、`debug-g`、`select=1:mpiprocs=1`、`walltime=00:10:00` 的interactive allocation；成功后打印job/node/Python身份再运行测试。

## 2026-08-09 00:12 JST — `p0-final-compute-validation-allocation-attempt2`（连续失败 2 次）

- 环境：Miyabi-G login node；仍未获得compute allocation、未执行pytest。
- 命令：`qsub -I -q debug-g -l select=1:mpiprocs=1 -l walltime=00:10:00 -W group_list=xg24i002`，通过当前自动化PTY调用。
- 预期：命令保持interactive session并返回job/node。
- 实际：客户端只输出 `qsub: Job has interactive requested` 后退出，`qstat`确认 `No unfinished job found`；没有job ID或产物。资源参数和queue已合法，失败发生在自动化PTY与本站interactive qsub握手层。
- 下一轮：不再重试interactive握手；新增范围精确的P0 batch validation脚本，先按仓库规则运行全体PBS `bash -n`、确认literal group和10分钟walltime，再正常 `qsub`。脚本运行focused suite后运行full suite，并把唯一日志写入P0 evidence目录。
