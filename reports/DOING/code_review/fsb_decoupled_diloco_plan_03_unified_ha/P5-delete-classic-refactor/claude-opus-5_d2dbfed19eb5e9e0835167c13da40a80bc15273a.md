# P5-delete-classic-refactor 独立代码审查（claude-opus-5）

- **Plan**：`fsb_decoupled_diloco_plan_03_unified_ha`
- **Phase**：`P5-delete-classic-refactor`
- **Base commit**：`77e047cc5e291153736f9abbffb8986e6b912330`（`docs: close P4 mandatory fenced runtime`）
- **Target commit**：`d2dbfed19eb5e9e0835167c13da40a80bc15273a`（`refactor: remove classic and fragment runtimes`）
- **审查范围**：`git diff 77e047cc5e291153736f9abbffb8986e6b912330 d2dbfed19eb5e9e0835167c13da40a80bc15273a`（214 files changed, 5,903 insertions(+), 42,712 deletions(-)）
- **Reviewer 角色**：只读。除本报告外未修改任何文件、未改动 Git 状态、未 qsub/qdel、未删除 run 数据。
- **审查者可复现的验证环境**：login node `miyabi-g1`（无 PBS 分配），`.venv/bin/python` 3.13.13。仅运行了不加载 torch 的轻量静态检查与小体量测试文件；未在登录节点运行完整 pytest / GPU workload。

---

## 0. 结论摘要

**CHANGES_REQUIRED**

在 review target commit `d2dbfed` 上：

1. 仓库测试套件是**红**的——`tests/test_plan03_checker.py::test_plan03_checker_blocks_real_tracked_fragment_boundary_drift` 必然失败，因为它 `git clone` 当前 HEAD 后去读一个已被本 commit 删除的 fragment config。P5 的 evidence artifact 自述来源是 *pre-commit worktree*，因此 `573 passed` 这条证据不绑定被审查的 commit。
2. Plan §11.12 要求"使用一个实现避免七份逻辑漂移"的唯一 Checker，其中**三条 phase 门禁路径在本 commit 后硬损坏**：`verify_p3_operational_contracts()` 抛 `FileNotFoundError`、`verify_boundaries()` 抛 `KeyError`、`verify_p4_migration_contracts()` 永久返回 3 条 difference。P5 的 PBS 只调用了新增的 `--verify-p5-contracts`，因此这些回归没有被门禁捕获，且守护它们的测试是被删除/改写成"断言坏状态"而不是修复。

删除本身（classic/fragment writer、8 fragment config + 5 fragment PBS + 1 历史 control pair、旧 schema/bootstrap、legacy reader 边界、docs 同步、逐 test-function 删除记账）质量很高，且记账 artifact 可由 `scripts/miyabi/build_plan03_p5_test_accounting.py` **逐字节复现**（我已验证）。问题集中在 Checker/gate 生命周期与残留 dead surface，不在 runtime 正确性。

| 严重度 | 数量 |
|---|---:|
| Critical | 1 |
| High | 2 |
| Medium | 5 |
| Low | 5 |

---

## 1. 审查覆盖范围

### 已审查的源代码
`fs_diloco/core/config.py`、`core/config_v4.py`、`legacy/__init__.py`、`legacy/config_v1_v3.py`、`legacy/fragment_v0.py`（新增）、`legacy/reader.py`（新增）、`modeling/training.py`、`protocol/__init__.py`、`protocol/merge.py`、`runtime/{learner,syncer}_entrypoint.py`、`runtime/{learner,syncer}_v4.py`、`storage/admission.py`（自 `protocol/admission_v4.py` rename R099）、`storage/control.py`（自 `protocol/control_v4.py` rename R098）、`storage/artifact_policy.py`、`storage/leader_lease.py`、`storage/paths.py`、`storage/run_initializer.py`、`baselines/protocol.py`、`tools/{analysis,authorize_static_replacement,eval_lm_harness,publish_quality_gate,validation_eval}.py`，以及全部 20 个被删除的 production 源文件与被删除的 Plan01/02 tool/probe。

### 已审查的测试
新增 `tests/architecture/test_p5_removed_runtime.py`、`tests/legacy/test_legacy_v1_v3_reader.py`、`tests/tools/test_authorize_static_replacement.py`；改写 `tests/test_config.py`、`tests/test_fragment_analysis.py`、`tests/test_inner_scheduler.py`、`tests/test_plan03_checker.py`、`tests/test_validation_eval.py`、`tests/runtime/test_p4_mandatory_runtime.py`、`tests/protocol/test_p3_accounting_selection_cursor.py`、`tests/storage/test_authority_p3_operational.py`、`tests/support/__init__.py`；38 个被删除的 test/support 文件（252 个 test function）。

### 已审查的配置 / PBS / launcher / Checker / 文档
26 个保留 config 的 removed-key 清理；8 fragment config + 5 fragment PBS + 1 历史 control pair + 1 classic-only `terminal_capture` config 的删除；`scripts/miyabi/run_plan03_phase5_tests.pbs`（新增）、`run_plan03_phase4_tests.pbs`、`run_plan03_phase4_error_successor.pbs`；`scripts/miyabi/check_plan03.py`、`scripts/miyabi/build_plan03_p5_test_accounting.py`（新增）；`fs_diloco/tools/launch_independent_run.py` 的 README 用法；`README.md`、`docs/00..08`、`docs/README.md`、`docs/modules/*.md`；requirement matrix、`progress.md`、`code_review.md`、`failures.md` 与 9 个新 artifact。

### 我实际执行过的验证（只读）
| 检查 | 结果 |
|---|---|
| `python -m compileall -q fs_diloco tests scripts/miyabi` | PASS |
| `.venv/bin/ruff check fs_diloco tests scripts/miyabi` | PASS |
| `.venv/bin/ruff format --check`（P5 修改文件子集） | PASS |
| `bash -n scripts/miyabi/*.pbs` | PASS；`run_plan03_phase5_tests.pbs:4` literal `group_list=xg24i002` |
| `git diff --check <base> <target>` | PASS |
| markdown link + 仓库路径引用扫描（README + docs/**） | 0 broken |
| `check_plan03.py --verify-p5-contracts` | **PASS**（differences=[]） |
| `check_plan03.py --verify-boundaries` | **BLOCKED**（`KeyError`，见 High-2） |
| `check_plan03.py --verify-p3-operational-contracts` | **BLOCKED**（`FileNotFoundError`，见 High-1） |
| `check_plan03.py --verify-phase-requirements P3-operational-robustness` | **BLOCKED**（同上） |
| `pytest tests/architecture tests/legacy tests/tools tests/test_config.py tests/test_fragment_analysis.py` | 108 passed |
| `pytest tests/test_plan03_checker.py` | **1 failed, 19 passed**（见 Critical-1） |
| 复现 `build_plan03_p5_test_accounting.py --output /tmp/... --current-collected 573` | 与 tracked artifact **完全一致** |
| 记账 artifact 的 57 条 replacement assertion 在当前树中存在性 | 57/57 存在 |
| 记账 artifact 的删除清单 vs `git diff --name-status` + AST | 252/252 function、37/37 test 文件完全吻合 |

---

## 2. Findings

### Critical

#### C-1 review target commit 上仓库测试套件是红的；P5 evidence 绑定的是 pre-commit worktree

**文件/行**：`tests/test_plan03_checker.py:81-95`（失败点 `:89`）；证据 `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260809-171100_p5-tests_pass.json`

**证据**

```
$ .venv/bin/python -m pytest -q tests/test_plan03_checker.py
E  FileNotFoundError: [Errno 2] No such file or directory:
   '/tmp/pytest-of-x10041/pytest-13/test_plan03_checker_blocks_rea0/clone/
    configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml'
tests/test_plan03_checker.py:89
FAILED tests/test_plan03_checker.py::test_plan03_checker_blocks_real_tracked_fragment_boundary_drift
1 failed, 19 passed in 39.87s
```

测试在 `:83-86` `git clone --no-hardlinks $ROOT` 后于 `:87-89` 读取 `configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml`。该 config 正是本 commit 删除的 8 个 fragment config 之一。`git clone` 取的是 **committed HEAD**，因此：

- 在 P5 编译期（HEAD 仍是 `77e047c`、删除只在 worktree），clone 里文件还在 → 测试通过；
- 在 `d2dbfed` 被 commit 之后，clone 里文件消失 → 测试必然失败。

evidence artifact 自己写明了这一点：

```json
"source_base": "77e047cc5e291153736f9abbffb8986e6b912330",
"source_tree": "review-target worktree before P5 commit",
"full_pytest": {"collected": 573, "passed": 573, "failed": 0}
```

**失败场景**：任何人在 `d2dbfed`（或其任何后代）上执行 `pytest -q` 或 `pytest -q tests/test_plan03_checker.py`，都会得到 1 个 failure。P5 PBS `scripts/miyabi/run_plan03_phase5_tests.pbs:69,71` 的两次 pytest 都覆盖这个文件，因此**该 PBS 在 target commit 上会以非零退出结束**。

**影响的验收条件**：Plan §10.5「unified runtime、baseline、analysis/eval 完整回归通过」在 target commit 上不成立；§11.13 要求 phase review 冻结的是 review-target commit，而现有 evidence 不绑定该 commit（也缺 `source_commit` / `source_identity.git_dirty` 字段，见 M-2）。

**修复建议**

1. 让该测试对 HEAD 无关：改为用 `inventory(clone, source_ref=FROZEN_COMMIT)` 或直接从 `git show <frozen>:configs/...` 取内容构造 drift fixture，而不是从 clone 的工作树读文件；或改为完全合成一个含 `fragments: {enabled: true}` 的 config 写入 clone。
2. 修复后在 **clean 的、已 commit 的 target** 上重跑 `run_plan03_phase5_tests.pbs`，并生成带 `source_commit` + `git_dirty=false` 的新 evidence artifact 替换 `20260809-171100_p5-tests_pass.json`。

**缺失测试**：需要一条 meta 检查（可放进 `tests/architecture/`）断言 `tests/` 中不存在依赖"当前 HEAD 之前才存在的 tracked 路径"的 fixture——即所有 `git clone`/`ls-tree` 型测试必须显式指定 `source_ref`。

---

### High

#### H-1 `verify_p3_operational_contracts()` 读取已删除文件，P3 phase 门禁永久 BLOCKED

**文件/行**：`scripts/miyabi/check_plan03.py:778-822`，具体 `:782`（`fs_diloco/storage/fenced_store.py`）与 `:783`（`fs_diloco/runtime/launch_outbox.py`）；调用点 `:978-984`（`--verify-phase-requirements P3-operational-robustness`）与 `:985-990`（`--verify-p3-operational-contracts`）

**证据**

```
$ python scripts/miyabi/check_plan03.py --verify-p3-operational-contracts \
    --expect .../20260808-223500_p0-runtime-surface-inventory_review.json \
    --inventory-output /tmp/p5_review_check.json
BLOCKED
$ jq -r '.error' /tmp/p5_review_check.json
FileNotFoundError: [Errno 2] No such file or directory:
  '/work/.../fs_diloco/storage/fenced_store.py'

$ python scripts/miyabi/check_plan03.py --verify-phase-requirements P3-operational-robustness ...
BLOCKED   # 同一 FileNotFoundError
```

两个文件都在本 commit 的删除清单里（`check_plan03.py:56` 和 `:52` 自己把它们列进 `P5_REMOVED_SOURCE`）。第 790 行还检查 `resolve_manual_review_launch_request`——该 symbol 在当前树中已完全不存在。

作者显然意识到这一类问题：`_bound_mutators()` 在 `:147-149` 专门加了 `if source_ref is None and not (root / path).is_file(): return []` 的守卫，但 `verify_p3_operational_contracts` 没有同样处理，且原本守护它的 `tests/test_plan03_checker.py::test_plan03_checker_guards_reviewed_cross_file_operational_contracts` 被删除（diff 中被 `test_plan03_checker_guards_p5_removal_and_compatibility_contracts` 替换）。

**失败场景**：P6 §11.13 需要对 target 做全 phase 的 current-state 复核；届时任何 `check_plan03.py --verify-phase-requirements P3-operational-robustness`（或 `--verify-p3-operational-contracts`）都返回 `BLOCKED` + 一条与 P3 语义无关的 `OSError`，无法区分"P3 契约破了"和"Checker 自己坏了"。

**修复建议**：把 P3 operational contract 的源码依据切换到 `_read_text(root, path, source_ref=<P3 frozen commit>)`（该函数已支持 `source_ref`，见 `:329-341` 的用法），或按 `_bound_mutators` 的模式对已迁移到 `storage/authority.py` 的等价不变量重写检查（`ELSE COALESCE(uncertainty_deadline, ?) END`、`reservation_released_at IS NULL`、`terminal_uncertain` 现在的归属需要重新定位）。不要通过"删除测试"了事。

**缺失测试**：恢复一条 `assert verify_p3_operational_contracts(ROOT) == []` 的 current-tree 测试；再加一条断言 `check_plan03.py --verify-phase-requirements P3-operational-robustness` CLI 只打印 `PASS`。

---

#### H-2 `verify_boundaries()` 在当前树抛 `KeyError`，`--verify-boundaries` 相位门禁崩溃；`verify_p4_migration_contracts()` 永久报差异且测试被改写成断言坏状态

**文件/行**：`scripts/miyabi/check_plan03.py:306-328`（`_boundary_manifest`，KeyError 发生在 `:328`）、`:419-446`（`verify_boundaries`，`:443` 调用）、`:331-417`（`verify_p4_migration_contracts`）、`:926-946`（main 中的 `--verify-boundaries` 分支）；`tests/test_plan03_checker.py:126-131`

**证据**

```
$ python scripts/miyabi/check_plan03.py --root . --expect <frozen> --verify-boundaries \
    --inventory-output /tmp/p5_bounds.json
BLOCKED
$ jq -r '.error' /tmp/p5_bounds.json
KeyError: 'configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml'
```

```
# 直接调用（venv python 3.13）
verify_boundaries            RAISED KeyError 'configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml'
verify_p4_migration_contracts -> ['config-migration.full-path-inventory',
                                  'config-migration.fragment-path-inventory',
                                  'config-migration.historical-path-inventory']
```

`_boundary_manifest()` 用 frozen `migration_boundaries` 里的 fragment/historical 路径去索引 `payload["manifest_sha256"]`；对 **current tree** 的 inventory，这些键已随文件删除而消失，直接 KeyError。`--verify-boundaries` 正是 P4 phase-final 的调用方式（`progress.md` 2026-08-09 16:00 记录"final tracked-evidence Checker passed with frozen/current boundary verification"），本 commit 之后该调用不再可用。

`verify_p4_migration_contracts()` 同理，它比较 frozen 与 current 的 config 分类清单，P5 删除后必然报 3 条差异。原测试 `test_plan03_p4_semantic_migration_allows_only_the_frozen_transform`（含 baseline-semantic 与 full-semantic 的真实 drift 反例）被删除，替换为 `tests/test_plan03_checker.py:126-131` 的 `test_plan03_p4_semantic_migration_still_detects_post_p4_config_changes`，它现在**断言这两条 difference 存在**——把一个坏掉的门禁固化成了期望行为，同时丢掉了原来对 baseline/full config 语义漂移的两个真实反例覆盖。

**失败场景**：P6 §11.13 phase-final 需要 `--verify-boundaries [+ --require-tracked-evidence]` 的绿灯；现在它要么崩成 `KeyError`（无差异明细），要么在修掉 KeyError 后被 3 条恒定差异永久 BLOCK。同时 P4 config 迁移语义漂移不再被任何测试守护。

**修复建议**

1. 给 `migration_boundaries` 增加 P5 生效后的"已删除"语义：`_boundary_manifest()` 对 `fragment_enabled_configs_delete_in_p5` / `historical_full_control_archive_separately` 在 current payload 中缺失时应视为"符合预期删除"，而不是 KeyError；对应的 `boundary_counts` 期望值也需要给出 P5 后的目标值（而不是复用 P0 frozen 计数）。
2. `verify_p4_migration_contracts()` 的分类比较应改为"frozen full/baseline 集合 ⊆ current，且 fragment/historical 集合在 P5 后必须为空"。
3. 恢复被删除的 baseline-semantic / full-semantic drift 反例（可用 `git show <frozen>:configs/...` 构造 clone，避免 C-1 那类 HEAD 依赖）。

**缺失测试**：`assert verify_boundaries(inventory(ROOT), _expected()) == []` 的 current-tree 断言；以及 `check_plan03.py --expect <frozen> --verify-boundaries` 的 CLI 端到端 `stdout == "PASS\n"` 测试（现有 `tests/test_plan03_checker.py:42-59` 只覆盖了不带 `--verify-boundaries` 的调用，所以没有捕获这个回归）。

---

### Medium

#### M-1 `load_query_config_snapshot()` 把 v4-only key 当作 legacy 特征，导致损坏的 v4 snapshot 被静默降级

**文件/行**：`fs_diloco/legacy/config_v1_v3.py:20-22`（`_LEGACY_TOP_LEVEL_RUNTIME_KEYS` 含 `"coordination"`、`"maintenance"`）、`:86-103`（`load_query_config_snapshot`）；消费方 `fs_diloco/tools/validation_eval.py:262`、`tools/publish_quality_gate.py:163`、`tools/eval_lm_harness.py:197`

**证据**（我实测复现，输入基于仓库自有的 `configs/fs_diloco_tiny_ha_static.yaml`）

```
unknown-leader-key  strict raises: ValueError unknown coordination.leader keys: lease_duration_secondz
unknown-leader-key  query projection SUCCEEDED -> fs_diloco_tiny_ha_static

invalid-lease       strict raises: ValueError coordination.leader.lease_duration_seconds must be > 0
invalid-lease       query projection SUCCEEDED -> fs_diloco_tiny_ha_static
```

`load_query_config_snapshot` 只捕获 `ValueError` 后用 `_has_legacy_runtime_keys()` 判定是否允许降级，而该判定把 v4 必备的 `coordination` / `maintenance` 也算作"已知 v1-v3 runtime key"。任何**当前 v4** resolved snapshot（它一定含这两个 key）只要 strict 解析失败，就会走 legacy 投影：`_project_legacy_payload()` 直接把整段 `coordination`/`maintenance` 丢掉，再用 `Config` 的**纯结构性** `validate()` 通过。

这与本仓库自己的两处声明不符：

- `fs_diloco/legacy/config_v1_v3.py:88-90` docstring：「Unknown keys do not trigger a compatibility downgrade. Only a snapshot containing a known v1-v3 runtime key is eligible for the legacy projection.」
- `docs/08-compatibility-and-migration.md`：「只有识别到已知旧 runtime key 才进入 legacy projection。未知拼写不会触发宽松 downgrade。」

**失败场景**：一个 v4 run 的 `control/run_config.resolved.yaml` 因为部分写入/人工编辑而在 `coordination.leader` 里出现拼写错误或非法 lease 参数；`python -m fs_diloco.tools.validation_eval --run-root <run>` 不再 fail-closed，而是用一个丢掉 leader/maintenance 的投影继续跑，并把 validation 结果写回 `metrics/`，附带的 config 指纹与实际 run 语义不一致。

**修复建议**：把降级判据收紧为"含 v1-v3 **专有** key"（`init` / `fragments` / `failure_sim` / `coordination.syncer_ha` / `coordination.recovery_submission` / `sync.upload_mode` / `sync.stop_after_global_tokens` / `sync.capture_terminal_predecessor_for_eval` / `liveness.quorum_policy` / `inner_optimizer.reset_on_global_update` / `learner.prediction_reconcile_timeout_seconds`），把 `coordination` / `maintenance` 从 `_LEGACY_TOP_LEVEL_RUNTIME_KEYS` 移除；对 P4 时代含 `coordination.recovery_submission` 的 snapshot 用嵌套判据单独放行。

**缺失测试**：新增反例——(a) 含未知 `coordination.leader` 子键的 v4 snapshot，(b) `coordination.leader.lease_duration_seconds = 0` 的 v4 snapshot：两者都必须让 `load_query_config_snapshot` **抛出**原始 `strict_error`，而不是投影成功。现有 `tests/test_validation_eval.py:149-205` 只覆盖了"旧 key 存在 → 投影成功"的正向路径。

---

#### M-2 P5 三条 requirement 没有 `PLAN03_REQUIREMENTS` owner 绑定，phase 无法按 §11.12 关闭

**文件/行**：`plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv`（`P5-FRAGMENT` / `P5-ARCH` / `LEGACY-01` 三行，status 由 `pending` 改为 `completion-candidate`）；`fs_diloco/legacy/reader.py`、`fs_diloco/legacy/config_v1_v3.py`、`fs_diloco/legacy/fragment_v0.py`、`tests/architecture/test_p5_removed_runtime.py`、`tests/legacy/test_legacy_v1_v3_reader.py`（均无 `PLAN03_REQUIREMENTS`）

**证据**

```
$ grep -rn "PLAN03_REQUIREMENTS" fs_diloco tests | grep -i "P5\|LEGACY"
(no output)

$ verify_phase_requirements(ROOT, matrix, "P5-delete-classic-refactor", expected_source_commit=HEAD)
DIFFS ['requirements.P5-FRAGMENT.status',  'requirements.P5-FRAGMENT.artifact-contract',
       'requirements.P5-FRAGMENT.implementation', 'requirements.P5-FRAGMENT.tests',
       'requirements.P5-ARCH.status', 'requirements.P5-ARCH.implementation',
       'requirements.P5-ARCH.tests', 'requirements.P5-ARCH.structured-checker-evidence',
       'requirements.LEGACY-01.status', 'requirements.LEGACY-01.implementation',
       'requirements.LEGACY-01.tests', 'requirements.LEGACY-01.structured-checker-evidence',
       'requirements.P6-DOCS.*']
```

`implementation` / `tests` 两条差异说明**没有任何**源文件或测试文件把自己声明为这三条 invariant 的 owner。`structured-checker-evidence` 差异则来自 `20260809-171100_p5-tests_pass.json` 缺 `source_commit` 与 `source_identity.git_dirty` 字段（`verify_phase_requirements` 在 `check_plan03.py:717-760` 明确要求）。

`failures.md` 2026-08-09 17:08 那条记录显示同类问题（DMB-05 owner 未迁移）在 attempt 5 被 full suite 捕获过；P5 自己的三条 requirement 之所以没被捕获，是因为没有任何测试对 `P5-delete-classic-refactor` 调用 `verify_phase_requirements`（`tests/test_plan03_checker.py:217-253` 只做了 P3）。

**失败场景**：P5 按 §11.13 走 phase-final 时，`check_plan03.py --verify-phase-requirements P5-delete-classic-refactor --require-tracked-evidence` 返回 `BLOCKED`，且没有任何前置门禁提示过这一点。

**修复建议**：在 `fs_diloco/legacy/reader.py`（`LEGACY-01`）、`fs_diloco/storage/admission.py` 或 `fs_diloco/storage/control.py`（`P5-ARCH`）、`fs_diloco/legacy/fragment_v0.py`（`P5-FRAGMENT`）加 `PLAN03_REQUIREMENTS = frozenset({...})`，并在 `tests/legacy/test_legacy_v1_v3_reader.py`、`tests/architecture/test_p5_removed_runtime.py` 加对应 marker；重跑并生成带 `source_commit` / `git_dirty` 的 P5 evidence。

**缺失测试**：`tests/test_plan03_checker.py` 增加与 P3 同构的 `test_plan03_p5_requirement_checker_binds_implementation_tests_and_evidence`。

---

#### M-3 writer 删除后残留不可达的 production surface（违反 §10.5 dead-entry scan）

**文件/行**

| 残留 | 位置 | 现状 |
|---|---|---|
| `candidate_launch_outbox` 表 | `fs_diloco/storage/schema_v4.sql:533` | 仅由 `tests/storage/test_schema_v4.py:46-47` 断言存在 |
| `LeaderSession.record_candidate_launch_request` | `fs_diloco/storage/authority.py:1883` | 生产端零调用者（`runtime/launch_outbox.py` 已删、`coordination.recovery_submission` 已删）；仅 `tests/storage/test_authority_p3_operational.py:813-996` 调用 |
| `LeaderSession.transition_candidate_launch_request` | `fs_diloco/storage/authority.py:1927` | 同上 |
| `fs_diloco/runtime/pbs_scheduler.py`（327 行，含 `qstat` 调用） | 整个模块 | 生产端零 import；只有 `tests/support/pbs.py:8` 引用 `PBSJobObservation`，而 `FakePBS` 本身在 `tests/` 中零使用 |
| `fs_diloco/observability/phase1_performance.py` | 整个模块 | 全仓零引用（消费方 `tools/phase1_matched_performance.py`、`plan02_*` 已随本 commit 删除） |
| `validate_global_adoption_strategy` | `fs_diloco/runtime/adoption.py:657-658` | 本 commit 把 `core/config.py` 的唯一调用点换成本地副本后，零调用者 |

**证据**

```
$ grep -rn "record_candidate_launch_request\|transition_candidate_launch_request" \
    --include="*.py" fs_diloco tests scripts | grep -v storage/authority.py
tests/storage/test_authority_p3_operational.py:813 ... (仅测试)

$ grep -rn "phase1_performance" --include="*.py" . | grep -v .venv
(no output)

$ grep -rn "validate_global_adoption_strategy" --include="*.py" fs_diloco tests scripts
fs_diloco/runtime/adoption.py:657:def validate_global_adoption_strategy(config: Any) -> None:

$ grep -rn "FakePBS" --include="*.py" tests | grep -v tests/support/
(no output)
```

**失败场景**：Plan §10.5 的 gate「import/command surface/dead-entry scan 通过」在本 commit 上不成立。运维层面：`candidate_launch_outbox` 依旧被 fresh v4 DDL 创建，操作者读 schema 会以为 recovery submission 仍是活的能力，而 README/docs 已声明其被删除（`docs/modules/runtime-syncer.md:19`「recovery submission 和 launch-outbox loop 已删除」）——schema 与文档直接矛盾。

**修复建议**：明确 disposition。要么（a）随 writer 一并删除 `candidate_launch_outbox` DDL、两个 authority mutator、`pbs_scheduler.py`、`phase1_performance.py`、`validate_global_adoption_strategy`、`tests/support/pbs.py` 及对应测试；要么（b）在 `docs/04-data-flow.md` / `docs/modules/storage.md` 显式记录它们作为 P6 dynamic scale-out 的预留 surface 并给出启用路径。二选一，不要留在"文档说删了、代码还在"的状态。

**缺失测试**：`tests/architecture/` 增加 dead-entry scan——遍历 `fs_diloco/**/*.py`，断言每个模块至少被另一个 `fs_diloco` 模块或一个 entrypoint import（白名单显式列出 shim 与 `__init__`）。

---

#### M-4 global adoption 策略校验出现两份实现

**文件/行**：`fs_diloco/core/config.py:463-484`（新增 `_validate_global_adoption_config`）vs `fs_diloco/runtime/adoption.py:381-390`（`RebaseGlobalAdoptionStrategy.validate`）、`:480-496`（`PredictGlobalAdoptionStrategy.validate`）、`:649-664`（dispatcher）

**证据**：本 commit 把 `core/config.py` 里的 `from ..runtime.adoption import validate_global_adoption_strategy` 换成了行内复制的 `_validate_global_adoption_config`。真实 runtime 走的是 `fs_diloco/runtime/learner_v4.py:378` 的 `make_global_adoption_strategy(config)`（内部调 `strategy_type.validate(config)`），而 `resolve_config()` 走行内副本。两份逻辑目前语义等价（我逐条比对过 `adopt_global_after_upload` / `poll_latest_during_inner_steps` / `nesterov` / `weight_decay=0` / `reconcile_timeout_seconds>0`），但已不再共享实现。

**失败场景**：将来给 `predict_post_publish_global` 增加一条新约束时，只改 `adoption.py` 会让 `resolve_config()`（baseline 与共享 config 测试路径）继续接受非法配置；只改 `config.py` 则真实 learner 不受保护。Plan §10.3 明确要求「不保留两套 …… implementation」。

**修复建议**：把策略约束表下沉到 `fs_diloco/core/`（例如 `core/adoption_rules.py`，纯数据/纯函数、无 torch 依赖），让 `core/config.py` 与 `runtime/adoption.py` 共同引用；这样既保住 core→runtime 不能反向依赖的边界（这正是本次拆分的动机），又消除副本。

**缺失测试**：一条参数化测试，对同一组非法 config 同时断言 `resolve_config()` 与 `make_global_adoption_strategy()` 抛出同一类错误。

---

#### M-5 P6 门禁所需的 harness 被删除且无替代

**文件/行**：删除的 `scripts/miyabi/publication_crash_probe.py`（435 行）、`scripts/miyabi/plan03_p0_performance.py`（388 行）、`scripts/miyabi/run_plan03_phase{0,1,2,3}_tests.pbs`、`tests/test_plan03_p0_performance.py`、`tests/test_plan03_p0_red.py`

**证据**：两个 probe 的 import 头确实与 classic writer 强耦合（`publication_crash_probe.py` import `runtime.syncer`、`storage.maintenance`、`storage.sqlite_store`；`plan03_p0_performance.py` 驱动 classic/static-HA 的 5-pair 配对试验），因此**删除本身是合理的**。问题是没有 v4 替代：

- Plan §11.5 **G4 publication crash matrix** 要求覆盖 15+ 个 crash point、每点 ≥10 次——当前仓库没有任何 crash-matrix 驱动脚本；
- Plan §11.11 **G10 paired performance** 要求 20 paired repeats、AB/BA 交替、共同 timer anchor——保留下来的 `fs_diloco/tools/paired_performance.py` 只有 `paired_noninferiority()` 统计函数（25 行起），没有编排；
- `run_plan03_phase{0,1,2,3}_tests.pbs` 删除后，P0–P3 的 focused/full 门禁无法按原命令复跑（P6 §11.13 要求对 target 做全 phase current-state 复核）。

**失败场景**：进入 P6 时发现 G4/G10 没有可执行 harness，需要在验收阶段临时新写，违背 §11.1 G0「所有测试/实验预先定义 success evidence」。

**修复建议**：在 P5 收尾或 P6 开头显式记录这三项的 successor（v4 crash probe、v4 paired-performance driver、统一的 `run_plan03_phase_tests.pbs --phase <id>`），并把该 gap 写入 `progress.md` 与 requirement matrix 的 P6 行，而不是让它隐含消失在 `-42,712` 行删除里。

**缺失测试**：无需单测；需要 plan/matrix 层面的显式 disposition 记录。

---

### Low

#### L-1 `_tracked()` 名不副实：现在包含 untracked 文件

**文件/行**：`scripts/miyabi/check_plan03.py:84-93`

```python
output = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", prefix)
```

变更动机可追溯（`failures.md` 2026-08-09 16:37 记录 attempt 2 因新增未 tracked 文件漏掉 format scope 而失败），对"删除面不得复活"是**更严格**的，但函数名与 §10.5「不存在当前 tracked runtime」的措辞已不一致，且一个临时放在 `configs/` / `tests/` / `fs_diloco/` / `scripts/miyabi/` 下的未提交草稿文件会改变 current inventory、进而可能让 `verify_boundaries` 的 `boundary_counts` 假阳性。建议改名为 `_worktree_files()` 并在 docstring 说明"cached + untracked non-ignored"，同时确认 `verify_tracked_evidence()`（`:824-850`，用的是独立的 `git ls-files --error-unmatch`）语义不受影响——我已确认不受影响。

#### L-2 `open_query_only_database()` 丢失 busy timeout，且 URI 未转义

**文件/行**：`fs_diloco/legacy/reader.py:26-41`；对比被删除的 `fs_diloco/storage/schema_bootstrap.py:509-521`

旧 `open_readonly()` 用 `sqlite3.connect(..., timeout=60.0)`；新实现使用默认 5 秒。`fs_diloco/tools/analysis.py:98` 通过 `open_query_only_database as open_readonly` 消费它，因此 `python -m fs_diloco.analysis <运行中的 run>` 在写者持锁时会比以前早 55 秒放弃并抛 `sqlite3.OperationalError: database is locked`。另外 `f"file:{path.as_posix()}?mode=ro"` 未对路径做 URI 转义，run root 路径含 `?` 或 `#` 时 URI 解析错误。建议恢复 `timeout=60.0` 并用 `urllib.parse.quote(path.as_posix())`。

**缺失测试**：一条用含 `#` 的 tmp 路径构造 legacy fixture 的测试。

#### L-3 删除面测试的字面量匹配偏弱

**文件/行**：`tests/architecture/test_p5_removed_runtime.py:101-115`、`:78-84`；`scripts/miyabi/check_plan03.py:568-583`

DDL 检查用 `f"create table {table}" not in ddl`；当前两个 schema 文件确实全部使用 `CREATE TABLE x`（35 + 7 处，0 处 `IF NOT EXISTS`），所以现在有效，但一旦有人写 `CREATE TABLE IF NOT EXISTS fragments` 就会静默通过。同样 `test_retained_configs_cannot_express_removed_runtime_modes` 只查 `fragments` / `init` / `coordination.syncer_ha`，没查 `failure_sim` / `sync.stop_after_global_tokens` / `sync.capture_terminal_predecessor_for_eval`（Checker 查了，测试没查）。建议改为解析 `sqlite3` 内存库后读 `sqlite_master`，并把 config 检查项与 `verify_p5_contracts` 对齐。

#### L-4 docstring / README 与实际结构的小幅漂移

- `fs_diloco/observability/__init__.py:1` 仍写 `"""Structured logs, CSV metrics, and experiment telemetry."""`，但 `observability/metrics.py` 已删除。
- 重写后的 `README.md` 目录段不再列出保留的 `fs_diloco/{learner,syncer,analysis,eval_lm_harness}.py` shim（§10.1 明确要求保留）、`configs/`、`scripts/`、`tests/`、`docs/`、`reports/`；文档段也不再链接 `docs/00-glossary.md` 与 `docs/05-code-structure.md`（两者仍从 `docs/README.md` 可达）。链接扫描 0 broken，属可读性问题。

#### L-5 §10.3 目标结构中的 `runtime/services/` 未落地

Plan §10.3 的目标布局列出 `runtime/services/`（ingest / merge / publication / terminal / scheduler reconcile）。当前树没有该包：`runtime/syncer_v4.py` 991 行承担 admission+ingest+selection+merge+publication+terminal loop，`storage/authority.py` 达 5,122 行。§10.3 同时声明"架构门禁按职责，不按任意行数"，而四条职责门禁（protocol 不依赖 Path/storage/runtime、runtime 不 import legacy、baselines 不 import runtime、entrypoint 不拼 SQL/不调 qsub/qstat）我已逐条验证**全部通过**，`docs/05-code-structure.md` 也如实描述了当前结构（没有虚构 `services/`）。因此这不是缺陷，但属于与 plan 文本的显式偏离，应在 phase 收尾时明确记录为"已接受的偏离"或排入 P6。

---

## 3. 未发现问题的检查项

以下项目经检查未发现 finding，记录以界定审查边界：

- **删除完整性**：20 个 production 源文件、8 fragment config、5 fragment PBS、1 历史 control config/PBS pair 全部不存在；`check_plan03.py --verify-p5-contracts` differences=[]；`tests/architecture/test_p5_removed_runtime.py` 全绿。额外删除的 `configs/fs_diloco_gpt2_wikitext2_8l_5000steps_terminal_capture.yaml` 属计划外，但已在 `20260809-171100_p5-fragment-archive_review.json` 的 `additional_classic_only_config_deleted` 里给出理由（writer 已随 classic syncer 删除、保留 key 会暴露 no-op 选项），且 query-only 评估侧 `resolve_terminal_predecessor_checkpoint` 保留并仍有 4 处测试覆盖（`tests/test_validation_eval.py:96,102,107`）。
- **归档 tag**：`archive/classic-full-v1-final` 与 `archive/fragment-v0-final` 均解析到 `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`，与 `check_plan03.py:39` 的 `FROZEN_FULL_COMMIT`、`tests/architecture/test_p5_removed_runtime.py:129`、README、`docs/01-overview.md:33`、`docs/08` 完全一致。
- **导入边界**：`fs_diloco/protocol/*.py` 无 `pathlib` / `..storage` / `..runtime`；`fs_diloco/runtime/*.py` 无 `legacy`；`fs_diloco/baselines/*.py` 无 `runtime`（`baselines/protocol.py` 的 `maybe_autocast` 已迁到 `modeling/training.py:12-17`）；两个 entrypoint 无 `sqlite3` / `qsub` / `qstat` / SQL 字面量。`python -m compileall` 全绿，无悬挂 import。
- **rename 正确性**：`protocol/admission_v4.py → storage/admission.py`（R099）与 `protocol/control_v4.py → storage/control.py`（R098）只改相对 import 路径，`PLAN03_REQUIREMENTS` marker 与逻辑未动；所有调用点（`runtime/{learner,syncer}_entrypoint.py`、`runtime/{learner,syncer}_v4.py`、`tools/authorize_static_replacement.py`、`tests/runtime/test_p4_mandatory_runtime.py`、`scripts/miyabi/run_plan03_phase4_tests.pbs:51-52`）已同步。
- **config schema 收敛**：`InitSection` / `CoordinationSection` / `SyncerHASection` / `RecoverySubmissionSection` / `FragmentSection` / `FailureSimSection` 及全部相关 resolver 校验（约 190 行）删除干净；`REMOVED_CONFIG_KEYS` 与 `_REMOVED_V4_PATHS` 同步扩充；`legacy_oracle` profile 移除、默认切到 `full_v4_shared`；26 个保留 config 的 `failure_sim` / `coordination.recovery_submission` 块清理完毕；`config_v4_to_dict` 不再泄漏旧键。
- **stop-target 校验归位**：`code_review.md` 中的 Medium（"shared `resolve_config` 拥有一条它看不到的 v4-only 规则"）已按建议修复——检查移到 `ConfigV4.validate`（`fs_diloco/core/config_v4.py:175-180`），同时看得见 `stop_after_outer_steps` 与 `stop_after_direct_weight_tokens_applied`；`tests/protocol/test_p3_accounting_selection_cursor.py:34-40` 覆盖 direct-token-only 正例。
- **legacy reader 的 query-only 不变量**：`mode=ro` + `PRAGMA query_only=ON` 并回读校验；`LegacyRunReader` 强制 DB 位于 run root 内；`export_legacy_summary` 拒绝写入 run root（含等值路径）；`tests/legacy/test_legacy_v1_v3_reader.py:101-122` 对 full 与 fragment 两种 fixture 断言 inode/size/mtime_ns/sha256 前后完全不变，并断言 `CREATE TABLE` 被拒；token 语义按 `legacy_total_seen_tokens` + `legacy-v1-v3-query-only` semantic version 导出，未做伪换算（`:137` 断言 `"total_seen_tokens" not in payload`）。
- **v4 DDL 不含 fragment 表**：`schema_v4.sql` / `schema_v4_dynamic.sql` 均无四张 fragment 表；Checker 与测试双重校验。
- **`run_initializer` 简化**：删除 `protocol_version == V4` 的分支后统一走 `LeaderAuthority.read.integrity_check()`；protocol_version 的校验仍由 `fs_diloco/core/run_descriptor.py:93-98` 的 `checks` 保证，未产生校验空洞；`bootstrap_mode` 仍用于 identity 比对，不是死变量。
- **paths / artifact_policy 清理**：`fragments*` / `syncer_launch_claims` / `recovery_submission_history_jsonl` 属性与目录创建同步删除，`_MUTABLE_SUBTREES` 与 `build_artifact_policy()` 一致。
- **operator 工具健壮性**：`tools/authorize_static_replacement.py:40-55` 新增 `FileExistsError → parser.error("...issue the replacement with a fresh --new-attempt-id")`，避免不可变 authorization 冲突以 traceback 形式暴露；`tests/tools/test_authorize_static_replacement.py` 覆盖。
- **测试删除记账**：37 个测试文件 / 252 个 test function 与 `git diff` + AST 结果**逐条吻合**（唯一差额 `tests/support/tmp_authority.py` 是 helper，无 test function）；57 条 replacement assertion 在当前树中 **57/57 存在**；分类 208 migrate / 4 retain-legacy-reader / 40 delete-obsolete；`build_plan03_p5_test_accounting.py --output ... --current-collected 573` 的重生成结果与 tracked artifact **完全相同**（fail-closed：replacement 不存在时直接报错，见 `failures.md` 17:04 记录）。
- **静态门禁**：ruff lint / 选定 scope 的 ruff format / compileall / 全仓 `bash -n` / `git diff --check` 全部通过；`run_plan03_phase5_tests.pbs` 有 literal group ID `xg24i002`、`walltime=00:10:00`（≥10 分钟下限）、`CUDA_VISIBLE_DEVICES=""`。
- **文档同步**：README + `docs/00..07` + 全部 `docs/modules/*.md` 改为 Full Protocol v4 current-state；只新增 `docs/08-compatibility-and-migration.md`（符合 §10.4 "只新增"约束）；markdown link 与仓库路径引用扫描 0 broken；docs 中未出现已删除的脚本/config 名；具体 job ID 与性能数字留在 `reports/`。README 的 `launch_independent_run` 示例参数（`--syncer-walltime` / `--learner-walltime`）与 `fs_diloco/tools/launch_independent_run.py:188-195` 一致。
- **P4 PBS 同步**：`run_plan03_phase4_error_successor.pbs:59,179` 由 `recovery_submission.enabled is False` 改为 `"recovery_submission" not in config["coordination"]`，与新 v4 envelope 一致。

---

## 4. 建议的修复顺序

1. **C-1**：修 `tests/test_plan03_checker.py:81-95` 的 HEAD 依赖，在 clean 的 target commit 上重跑 P5 PBS，重发 evidence。
2. **H-1 / H-2**：修 Checker 的三条坏路径，恢复被删/被改写成"断言坏状态"的守护测试，并把 `--verify-boundaries` 加进 P5 PBS 的 Checker 调用（`run_plan03_phase5_tests.pbs:64-68` 当前只调 `--verify-p5-contracts`，正是这些回归逃逸的原因）。
3. **M-1**：收紧 legacy 降级判据 + 两条反例测试。
4. **M-2**：补 `PLAN03_REQUIREMENTS` owner 与 P5 requirement checker 测试，重发带 `source_commit` / `git_dirty` 的 evidence。
5. **M-3 / M-4**：dead surface 的明确 disposition；adoption 校验去重。
6. **M-5**：把 G4 / G10 harness 的 successor 写进 plan/matrix/progress。
7. **L-1..L-5**：可与上述任一轮合并。

---

## 5. 最终判定

# CHANGES_REQUIRED

阻塞项：**C-1**（review target commit 上测试套件为红，且 phase evidence 绑定的是 pre-commit worktree）、**H-1**、**H-2**（Plan §11.12 唯一 Checker 的三条 phase 门禁路径在本 commit 后硬损坏，且守护测试被删除或改写成断言坏状态）。

在 C-1 与 H-1/H-2 修复、并在 clean 的 review-target commit 上重跑 P5 完整门禁产出新 evidence 之前，`P5-delete-classic-refactor` 不应按 Plan §10.5 / §11.13 关闭。M-1、M-2 应在同一轮修复（M-2 直接决定 phase 能否通过 `--verify-phase-requirements`）；M-3..M-5 与 L-1..L-5 可作为同 phase 内的后续 commit 或显式记录的 P6 follow-up。
