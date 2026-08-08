# Plan 03 实施进度

## 2026-08-08 22:13 JST — C00/C01 freeze

- Phase：`P0-freeze-oracles`。
- Plan branch：`plan03`。
- Plan branch point / implementation base：`a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`；执行开始时 worktree clean。
- Static review baseline：`7e4205adfdbcb561c752493dfbeba7976de204d7`，且是 branch point 的 ancestor。两者差异只包含修订后的 Plan 03 及 requirement matrix。
- `archive/classic-full-v1-final` 和 `archive/fragment-v0-final` 均为 annotated tag，peeled target 均为完整 commit `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`；未移动既有 tag。
- 当前盘点起点：58 个 tracked `test_*.py`、40 个 repository YAML、37 个 Miyabi PBS、209 个 `fs_diloco/` 文件、1 个 SQL schema。递归扫描包含 `configs/5000/`；torch baseline 的 4 个 config、2 个 PBS、3 个测试文件明确列为保留边界。
- P0 尚未通过：compute-node fresh baseline、RED finding、deterministic oracle、shared-FS probe、performance feasibility 和 phase review 仍待完成。

## 2026-08-08 22:24 JST — `p0-fresh-baseline` PASS

- 目标：在冻结 source identity 上重新确定 collection 数和完整测试 baseline。
- 环境：Miyabi-G compute node `mg0004`，interactive PBS job `2508036.opbs`；Python 3.13.13，PyTorch 2.13.0+cu132，pytest 9.1.1；Linux 5.14 aarch64；source commit `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`。
- 命令：`.venv/bin/python -m pytest --collect-only -q`；`.venv/bin/python -m pytest -q`。
- 结果：准确 collection 为 526；完整测试 `526 passed in 32.00s`。此前的环境激活失误已单独记录在 `failures.md`，不计为代码 baseline 失败。
- Artifact：`artifacts/20260808-222400_p0-fresh-baseline_pass.json`。
- 尚未覆盖：P0 新增依赖/测试底座、RED reproduction、oracle、FS capability 和 performance feasibility；本结果仅冻结未改 production semantics 的当前 baseline。

## 2026-08-08 22:55 JST — `p0-inventory-correction` REVIEW

- 更正 C00 盘点中的一个非 tracked 口径：此前 shell `find fs_diloco -type f` 得到的 209 包含 cache/生成文件，不能作为 source inventory。以 `git ls-files` 为准的递归 artifact 记录 71 个 tracked `fs_diloco/` source/package files；其他冻结数字不变：58 个 baseline test files、40 configs、37 PBS、1 SQL schema、42 bound mutators、8 fragment-enabled configs、5 fragment PBS、4/2/3 torch baseline config/PBS/test retain boundary。
- Artifact：`artifacts/20260808-223500_p0-runtime-surface-inventory_review.json`；逐文件 SHA-256、tag peeled target 和递归 `configs/5000/` anchor均已保存。

## 2026-08-08 22:56 JST — `p0-shared-fs-capability` PASS WITH FROZEN FALLBACK

- 环境：Miyabi-G compute node `mg0004`，interactive PBS job `2508036.opbs`；仅操作 shared reports filesystem 下由 `tempfile.mkdtemp` 创建的精确临时目录，结束时已删除。
- 命令：`.venv/bin/python scripts/miyabi/plan03_fs_capability.py --shared-parent /work/xg24i002/x10041/fsb_decoupled_diloco/reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts`。
- 原生结果：same-directory hard-link create-no-replace PASS（碰撞 `EEXIST`、同 inode）；dir-fd/openat `O_NOFOLLOW` PASS（symlink `ELOOP`）；parent directory fsync PASS；SQLite `DELETE` journal writer contention PASS（第二 writer约 0.051 秒后 `SQLITE_BUSY`，first rollback后成功，integrity `ok`）。
- 关键限制：directory `renameat2(RENAME_NOREPLACE)` 在正式 shared FS 返回 `EINVAL`，已记录 attempt1 failure，不能作为 v4 依赖。
- 冻结 fallback：exclusive `mkdir(final)` + `.identity` reservation + staged immutable object hard-links + last create-no-replace `.complete` marker。4 个 pre-marker crash prefix均对 reader不可见且同identity retry后可见；异identity和completed-root overwrite均fail closed。计划、matrix和 P3 `INIT-01` 已在实现前同步，禁止退化为覆盖写。
- Artifact：`artifacts/20260808-225600_p0-shared-fs-capability_pass.json`。

## 2026-08-08 23:08 JST — `p0-oracle-red-and-retention` PASS

- Dev/test bottom layer：`hypothesis`、`pytest-timeout`和`sortedcontainers`已进入 `uv.lock`；pytest strict markers/strict xfail已配置；`tests/support/`提供 virtual clock、fault tape、tmp dynamic authority、fake PBS、deterministic IDs和固定 paired-bootstrap实现。
- Deterministic oracle：固定 theta、2个proposal tensor/ID、arrival order、selection、token weight和Nesterov outer state；classic base store与static HA store共同 projection逐项比较 selected IDs/order、weights、merged tensor、theta、outer step/momentum和 predecessor。结果 exact/`torch.equal`。
- 命令：`.venv/bin/python -m pytest -q tests/test_plan03_support.py tests/reference/test_plan03_classic_static_oracle.py tests/test_plan03_p0_red.py`；结果 `4 passed, 5 xfailed in 1.64s`。两次 oracle fixture失败已在修改前记录，第三次通过，未触发三连败升级。
- RED证据：`.venv/bin/python -m pytest -q --runxfail tests/test_plan03_p0_red.py`按预期 `5 failed in 0.34s`，且 traceback分别到 H-01a selection-time stale abort、H-01b stale row被reset pending、H-05只有低3个contributor被服务、H-06 transient registration被unlink、H-07 known PBS no-record立即failed；attempt1的fixture错误证据已排除。
- Artifacts：`artifacts/20260808-230800_p0-red-reproduction_pass.json`、`artifacts/20260808-230800_p0-maintenance-retention_review.json`、`artifacts/20260808-223000_p0-performance-method_review.json`。
- Maintenance retention 已冻结为计划 §3.5 的保守值和解析公式；G6不得根据10k结果放宽。

## 2026-08-08 23:12 JST — `p0-modified-full-suite` PASS

- 环境：Miyabi-G compute `mg0004`，interactive PBS job `2508036.opbs`；source worktree包含P0 tests/support/report changes，production protocol semantics未修改。
- 命令：`.venv/bin/python -m pytest --collect-only -q`；结果 535 collected（baseline 526 + 9 P0 tests）。`.venv/bin/python -m pytest -q`；结果 `530 passed, 5 xfailed in 32.45s`。
- 5 xfail恰为 P0 accepted behavior RED，均已用 `--runxfail`检查目标failure；核心 invariant最终修复阶段必须变为普通passing，P6 xfail=0。
- 静态门禁：`ruff check`和`ruff format --check`对12个P0新增/修改Python文件通过；`git diff --check`通过。
- Artifact：`artifacts/20260808-231200_p0-modified-full-suite_pass.json`。
- 尚未覆盖：paired 2-learner tiny performance feasibility和P0双模型phase review；因此P0仍不是complete。

## 2026-08-08 23:31 JST — `p0-paired-tiny-feasibility` PASS

- 环境：Miyabi-G compute `mg0003`，interactive PBS job `2508070.opbs`；2 learner synthetic tiny，classic与static HA使用同一配置，唯一策略差异是 `coordination.syncer_ha.enabled`；每arm fresh run root。
- 方法：两arm各1次不计时prewarm；5 pairs按AB/BA交替；timer从spawn syncer+2 learners前到三进程clean exit；各trial SQLite integrity `ok`、final v2、committed selected tokens 256，workload exact-equivalent。
- 结果：classic秒 `[6.566772, 6.595030, 6.654641, 6.622485, 6.611745]`；static HA秒 `[5.992340, 5.061545, 6.124438, 6.287804, 6.029676]`；signed paired overhead `[-0.087476, -0.232521, -0.079674, -0.050537, -0.088036]`；median `-8.748%`；固定seed one-sided 95% paired-bootstrap upper `-5.054%`。负差异未clip。
- P0结论仅为方法和运行可行；这不是最终v4相对archive classic的G10正式门禁，也不允许改变已冻结10% margin/CI/20-pair上限。
- 前两次runner/config source identity失败已逐次记录，第三次通过，未触发三连败升级。
- 成功trial的临时run roots、W&B offline logs、payload/checkpoint均在提取authority summary后由精确scratch cleanup删除；未保留冗余训练产物。
- Artifact：`artifacts/20260808-233100_p0-paired-tiny-feasibility_pass.json`。

## 2026-08-09 00:16 JST — `p0-final-compute-validation` PASS

- PBS提交前已重新执行 `bash -n scripts/miyabi/*.pbs`，P0 batch脚本使用literal group `xg24i002`、`debug-g`、单节点和证据支持的最短10分钟walltime；job `2508127.opbs`在compute node `mg0004` clean exit。
- 最终focused suite（support、deterministic oracle、P0 RED）为 `4 passed, 5 xfailed in 1.85s`；最终完整suite为 `530 passed, 5 xfailed in 37.27s`。5项xfail仅是P0冻结的accepted behavior defects；其目标traceback已有独立 `--runxfail` 证据，后续修复阶段必须转为普通passing。
- 静态最终核对：requirement matrix精确97行/10字段/97个唯一主键，10个P0条目均为completion-candidate且evidence存在；全部P0 JSON可解析；42个 `_BOUND_MUTATORS` 与42行disposition一一对应且仅含keep/merge/delete；Ruff check/format、`git diff --check`、全体PBS shell语法和group placeholder扫描通过。
- 一次性静态wrapper的三次命名假设失败已逐次记录，并在第四次前完成源码/CSV全面定位审查；第四次42项核对通过。两次interactive qsub握手失败也已记录，转为经静态校验的batch job后通过。
- Artifacts：`artifacts/20260809-001500_p0-final-compute_pass.json`及其SHA-256绑定的原始log。
- P0所有技术门禁现为completion candidate；仍需创建review-target commit并完成独立Codex + fresh Claude双模型phase review，处理finding后才能把matrix状态改为complete并进入P1。

## 2026-08-08 23:48 JST — `P0-freeze-oracles` 双模型review remediation PASS

- 冻结review范围：base `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`，target `1563e34d8ae582d46e629f6f8bf419e084318043`，base已验证为target ancestor。Codex报告为 `reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/P0-freeze-oracles/gpt-5.6-sol_1563e34d8ae582d46e629f6f8bf419e084318043.md`；在读取Claude前已落盘。fresh Claude报告为同目录 `claude-opus-5_1563e34d8ae582d46e629f6f8bf419e084318043.md`，实际canonical model `claude-opus-5`、session `4983d9c0-032a-463b-bfc4-86dcd3765249`、subtype success、无permission denial；调用核验JSON已并列保存。
- Review结论：两份均为`CHANGES_REQUIRED`。Codex发现1 High/2 Medium；Claude发现4 High/9 Medium/11 Low。去重后所有High和Medium均完成处置：
  - **fixed**：oracle现在让classic/HA各自的proposal、v0/v1 global weight和outer state经过独立filesystem artifact及各自store path往返，predecessor从store读取，两臂分别对golden；不再以共享tape纯计算作为全部证据。
  - **fixed**：主performance timer在fresh root和任何arm-specific init前开始；HA initializer已计时。workload coverage/signature mismatch在统计前BLOCKED；duration两臂先验finite positive；stdout改per-actor file并有>1MiB测试，kill后wait；source identity helper不污染`sys.path`。正式G10仍要求clean source。
  - **fixed**：`check_plan03.py --expect`按frozen source commit重建count/list/hash/tag并比较，漂移非零退出，stdout仅`PASS/BLOCKED`；YAML解析、AnnAssign/frozenset和historical boundary有覆盖。matrix evidence/contract、triage finding binding和normal-git-add discoverability进入测试。
  - **fixed**：initializer fallback改为parent-sibling hard-link identity reservation先行，再mkdir final、manifest SHA-256对象hard-link和last complete manifest；13个pre-visibility prefix不可见且retry，2个post-marker durability prefix已visible且retry；预存final、different identity、completed root和hash collision fail closed。正式shared-FS job已通过。
  - **fixed**：5个RED都用`raises=AssertionError`约束；H-01a只把精确selection-fence RuntimeError转换为AssertionError，其他异常真失败。`--runxfail`精确5项的完整traceback log已入库并绑定hash。
  - **fixed**：PBS不再硬编码覆盖tracked output；新artifact用timestamp；本plan reports artifacts由`.gitignore`明确放行。7项matrix artifact contract漂移已同步。36项静态finding与4项executable RED在triage artifact分开标记。tools/scripts/pytest marker文档已同步。
  - **deferred-with-justification**（Claude M-9）：secondary authority-active protocol duration的实现依赖P4 runtime lifecycle events，owner为P6 `check_plan03.py` G10 mode；计划和预注册artifact已把它列为必报诊断，但end-to-end总时长仍是唯一non-inferiority主门禁，延期不削弱P0/P1 correctness。
- Low处置：L-1/L-2/L-3/L-4/L-5/L-7/L-8/L-10/L-11已修；L-6只对新dev依赖增加下界，`uv.lock`继续按仓库现有environment-specific policy忽略，正式G10以clean commit+source fingerprint+独立venv绑定；L-9已在预注册artifact明确P0 dirty feasibility不是正式G10。奇数5-pair的3/2首臂不平衡作为已记录方法属性保留，不能在观察结果后临时改序。
- 最终compute validation：提交前全体PBS `bash -n`通过，两个脚本均literal group `xg24i002`、single-node `debug-g`、证据支持且符合最短限制的10分钟walltime。job `2508424.opbs`（mg0012）先得checker`PASS`和formal FS PASS，focused `24 passed, 5 xfailed in 6.93s`，RED `5 failed in 0.35s`，full `550 passed, 5 xfailed in 40.54s`。核心artifact：`artifacts/20260808-234540_p0-phase-review-remediation-tests_pass.json`、`artifacts/20260808-234548_p0-shared-fs-capability_pass.json`、`artifacts/20260808-234556_p0-red-runxfail_review.log`。
- 最终paired feasibility：job `2508425.opbs`（mg0013），2 warmups+5 pairs全部clean exit/SQLite integrity ok/final v2/tokens256，fresh scratch已删除。classic秒`[6.674916,6.675259,6.625051,6.775157,6.875323]`，HA（含约0.15s initializer）秒`[6.481202,6.230164,6.330400,6.330765,6.228532]`；signed delta`[-0.029021,-0.066678,-0.044475,-0.065591,-0.094074]`，median `-6.559%`，one-sided95% upper `-2.902%`。P0只证明方法可行，不是G10。Artifact：`artifacts/20260808-234542_p0-paired-tiny-feasibility_pass.json`。
- 失败升级：前三次remediation batch失败已逐次记录，第三次后在第四次前完成全面data/control/persistence/recovery审查；第四次通过。成功产物已降为结构化summary、一个batch log、一个RED raw log和FS/performance JSON；重复成功per-rank tails、run/checkpoint/log scratch和superseded success artifacts已删除，失败log全部保留。

## 2026-08-09 00:34 JST — P0 第二轮增量review remediation validation PASS

- Review范围为`1563e34d8ae582d46e629f6f8bf419e084318043..0993737978da3c52990734cb6eef1aee84172d1f`。Codex报告先落盘，发现checker未保存differences和oracle negative test不走真实比较路径；fresh Claude Opus 5报告随后落盘，结论`CHANGES_REQUIRED`，含1 High、5 Medium、13 Low。调用session/canonical model已由invocation JSON绑定。
- High已修：filesystem fallback不再用单次进程内`reservation_created`布尔决定是否认领final。新建reservation遇既有final会清理并fsync自身reservation后重复fail closed；原staging由reservation同inode证明恢复权，异staging在final identity出现前不能mkdir/补identity；same-staging peer先mkdir时等待同inode identity并收敛。reader拒绝协议外条目。reservation与run同生命周期，已完成run只可在不依赖sibling的全量自检后显式same-inode repair。
- 5项Medium已修：checker同时验证冻结snapshot与当前tracked migration boundary并保存differences；reservation生命周期/repair写入INIT-01；FS scratch改到实际`runs/` parent且artifact按结果命名；被证伪的旧FS artifact从matrix解除绑定；新FS artifact含timezone timestamp和source identity。
- Low处置：oracle negative test复用主比较函数并实际篡改theta，补非零staleness/乱序/quorum截断tape；RED摘要精确匹配；performance clock显式注入，主timer在最后actor wait返回时停止，补classic分支；fresh clone创建runs；phase-final提供tracked-evidence gate；P0 performance artifact显式非正式。正式G10改为固定20 pairs、AB/BA各10次。全局放开所有reports artifact会暴露大量历史ignored产物，因此`.gitignore`仍按plan显式放行，此项作为repository-wide policy不在Plan03扩 scope。
- compute验证：attempt 1/2只暴露matrix contract生命周期与file/directory evidence fixture错误，均逐次记录；attempt 3 job `2508527.opbs`在`mg0003`通过。checker/FS probe PASS；focused `30 passed, 5 xfailed in 12.37s`；RED精确`5 failed in 0.34s`；full `556 passed, 5 xfailed in 44.74s`。核心artifact为`20260809-003335_p0-phase-review-remediation-tests_pass.json`、`20260809-003335_p0-current-boundary-check_review.json`、`20260809-003337_p0-shared-fs-capability_pass.json`和`20260809-003353_p0-red-runxfail_review.log`。
- paired feasibility job `2508517.opbs`也通过，artifact `20260809-002758_p0-paired-tiny-feasibility_pass.json`显式`is_formal_gate=false`；P0不使用observed effect改变10% margin。

## 2026-08-09 — `P0-freeze-oracles` phase-final PASS

- 最终增量审查范围为base `0993737978da3c52990734cb6eef1aee84172d1f`、target `1024cf53df603c0468b36e05a44f007eec0865a6`，ancestry已验证。独立Codex报告`gpt-5.6-sol_1024cf53df603c0468b36e05a44f007eec0865a6.md`结论`APPROVE_WITH_FOLLOWUPS`，无Critical/High/Medium；3项Low分别绑定P3结构化FS failure artifact、P3真实多进程/跨节点initializer并发测试和P5 current-boundary snapshot增强，不阻断P0。
- fresh Claude Opus 5调用实际canonical model为`claude-opus-5`、session `138a8a42-9f07-45fa-8393-3a2f414264c1`，无permission denial；在返回报告前触发可核验HTTP 429账户session limit，resume在0 token处收到相同限制。依据当前`plans/AGENTS.md`记为`skipped-session-limit`且不阻断；attempt metadata和skip disposition均保存，未伪造Claude报告。
- phase-final静态门禁在target commit后执行：`check_plan03.py --expect ... --verify-boundaries --require-tracked-evidence`输出`PASS`，frozen inventory、当前migration boundaries和所有non-pending evidence均零difference。用户对`plans/AGENTS.md`的会话限额规则修改属于target外既有worktree改动，未纳入phase commit。
- P0关联compute证据仍为job `2508527.opbs`：focused `30 passed, 5 xfailed`、RED精确`5 failed`、full `556 passed, 5 xfailed`；FS、current boundary、paired feasibility及失败日志均已tracked。10条P0 matrix requirement更新为`complete`，P1现在可以开始。
