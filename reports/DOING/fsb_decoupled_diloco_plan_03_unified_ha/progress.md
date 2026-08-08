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
