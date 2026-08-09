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

## 2026-08-09 01:59 JST — `p1-typed-foundation-tests` PASS（连续失败计数归零）

- 范围：P1 typed proposal/receipt/contributor boundary、fresh authority v4 static/dynamic DDL、schema/marker/hash reopen validation、explicit fenced command/read surface、static attempt generation、contributor progress、payload identity/tensor validation、v4 config profile，以及torch baseline依赖方向。
- 实现：新增独立v4版本常量；strict JSON/typed protocol对象拒绝unknown/version/type/nonfinite/step/token/cursor/path错误；base/dynamic完整DDL不执行legacy schema，static不建dynamic表；`LeaderAuthority`只暴露lease lifecycle、typed read model和`LeaderSession` named commands，无`conn/execute/__getattr__`；v0经prepare+commit，command replay按request hash幂等；static logical launch/attempt/generation和receipt hash chain在事务内推进；payload在同一fd核对regular/no-symlink/inode/digest/safetensors schema/dtype/numel/finite及最终pathname；optimizer helper移到`modeling.training`，baseline不再import runtime learner。
- config：所有既有dataclass section继承纯structural `validate()`，`load_config`、resolved snapshot和`resolve_config`调用同一顶层profile validator；baseline entry显式传`torch_baseline`。`ConfigV4`另行拒绝removed wire keys、ambiguous token stop和streaming，并验证leader/maintenance cross-field；production v1-v3 entry仍使用`legacy_oracle`，P4前未切换协议。
- 环境/命令：提交前全体PBS `bash -n`和literal group检查通过；最终PBS job `2508645.opbs`，Miyabi-G compute `mg0021`，`debug-g`/1 node/literal group `xg24i002`/10分钟walltime。Ruff和current-boundary checker均PASS；focused命令见artifact，结果`233 passed in 20.51s`；完整suite `620 passed, 5 xfailed in 42.97s`。
- 失败闭环：attempt1 job `2508626`仅为profile拒绝message assertion漂移；attempt2 job `2508633`暴露pathname inode复检顺序错误；均在修复前完整记录并保留raw log。attempt3全组通过，连续失败计数归零，未触发三连失败升级。
- checker闭环：初次static gate唯一差异来自P0把整个baseline package字节冻结到P4，与P1明确的`baselines/train.py`import迁移冲突；现在只放行该composition文件，仍冻结baseline protocol/artifact/health、4 configs、2 PBS和3原有tests，新增protocol drift反例。一次性wrapper把PBS误交Ruff的失败也已记录，repo无需修改。
- Artifacts：`artifacts/20260809-015900_p1-typed-foundation-tests_pass.json`及其SHA-256绑定的最小raw log；失败日志`20260809-014750...attempt1_fail.log`、`20260809-015127...attempt2_fail.log`；版本边界`artifacts/20260809-015400_p1-version-boundary_review.json`。
- 尚未完成：这是review-target前技术门禁，不是P1 phase-final。P0冻结的5项RED仍按设计xfail，分别由P2/P3移除；dynamic registration/replacement完整application semantics、safe ingest crash matrix、publication artifact create-no-replace和token/fairness/scheduler属于后续phase。

## 2026-08-09 02:23 JST — `p1-review-remediation-validation-attempt2`

- 目标/范围：处置Codex对review-target `0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e`的全部5 High、3 Medium、1 Low finding；Claude fresh reviewer因账户session limit的零token HTTP 429按规则跳过且不阻断。实现覆盖typed direct construction、receipt linkage、pending supersession、static replacement、42-mutator phase contract、v4 config、same-FD filesystem、busy timeout和command identity。
- finding处置：H1 `fixed`（proposal/receipt/fence `__post_init__`与wire decoder共享同一不变量）；H2 `fixed`（ingest事务逐项核对receipt/proposal共同immutable字段）；H3 `fixed`（static active replace/terminal与batch、intent、proposal、token fate同事务）；H4 `fixed`（移除不可能表达insert-first的pending immediate UNIQUE，authority在同事务insert后终结lower pending，selected仍有DB partial UNIQUE）；H5 `fixed`（plan不再把未来mapping冒充当前实现，42个旧mutator精确绑定target+owner phase并由architecture test核对）；M1/M2/M3 `fixed`（只删除`init.resume`、二次same-FD digest+final fstat+nonblocking open、验证busy timeout）；L1 `fixed`（统一128字符safe identity且事务前拒绝）。没有finding被rejected或deferred。
- 第一次修缮验证：job `2508666.opbs`在Ruff/Checker通过后由新增same-inode mutation RED发现仅靠mtime/ctime不足，focused `1 failed, 246 passed`；已先记录于`failures.md`并保留完整log `artifacts/20260809-022000_p1-review-remediation-validation-attempt1_fail.log`，随后改用同一fd完整二次digest证伪bytes变化。
- 最终命令/环境：预提交前`bash -n scripts/miyabi/*.pbs`、literal group placeholder扫描、Ruff/py_compile/Checker静态门禁均通过；`qsub scripts/miyabi/run_plan03_phase1_tests.pbs`，PBS job `2508667.opbs`，compute `mg0008`，`debug-g`、`select=1:mpiprocs=1`、`walltime=00:10:00`、group `xg24i002`，Python 3.13.13、pytest 9.1.1、torch 2.13.0+cu132。
- 结果/证据：Ruff PASS、Plan03 boundary Checker PASS；focused `247 passed in 20.56s`；full `634 passed, 5 xfailed in 43.52s`。结构化证据 `artifacts/20260809-022300_p1-review-remediation-tests_pass.json`；最小保留log `artifacts/20260809-022300_p1-review-remediation-tests_pass.log`，SHA-256 `51a5f200b454ea4061e02259e959ef14fab3b1f39d75c83db2a0b09d5f82df50`（仅规范化行尾空白）。
- 尚未覆盖/后续：5个P0冻结RED仍按计划由P2/P3修复；P2-P4 concern-specific authority commands必须在各自owner phase完成，P1 mapping只冻结完整无遗漏的迁移责任。该次修复改变typed public API、fresh DDL和static concurrency protocol，按`plans/AGENTS.md`须创建新review-target并做增量Codex审查；Claude若仍为session-limit直接跳过。

## 2026-08-09 — `P1-typed-foundation` phase-final PASS

- 增量审查范围：base `0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e`、target `513068ebcd507c02068b2ea09fc1c82f70dbfe91`，ancestry已验证。独立Codex报告 `reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/P1-typed-foundation/gpt-5.6-sol_513068ebcd507c02068b2ea09fc1c82f70dbfe91.md` 结论 `APPROVE_WITH_FOLLOWUPS`，确认首次审查5 High/3 Medium/1 Low均fixed，无新的Critical/High/Medium。
- Claude reviewer：fresh session `25c4e920-96d0-495a-b7ef-d668c7b1ca59`在0 input/output token处收到HTTP 429账户session limit；按用户和`plans/AGENTS.md`不重试，记为`skipped-session-limit`且不阻断。调用和skip metadata保存于同目录target同名JSON。
- 新Low处置：L2 direct/wire validator重复为`deferred-with-justification`，owner=P5 protocol模块收敛，当前两条入口均有直接负例且无绕过；L3 same-FD双完整scan为`deferred-with-justification`，owner=P6 G10，正式性能证据前不以优化牺牲内容identity；L4缺少双current-contributor重归属专门用例为`deferred-with-justification`，owner=P2 adversarial safe-ingest matrix，当前stable key/fence JSON已走与其他receipt mismatch相同的事务前分支。三项均不削弱P1正确性门禁。
- phase证据：job `2508667.opbs` focused `247 passed`、full `634 passed, 5 xfailed`，Ruff/Checker PASS；`20260809-022300_p1-review-remediation-tests_pass.json`和最小log已tracked。8项P1完成门禁以及持续回归的BASE-01更新为`complete`；MODE-02因actor pre-torch binding/cutover gate属于P4，保持`pending`。
- 结论：P1完成，plan状态推进到P2。5项P0冻结xfail和P2-P4 concern-specific command仍按owner phase阻塞后续完成；P1没有宣称这些未来行为已实现。用户对`plans/AGENTS.md`的既有修改始终在phase commits/review targets之外。
## 2026-08-09 03:08 JST — `p2-correctness-measurement` implementation validation PASS

- 实现了v4 proposal adjudication闭环：payload在事务外按canonical path、non-symlink regular inode、两轮same-FD digest和safetensors schema验证；authority事务显式区分accepted/exact replay/logical conflict/identity collision，ordinary INSERT成功后才supersede，terminal observation先持久化再推进带FK的单调frontier。conflict/quarantine诊断有界；每contributor quiescent active proposal不超过1 pending+1 selected。
- 实现了dynamic admission/current fence和唯一`retire_incarnation`事务路径。selection逐row处置stale fence并返回`SelectionAttempt.invalid_update_ids`；commit conflict返回`MergeFenceConflict`，invalid row dropped、still-current peer才reset pending，abandoned batch中的update可进入新batch。draining仅允许matching `final_update_id`，replacement/revoked/stopped/expired同步终结成员引用、proposal、token fate、batch和intent。
- 实现了structured visibility聚合：同object/pointer signature upsert，NOT_FOUND要求grace且至少3次，MALFORMED要求同fingerprint、grace且至少2次，TRANSIENT_IO恢复不drop且只在operator deadline进入manual review，identity mismatch立即fail closed且不unlink。pointer切换归档且per-contributor有界。
- 实现了create-no-replace immutable publication和fixed publication intent：same-directory temp/fsync/hard-link/parent-fsync，exact bytes replay幂等、collision不覆盖；prepared intent早于I/O，commit前从同一inode复核artifact size/digest/safetensors内容，并由weight tensor order与outer `theta`各自计算exact tensor digest。commit后pair同时committed；abandon/takeover产生orphan及`lease+2skew`之后才可claim的GC candidate。
- P0的H-01a、H-01b、H-06 strict RED已改为v4普通GREEN；仅P3 owner的H-05/H-07保留xfail。
- compute门禁：提交前`bash -n scripts/miyabi/*.pbs`通过；job `2508748.opbs`在`mg0002`、single-node `debug-g`、literal group `xg24i002`、10分钟walltime运行。Ruff/checker PASS；phase Hypothesis profile focused suite `139 passed, 2 xfailed in 4.31s`；full suite `711 passed, 2 xfailed in 49.30s`。publication 5个crash prefix各重复10次。
- 成功证据：`artifacts/20260809-030725_p2-correctness-tests_pass.json`及SHA-256绑定log。前4次失败均已保留；attempt1是format scope误含未修改旧文件，attempt2暴露checkpoint共享storage及visibility lease fixture，attempt3暴露batch history的错误update唯一约束和GC lease续期fixture，attempt4 focused已全绿但full suite发现low-level tensor identity导入环；均在下一轮前按实际根因修复，最终连续失败计数归零。

## 2026-08-09 03:30 JST — `p2-review-remediation-validation-attempt2` PASS

- review-target `e4aba3ee0aee804b8deabb77a9b28bafcbcac7ef`的独立Codex报告结论`CHANGES_REQUIRED`，共2 High、3 Medium、1 Low。Claude fresh session `91f31814-fe63-4539-82bf-74c091113592`在0 input/output token处收到HTTP 429账户session limit，按用户与`plans/AGENTS.md`记为`skipped-session-limit`且不阻断。
- H1 `fixed`：conflict/collision在任何terminal observation/frontier前先绑定已连续摄取的receipt及共同immutable字段，acceptance再单独核对planned ID/digest；visibility只接受matching `proposal_expected` receipt，gap不能推进。H2 `fixed`：immutable publisher统一无write bits、metadata fsync，writable existing target与显式writable mode均fail closed。
- M1 `fixed for P2`：相同conflict的不同command重复返回terminal disposition而不再触发UNIQUE rollback；per-contributor hot quarantine按冻结64条裁剪，older observation/conflict仍作为append-only audit保留。完整observation archive/rollup/prune不在P2伪实现，继续由P3 `AUDIT-02/AUDIT-04`负责。M2 `fixed`：同sequence不同signature立即`IDENTITY_MISMATCH`，只允许严格递增signature进入archive；old replay不重置live tracker。M3 `fixed`：command hash只含immutable proposal，请求record fast path先于repeatable filesystem I/O，同command在object缺失后仍返回已提交结果。L1 `fixed`：4份raw failure log仅规范化行尾空白，诊断内容保留。
- 新RED覆盖missing-receipt collision/visibility gap、same-sequence pointer collision、old replay、same conflict重放、70个distinct conflict的64条hot quarantine bound、object删除后的command replay、immutable in-place write/writable replay拒绝。额外补齐`ReadResult/VisibilityDecision`直接构造的typed status边界。
- attempt1 job `2508777.opbs`唯一失败是既有rename-race测试耦合到两种都正确的fail-closed诊断顺序，已在`failures.md`记录并只收敛断言；production verifier未改。最终job `2508780.opbs`在`mg0003`通过Ruff/format/checker，focused `142 passed, 2 xfailed in 4.52s`，full `714 passed, 2 xfailed in 49.20s`；连续失败计数归零。
- 证据：`artifacts/20260809-032912_p2-review-remediation-tests_pass.json`与最小log（SHA-256 `4e9b64d8bfb57e9f37ba8c3e9a1b7522ef96bc0dd0cf3e296490bc49da194db4`）。本次改变immutable写保护、frontier/receipt协议和command replay边界，必须冻结新review-target并按增量门禁复审后才可phase-final。

## 2026-08-09 — `P2-correctness-measurement` phase-final PASS

- 增量复审范围：base `e4aba3ee0aee804b8deabb77a9b28bafcbcac7ef`、target `8b6e40c772459c5debcec25c93254adac025f7ee`，ancestry已验证。独立Codex报告 `reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/P2-correctness-measurement/gpt-5.6-sol_8b6e40c772459c5debcec25c93254adac025f7ee.md` 结论`APPROVE_WITH_FOLLOWUPS`，确认首次审查2 High/3 Medium/1 Low均完成P2处置，无新的Critical/High/Medium。
- Claude reviewer：fresh session `aa963dfe-b5dd-499f-bd06-0d568c440032`在0 input/output token处收到HTTP 429账户session limit；按用户及`plans/AGENTS.md`不重试，记为`skipped-session-limit`且不阻断，调用/skip metadata保存于同一phase目录。
- 新Low处置：L2 command request canonical/hash逻辑重复为`deferred-with-justification`，owner=P5 authority职责收敛；当前两条路径使用相同算法且object缺失replay RED已通过。L3 quarantine bound通过constructor默认注入为`deferred-with-justification`的runtime wiring follow-up，owner=P4必须显式传validated maintenance值，P3 `AUDIT-02/AUDIT-04`负责observation/conflict audit archive与prune；当前P2 standalone authority严格验证并使用冻结64条bound。
- phase门禁证据：job `2508780.opbs`在`mg0003`通过Ruff/format/Checker，focused `142 passed, 2 xfailed`、full `714 passed, 2 xfailed`；publication crash matrix仍为每点10次。剩余2项xfail精确属于P3 H-05公平选择与H-07 scheduler uncertainty。
- `AUTH-06`、`PROP-04..10`、`DMB-01..04/06..08`、`FS-01..03`和`PUB-01`共19项matrix requirement更新为`complete`，持续回归`BASE-01`追加P2 full-suite证据。P2完成，计划推进到P3；未冒充完成P3的token/fairness/scheduler/cursor/audit/initializer/terminal工作。
## 2026-08-09T04:06:40+09:00 — P3 operational foundation static validation

- Work unit/experiment: `p3-static-validation` attempt 2; validates the first P3 implementation slice before compute-node tests.
- Implemented scope: deterministic segment accounting and indexed cursor types; persistent committed-version fairness; authority token rollup, full dynamic resume state, terminal fence snapshot/ack and post-close admission gate; scheduler uncertainty states/operator CAS request; staged no-replace initializer and descriptor attestation; immutable audit batches/history pruning; per-attempt telemetry; signed matched-workload checker. H-05/H-07 P0 RED tests were converted to ordinary GREEN expectations.
- Command/environment: `.venv/bin/ruff check fs_diloco tests scripts/miyabi && .venv/bin/python -m compileall -q fs_diloco tests scripts/miyabi && git diff --check` on `miyabi-g1` login/control-plane node. This is static validation only; no pytest/torch runtime was run on the login node.
- Result/evidence: PASS for Ruff, compileall and diff whitespace; `artifacts/20260809-040640_p3-static-validation-attempt2_pass.log`.
- Remaining risk/non-goal: behavior has not yet been exercised on a PBS compute node; initializer crash recovery, SQLite archive FK order, legacy scheduler reconciliation and the full existing suite remain unverified. Runtime cutover/wiring remains P4, not this P3 foundation slice.
## 2026-08-09T04:07:47+09:00 — P3 compute validation presubmit gate

- Work unit: `p3-pbs-presubmit-static`; validates every Miyabi PBS script and the new focused/full P3 test job before submission.
- Command/result: `bash -n scripts/miyabi/*.pbs` passed; repository-wide PBS group placeholder scan passed; `run_plan03_phase3_tests.pbs` has literal `group_list=xg24i002`; `git diff --check` passed. Evidence: `artifacts/20260809-040747_p3-pbs-presubmit-static_pass.log`.
- Cost/runtime estimate: one `debug-g` allocation, one node/one process, expected 2–4 minutes based on P2 full suite (`49.20s`) plus the new initializer/archive tests and duplicated focused/full coverage. Requested `00:10:00`, the repository minimum, to cover queue prologue, filesystem variance and orderly teardown without asking for the materially longer production defaults.
- Success evidence predefined: Ruff and changed-scope format checks PASS; Plan03 boundary Checker PASS; focused P3/authority/architecture group has zero failures and zero xfails; full suite has zero failures and zero core-invariant xfails; PBS emits `PLAN03_PHASE3_TESTS_COMPLETE=<job-id>`.
- Retention: keep one combined job log and a structured summary; no model/checkpoint payload is generated by this unit-test job.
## 2026-08-09T04:12:06+09:00 — P3 PBS format-scope remediation

- Work unit: `p3-pbs-format-scope-remediation`; narrows attempt-1's invalid whole-directory format gate to the 28 P3 changed/new Python files while keeping repository-wide Ruff diagnostics.
- Validation: all `scripts/miyabi/*.pbs` passed `bash -n`; group placeholders remain absent; all 28 scoped files passed `ruff format --check`; `git diff --check` passed on `miyabi-g1` without running runtime tests. Evidence: `artifacts/20260809-041206_p3-pbs-format-scope-remediation_pass.log`.
- Attempt-1 disposition: fixed. No production behavior changed; the next compute attempt uses the same one-node `debug-g`, `00:10:00` cost and the same Checker/focused/full pytest success conditions.
## 2026-08-09T04:15:40+09:00 — P3 Checker boundary remediation

- Work unit: `p3-checker-boundary-remediation`; fixes compute attempt 2's sole Checker blocker.
- Change/invariant: `_boundary_manifest` no longer hashes the entire mutable implementation of `fenced_store.py`; the exact 42-name `_BOUND_MUTATORS` list/count remains independently frozen, and config/fragment/PBS/baseline migration paths and hashes remain unchanged. A regression test proves non-mutator implementation drift is permitted while adding an unreviewed mutator still yields `inventory.bound_mutators`.
- Static validation: changed files formatted; repository Ruff and compileall passed; Plan03 `--verify-boundaries` now prints `PASS`; all PBS scripts passed `bash -n`; `git diff --check` passed. Evidence: `artifacts/20260809-041540_p3-checker-boundary-remediation_pass.log`.
- Attempt-2 disposition: fixed without weakening the actual authority mutation-surface boundary. Compute attempt 3 remains the same experiment and is the last allowed local iteration before the three-failure comprehensive-review rule would trigger.

## 2026-08-09T04:24:53+09:00 — P3 comprehensive-review remediation static gate

- Work unit: `p3-attempt4-remediation-static`; implementation follows the mandatory comprehensive Codex/GPT review recorded after three consecutive `p3-compute-validation` failures.
- Fixed behavior: persistent fair selection now orders by `(committed_service_count, last_selected_committed_version_or_minus_one, stable_key)` and charges only in `commit_merge`; token summary queries terminal hard-crash bounds even without a token-rollup row; legacy tamper fixtures preserve and assert INIT-01's read-only publication before explicitly simulating a privileged adversary.
- New counterexamples: exact first 16 fair batches, pure failed-selection no-credit, authority prepare/abandon/reselect no-credit, empty-ledger hard-crash gap, and receipt-bearing final acknowledgement with zero gap/balanced tokens. The narrower plan key is `rejected-with-evidence`: attempt 3's deterministic `500/333` service split disproves the frozen count-fairness requirement, while the revised key uses already-transactional persistent credit.
- Static validation: scoped format, repository Ruff, compileall, authoritative Plan03 boundary Checker, `git diff --check`, every PBS `bash -n`, and literal-group placeholder scan all passed on `miyabi-g1`; no runtime test ran on the login node. Evidence: `artifacts/20260809-042453_p3-attempt4-remediation-static_pass.log`.
- Attempt-4 compute success condition remains unchanged: Ruff/format/Checker pass, focused group has zero failures/xfails, full suite has zero failures/core xfails, and the PBS completion marker is emitted. The job remains one `debug-g` node/process with the repository-minimum `00:10:00` walltime.

## 2026-08-09T04:28:12+09:00 — P3 scheduler legacy-contract remediation static gate

- Work unit: `p3-scheduler-test-contract-static`; addresses attempt 4's two full-suite-only failures without changing production behavior.
- Test correction: both older dynamic outbox cases now use an injected mutable wall clock and require known-job `unknown/no_record` to enter `terminal_uncertain` while retaining capacity, then enter `manual_review` after the persisted deadline while still retaining the anti-duplicate tombstone. The former immediate-failed/release assertion is rejected because it is precisely H-07/SCHED-02/SCHED-05's accepted defect.
- Static group: repository Ruff, changed-test format, compileall, authoritative Checker, diff whitespace, all PBS syntax, and literal group scan passed; the newly touched legacy test is included in the P3 PBS format scope. Evidence: `artifacts/20260809-042812_p3-scheduler-test-contract-static_pass.log`.
- Runtime remains unverified until the identical compute focused+full gate reruns; no pytest/torch workload ran on the login node.

## 2026-08-09T04:30:47+09:00 — `p3-compute-validation` attempt 5 PASS

- Compute gate: PBS job `2508861.opbs` ran on `mg0003`, one `debug-g` node/process, literal group `xg24i002`, `00:10:00` walltime; Python 3.13.13, pytest 9.1.1, Hypothesis 6.165.2 and torch 2.13.0+cu132 with `plan03-phase` profile.
- Results: repository Ruff PASS; 31 changed files formatted; authoritative Plan03 Checker PASS; focused P3/protocol/storage/observability/tools/architecture group `239 passed in 8.14s`; full suite `745 passed in 50.54s`; zero core-invariant xfails; terminal marker `PLAN03_PHASE3_TESTS_COMPLETE=2508861.opbs` emitted. The post-review failure counter resets to zero.
- Verified dispositions: revised service-count-first ordering meets deterministic prefix/1000-round fairness and failed-selection no-credit tests; empty-ledger terminal gap is visible; read-only initializer identity tamper tests reach fail-closed checksum validation; both legacy scheduler cases now prove bounded uncertainty/deadline/manual-review without capacity release.
- Evidence: structured `artifacts/20260809-043047_p3-compute-validation-attempt5_pass.json` and minimal log `artifacts/20260809-043047_p3-compute-validation-attempt5_pass.log`; original root log `fsdiloco_plan03_p3.o2508861`, SHA-256 `67b6ebd0a872a4318f9c0ad64daf54ebeafc502f86b8c112bc713aaa432224fc`.
- Remaining P3 work before review target: complete unified v4 golden/attribution, requirement-to-evidence/Checker mapping, and close any implementation gaps found by current-state P3 audit. This successful unit gate does not yet declare the phase complete.

## 2026-08-09T04:51:06+09:00 — P3 current-state hardening static gate

- Work unit: `p3-current-state-hardening`; closes requirements that the initial green unit slice did not yet prove. Initializer now removes its writable hard-linked staging alias only after complete validation, handles post-marker crash cleanup, prevalidates every source/manifest path, refuses parent symlink traversal, and performs full protocol-entry validation before explicit reservation repair.
- Operational robustness: indexed data uses explicit shard identity and a shared keyed bijection for bounded non-overlap; artifact policy is versioned/hashed and `clean_run` consults both policy and live SQLite references; audit compaction publishes partition then hashed manifest, folds batch rows into durable cursors, and exposes only fenced source-GC claim/completion; actor attestation is cleanup-protected.
- Authority semantics: terminal close freezes a per-cycle hard-crash budget, rejects larger claims, sums multiple incarnation gaps, and has a post-close contiguous-current-cycle/matching-update test; scheduler transitions use an explicit graph, mandatory persistent deadlines and uncertainty-only operator scope; SQL selection adapter now executes the eight-round N=8/quorum=3 counterexample that diverges from the rejected narrow key.
- Golden/data evidence: `tests/fixtures/golden/unified_v4_trace.json` covers unchanged full-quorum anchor, H-02 replace/receipt-only deltas and H-05 truncated fairness with separate reduction order. Attribution is `artifacts/20260809-043200_p3-trace-rebaseline_review.json`; any other drift is forbidden.
- Static validation: changed scope formatted; repository Ruff, compileall, authoritative Checker, diff whitespace, every PBS syntax and literal-group scan passed. Evidence: `artifacts/20260809-045106_p3-current-state-hardening-static_pass.log`. No runtime workload ran on the login node; the expanded focused/full compute gate is still required.

## 2026-08-09T04:56:18+09:00 — P3 expanded hardening compute gate PASS

- PBS job `2508887.opbs` ran on `mg0007`, one `debug-g` node/process, literal group `xg24i002`, `00:10:00` walltime; Python 3.13.13, pytest 9.1.1, Hypothesis 6.165.2 and torch 2.13.0+cu132 with the phase profile.
- Static gates passed: repository Ruff, 37 changed-file format checks, and authoritative Plan03 frozen-inventory/current-boundary Checker.
- Expanded focused suite passed `261 passed in 8.58s`; full suite passed `768 passed in 51.63s`; zero core xfails and marker `PLAN03_PHASE3_TESTS_COMPLETE=2508887.opbs` were emitted. The `p3-expanded-hardening-validation` failure counter resets to zero.
- The three attempt-1 fixture errors are falsified: the derived golden maximum wait is 2, per-contributor command IDs do not collide, and the legal scheduler uncertainty edge rejects a missing persisted deadline. Initializer/data/policy/audit/terminal/scheduler/golden additions all executed in the focused group.
- Evidence: `artifacts/20260809-045618_p3-expanded-hardening-validation-attempt2_pass.json` and `.log`; original root log SHA-256 `4cb285091af9490964142efc7c8bf13dea1db36d9ed11f4ee784cb35d5307245`.

## 2026-08-09T05:12:20+09:00 — P3 requirement/self-check compute gate PASS

- PBS job `2508897.opbs` on `mg0003` passed repository Ruff, 39-file format scope and the extended Checker including all 40 `P3-operational-robustness` requirement bindings.
- Focused P3/storage/protocol/architecture suite: `267 passed in 9.69s`; full suite: `775 passed in 53.21s`; marker `PLAN03_PHASE3_TESTS_COMPLETE=2508897.opbs`; zero core xfails.
- This gate exercised full post-marker descriptor/source/policy/SQLite reopen+integrity validation, broken-symlink reservation collision, every manifest object-link crash point, every initializer directory-fsync failure point, explicit monotonic wall-jump independence, phase requirement ownership, and tracked golden attribution. Subsequent current-state review found additional merge-weight, archive-index and no-replace hardening, so this is retained evidence but not the final P3 review target gate.
- Evidence: `artifacts/20260809-051220_p3-requirement-selfcheck-validation-attempt1_pass.json` and `.log`; original log SHA-256 `c486efd6dd3233db622d55c5d60ba9657513aae49307368f66a5a8bc4e2bcd63`.

## 2026-08-09T05:28:46+09:00 — P3 final-audit compute gate PASS

- PBS job `2508933.opbs` ran on `mg0003`, one `debug-g` node/process, literal group `xg24i002`, `00:10:00` walltime limit and `00:01:12` elapsed. It passed repository Ruff, the 40-file format scope, frozen/current boundary checks and all 40 P3 requirement ownership/evidence bindings.
- Focused protocol/storage/observability/tools/architecture suite passed `285 passed in 9.88s`; full suite passed `794 passed in 53.23s`; zero core xfails; marker `PLAN03_PHASE3_TESTS_COMPLETE=2508933.opbs`. The `p3-final-audit-validation` failure count resets to zero.
- This final pre-review-target gate includes signed-median workload comparison, strict finite positive merge weights, v4 authority bootstrap no-replace publication, actor runtime attestation, bounded audit batch indexes, lexical symlink-safe archive/cleanup paths, and the corresponding adversarial tests. Attempt 1's fixture-only resolved-path mistake is falsified without weakening the production rejection.
- Structured/minimal evidence: `artifacts/20260809-052846_p3-final-audit-validation-attempt2_pass.json` and `.log`; original root log SHA-256 `606914975fe37b55edc7da0a5f6e994269584c59b02865243b54943b3b05ddd0`.
- The P3 phase-gate finding set H-02/H-05/H-07/H-08/H-09 and M-01..M-03/M-05..M-12/M-15 is fully mapped to fixed behavior, earlier-phase regressed behavior, or H-09's explicit P6 final-measurement deferral in `artifacts/20260809-052900_p3-accepted-finding-dispositions_review.json`. P3 remains a completion candidate until its frozen Codex review and any required remediation finish.

## 2026-08-09 06:30 JST — P3 frozen review remediation PASS

- Frozen review range: base `225db163ee5bbfbf16bba3d59e06c4fbd6d789f8`, target `de3d27879fdef188afa03a233acd4b40d90e5feb`; ancestry verified. Codex report was saved before Claude was invoked/read and returned `CHANGES_REQUIRED` (1 Critical, 4 High, 2 Medium, 1 Low). Claude Opus 5 session `edeb9975-aa7d-4dc3-89cb-3e978970483a` completed with no permission denial and returned `CHANGES_REQUIRED` (1 Critical, 3 High, 8 Medium, 7 Low); invocation metadata is retained beside both reports.
- Claude nevertheless ran local CPU pytest/reproducers on login node `miyabi-g1`, contrary to repository runtime policy. Those observations are not accepted as validation evidence. Every accepted behavior finding was independently covered by source inspection and the later PBS compute gates.
- Blocking fixes: descriptor startup validation is bounded and accepts explicit mutable control publications without scanning audit history; initializer retry binds full resolved config/mode/git/source identity; scheduler deadlines are first-write-wins and reservation accounting uses tombstones; no-job uncertainty reaches manual review; terminal close snapshots cannot be rewritten and final ack validates `proposal_expected/planned_update_id`; audit archive retains the latest dependency closure and GC claims survive leader epoch change; clean_run fails closed on missing policy and deletes only through anchored `dir_fd/O_NOFOLLOW` parent chains.
- Additional corrections: authority schema revision bumped independently from 4 to 5; golden test now executes v4 merge/outer-optimizer math; telemetry identity fields are reserved and directory-durable; duplicate stream cursor writes were removed; token fate preserves applied version; Checker retains and validates structured per-ID evidence plus reviewed cross-file operational contracts. Matrix wording now truthfully assigns production actor/accounting/cursor/telemetry/attestation wiring to pending `P4-MIGRATE` instead of claiming it in P3.
- All Codex/Claude findings have an explicit fixed/accepted/deferred-low disposition in `artifacts/20260809-063000_p3-review-finding-dispositions_review.json`. Only Claude F-15/F-18 operator UX CLIs are deferred-low to P4; F-16 is an intentional expected-state-hash-fenced and audited operator override. No Critical/High/Medium remains open.
- Remediation compute attempt 1 (`2508967.opbs`) and attempt 2 (`2508969.opbs`) exposed only initializer/test-contract mistakes and are fully retained in `failures.md`; attempt 3 passed. After adding structured-evidence content validation, final job `2508975.opbs` on `mg0006` passed Ruff, 40-file format, frozen/current boundaries, 40/40 structured requirements, focused `294 passed in 10.35s`, full `808 passed in 54.61s`, and emitted the completion marker. Final artifacts: `artifacts/20260809-062900_p3-review-remediation-tests_pass.{json,log}` and `artifacts/20260809-062900_p3-review-remediation-requirements_pass.json`.
- Because remediation changed destructive-cleanup safety, initializer identity, terminal/audit persistence, scheduler state semantics and the independent authority schema revision, `plans/AGENTS.md` requires a new incremental review target before P3 phase-final. The next review base is `de3d27879fdef188afa03a233acd4b40d90e5feb`; P3 is not yet phase-final.

## 2026-08-09 07:23 JST — P3 second incremental review remediation PASS

- Incremental review target：base `9e1b8238c11b15b88883dffc868ef2cd89adb1b9`，target `37eeaef70f417820775ad73c01403d3e113bc082`。Codex报告按规则先保存，确认所有上一轮Critical/High/Medium protocol blockers已关闭，仅发现1项Medium phase-final checker output bootstrap问题和2项Low。
- Claude Opus 5 fresh session `1cc936f5-fba7-4d03-8667-a9c068f576b2`在完成静态读取但尚未生成final report时收到HTTP 429 session limit；按用户明确指示和`plans/AGENTS.md`不重试、记为`skipped-session-limit`且不阻断。调用/skip metadata保存在phase review目录。
- Codex M1已修：指定`--inventory-output`同时从missing-file gate和structured evidence读取中排除；独立runtime artifact仍必须存在、PASS、覆盖对应requirement且source commit匹配。synthetic RED证明self-only/stale-source继续BLOCKED，真实首次生成得到40/40 PASS。
- Production remediation：sequence-zero clean terminal ack、identity mode交叉检查、wandb symlink non-following cleanup、显式legacy-policy override、per-episode scheduler deadline re-arm、manual-review fenced reservation release、static/dynamic v4 tombstone和authority schema revision 6均已完成。P4仍只承担已在matrix标明的runtime actor/operator ingestion wiring，不回退P3原语。
- 三次incremental validation失败和mandatory comprehensive review均已记录；review后job `2509035.opbs` on `mg0005`通过Ruff、40-file format、P3 operational checks、focused `296 passed in 10.38s`、full `814 passed in 55.15s`并发出completion marker。target-bound evidence：`artifacts/20260809-071821_p3-incremental-remediation-tests_pass.{json,log}`和`artifacts/20260809-071900_p3-incremental-remediation-requirements_pass.json`。
- Finding dispositions：`artifacts/20260809-072300_p3-incremental-review-finding-dispositions_review.json`。Codex Low L1延后到P4 typed operator boundary，L2接受为可选JUnit增强；没有开放Critical/High/Medium。P3可以在evidence/review文件tracked并通过最终tracked-evidence静态门禁后phase-final。

## 2026-08-09 07:59 JST — P4 mandatory-runtime initial smoke gate PASS

- 工作单元：`p4-mandatory-runtime-smoke`。覆盖strict v4 retained-full config load/migration safety、launcher最短walltime和dynamic partial receipts、admission前torch import sentinel，以及真实static/dynamic unified runtime的initializer→candidate→admission→proposal/receipt→merge→terminal链路。
- 修复：attempt 1将浮点equality-boundary test fixture从`0.15` 改为`0.16`，不放宽生产heartbeat约束；attempt 2证明dynamic producer使用含`:`的placement ID与authority safe-identity边界冲突，现改为由hostname/accelerator完整输入hash生成的稳定opaque identity，并新增共享validator回归。两次失败均已在`failures.md`先行记录。
- 命令/环境：提交前全部PBS `bash -n`、literal group扫描、compileall、Ruff、17文件format和`git diff --check`通过；`qsub -l walltime=00:10:00 scripts/miyabi/run_plan03_phase4_tests.pbs`，job `2509072.opbs`，`mg0005`、`debug-g`单节点/单进程、group `xg24i002`，实际walltime `00:00:27`。
- 结果：focused `33 passed in 6.02s`；static两learner在v2 finalized，direct tokens 256、ledger balance 0；dynamic单learner成功admit并在v12 finalized，direct tokens 3840、ledger balance 0；两者SQLite integrity、无pending/selected和terminal断言全部通过。PBS exit status 0，stageout status 1仅表示Miyabi保留joined stdout，无stderr缺失。
- 证据：`artifacts/20260809-075927_p4-mandatory-runtime-smoke-pass.json`；原始合并日志`fsdiloco_plan03_p4.o2509072`；run roots `runs/fs_diloco/plan03_p4_{static,dynamic}_2509072`。
- 尚未覆盖：这是P4初始smoke，不代表phase gate。必须继续补齐static rerun/stale generation、candidate takeover、dynamic replacement、error-terminal successor、cache repair/authority-missing/manual successor、全部repository-owned PBS迁移、Plan01 v4 regression、full suite和phase review。
# 2026-08-09 08:29 JST — P4 accounting/cursor terminal flow control passed

- After the required three-failure review, PBS job `2509099.opbs` passed 36 focused tests plus real static and dynamic strict-v4 pipelines.
- The new current-epoch receipt acknowledgement prevents an unbounded learner publication backlog without exposing SQLite to learners. Static contributors closed at `2/2`; the dynamic contributor closed at the allowed `12/13` in-flight boundary. All three terminal fences were `acked`, total hard-crash gap was zero, and both token ledgers balanced.
- Evidence: `artifacts/20260809-082940_p4-accounting-terminal-runtime-validation-attempt4_pass.json`, raw stdout `fsdiloco_plan03_p4.o2509099`, run roots `runs/fs_diloco/plan03_p4_{static,dynamic}_2509099`.
- This is a focused P4 runtime objective, not the phase gate. Takeover, static rerun fencing, dynamic replacement, error/successor, cache repair, authority-missing fail-closed, launcher/PBS migration, Plan01 regression, full suite, requirement closure, and phase review remain open.

# 2026-08-09 09:00 JST — P4 exact dynamic replacement gate PASS

- Work unit/experiment: `p4-dynamic-replacement-validation`, attempt 6 (post-mandatory-review attempt 3). The implementation now derives receipt object paths from `(stable contributor key, complete contributor-fence digest, cycle sequence)`, so two incarnations cannot reserve each other's immutable object identity. Syncer discovery is bounded to the current fence's last/next expected receipt and retains a legacy-layout fallback; the current fence is revalidated before authority replay/ack. Receipt/proposal metadata is cleanup-protected by the artifact policy.
- Command/environment: after `bash -n` for every PBS/shell launcher, literal `group_list=xg24i002` coverage, scoped Ruff/format, compileall and `git diff --check` passed on the login node, `qsub -l walltime=00:10:00 scripts/miyabi/run_plan03_phase4_dynamic_replacement.pbs` ran as job `2509215.opbs` on `mg0005` (`debug-g`, one node/process), actual walltime `00:00:17`.
- Result: PASS/exit 0. The paused epoch-1 instance resumed first, published only in its own receipt namespace, received no authority acknowledgement, and exited 0 after observing terminal. The epoch-2 replacement alone applied versions 1–20; old/new states are `revoked`/`stopped`. Terminal finalized at v20 with 6,400 direct tokens, one `acked` fence, zero hard-crash gap, zero outstanding token balance, and `all_learners_stopped=true`.
- Evidence: `artifacts/20260809-090059_p4-dynamic-replacement-validation-attempt6_pass.json`; joined stdout `fsdiloco_p4_replace.o2509215` (SHA-256 `90f251170d676845cbe66581239d0d010fe423528a8611a5313409d6ead16f2d`); run root `runs/fs_diloco/plan03_p4_dynamic_replace_2509215`; process/boundary logs `logs/qsub_plan03_p4_dynamic_replace_2509215/`. After evidence persistence, `clean_run --delete` removed exactly 22 terminal-only proposal pointer/payload files (174,441 bytes) and retained authority/checkpoints/receipts/proposals/control evidence; the immutable inventory is `artifacts/20260809-090059_p4-dynamic-replacement-validation-attempt6_cleanup.json`.
- Scope/risk: this resets the replacement experiment failure count and proves exact manual replacement under the tested race order. Automatic liveness detection/launch authorization, candidate takeover, static-attempt rerun, error-control successor, full-suite compatibility, and the final P4 review target remain open.

# 2026-08-09 09:09 JST — P4 two-host candidate takeover gate PASS

- Work unit/experiment: `p4-two-candidate-takeover-validation`, attempt 3. The retained two-node resume launcher now uses a strict validated short-lease config, starts epoch 1 on `mg0484`, kills it only after v1 commits, then starts an independent candidate on `mg0488` and requires lease-expiry acquisition rather than sharing the original process lifetime.
- Command/environment: static config validation, all-PBS/shell `bash -n`, literal group scan and `git diff --check` passed before `qsub -l walltime=00:10:00 scripts/miyabi/run_2node_resume_regression.pbs`. Job `2509225.opbs` used two `small-g` nodes (PBS routed the requested `regular-g` script), group `xg24i002`, and finished exit 0 in `00:00:22` against a `00:10:00` limit.
- Result: authority epoch 1 persisted as `expired` and superseded by epoch 2; epoch 2 persisted as `released`. Versions 0–20 are contiguous: epoch 1 owns v0–v1 and the successor owns v2–v20. The run finalized at v20 with 1,280 direct tokens, an `acked` static terminal fence, zero hard-crash gap, balanced ledger, and `all_learners_stopped=true`.
- Evidence: `artifacts/20260809-090925_p4-two-candidate-takeover-validation-attempt3_pass.json`; joined stdout `fsdiloco_resume_2node.o2509225` (SHA-256 `e03e31168893f20c5a8106dedd23d81ace9f444ebb7ed097d838b83069390d14`); run root `runs/fs_diloco/20260809_090813_fs_diloco_2node_resume`; retained launcher logs `logs/qsub_20260809_090813_fs_diloco_2node_resume/`. Evidence-bound cleanup removed exactly 21 terminal proposal pointer/payload files (166,042 bytes); inventory: `artifacts/20260809-090925_p4-two-candidate-takeover-validation-attempt3_cleanup.json`.
- Follow-up: the expected SIGKILL produced a misleading inherited ERR-trap line even though the gate exited 0; the wrapper now captures `wait` through an `||` status assignment so future logs do not label the intentional fault as a job error. Error-control publication/successor recovery, same-logical-launch static rerun fencing, launcher-managed automatic recovery, full-suite compatibility and phase review remain open.

# 2026-08-09 09:11 JST — P4 mandatory runtime regression PASS after fenced receipt namespace

- Work unit: `p4-mandatory-runtime-regression`. It reruns the complete focused P4 mandatory-runtime suite and both real strict-v4 tiny pipelines after receipt path identity, bounded current-fence discovery, artifact-policy protection, baseline/config migration, admission and terminal-flow changes.
- Command/environment: pre-submit all-PBS/shell syntax, literal groups, repository Ruff, compileall and diff checks passed; job `2509228.opbs` ran `qsub -l walltime=00:10:00 scripts/miyabi/run_plan03_phase4_tests.pbs` on `mg0006`, one `debug-g` node/process, group `xg24i002`, and exited 0 in `00:00:27`.
- Results: focused `47 passed in 6.54s`. Static two-learner pipeline finalized at v2 with 192 applied tokens; both fence-specific receipt namespaces were discovered and both terminal fences were `acked` at their frozen cycles. Dynamic pipeline finalized at v12 with 3,840 applied tokens; its one allowed in-flight cycle was adjudicated/dropped and final `12/13` bound was gracefully acknowledged. Both ledgers balanced, hard-crash gaps were zero, and the completion marker was emitted.
- Evidence: `artifacts/20260809-091119_p4-mandatory-runtime-regression_pass.json`; joined stdout `fsdiloco_plan03_p4.o2509228` (SHA-256 `1f0be9311f1001c569c3ca8fb451142a67bec8d23ec12de3cfdbe7c467634934`); run roots `runs/fs_diloco/plan03_p4_{static,dynamic}_2509228`. Evidence-bound cleanup removed 5 static and 14 dynamic terminal pointer/payload files (134,273 bytes total); inventories are the paired `20260809-091119_p4-mandatory-runtime-regression-{static,dynamic}_cleanup.json` artifacts.
- Remaining scope: the focused gate does not replace the repository full suite, Plan01 v4 regression, static same-launch rerun, candidate error/successor gate, launcher-managed liveness/recovery, requirement evidence closure or frozen phase review.

# 2026-08-09 09:34 JST — P4 Plan01 strict-v4 regression and full suite PASS

- Work unit/experiment: `p4-plan01-v4-regression`, attempt 4 after the mandatory three-failure comprehensive review. The retained classic test oracle now receives only a fully validated compatibility projection, while production launch/init paths remain strict `ConfigV4`; old launcher and initializer tests were migrated to the current list-shaped receipt and mandatory-leader contracts. Graceful terminal acknowledgement preserves the declared final update only while the dynamic instance is `draining`, adjudicates it, then clears that transient identity on retirement.
- Command/environment: all Miyabi PBS/shell syntax, literal group, scoped Ruff/format, py_compile and diff checks passed before `qsub -l walltime=00:10:00 scripts/miyabi/run_plan01_regression.pbs`. Job `2509248.opbs` ran on `mg0022`, one `debug-g` node/process, group `xg24i002`, and exited 0 in `00:01:14`.
- Results: the complete repository suite passed `863 passed in 63.49s`. The subsequent strict-v4 two-learner smoke finalized contiguously at v2 with 192 direct tokens, SQLite integrity `ok`, no pending/selected update, two `acked` terminal fences, zero hard-crash gap, all learners stopped, three actor attestations, three JSONL telemetry streams and no legacy CSV output.
- Evidence: `artifacts/20260809-093435_p4-plan01-v4-regression-attempt4_pass.json`; joined stdout `fsdiloco_plan01_regression.o2509248` (SHA-256 `47c9f1e0062fa92e44a13c1b22678f6127b4b99a20723243d992bea0c26d1fb2`); run root `runs/fs_diloco/plan01_regression_2509248`. Evidence-bound cleanup removed exactly 6 terminal proposal pointer/payload files (34,298 bytes); immutable inventory: `artifacts/20260809-093435_p4-plan01-v4-regression-attempt4_cleanup.json`.
- Remaining P4 scope: same-logical-launch static rerun fencing, error-terminal successor recovery, fixed-cache corruption/repair, authority-missing fail-closed/manual successor, launcher dry-run/partial-receipt coverage, complete legacy→v4 migration inventory, requirement closure and frozen phase review.

# 2026-08-09 09:42 JST — P4 static rerun and candidate-error/manual-successor gates PASS

- Static same-logical-launch rerun: job `2509256.opbs` on `mg0001` paused generation-1 attempt `attempt-old-2509256` immediately after admission, admitted `attempt-new-2509256` under the same durable logical launch at generation 2, then resumed the stale process. The old process exited 0 after observing authority terminal and committed zero versions after the replacement boundary; generation 2 alone applied v1–v20. The binding/history states are `terminal`/`replaced`, cursor remained continuous, the terminal fence is `acked`, the hard-crash gap and token balance are zero, and SQLite integrity is `ok`.
- Candidate error/manual successor: job `2509257.opbs` on `mg0006` injected a Python error only after epoch 1 had durably committed v2. The entrypoint published immutable `candidate_error` control, fenced the epoch as `error`, and exited nonzero. With automatic recovery submission explicitly disabled, the wrapper polluted the fixed latest cache and manually started a successor; epoch 2 ignored the cache, resumed authority at v3, repaired the cache, and finalized v20 contiguously. Epoch ownership is v0–v2 / v3–v20, with one graceful terminal fence, zero hard-crash gap and a balanced ledger (one superseded 64-token receipt is explicitly dropped).
- Commands/evidence: both scripts passed pre-submit PBS/shell syntax and static code gates, requested the repository-minimum `00:10:00`, and exited 0 in `00:00:20`/`00:00:19`. Evidence artifacts are `20260809-094250_p4-static-rerun-validation-attempt1_pass.json` and `20260809-094250_p4-error-successor-validation-attempt1_pass.json`; joined stdout hashes are recorded there. Evidence-bound cleanup deleted 22 terminal pointer/payload objects from each run (174,275 and 174,307 bytes), with paired immutable cleanup manifests.
- Remaining P4 work is the exhaustive old→new config/PBS semantic migration inventory, Checker/requirement closure, final P4 compute regression and frozen phase review. Launcher dry-run/partial receipts, pre-torch admission, authority-missing fail-closed and fixed-cache behavior are already in the mandatory focused/full regression suite.

# 2026-08-09 09:50 JST — P4 exhaustive precommit regression PASS

- Job `2509268.opbs` on `mg0005` ran the expanded P4 gate: repository Ruff, 20-file format scope, frozen/current Checker, exact P4 semantic migration contracts, 50 focused P4 tests, the complete repository suite, and real static/dynamic strict-v4 pipelines. It exited 0 in `00:01:32` under the explicit `00:10:00` limit.
- Results: Checker `PASS`; focused `50 passed in 6.46s`; full suite `866 passed in 62.56s`. Static finalized v2 with two `acked` fences and 256 direct tokens; dynamic finalized v12 with one `acked` fence and 3,840 direct tokens. Both SQLite integrity checks and ledgers passed with zero hard-crash gap and zero balance.
- The Checker now separates authorized P4 config changes from still-frozen boundaries: every retained full config must be semantically identical to the migration of `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f:path`; torch baseline configs may add only the shared schema marker; fragment configs/PBS, the historical no-fragment control pair, baseline PBS/tests/protocol and archive tags remain byte/list frozen. Negative tests alter baseline/full training semantics and require `BLOCKED`.
- Evidence: `artifacts/20260809-095029_p4-precommit-regression_pass.json`; stdout SHA-256 `915536742df55fd2d31c6904e9a172c0f19b1d5767a95b417e881eee23fd7b7d`. Evidence-bound cleanup removed 6 static and 13 dynamic terminal pointer/payload objects (134,273 bytes total), with paired cleanup manifests.
- This is dirty-worktree precommit evidence. Next freeze the C13/C14 implementation target, generate the target-bound old→new migration matrix, rerun the same gate against that clean source identity, close `P4-MIGRATE`/AUTH requirement evidence, then perform the mandatory frozen Codex/Claude phase review.

# 2026-08-09 10:05 JST — P4 clean target-bound acceptance and requirement closure PASS

- Frozen implementation target `680ea94e9e1c718f519dc570d6922a5f9d1e6e20` was checked out in a detached clean worktree. Every runtime descriptor recorded `git_dirty=false` and source fingerprint `sha256:1e4aab424cecd6662db97d9f1809e7c9483f6d7ab5535dfcfe2f955f6e8ce8a2`. Pre-submit `bash -n` covered every Miyabi PBS/shell and local shell; all PBS files had literal group `xg24i002`. Prior elapsed evidence was at most `00:01:32`, so all six submissions used the repository-minimum `00:10:00` walltime.
- Comprehensive job `2509272.opbs` on `mg0005` exited 0 in `00:01:30`: Ruff, 20-file format, P4 boundary Checker, focused `50 passed in 6.24s`, full `866 passed in 60.56s`, static v2/256-token and dynamic v12/3,840-token real pipelines all passed. Terminal fences were respectively 2/1 `acked`; hard-crash gaps, pending/selected rows and ledger imbalances were zero; SQLite integrity was `ok`.
- Independent target-bound behaviors also passed: dynamic replacement `2509281.opbs` (`mg0008`, 17s), same-logical-launch static rerun `2509280.opbs` (`mg0007`, 19s), candidate-error plus manual successor/cache repair `2509277.opbs` (`mg0006`, 18s), two-host candidate takeover `2509279.opbs` (`mg0843+mg0550`, 22s), and Plan01 v4/full regression `2509278.opbs` (`mg0005`, 73s). Stale learner/leader post-boundary commits and duplicate versions were zero; error epoch 1 owned v0-v2 and successor epoch 2 owned v3-v20; failed-leader takeover owned v0-v1/v2-v20; all terminal states and integrity checks passed.
- The exhaustive migration matrix covers the exact 111-path union under recursive `configs`, `scripts/local` and `scripts/miyabi`: 27 full configs migrated semantically in place, 4 torch baselines changed only by the shared schema marker, 8 fragment configs plus 5 fragment PBS remain byte-frozen for P5 deletion, the separate historical control config/PBS remain frozen for P5 archive, 7 retained full PBS and 3 role PBS use v4, and every added/retained tool has an explicit non-writer classification. Both archive tags still peel to `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`. Artifact: `artifacts/20260809-095600_p4-config-migration-matrix_pass.json`.
- Structured aggregate `artifacts/20260809-100200_p4-target-bound-runtime_pass.json` closes all eight P4 matrix requirements. Static Checker `artifacts/20260809-100500_p4-target-requirements_pass.json` bound evidence to target `680ea94...`, returned `PASS`, and reported zero frozen-boundary, P4 migration-contract or per-requirement differences.
- Evidence-bound cleanup inventoried and removed exactly 117 terminal pointer/payload objects (898,996 bytes) across the seven target runs, retaining authority, checkpoints, control, configuration and audit state; the seven `20260809-100400_p4-target-*_cleanup.json` manifests are immutable. Four external process-log directories (59,896 bytes; mostly empty logs plus duplicated source identity/init data) were separately inventoried and removed after their facts were captured in the structured aggregate and joined PBS outputs.
- Remaining P4 gate: commit the evidence/checker binding, run the affected Checker test plus final tracked-evidence gate on compute/static environments as applicable, then complete the mandatory frozen Codex review and Claude review unless Claude reports a session-limit skip. No P4 correctness requirement remains pending.

# 2026-08-09 10:07 JST — P4 evidence-binding regression PASS

- After teaching the phase requirement checker to accept its mandatory `checker requirements.<ID>` token alongside a requirement-specific artifact contract, job `2509304.opbs` on `mg0005` reran repository Ruff, the 20-file format scope, frozen/current boundary checks, `50 passed in 6.31s` focused tests, `866 passed in 63.84s` full tests, and both real pipelines. It exited 0 in `00:01:33` under the explicit `00:10:00` limit.
- Static finalized v2/256 tokens with two acknowledged terminal fences; dynamic finalized v12/3,840 tokens with one. Both had zero hard-crash gap and SQLite integrity `ok`. Evidence: `artifacts/20260809-100700_p4-evidence-binding-regression_pass.json`. Evidence-bound cleanup removed 6+14 redundant terminal objects (142,545 bytes); manifests are the paired `20260809-100700_p4-evidence-binding-regression-*_cleanup.json` artifacts.

# 2026-08-09 10:57 JST — P4 mandatory-review remediation validation PASS

- Work unit: `p4-review-remediation-validation`, attempt 3. The accepted mandatory-review regressions now cover default active-static duplicate rejection, exact operator-owned replacement authorization, current-fence admission supersession, pre-pointer publication retry, one-shot request disposition/hot-path archival, exact committed-lease heartbeat lifetime, strict anchored latest-head decoding, and migration CAS/no-partial-output behavior. A runtime-discovered same-epoch repair case also proves that immutable admission resume snapshots are not regenerated after contributor progress advances.
- Command/environment: all Miyabi/local PBS/shell syntax passed before submission; job `2509420.opbs` ran `qsub -l walltime=00:10:00 scripts/miyabi/run_plan03_phase4_tests.pbs` on `mg0003`, one `debug-g` node/process, literal group `xg24i002`. It recorded source commit `0e8b14e...`, dirty source fingerprint `sha256:206a42251ecce5db3f44887771b6aed65b73a9c83de6ec0e69bdf8e423099f12`, and exited 0.
- Results: Ruff, 20-file format gate and Plan03 Checker passed; focused `59 passed in 6.42s`; complete repository `875 passed in 62.58s`. The real static pipeline finalized v2/256 tokens with two `acked` fences; dynamic finalized v12/3,840 tokens with one. Both authority databases had integrity `ok`, zero hard-crash gap and no outstanding work. Structured evidence: `artifacts/20260809-105700_p4-review-remediation-validation-attempt3_pass.json`; joined stdout SHA-256 `0b41bfed075c24873f205f84fa6b15df80d0bc15ede047b973aebe9c25371bd0`.
- Failure disposition: attempt 1's immutable-response collision and attempt 2's response-before-pointer race are fixed and their RED tests remain ordinary green tests. This successful attempt resets the remediation-validation consecutive failure count. Still required before a new frozen review target: actual-process unauthorized duplicate evidence, explicit-authorization static rerun, dynamic replacement and candidate-successor regressions against the revised protocol, evidence-bound cleanup, finding-by-finding disposition and updated requirement evidence.

# 2026-08-09 11:03 JST — P4 operator-authorized static replacement gate PASS

- Work unit/experiment: `p4-review-static-duplicate-rerun`, attempt 2. Job `2509429.opbs` on `mg0003` proved that a same-logical-launch duplicate without an operator request is rejected before its pre-torch admission signal while the generation-1 binding remains active. An immutable operator request naming the exact old and new fences then authorized generation 2.
- The replacement alone applied versions 1–20 and finalized 1,280 direct tokens. The old binding moved to `replaced`, the new binding ended `terminal`, the terminal fence was `acked`, the hard-crash gap and outstanding token balance were zero, and SQLite integrity was `ok`. The resumed old process exited cleanly after observing supersession/terminal state.
- The PBS wrapper exited 0 in `00:00:22` under the explicit `00:10:00` limit. Evidence: `artifacts/20260809-110300_p4-review-static-duplicate-rerun-attempt2_pass.json`; joined stdout `fsdiloco_p4_static_rerun.o2509429` (SHA-256 `6b39cc05a561ca2fd325fd991aae62fa5a0feed642d52bbcec3547f2cc02b8fb`). This resets the specialty experiment failure count; cleanup follows only from this persisted PASS evidence.
- Evidence-bound cleanup subsequently removed exactly 23 terminal proposal objects (182,547 bytes) and retained authority, configuration, control/audit state and representative process logs. Immutable inventory: `artifacts/20260809-110300_p4-review-static-duplicate-rerun-attempt2_cleanup.json`.

# 2026-08-09 11:15 JST — P4 admission hot-path/publication remediation PASS

- Work unit: `p4-admission-remediation-validation`. Invalid regular request objects are now classified before any actor-derived path is used, durably archived/disposed by raw-byte hash, and removed from the bounded hot tree. Admission business rejections are separated from response/disposition/storage failures, so an authority-committed fence cannot be contradicted by a rejection after an I/O failure. Partial response publication retry reuses the exact immutable resume snapshot even if contributor progress advances.
- Job `2509446.opbs` on `mg0011` passed all static gates, `63 passed in 6.58s` focused tests and `879 passed in 66.51s` repository tests. Real static and dynamic strict-v4 pipelines finalized at v2/256 and v12/3,840 respectively, with 2/1 acknowledged terminal fences, zero hard-crash gap and SQLite integrity `ok`; PBS exited 0 in `00:01:37` under `00:10:00`.
- Evidence: `artifacts/20260809-111500_p4-admission-remediation-validation_pass.json`; joined stdout `fsdiloco_plan03_p4.o2509446` (SHA-256 `0a378648c9505b42627daf7c2dffb022d4e1b20a3ea8eb0659f89345af79df6`). This closes both admission RED groups and adds an actual two-publisher migration-output concurrency regression. Specialty dynamic replacement, candidate successor/takeover and final target-bound review gates remain.
- Evidence-bound cleanup removed 6 static and 14 dynamic terminal proposal objects (142,545 bytes total) while retaining authority, configuration and audit/control state. Inventories: `artifacts/20260809-111500_p4-admission-remediation-validation-{static,dynamic}_cleanup.json`.

# 2026-08-09 11:22 JST — P4 post-review dynamic replacement gate PASS

- Job `2509456.opbs` on `mg0003` reran the real dynamic replacement boundary against strict request validation, current-admission pointers and hot-request disposition/archive. The paused epoch-1 instance remained isolated in its own receipt namespace, committed no versions, and became `revoked`; the exact epoch-2 replacement alone committed v1–v20 and ended `stopped`.
- The run finalized 6,400 direct tokens with one `acked` terminal fence, zero hard-crash gap/outstanding tokens and SQLite integrity `ok`. PBS exited 0 in `00:00:17` under `00:10:00`. Evidence: `artifacts/20260809-112200_p4-review-dynamic-replacement_pass.json`; joined stdout `fsdiloco_p4_replace.o2509456` (SHA-256 `40c11b64fe6cf4e4256dca0b24a4f3124d3b673a682f32d792eb9a8fc73adaa7`).
- Evidence-bound cleanup removed exactly 22 terminal proposal objects (174,441 bytes); inventory: `artifacts/20260809-112200_p4-review-dynamic-replacement_cleanup.json`.

# 2026-08-09 11:24 JST — P4 post-review candidate-error/manual-successor gate PASS

- Job `2509464.opbs` on `mg0003` injected failure after epoch 1 durably committed v2, persisted immutable `candidate_error`, fenced epoch 1 as `error`, polluted the fixed latest cache, and started an explicit successor with automatic submission disabled. Epoch 2 resumed from authority at v3, repaired the cache and finalized v20 contiguously.
- The final run applied 1,280 direct tokens with an `acked` terminal fence, zero hard-crash gap/outstanding tokens and SQLite integrity `ok`; epoch ownership was v0–v2 then v3–v20. PBS exited 0 in `00:00:19` under `00:10:00`. Evidence: `artifacts/20260809-112430_p4-review-error-successor_pass.json`; joined stdout `fsdiloco_p4_error_successor.o2509464` (SHA-256 `25d1bc445cc6459019b9e0a58625c5f1322fad9f2c2eb669648a546d2abcccc8`).
- Evidence-bound cleanup removed exactly 22 terminal proposal objects (174,307 bytes); inventory: `artifacts/20260809-112430_p4-review-error-successor_cleanup.json`.

# 2026-08-09 11:27 JST — P4 post-review two-host lease takeover gate PASS

- Job `2509465.opbs` used independent candidates on `mg0108` and `mg0109`. Epoch 1 committed v0–v1, was killed, and remained `expired`; only after committed lease expiry did epoch 2 supersede it and commit v2–v20. This validates takeover against the exact committed-lease heartbeat publication rather than a locally reconstructed lifetime.
- The run finalized 1,280 direct tokens with one `acked` terminal fence, zero hard-crash gap/outstanding tokens, contiguous versions and SQLite integrity `ok`. PBS exited 0 in `00:00:22` under `00:10:00`. Evidence: `artifacts/20260809-112730_p4-review-two-candidate-takeover_pass.json`; joined stdout `fsdiloco_resume_2node.o2509465` (SHA-256 `f94d2c53cc986f32a976fb814e4c2af1933261d8ecfef65ba303a0fef9359c26`).
- Evidence-bound cleanup removed exactly 21 terminal proposal objects (166,042 bytes); inventory: `artifacts/20260809-112730_p4-review-two-candidate-takeover_cleanup.json`.

# 2026-08-09 11:35 JST — P4 post-review Plan01 compatibility regression PASS

- Job `2509471.opbs` on `mg0003` passed the complete repository suite (`879 passed in 67.84s`) and the retained strict-v4 two-learner compatibility smoke after admission/request and committed-heartbeat remediation.
- The runtime finalized v2/256 direct tokens with two acknowledged terminal fences, zero hard-crash gap, no pending/selected update, three actor attestations, three JSONL telemetry streams, no legacy CSV and SQLite integrity `ok`. PBS exited 0 in `00:01:18` under `00:10:00`. Evidence: `artifacts/20260809-113530_p4-review-plan01-regression_pass.json`; joined stdout `fsdiloco_plan01_regression.o2509471` (SHA-256 `b02d1160c8811adfa9860a3b27c4f9e36fafd89d56756ce703c6875127d4021e`).
- Evidence-bound cleanup removed exactly 6 terminal proposal objects (34,298 bytes); inventory: `artifacts/20260809-113530_p4-review-plan01-regression_cleanup.json`.

# 2026-08-09 11:44 JST — P4 admission archive collision/strict-reader remediation PASS

- Job `2509482.opbs` on `mg0007` closed all three RED cases from job `2509478`: identical malformed bytes now share a content-addressed archive/disposition without path-dependent content; a pre-existing disposition must pass exact identity, field-set, outcome/fence and response/rejection-path validation before the hot request can be removed; admission responses and their nested resume objects reject unknown fields.
- The unchanged comprehensive gate passed Ruff, the 20-file format scope, the Plan03 Checker, `66 passed in 6.59s` focused tests, and `882 passed in 64.10s` full tests. Real strict-v4 static/dynamic pipelines finalized at v2/192 and v12/3,840 direct tokens, with 2/1 acknowledged terminal fences, zero hard-crash gaps, balanced applied-token ledgers and SQLite integrity `ok`. PBS exited 0 in `00:01:33` under the explicit `00:10:00` limit.
- Evidence: `artifacts/20260809-114430_p4-admission-collision-validation_pass.json`; joined stdout `fsdiloco_plan03_p4.o2509482` (SHA-256 `739ee4f69456df7e7cd8a163fbaab17ca2adde8975c73ef418bfcb667247d612`). This resets the collision/strict-reader failure count. Evidence-bound cleanup is performed only after this PASS artifact is persisted.
- The persisted PASS evidence then authorized cleanup: dry-run and delete inventories matched, and `clean_run` removed exactly 5 static plus 13 dynamic terminal proposal objects (126,001 bytes) while retaining authority, configuration, checkpoints and audit/control state. Immutable inventories: `artifacts/20260809-114430_p4-admission-collision-validation-{static,dynamic}_cleanup.json`.

# 2026-08-09 11:49 JST — P4 first-review finding disposition complete

- All findings from the frozen `f849214b..0e8b14ed` Codex review are now explicitly disposed as `fixed`: H1 operator-owned static replacement, H2 current-fence admission/revalidation, M1 exact committed-lease heartbeat, M2 migration CAS/create-no-replace, M3 anchored strict latest reads, and M4 bounded disposition/history hot path. No finding is rejected or deferred. The finding-to-code, RED, passing test and specialty-job mapping is `artifacts/20260809-114950_p4-mandatory-review-finding-dispositions_review.json`.
- Claude session `63788ac1-d516-4594-b851-8fb540f74ef4` returned an explicit HTTP 429 account session limit at zero input/output tokens; its invocation and skip metadata are retained under the P4 review directory. Per the user instruction and `plans/AGENTS.md`, this is `skipped-session-limit`, is not retried, and does not block the required Codex remediation/review flow.
- The latest dirty-source gate is job `2509482.opbs` (`66` focused, `882` full, static/dynamic real pipelines); the specialty gates cover unauthorized/authorized static replacement, dynamic replacement, candidate error/manual successor, two-host expired-lease takeover, and Plan01 compatibility. One additional fail-closed regression for non-finite operator-request timestamps was added after that job and remains pending validation on the frozen remediation target; therefore P4 is not yet phase-final.

# 2026-08-09 11:52 JST — P4 review-remediation precommit gate PASS

- Job `2509505.opbs` on `mg0007` validated the final dirty-source remediation tree, including the added non-finite operator-authorization timestamp rejection. Ruff, 20-file format and Plan03 Checker gates passed; focused tests were `67 passed in 6.36s`, the full repository was `883 passed in 63.89s`, and the real strict-v4 static/dynamic pipelines finalized at v2/256 and v12/3,840 applied tokens.
- Both authority databases passed integrity, all 2/1 terminal fences were acknowledged and hard-crash gaps were zero. PBS exited 0 in `00:01:32` under `00:10:00`. Evidence: `artifacts/20260809-115240_p4-review-remediation-precommit_pass.json`; joined stdout SHA-256 `38a4d5385e2d297ce33af128f1ca8b2c7b627b1dbc47d8ec30b217a0141fb67c`.
- This closes dirty-tree implementation validation but does not substitute for the clean frozen-target acceptance, target-bound requirement evidence or mandatory incremental review. Cleanup follows from the persisted PASS artifact before freezing the implementation commit.
- Evidence-bound cleanup dry-ran and then removed exactly 8 static plus 13 dynamic terminal proposal objects (150,817 bytes), retaining authority/configuration/checkpoint/control/audit state. Inventories: `artifacts/20260809-115240_p4-review-remediation-precommit-{static,dynamic}_cleanup.json`.

# 2026-08-09 11:57 JST — P4 frozen remediation target main gate PASS

- Frozen implementation commit `97b98689123e081117501bd26bd68058589b78f2` was checked out in a detached clean worktree. Job `2509517.opbs` on `mg0006` captured `git_dirty=false` and source fingerprint `sha256:14322dff478e3e5c305d32e225e1f7c91bf3029ea36bbf8c9c006f08f5d643e1`, then passed Ruff, the 20-file format gate, Plan03 boundaries, `67` focused tests, `883` full tests and both real pipelines.
- Static finalized v2/256 tokens with two acknowledged fences; dynamic finalized v12/3,840 with one. SQLite integrity and token fate checks passed and hard-crash gaps were zero. PBS exited 0 in `00:01:31` under `00:10:00`. Evidence: `artifacts/20260809-115733_p4-remediation-target-main_pass.json`; joined stdout SHA-256 `30c7ceb42960fb4517e6831c749822b7a8b022a77d240488643be61739632ab2`.
- This target-bound main gate is one component of final P4 evidence; the specialty replacement/successor/takeover/compatibility gates and requirement aggregation remain before review-target freeze.
- Evidence-bound cleanup dry-ran and removed 6 static plus 14 dynamic terminal proposal objects (142,545 bytes); immutable inventories are `artifacts/20260809-115733_p4-remediation-target-main-{static,dynamic}_cleanup.json`.

# 2026-08-09 12:00 JST — P4 frozen-target static authorization/rerun gate PASS

- Job `2509520.opbs` on `mg0005` first rejected the same-launch duplicate before admission (`UNAUTHORIZED_DUPLICATE_STATUS=1`) while generation 1 remained active, then accepted only the exact operator request naming the old and new attempt fences. The resumed old actor exited without post-replacement commits; generation 2 alone applied v1–v20 and finalized 1,280 tokens.
- The old binding is durably `replaced`, the new binding reached terminal, the one terminal fence is `acked`, hard-crash gap is zero, token fates balance and SQLite integrity is `ok`. Clean target/source identity matches `97b9868...`; PBS exited 0 in `00:00:23` under `00:10:00`. Evidence: `artifacts/20260809-120000_p4-remediation-target-static-rerun_pass.json`; stdout SHA-256 `4494b9839b58ed54d8939becac4d24da2a119fe8ca1003deadd4ccff4d74af02`.
- Evidence-bound cleanup dry-ran and removed 22 terminal proposal objects (174,275 bytes); inventory: `artifacts/20260809-120000_p4-remediation-target-static-rerun_cleanup.json`.

# 2026-08-09 12:02 JST — P4 frozen-target dynamic replacement gate PASS

- Job `2509526.opbs` on `mg0005` paused dynamic stream epoch 1 before any commit, admitted the exact replacement as stream epoch 2, then resumed the stale actor. The old instance committed nothing, finished `revoked`, and the replacement alone applied v1–v20 before reaching `stopped`.
- The run finalized 6,400 direct tokens with one acknowledged terminal fence, zero hard-crash gap/outstanding tokens and SQLite integrity `ok`. Target identity is clean `97b9868...`; PBS exited 0 in `00:00:16` under `00:10:00`. Evidence: `artifacts/20260809-120250_p4-remediation-target-dynamic-replacement_pass.json`; stdout SHA-256 `7056d851220ca9b0bc4b0085dbac16ae66110761e01eb3b3a2a293bb45f3f834`.
- Evidence-bound cleanup dry-ran and removed 23 terminal proposal objects (182,713 bytes); inventory: `artifacts/20260809-120250_p4-remediation-target-dynamic-replacement_cleanup.json`.

# 2026-08-09 12:04 JST — P4 frozen-target candidate-error/manual-successor gate PASS

- Job `2509532.opbs` on `mg0005` injected the candidate failure only after epoch 1 committed v2. The first candidate published immutable error control, fenced epoch 1 as `error` and exited nonzero; the explicit successor ignored/repaired the polluted convenience cache, acquired epoch 2 and resumed from authority at v3.
- Version ownership is contiguous and unique: epoch 1 owns v0–v2, epoch 2 owns v3–v20 and ends `released`. The run finalized 1,280 direct tokens with an acknowledged terminal fence, zero hard-crash gap/outstanding tokens and integrity `ok`. Clean target identity is `97b9868...`; PBS exited 0 in `00:00:19`. Evidence: `artifacts/20260809-120450_p4-remediation-target-error-successor_pass.json`; stdout SHA-256 `9a3b6bfccf2ff16e82062ed84ee319c6662ae61d4d0bbf6519b7be72447782b3`.
- Evidence-bound cleanup dry-ran and removed 22 terminal proposal objects (174,307 bytes); inventory: `artifacts/20260809-120450_p4-remediation-target-error-successor_cleanup.json`.

# 2026-08-09 12:08 JST — P4 frozen-target two-host expired-lease takeover gate PASS

- Job `2509537.opbs` ran independent candidates on `mg0255` and `mg0256`. Epoch 1 committed v0–v1 and was intentionally killed; only after the exact committed lease expired did the second host acquire epoch 2 and commit v2–v20. Epoch states are `expired` then `released`, with no overlap or duplicate version.
- The run finalized 1,280 direct tokens, its terminal fence is acknowledged, hard-crash gap/outstanding token balance are zero and integrity is `ok`. The source descriptor attests clean target `97b9868...`. PBS routed the two-node request to `small-g`, exited 0 in `00:00:22` under `00:10:00`. Evidence: `artifacts/20260809-120830_p4-remediation-target-two-host-takeover_pass.json`; stdout SHA-256 `46ac813b35bcf95b11615e2c4eb545ebd2722c933f468c96a7be5072beac3e48`.
- Evidence-bound cleanup dry-ran and removed 21 terminal proposal objects (166,042 bytes); inventory: `artifacts/20260809-120830_p4-remediation-target-two-host-takeover_cleanup.json`.

# 2026-08-09 12:11 JST — P4 frozen-target Plan01 compatibility regression PASS

- Job `2509546.opbs` on `mg0005` passed the complete repository suite (`883 passed in 64.55s`) from the clean remediation target, then passed the retained strict-v4 two-learner compatibility smoke.
- The smoke finalized v2/256 direct tokens with two acknowledged terminal fences, zero hard-crash gap, no pending update, three actor attestations, three JSONL telemetry streams, no legacy CSV and SQLite integrity `ok`. The descriptor attests clean `97b9868...`; PBS exited 0 in `00:01:15` under the evidence-based `00:10:00` override. Evidence: `artifacts/20260809-121130_p4-remediation-target-plan01-regression_pass.json`; stdout SHA-256 `0df3c3eac3a9b1578b8c90043c435610da004b0f7365db6dc57ba466d4466198`.
- Evidence-bound cleanup dry-ran and removed 7 terminal proposal objects (42,570 bytes); inventory: `artifacts/20260809-121130_p4-remediation-target-plan01-regression_cleanup.json`.

# 2026-08-09 12:15 JST — P4 frozen remediation target aggregate PASS

- Aggregate `artifacts/20260809-121510_p4-remediation-target-runtime_pass.json` binds all six clean-target jobs to implementation commit `97b98689123e081117501bd26bd68058589b78f2` and covers `AUTH-02/03/04/05/07/09/10`, `MODE-02` and `P4-MIGRATE`. Across the main, static/dynamic replacement, error-successor, two-host takeover and Plan01 gates, no stale actor/leader committed after its fence, versions stayed unique/contiguous, every terminal fence acknowledged with zero hard-crash gap, and every authority database passed integrity.
- The original 111-entry migration matrix remains the exhaustive frozen-source→P4 inventory. A target delta (`artifacts/20260809-121500_p4-remediation-migration-delta_pass.json`) proves configs and local launchers are byte-identical to the earlier P4 target; only the Checker evidence rule and static authorization/rerun validation PBS changed under `scripts/miyabi`, both with explicit validation. Both archive tags still peel to the frozen source `a00a3d6...`.
- All first-review findings are now `fixed-and-target-validated`; no finding is rejected or deferred. Remaining P4 completion gates are the source-targeted requirement Checker, evidence commit and mandatory incremental Codex/Claude review (Claude only skippable on an explicit session-limit response).

# 2026-08-09 12:17 JST — P4 remediation target requirement binding PASS

- `check_plan03.py` was run with the frozen P0 inventory, current-boundary and P4 migration verification, `--verification-target-ref 97b98689123e081117501bd26bd68058589b78f2`, and `--verify-phase-requirements P4-mandatory-fenced-runtime`. It returned `PASS`: zero frozen/current boundary or P4 semantic migration differences, and all eight P4 matrix rows have implementation owners, test owners and structured target-matching evidence.
- Structured Checker evidence: `artifacts/20260809-121700_p4-remediation-target-requirements_pass.json` (SHA-256 `f45b52324864ed34695ef2105a89c45915c1afbfe9e03c943651d0ab098a69bb`). The Checker output path was intentionally excluded from self-reference during generation; after the evidence commit, a separate `--require-tracked-evidence` run must pass before the mandatory review begins.

# 2026-08-09 12:19 JST — P4 final tracked-evidence gate PASS

- After evidence commit `52a098097cb4124b1adaa8e61a073c00afe93f04`, the full phase-final Checker was rerun with `--verify-boundaries --require-tracked-evidence --verification-target-ref 97b9868... --verify-phase-requirements P4-mandatory-fenced-runtime`. It returned `PASS`, with zero tracked-evidence, boundary, migration-contract or requirement differences and all eight P4 rows passing.
- Compact evidence: `artifacts/20260809-121900_p4-final-tracked-evidence-gate_pass.json`; the complete temporary Checker JSON had SHA-256 `e1d184e69671585560efe0bfffc2c99b19d00d35af435ebb3e266fe82269f88c`. The frozen review increment will be `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4..<latest evidence target>`; ancestry and exact target are verified after this record is committed.
# 2026-08-09 P4 incremental review findings accepted and RED-locked

- Persisted the independent Codex report before invoking Claude for incremental target `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4..d18fae055b5beec1887f38c3f2070f0bf6ec901b`.
- Claude invocation completed successfully as actual model `claude-opus-5`, session `90ee9fa3-3bf2-4179-bb77-1769a6e4a564`; both reports conclude `CHANGES_REQUIRED`.
- Accepted for remediation: unreadable/transient hot-request separation, invalid UTF-8 disposal, canonical valid history, request-specific response/rejection keys, attempt-ID reuse, stable cross-epoch command replay, exact consumer-equivalent disposition validation/current pointer, MODE-02 phase/evidence binding, and final-target Checker evidence.
- Deferred to P6 bounded-resource ownership: retained admission audit/authorization compaction and request-size limits, with G6 tests required before plan completion. Low operational findings remain recorded for later disposition.
- RED job `2509636.opbs` produced the expected `67 passed, 10 failed`; evidence is `artifacts/20260809-125540_p4-incremental-review-red_fail.json`.
# 2026-08-09 P4 incremental remediation narrowed to one replay defect

- Job `2509653.opbs` moved the accepted review set from 10 RED failures to one: `76 passed, 1 failed`.
- Implemented three-state hot observations with per-request failure isolation, canonical valid history, fence-namespaced responses, request-hash rejections, exact shared response/rejection validators, current-pointer validation, stable admission command identity, MODE-02 P4 ownership, and source-tree-equivalent evidence binding in the Checker.
- The remaining cross-epoch conflict was traced to authorization-derived command payload reconstruction and was corrected with an exact-current-binding idempotent path.
# 2026-08-09 P4 incremental remediation focused GREEN

- Job `2509656.opbs` passed all 77 P4 focused tests, including the cross-epoch committed-admission replay regression.
- The full suite's only failure was the independent current-second rollover flake in `tests/test_source_identity.py`; the source and test tree will be rerun unchanged before treating it as a product defect.
# 2026-08-09 P4 incremental remediation tests and static runtime GREEN

- Job `2509663.opbs` passed 77 focused tests, 894 full tests, and the static v4 pipeline.
- The dynamic timeout exposed a stream-key/instance-key distinction in the revised current-pointer reader; the API now carries the stable contributor key explicitly.

# 2026-08-09 13:21 JST — P4 incremental review remediation precommit gate PASS

- Work unit `p4-incremental-remediation-precommit` passed on its fourth run after the mandatory three-failure comprehensive review. Job `2509702.opbs` ran on `mg0007`, one `debug-g` node/process, literal group `xg24i002`, and exited 0 in `00:01:36` under the evidence-based `00:10:00` limit.
- Static gates passed repository Ruff, the 20-file format scope, and the frozen/current boundary Checker. The focused admission/runtime suite passed `78 passed in 6.76s`; the complete repository suite passed `896 passed in 66.63s`.
- The real static pipeline finalized at v2 with 256 direct tokens, two acknowledged terminal fences, zero hard-crash gap and no pending/selected update. The dynamic pipeline finalized at v12 with 3,840 direct tokens, one acknowledged fence and the same zero-gap/empty-pending invariants. Both authority databases passed `PRAGMA integrity_check`.
- This GREEN run falsifies all three preceding causes: committed cross-epoch admission replay is idempotent, the unrelated source-identity second-boundary test did not recur, and the dynamic reader resolves the stable stream-key pointer while validating the instance/fence response.
- Evidence: `artifacts/20260809-132119_p4-incremental-remediation-precommit_pass.json`; joined stdout `fsdiloco_plan03_p4.o2509702` (SHA-256 `58e13f5dcf0ff5c5dbcbd4a9217e79c0c171989e13ef847428a72a2f5c0caedc`). This is dirty-worktree precommit evidence; clean frozen-target main and affected specialty gates remain required before phase-final.
- After evidence persistence, `clean_run` dry-ran and deleted exactly 8 static plus 13 dynamic terminal proposal objects (150,817 bytes), retaining authority/configuration/control/history evidence. Immutable inventories are `artifacts/20260809-132200_p4-incremental-remediation-precommit-{static,dynamic}_cleanup.json`.

# 2026-08-09 13:30 JST — P4 final dirty-tree remediation regression PASS

- After aligning the valid history object bytes with their canonical digest and moving the static test admission signal after the final pre-torch revalidation, job `2509752.opbs` reran the complete P4 gate on `mg0007`. It exited 0 in `00:01:35` under the required `00:10:00` walltime.
- Static gates passed Ruff, 20-file format, frozen/current Checker, focused `78 passed in 6.59s`, and full `896 passed in 66.11s`. The real static/dynamic pipelines finalized at v2/v12 with 192/3,840 direct tokens, 2/1 acknowledged fences, zero hard-crash gap, no pending/selected updates, and SQLite integrity `ok`.
- Evidence: `artifacts/20260809-133021_p4-incremental-remediation-final-precommit_pass.json`; joined stdout SHA-256 `a9b6aafbc94c1eb2d9fc8a8da480cb9eec5f951a6c63be8aebb3ff492cfb0cde`. The changed MODE-02 pause boundary still requires the clean-target static-rerun specialty gate.
- Evidence-bound cleanup deleted exactly 5 static plus 13 dynamic terminal proposal objects (126,001 bytes) after inventory, retaining the authority and control history. Manifests: `artifacts/20260809-133100_p4-incremental-remediation-final-precommit-{static,dynamic}_cleanup.json`.

# 2026-08-09 13:37 JST — P4 clean target 352318f unaffected component gates PASS

- Clean target `352318fa67115b64a3ddfb38145ca1dc20bf253f` passed four target-bound components while its two replacement specialty gates independently exposed the pending-pointer defect recorded in `failures.md`.
- Main job `2509827.opbs` passed Ruff/format/Checker, focused `78`, full `896`, and real static/dynamic pipelines in `00:01:32`. Plan01 compatibility job `2509825.opbs` passed full `896` plus strict-v4 smoke in `00:01:13`. Error/manual-successor job `2509828.opbs` passed in `00:00:20`; two-host expiry takeover job `2509826.opbs` passed on `mg0843+mg0075` in `00:00:23`.
- Every run attested `git_dirty=false`, source fingerprint `sha256:c9a6c8f...`, finalized with acknowledged terminal fences, zero hard-crash gap and SQLite integrity `ok`. Evidence: `artifacts/20260809-133700_p4-remediation-target352-{main,plan01-regression,error-successor,two-host-takeover}_pass.json`.
- These results remain auditable component evidence but do not make target 352318f phase-final. The one-line pending-pointer fix changes the source tree, so the final aggregate will be bound to the successor target and rerun the affected main/static/dynamic gates.

# 2026-08-09 13:40 JST — P4 pending-pointer remediation precommit gate PASS

- Job `2509843.opbs` validated the direct RED-to-GREEN correction: a nonmatching current pointer now keeps an unprocessed request pending instead of manufacturing `AdmissionSupersededError`; request-specific rejection and exact matching response remain the only successful exits from initial admission polling.
- On `mg0007`, Ruff/format/Checker passed, focused `78 passed in 6.51s`, full `896 passed in 67.13s`, and real static/dynamic pipelines finalized v2/v12 with 256/3,840 direct tokens, acknowledged fences, zero hard-crash gap and integrity `ok`. PBS exited 0 in `00:01:37` under `00:10:00`.
- Evidence: `artifacts/20260809-134020_p4-pending-pointer-remediation-precommit_pass.json`; stdout SHA-256 `bc40128bf02a365ff0fa7774fc8885f2f0c378a153caa56787ab7b2b1d0837a5`. Clean-target static and dynamic replacement gates remain the decisive falsification tests.

# 2026-08-09 13:48 JST — P4 final remediation target and finding disposition PASS

- Frozen source target `27f1a0388cbe27a32b24e6d5d72ff550818a4797` passed all six clean-target components: main job `2509856.opbs` (`78` focused, `896` full, real static/dynamic), post-revalidation static replacement `2509855.opbs`, dynamic replacement `2509854.opbs`, candidate-error successor `2509857.opbs`, two-host expired-lease takeover `2509858.opbs`, and Plan01 regression `2509853.opbs`.
- Every target run attested `git_dirty=false` and source fingerprint `sha256:585201b9e6fd6b7f5599185c37dcc7d23c555e66fcdeede6aac7d2d0d16e360f`; versions were contiguous and unique, all terminal fences acknowledged, hard-crash gaps were zero, and every authority database returned integrity `ok`. Aggregate: `artifacts/20260809-134500_p4-remediation-final-target-runtime_pass.json`.
- Evidence-authorized cleanup inventories are `artifacts/20260809-134500_p4-remediation-final-target-*_cleanup.json`; all seven successful run roots were cleaned after their PASS records were persisted.
- The source-targeted P4 requirement Checker returned `PASS` for the frozen/current boundary, migration surface and all nine P4-owned rows including MODE-02. Evidence: `artifacts/20260809-134700_p4-remediation-final-target-requirements_pass.json`.
- Every Codex and Claude finding from the `0e8b14ed..d18fae0` review now has an explicit disposition in `artifacts/20260809-134800_p4-incremental-review-finding-dispositions_review.json`. Protocol/fencing blockers are fixed; bounded-size, retention, steady-state I/O and transient pre-torch retry remain mandatory P6 work, while operator collision guidance is mandatory P5 work. Malformed actor-addressed rejection and lock-file unlinking are rejected with security/concurrency evidence.
