# Plan 03 失败记录

## 2026-08-09 00:28 JST — P0 second-review remediation batch attempt 1

- 环境/命令：Miyabi-G compute，PBS job `2508516.opbs`（`mg0005`）；提交前全体PBS `bash -n`通过，literal group `xg24i002`，10分钟walltime。
- 预期：冻结snapshot与当前migration boundary checker、修订后的shared-FS probe、focused/RED/full suite全部通过。
- 实际：checker与shared-FS probe均PASS并分别生成`20260809-002803_p0-current-boundary-check_review.json`和`20260808-152805_p0-shared-fs-capability_pass.json`；focused suite在P0-SURFACE contract校验失败，因为matrix已声明新的`_p0-current-boundary-check_review.json` contract，却尚未把同批刚生成的artifact绑定到`evidence_path`。结果为`1 failed, 28 passed, 5 xfailed`，batch在RED/full suite前fail-fast。
- 根因：新增证据contract与同批生成证据的matrix绑定顺序不一致，不是checker/FS/runtime协议失败。已把生成artifact绑定到P0-SURFACE；phase-final tracked evidence检查改为checker显式`--require-tracked-evidence`门禁，普通precommit pytest仍只验证可由正常`git add`发现，避免在artifact生成和commit之间制造循环依赖。
- 下一轮：先完成同staging并发窗口的持久化判定修订，再以同一batch目标重跑；若再次失败，按同一实验attempt 2记录。

## 2026-08-09 00:31 JST — P0 second-review remediation batch attempt 2

- 环境/命令：Miyabi-G compute，PBS job `2508522.opbs`（`mg0019`）；目标、资源和batch顺序与attempt 1相同。
- 预期：修正contract绑定后focused/RED/full suite通过；phase-final tracked gate实现本身由单测覆盖，但不在precommit状态要求新artifact已tracked。
- 实际：checker和含same-staging peer-race、reservation repair的新shared-FS probe PASS；focused suite为`2 failed, 28 passed, 5 xfailed`。第一项失败是通用evidence检查把合法目录contract `tests/support/`的多文件`git ls-files`输出错误地与目录字符串逐字比较；第二项是phase-final gate单测仍直接要求当前precommit worktree的同批新artifact已tracked，重现了attempt 1已识别的生命周期循环。
- 根因：从单文件evidence扩展到目录evidence时未定义prefix语义；同时把phase-final使用时点修正了，但测试fixture没有与该时点解耦。均为checker test建模错误，不是production/FS协议失败。
- 下一轮：目录evidence要求至少一个tracked child且全部位于prefix下；phase-final gate用隔离临时Git仓库分别证明tracked file/directory通过与untracked file阻塞。若attempt 3仍失败，将在第四次提交前按规则完成完整审查。

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

## 2026-08-08 23:32 JST — `p0-phase-review-remediation-validation-attempt1`（连续失败 1 次）

- 环境：Miyabi-G compute，PBS job `2508334.opbs`；single-node `debug-g`，literal group `xg24i002`，10分钟walltime。checker先在冻结commit inventory上PASS，尚未进入pytest。
- 命令：`qsub scripts/miyabi/run_plan03_phase0_tests.pbs`；脚本中的shared-FS命令为 `.venv/bin/python scripts/miyabi/plan03_fs_capability.py --shared-parent reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts --output <timestamp artifact>`。
- 预期：parent reservation fallback的全部pre-visibility crash prefix在retry前不可见，retry后可见；marker之后的durability steps保持completed可恢复。
- 实际：probe在 `crash prefix 13 became visible` 失败。prefix 13恰好是hard-link `.complete` marker之后、final目录fsync之前；marker已作为设计中的visibility linearization point，因此reader接受它是正确行为，测试却仍把它分类为pre-visibility并要求不可见。临时probe目录已由`finally`清理，没有生成结果artifact；原始完整log为仓库根 `fsdiloco_plan03_p0.o2508334`。
- 已确认根因：fault-step枚举没有区分marker link前的不可见prefix和marker link后的已提交但仍需durability fsync的prefix；不是publication protocol提前可见。staged marker和全部objects在link前已经fsync，且每个object link后final目录也已fsync。
- 下一轮：把marker hard-link定义为唯一visibility linearization point。只对marker前0..12个prefix断言不可见；marker后两个fsync prefix断言已visible、函数返回completed且同identity retry保持visible；unit test分别核对pre/post两组。其余代码和门禁不放宽。

## 2026-08-08 23:34 JST — `p0-phase-review-remediation-validation-attempt2`（连续失败 2 次）

- 环境/命令：Miyabi-G compute，PBS job `2508344.opbs`；资源和batch命令同attempt1。checker再次PASS；shared-FS probe在pytest前运行。
- 预期：pre/post visibility prefix分类通过后，completed final root对different identity fail closed。
- 实际：全部prefix已越过attempt1失败点；probe随后抛 `RuntimeError: completed root accepted a different identity`。实际内部调用使用identity B但仍传入identity A的staged complete manifest，`_load_staged_manifest`立即以 `completion manifest identity mismatch` fail closed；外层测试只接受旧实现的固定错误字符串 `final reservation identity collision`，把任何其他安全拒绝误报为“accepted”。临时目录已清理，无结果artifact；完整log `fsdiloco_plan03_p0.o2508344`。
- 已确认根因：collision fixture自身identity/manifest不一致，无法到达final parent reservation collision；同时assertion耦合到已删除的旧错误字符串。production fallback没有接受不同identity。
- 下一轮：构造identity B、manifest identity hash B且对象hash有效的完整staging B，再对已完成identity A final发布；精确要求在parent reservation create-no-replace处报告对应collision且final marker/hash保持A。若第三次仍失败，按规则在第四次前升级全面审查。

## 2026-08-08 23:36 JST — `p0-phase-review-remediation-validation-attempt3`（连续失败 3 次，已触发全面审查）

- 环境/命令：Miyabi-G compute，PBS job `2508354.opbs`；同一P0 remediation batch目标、配置和不变量。checker PASS；修订后的shared-FS formal probe PASS并生成 `artifacts/20260808-233610_p0-shared-fs-capability_pass.json`，随后focused pytest失败。
- 预期：checker/oracle/FS/performance support普通passing，5个accepted RED严格xfail且只能因目标behavior defect失败。
- 实际：`2 failed, 21 passed, 4 xfailed in 5.15s`。第一项是matrix consistency test要求每个completion-candidate evidence已经出现在`git ls-files`，但本轮新建且尚未到review-fix commit的 `20260808-233006_p0-performance-method-remediation_review.json` 合法地处于untracked/not-ignored状态。第二项H-01a在目标selection fence抛精确 `RuntimeError: dynamic update is not pending/current at selection: stale-before-select`；新增的 `xfail(raises=AssertionError)`因此把真实accepted defect暴露为normal failure。完整log：`fsdiloco_plan03_p0.o2508354`。
- 已确认根因：两个review remediation约束都把最终态条件放到了中间态。Git测试把“phase-final必须tracked”错误地要求为“任何precommit test时必须tracked”；H-01a的accepted defect本来就是selection API中止批次的RuntimeError，不是末尾state assertion。前两次FS fixture问题已修复且本轮formal probe独立通过；它们与这两个focused test失败不同，但验证目标相同，故仍按同一experiment三连败升级。
- 下一轮：暂停第四次运行，先在`code_review.md`完成输入/状态/持久化/恢复/输出的全面审查。预计把evidence test改为precommit时必须存在、非ignored且可由普通`git add`发现，phase-final另在staging/commit门禁核对tracked；H-01a只捕获精确message的目标RuntimeError并转换为AssertionError，其他异常继续作为normal failure。

## 2026-08-08 23:48 JST — remediation失败日志保留路径更正

- artifact cleanup时把前三次失败的PBS默认log从仓库根移动到长期报告目录，并只做了行尾空白规范化；此前记录中的根目录路径不再有效。
- attempt1：`artifacts/20260808-233203_p0-phase-review-remediation-attempt1_fail.log`，SHA-256 `d12697828c20d04ed0bc95bfd135b3bc99de3e7711246952b62914414479f7b3`。
- attempt2：`artifacts/20260808-233400_p0-phase-review-remediation-attempt2_fail.log`，SHA-256 `53a487405b81be0abc29e2f7f6e02f934a33322bac09d8045bb809034df3e174`。
- attempt3：`artifacts/20260808-233600_p0-phase-review-remediation-attempt3_fail.log`，SHA-256 `068b95b7b1d8fb228a9caa2e6c180b867745686e44e788e034d74146c67a4f6b`。
- 第四次在全面审查后通过，连续失败计数归零。重复的successful FS/RED/test/performance产物与PBS根日志已在保留最终结构化证据后精确删除；没有删除失败证据、source、config或user run。
