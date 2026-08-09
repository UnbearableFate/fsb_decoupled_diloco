# P5-delete-classic-refactor 增量代码审查（claude-opus-5）

- **Plan**：`fsb_decoupled_diloco_plan_03_unified_ha`
- **Phase**：`P5-delete-classic-refactor`
- **Base commit**：`d2dbfed19eb5e9e0835167c13da40a80bc15273a`（`refactor: remove classic and fragment runtimes`）
- **Target commit**：`eb56219e13817b1f659921ea093c2dfdfa473abd`（`fix: complete P5 runtime service remediation`）
- **审查范围**：`git diff d2dbfed19eb5e9e0835167c13da40a80bc15273a eb56219e13817b1f659921ea093c2dfdfa473abd`（62 files changed, 4,016 insertions(+), 679 deletions(-)）
- **Reviewer 角色**：只读。除本报告外未修改任何文件、未改动 Git 状态、未 qsub/qdel、未删除 run 数据、未 commit/push/开 PR。报告中不含任何 secret/token/凭据/完整环境变量。
- **验证环境**：login node，`.venv/bin/python` 3.13 + torch 2.13.0+cu132。只运行了 CPU-only、torch-free 的轻量测试子集与静态门禁；未在登录节点运行 full pytest / GPU workload / PBS 作业。
- **前序审查**：本轮是对 `claude-opus-5_d2dbfed...md`（CHANGES_REQUIRED，C-1/H-1/H-2/M-1..M-5/L-1..L-5）与 `gpt-5.6-sol_d2dbfed...md` 的 remediation 审查，同时对本 commit 新引入的 runtime service 做完整独立审查。

---

## 0. 结论摘要

**CHANGES_REQUIRED**

前一轮的阻塞项基本被真实修复（详见 §2）：`tests/test_plan03_checker.py` 的 HEAD 依赖已消除并在 target 上绿；`verify_p3_operational_contracts()` / `verify_boundaries()` / `verify_p4_migration_contracts()` 三条门禁在当前树全部 PASS 并被新增 current-tree 断言守护；`--verify-boundaries --verify-p3-operational-contracts` 已加入 P5 PBS；dead surface（`candidate_launch_outbox`、`phase1_performance.py`、`validate_global_adoption_strategy`、未被引用的 `pbs_scheduler.py`/`FakePBS`）已删除或接入；adoption 规则去重到 `core/adoption_rules.py`；legacy 降级判据收紧并补了反例；`runtime/services/` 按 §10.3 落地。

但本 commit 同时把 dynamic capacity/scheduler、manual terminal close 这两块**此前未被消费的 config surface 变成了活的 production runtime**，新代码带来 2 个可复现的 Critical 与 4 个 High：

1. **manual terminal close 完全不可用**：close reason 里的 operator 自由文本被直接拼进 `command_id`，必然违反 `_IDENTITY_RE`，`begin_terminal_close` 抛 `ValueError` 打死 syncer（我已端到端复现）。
2. **第二个 scale-out reservation 会打死 leader syncer**：`tick()` 选流不排除"已有未释放 reservation"的 stream，`_plan()` 只吞 budget 错误，`MembershipFenceError` 直接冒出 `run_fenced_syncer`。默认 `max_pending_launch_requests=2` 即可触发（我已复现）。
3. **launch state flapping 会把健康 job 误判成 manual_review**：`_transition` 的 `command_id` 不含 job/时间维度，command journal 命中 dedup 后返回旧结果而**不写库**，`qstat` 抖动（query_failed↔running）两轮后状态机卡死在 `terminal_uncertain`，随后超时升级 `manual_review`，该 learner 的 admission 被永久拒绝且 stream reservation 永不释放（我已复现）。
4. **legacy config projection 回归**：仓库里现存的 P4-era v4 run snapshot 在 base 上可被 `load_query_config_snapshot` 投影读取，在 target 上全部抛 `ValueError`，`validation_eval` / `eval_lm_harness` / `publish_quality_gate` 对这些 evidence run 全部失效（我已用真实 run 数据复现）。

此外 `scripts/miyabi/run_plan03_phase4_dynamic_replacement.pbs` 的手工 `--launch-request-id` 在新 admission 规则下必然被拒（§9.4 dynamic replacement 门禁破损），phase evidence 仍绑定 pre-commit worktree。

| 严重度 | 数量 |
|---|---:|
| Critical | 2 |
| High | 4 |
| Medium | 5 |
| Low | 5 |

---

## 1. 审查覆盖范围

### 源代码
`fs_diloco/runtime/services/{__init__,merge,terminal,dynamic_capacity}.py`（全新，911 行）、`fs_diloco/runtime/syncer_v4.py`（-310 行 inline merge/terminal，改为 service composition）、`fs_diloco/runtime/pbs_scheduler.py`（删除 candidate 提交/扫描，`submit_learner` 增加 stream/replacement 变量与 `-H` 历史查询）、`fs_diloco/runtime/{adoption,learner_entrypoint}.py`、`fs_diloco/core/{adoption_rules.py（新增）,config.py,versions.py}`、`fs_diloco/storage/{authority.py（+521）,admission.py,control.py,terminal_request.py（新增）,schema_v4.sql,schema_v4_dynamic.sql}`、`fs_diloco/legacy/{__init__,config_v1_v3,fragment_v0,reader}.py`、`fs_diloco/tools/{eval_lm_harness,validation_eval,request_terminal_close（新增）}.py`、`fs_diloco/cli.py`、删除的 `fs_diloco/observability/phase1_performance.py`。

### 测试
新增 `tests/runtime/test_{dynamic_capacity_service,terminal_service,pbs_scheduler}.py`、`tests/storage/test_{dynamic_admission_request,dynamic_launch_authorization,terminal_request}.py`；改写 `tests/test_plan03_checker.py`、`tests/storage/test_{authority_p2_dynamic,authority_p3_operational,p2_state_machine,schema_v4}.py`、`tests/{test_config,test_validation_eval}.py`、`tests/architecture/test_p5_removed_runtime.py`、`tests/legacy/test_legacy_v1_v3_reader.py`、`tests/support/pbs.py`。

### 配置 / PBS / launcher / Checker / 文档
`configs/fs_diloco_tiny_ha_dynamic_{2node,acceptance}.yaml`（learner_walltime → `00:10:00`）；`scripts/miyabi/run_plan03_phase5_tests.pbs`（新增 target/format scope、`--verify-boundaries`、`--verify-p3-operational-contracts`）；`scripts/miyabi/check_plan03.py`（`_tracked`→`_repository_files`、boundary/P4-migration 的 post-P5 投影、P3 operational contract 重定位）；`fs_diloco/cli.py close` 新 operator 入口；`README.md`、`docs/{02,03,05,06,07,08}`、`docs/modules/{observability,storage,tools}.md`；`reports/.../artifacts/20260809-191200_p5-review-remediation-precommit_pass.json`、`progress.md`、`failures.md`。未审查未变更的历史 report 正文。

### 我实际执行过的验证（只读）
| 检查 | 结果 |
|---|---|
| `python -m compileall -q fs_diloco tests scripts/miyabi` | PASS |
| `.venv/bin/ruff check fs_diloco tests scripts/miyabi` | PASS（All checks passed） |
| `bash -n scripts/miyabi/run_plan03_phase5_tests.pbs` | PASS |
| `git diff --check d2dbfed eb56219` | PASS |
| `check_plan03.py --expect <P0 frozen> --verify-boundaries --verify-p3-operational-contracts --verify-p5-contracts` | **PASS**（differences=[]，exit 0） |
| `check_plan03.py --verify-phase-requirements P5-delete-classic-refactor` | **BLOCKED**（见 H-4） |
| `check_plan03.py --verify-phase-requirements P3/P4` | BLOCKED（仅 `structured-checker-evidence`，属 evidence 绑定 HEAD 的既有语义，非本 commit 回归） |
| `pytest tests/test_plan03_checker.py tests/storage/test_{terminal_request,dynamic_admission_request,dynamic_launch_authorization}.py tests/runtime/test_pbs_scheduler.py` | **31 passed** |
| `pytest tests/runtime/test_{dynamic_capacity_service,terminal_service}.py tests/architecture tests/legacy tests/test_config.py tests/test_validation_eval.py` | **134 passed** |
| 全仓 dead-module 扫描（AST import 图） | 仅 `fs_diloco.eval_lm_harness`（`python -m` shim，预期） |
| 手写 repro：manual close / 重复 stream reservation / launch flapping / P4 snapshot 投影 | 4 项**全部复现**（见 §2 证据） |

---

## 2. Findings

### Critical

#### C-1 manual terminal close 把 operator 自由文本拼进 `command_id`，必然抛 `ValueError` 打死 syncer

**文件/行**：`fs_diloco/runtime/services/terminal.py:51`（构造 reason）、`:133-137`（`begin_terminal_close(command_id=f"terminal-close-{reason}")`）、`:171`（`finalize_terminal(command_id=f"terminal-finalize-{reason}")`）；约束在 `fs_diloco/protocol/_validation.py:13,96-100`（`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`），校验点 `fs_diloco/storage/authority.py:5097`；reason 字符集在 `fs_diloco/storage/terminal_request.py:26-27`（只限长度 1..256，不限字符）。

**证据**（我在 login node 的只读复现）

```
close reason: manual:terminal-26d9ffebb5af9fe5ba56d8468c518054:operator maintenance
finalize RAISED: ValueError command_id is not a safe protocol identity
```

即使 reason 不含空格也失败——`manual:` 分隔符本身就带 `:`，且 `terminal-close-manual:<32hex id>:<reason>` 长度轻易超过 128：

```
FAIL no-space: command_id is not a safe protocol identity  len 75   # ':' 非法
```

**失败场景**：run 配置 `terminal.admission_close_policy: manual`（`fs_diloco/tools/request_terminal_close.py:24-25` 强制要求）→ operator 按 `docs/07-operations.md:26-33` 执行 `python -m fs_diloco.cli close --reason "operator maintenance"` → 请求文件发布成功 → syncer 下一轮 `terminal_close_reason()` 返回 `manual:...` → `TerminalService.finalize()` 在 `begin_terminal_close` 抛 `ValueError`。该异常不在 `run_fenced_syncer` 的任何 except 内，leader 直接崩溃；successor 接管后读到同一个 immutable close request，再次崩溃 → crash loop，run 无法正常终态。

**影响的验收条件**：§10.5「unified runtime 完整回归通过」；`docs/07-operations.md` 新增的 Terminal close 运维流程；requirement matrix 的 `TERM-01/02/03`（`terminal.py:19` 自声明 owner）。

**修复建议**：把 command identity 与人类可读 reason 分开。`command_id` 用 `f"terminal-close-{request_id}"`（`request_id` 已是 `terminal-<32hex>`，本身就是合法 identity），把自由文本 reason 只作为 `reason` 参数传给 `begin_terminal_close`/`finalize_terminal`；或对所有 reason 统一 `hashlib.sha256(reason).hexdigest()[:16]` 后拼接。同时在 `publish_manual_terminal_request` 增加 reason 字符集白名单（可打印 ASCII、禁换行），并在 `terminal_close_reason` 返回值上加一条 `validate_identity(command_id_candidate)` 的 fail-fast 自检。

**缺失测试**：`tests/runtime/test_terminal_service.py` 现在只用 `terminal_close_reason` 断言 manual policy 的**字符串**（`:178-180`），从未把该字符串喂给 `TerminalService.finalize()`。需要一条端到端 case：manual policy + `finalize(reason=terminal_close_reason(...))` → 断言 `controller_status()["state"] == "finalized"`；并参数化覆盖含空格/冒号/非 ASCII/256 字符的 reason。

---

#### C-2 第二个 scale-out reservation 重选同一 stream，未捕获的 `MembershipFenceError` 打死 leader syncer

**文件/行**：`fs_diloco/runtime/services/dynamic_capacity.py:137-153`（`available = next(row for row in dynamic_streams() if row["state"] == "available" and row["current_instance_id"] is None)`，不排除已有未释放 reservation 的 stream）、`:218-235`（`_plan()` 的 `except RuntimeError` 只在 `"budget is exhausted" in str(exc)` 时返回 False，其余重新抛出）；authority 侧 `fs_diloco/storage/authority.py:2095-2113`（budget 检查在前、stream reservation 检查在后）；调用点 `fs_diloco/runtime/syncer_v4.py:226-232`（`capacity_service.tick(...)` 无任何 try/except）。

**证据**（`max_pending_launch_requests=2`，即 `ScalingSection` 的**默认值**，`fs_diloco/core/config.py:213`）

```
tick0 actions=() rows=[]
tick1 actions=('scale_out_planned', 'submitted') rows=[('launch-1ec971e', 0, 'submitted')]
tick2 RAISED MembershipFenceError: dynamic stream already has a launch reservation
```

`plan_dynamic_launch_request` 对 scale_out **不**更新 `streams` 表（只有 replacement 走 `_retire_dynamic_in_transaction`），因此 stream 0 在第一个 reservation 未释放时仍是 `state='available' AND current_instance_id IS NULL`，第二个窗口的 `next(...)` 再次选中它。`MembershipFenceError` 是 `RuntimeError` 子类（`authority.py:116`），但消息不含 `"budget is exhausted"`，`_plan` 直接 `raise`。

仓库自带的两个 dynamic config 把 `max_pending_launch_requests` 设成 1，budget 检查先命中（`authority.py:2101` 的 `RuntimeError` 被吞），所以现有测试与 acceptance config **掩盖**了这个缺陷；任何使用默认值或 >1 的 pending budget 的 run 都会踩到。

**失败场景**：`membership.mode=dynamic` + `scaling.enabled=true` + `max_pending_launch_requests >= 2`，持续低容量两轮后计划第一个 scale-out；下一轮仍低容量且 `productive + reserved < desired_contributors` → 重选同一 stream → leader syncer 以 `MembershipFenceError` 退出。successor 接管后 DB 状态不变，同样在第一次 `tick()` 崩溃 → crash loop，整个 run 停摆。

**影响的验收条件**：§9.4「dynamic replacement」与 §10.5「unified runtime 完整回归」；matrix `SCHED-01..SCHED-04`（`dynamic_capacity.py:18` 自声明 owner）。

**修复建议**：
1. stream 选择改为排除已有未释放 reservation 的 stream，例如先取 `reserved_streams = {row["stream_id"] for row in launches if row["role"] != "bootstrap" and row["reservation_released_at"] is None}`，再 `if row["stream_id"] not in reserved_streams`。
2. `_plan()` 把 `MembershipFenceError`（以及 `"scale-out stream is not available"`、`"dynamic launch request is already expired"` 等 planning-time 竞态）也归入"本轮放弃，记 telemetry 并返回 False"，只让真正的 invariant 违反（例如 `capacity observation is missing`）冒出来。
3. 在 `run_fenced_syncer` 里给 `capacity_service.tick(...)` 加与 `_admit_requests` 同级别的错误分层：`StaleLeaderTokenError`/`AuthoritySchemaError` 上抛，其余记 telemetry 后继续主循环——scheduler 是 best-effort 侧车，不应该有能力停掉 merge/publication。

**缺失测试**：`tests/runtime/test_dynamic_capacity_service.py` 的 `_runtime()` 固定 `max_pending_launch_requests=1`（`:62`），完全没有覆盖 pending>1。需要新增：`max_pending_launch_requests=2, streams>=2` 时连续两个低容量窗口必须产生**两个不同 stream** 的 reservation，且第三个窗口在无可用 stream 时安静返回而不是抛异常。

---

### High

#### H-1 launch-request 状态抖动被 command journal dedup 静默吞掉，健康 job 被误升级为 `manual_review` 并永久拒绝 admission

**文件/行**：`fs_diloco/runtime/services/dynamic_capacity.py:454-482`（`_transition` 的 `command_id = f"launch-{request_id}-{from}-{to}-{sha256(evidence+scheduler_state)[:12]}"`，不含 job id / 观察序号 / 时间）、`:350-391`（`submitted|started|terminal_uncertain` 分支）；dedup 语义在 `fs_diloco/storage/authority.py:5106-5116`（命中同 `command_id` + 同 request digest 时**返回缓存结果且不执行 operation**）；admission 侧 `authority.py:2559-2570`（`manual_review` 不在允许状态集内）。

**证据**（`qstat` 抖动 query_failed→running→query_failed→running）

```
after submit: submitted fake-1 None
step0 obs=query_failed -> state=terminal_uncertain deadline=105.2  actions=('terminal_uncertain',)
step1 obs=running      -> state=started            deadline=None   actions=('positive_evidence',)
step2 obs=query_failed -> state=terminal_uncertain deadline=107.4  actions=('terminal_uncertain',)
step3 obs=running      -> state=terminal_uncertain deadline=107.4  actions=('positive_evidence',)   <-- 库未更新
step4 obs=running      -> state=terminal_uncertain deadline=107.4  actions=('positive_evidence',)   <-- 永远卡死
```

step3 的 `(terminal_uncertain → started, evidence="live_qstat", scheduler_state="running")` 与 step1 完全同构，`command_id` 与 request digest 均相同 → `_command` 命中缓存，**telemetry 报告 `positive_evidence` 但 DB 行没有任何变化**。继续推进到 deadline 之后：

```
after deadline: manual_review ('manual_review',) released: None
admission REJECTED: MembershipFenceError dynamic launch authorization does not match
```

**失败场景**：dynamic scale-out job 在 PBS 队列里等待期间，`qstat` 出现两次瞬时失败（超时/服务端忙，`pbs_scheduler.py:105-125` 把 `OSError`/`TimeoutExpired`/非零 returncode 全部归为 `query_failed`，这是生产环境常见事件）。第二次恢复后 launch request 永久卡在 `terminal_uncertain`；`scheduler_uncertainty_timeout_seconds` 到期后升级 `manual_review`；此时那台 learner 真正启动并提交 registration，`admit_dynamic_incarnation` 因 `launch_row["state"] == "manual_review"` 抛 `MembershipFenceError` → 被 `syncer_v4.py:543-560` 归档成**永久 rejection**。stream reservation 的 `reservation_released_at` 始终为 NULL，占满 pending budget，scale-out 从此不可用，必须 operator 手工 CAS。

**影响的验收条件**：matrix `SCHED-02`/`SCHED-03`（anti-duplicate reservation 与 uncertainty 解析）、§10.5 回归；同时违反 `check_plan03.py:812-816` 自己声明的 `scheduler.positive-evidence-does-not-rearm-deadline` 契约的**意图**（SQL 层确实会清 deadline，但这条 SQL 因 dedup 根本没被执行）。

**修复建议**：让 `command_id` 对每次真实观察唯一而不是对"状态对+证据串"唯一，例如加入 `scheduler_observed_at`/单调 reconcile 序号/`pbs_job_id`：`f"launch-{request_id}-{row['state']}-{state}-{sha256(f'{evidence}|{scheduler_state}|{job_id}|{observation_seq}')[:12]}"`。更稳妥的做法是让 `transition_dynamic_launch_request` 在命中 dedup 时校验缓存结果里的 `state` 与库中当前 `state` 一致，不一致就抛 `CommandConflictError`，避免"命令声称成功但状态未变"的静默分叉。

**缺失测试**：新增 flapping 反例——`query_failed → running → query_failed → running` 后断言 `state == "started"` 且 `uncertainty_deadline is None`；再断言 deadline 到期时不会把仍有 positive evidence 的 request 升级为 `manual_review`。

---

#### H-2 `load_query_config_snapshot` 的 legacy projection 回归：仓库现存 P4-era v4 run snapshot 全部不可读

**文件/行**：`fs_diloco/legacy/config_v1_v3.py:20`（`_LEGACY_TOP_LEVEL_RUNTIME_KEYS` 移除 `coordination`/`maintenance`）、`:61-72`（`_project_legacy_payload` 只在 `coordination` **变空**时才 pop，`maintenance` 完全不再 pop）；消费方 `fs_diloco/tools/validation_eval.py:1`(import)/`:262`、`fs_diloco/tools/eval_lm_harness.py:213`、`fs_diloco/tools/publish_quality_gate.py`。

**证据**（用仓库里真实的 P4 run 数据）

```
$ .venv/bin/python -c "from fs_diloco.legacy.config_v1_v3 import load_query_config_snapshot; \
    load_query_config_snapshot('runs/fs_diloco/plan03_p4_dynamic_2509856/control/run_config.resolved.yaml')"
ValueError: config key coordination 字段已移除; it has no replacement; unknown config key: maintenance

$ .venv/bin/python -c "...同一个文件，用 d2dbfed 的 projection 规则..."
BASE projection OK -> synthetic-tiny
```

该 snapshot 的 top-level 键为
`['config_schema_version','coordination','data','failure_sim','inner_optimizer','io','learner','liveness','maintenance','membership','model','outer_optimizer','run','scaling','sync','syncer','terminal','torch_baseline','training','wandb']`，`coordination` 内为 `['leader','recovery_submission']`。

判定链条是对的：`failure_sim` 与 `coordination.recovery_submission` 让 `_has_legacy_runtime_keys()` 返回 True，snapshot **确实**有资格走 legacy projection。问题出在 projection 本身：`coordination` 去掉 `recovery_submission` 后还剩 `leader`（非空 → 不 pop），`maintenance` 从未被 pop，而 `Config`（shared schema）既没有 `coordination` 也没有 `maintenance` 字段 → `_from_dict` 直接失败。

**失败场景**：`python -m fs_diloco.tools.validation_eval --run-root runs/fs_diloco/plan03_p4_dynamic_2509856`、`python -m fs_diloco.eval_lm_harness export-checkpoint ...`、`publish_quality_gate` 对**任何 P4 阶段完成的 run**都以 `ValueError` 退出。这些正是 P4 的 tracked evidence run，P6 §11 的 acceptance/eval 阶梯需要对它们做离线评估。

**影响的验收条件**：matrix `LEGACY-01`（"完成的 run 可 query-only inspect/export/eval"，`legacy/reader.py:26` 自声明 owner）；§10.5「analysis/eval 完整回归通过」。

**修复建议**：把**降级资格判定**（要严）与**投影动作**（要全）解耦。保留收紧后的 `_has_legacy_runtime_keys()`，但在 `_project_legacy_payload()` 里无条件 `projected.pop("coordination", None)` 与 `projected.pop("maintenance", None)`（它们属于 ConfigV4 envelope，shared `Config` 永远装不下），并保留现有的嵌套 key 清理用于判定。这样既修好 base 的 M-1（未知拼写/非法 lease 不再静默降级，`tests/test_validation_eval.py:209-236` 继续绿），又不牺牲历史 v4 snapshot 的可读性。

**缺失测试**：新增一条以真实形状（`coordination.{leader,recovery_submission}` + `maintenance` + `failure_sim` + `sync.capture_terminal_predecessor_for_eval`）构造的 P4-era snapshot fixture，断言 `load_query_config_snapshot` **成功**并保留 model/data 字段；与现有的"非法 v4 snapshot 必须抛原始 strict_error"反例配对。

---

#### H-3 `run_plan03_phase4_dynamic_replacement.pbs` 的手工 launch request 在新 admission 规则下必然被拒；且不存在 operator 侧的替代路径

**文件/行**：`scripts/miyabi/run_plan03_phase4_dynamic_replacement.pbs:127-131`（`--launch-request-id "manual-replacement-${PBS_JOBID%%.*}" --stream-id 0 --replace-instance-id "$FIRST_INSTANCE"`）；新规则 `fs_diloco/storage/authority.py:2553-2578`（`launch_row is None → MembershipFenceError("dynamic launch authorization is missing")`，随后还要求 `launch_row["pbs_job_id"]` 与 registration 的 `pbs_job_id` 前缀一致）。

**证据**：该 request id 是脚本现场拼的字符串，`launch_requests` 表里不存在任何对应行；`plan_dynamic_launch_request` 只有 `DynamicCapacityService` 会调用，而 `fs_diloco/tools/` 下没有任何"创建 launch reservation"的 operator 工具（`resolve_scheduler_uncertainty.py` 只能对**已存在**的 request 做 expected-state CAS）。

```
$ grep -rn "launch-request-id" scripts/ fs_diloco/ tests/
scripts/miyabi/run_dynamic_learner.pbs:65            # 由 capacity service 注入，OK
scripts/miyabi/run_plan03_phase4_dynamic_replacement.pbs:129   # 手工伪造，将被拒
fs_diloco/tools/resolve_scheduler_uncertainty.py:67  # 只做 CAS，不创建 request
```

**失败场景**：重跑 P4 dynamic replacement 场景（§9.4 明确列为门禁行为之一，§11.13 要求对 target 做全 phase current-state 复核）时，替换 learner 的 admission 被永久 reject，脚本在 `Replacement learner exited before admission signal` 或 60 秒超时处失败。

**次生风险（同一脚本）**：该脚本用的 `configs/fs_diloco_tiny_ha_dynamic_2node.yaml` 现在 `scaling.enabled: true`、`scheduler_reconcile_interval_seconds: 1.0`、`cooldown_seconds: 1.0`、`max_total_launch_requests: 2`、`learner_queue: debug-g`。本 commit 之前 scaling 是惰性 config；之后 syncer 会真的在**计算节点内部**执行 `qsub scripts/miyabi/run_dynamic_learner.pbs`。脚本用 `kill -STOP` 冻结第一个 learner（`:120`），progress 停滞后 `productive` 归零 → 两个低容量窗口 → 真实提交最多 2 个 10 分钟的 debug-g 作业。提交前必须先决定这是期望行为还是应在测试 config 里关掉 scaling。

**修复建议**：二选一并显式记录——(a) 给该 PBS 增加一步"用 leader session 或新 operator 工具真正 plan+submit 一个 launch request（含 exact `pbs_job_id`）"，与 production 路径一致；或 (b) 承认 P4 手工 replacement 场景已被 capacity service 取代，把该脚本改成驱动 capacity service 的 replacement 路径，并同步更新 `docs/07-operations.md:17`（当前仍写"dynamic replacement 必须带 …… 明确 launch request ID"，却没说 operator 已无法自行产生它）。无论哪种，都应在 `progress.md` 记录该 P4 场景的 successor。

**缺失测试**：`tests/` 层已有 `test_dynamic_launch_authorization.py` 覆盖 authority 规则，但没有任何测试覆盖"repository-owned PBS 脚本的 learner 参数组合仍然可被 admission 接受"。建议加一条 meta 检查：扫描 `scripts/miyabi/*.pbs` 中 `fs_diloco.learner --launch-request-id` 的字面量，断言其来源只能是 `FS_DILOCO_LAUNCH_REQUEST_ID`。

---

#### H-4 phase evidence 仍绑定 pre-commit worktree，target commit 上 `--verify-phase-requirements P5` 依旧 BLOCKED

**文件/行**：`reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260809-191200_p5-review-remediation-precommit_pass.json:5-6`（`"source_base": "d2dbfed..."`、`"source_tree": "review-remediation worktree before incremental target commit"`，无 `source_commit`、无 `git_dirty`）；判定逻辑 `scripts/miyabi/check_plan03.py:737-788`；matrix 三行 status 仍为 `completion-candidate`。

**证据**

```
$ check_plan03.py --verify-phase-requirements P5-delete-classic-refactor
BLOCKED
diffs: ['requirements.P5-FRAGMENT.status', 'requirements.P5-FRAGMENT.artifact-contract',
        'requirements.P5-ARCH.status', 'requirements.P5-ARCH.structured-checker-evidence',
        'requirements.LEGACY-01.status', 'requirements.LEGACY-01.structured-checker-evidence',
        'requirements.P6-DOCS.status|artifact-contract|implementation|tests']
```

对比 base 的同一命令，`requirements.P5-*.implementation` 与 `.tests` 两类差异已经消失——上一轮 M-2 的 owner 绑定（`legacy/reader.py:26`、`legacy/fragment_v0.py:11`、`runtime/services/*.py`、对应 test 模块的 `PLAN03_REQUIREMENTS`）确实补齐了。剩下的是 status 生命周期与 evidence 绑定。

**失败场景**：按 §11.13 冻结 P5 时，`--verify-phase-requirements P5-delete-classic-refactor --require-tracked-evidence` 返回 BLOCKED；`20260809-191200_...json` 自己的 `remaining_gate` 字段与 `progress.md` 2026-08-09 19:12 条目都已承认"下一工作单元是创建 clean incremental review target、从 clean source 重跑同一门禁"。本 finding 只是把它记为**仍然阻塞 phase 关闭**，并叠加一条新事实：C-1/C-2/H-1/H-2 说明那次 precommit `605 passed` 并没有覆盖新 runtime service 的这些路径，因此重跑之前还需要先补齐 §2 中列出的缺失测试。

**修复建议**：先修 C-1/C-2/H-1/H-2/H-3 并补测试，再在 clean 的新 target 上重跑 `run_plan03_phase5_tests.pbs`，产出带 `source_commit=<新 target>` 与 `source_identity.git_dirty=false` 的 evidence，同时把 matrix 三行 status 与 `artifact_contract`（`P5-FRAGMENT` 目前是 `artifacts/<ts>_p5-fragment-archive_review.json` 而非 `checker requirements.P5-FRAGMENT`）一起收敛。

**缺失测试**：`tests/test_plan03_checker.py` 已有 P3 的 requirement-checker 测试，仍缺同构的 `test_plan03_p5_requirement_checker_binds_implementation_tests_and_evidence`。

---

### Medium

#### M-1 `MergeFenceConflict` 会让 bounded terminal merge 循环提前 `break`，剩余 final proposal 被静默丢弃

**文件/行**：`fs_diloco/runtime/services/merge.py:141-154`（冲突时 `return None`）；`fs_diloco/runtime/services/terminal.py:160-168`（`for _ in range(max_terminal_merges): ... if committed is None: break`）。

base 的 inline 实现在冲突时是 `continue`（重载 outer state 后立刻重试）。现在冲突与"没有可选 batch"被压成同一个 `None` 返回值，terminal 路径无法区分二者：一次 fence conflict 就会消耗掉整轮 bounded merge 并 `break`，其余已声明的 final update 只能走 `finalize_terminal` 的 `terminal_final_update_not_selected` 丢弃路径。正常主循环里同样退化——冲突后不再立刻重试，而是 `time.sleep(poll_seconds)`（`syncer_v4.py:239-243`）。

token ledger 本身仍守恒（我实测 `direct_dropped` 会精确记账，balance=0），所以这不是 bounded-state 破损，但 `terminal.max_terminal_merges > 1` 的配置语义没有兑现。

**修复建议**：`merge_once` 返回三态（`committed` / `NO_BATCH` / `FENCE_CONFLICT`），terminal 循环只在 `NO_BATCH` 时 `break`，冲突时继续消耗预算；主循环冲突时不 sleep。

**缺失测试**：`tests/runtime/test_terminal_service.py` 用 `MergeProbe` 桩替换了 `MergeService`（`:39-46`），`MergeService` 本身**没有任何直接单测**。需要 (a) `MergeService.merge_once` 的 fence-conflict 单测；(b) terminal 路径在首次冲突后仍能完成第二次 merge 的测试。

---

#### M-2 scheduler operator request 永不归档，每个 tick 全量重放

**文件/行**：`fs_diloco/runtime/services/dynamic_capacity.py:393-415`（`for path in sorted(root.glob("*.json"))`，处理后既不删除也不移动、不做 quarantine）；`fs_diloco/storage/paths.py:79-80`。

`apply_scheduler_operator_request` 用 `command_id=f"apply-{sha256}"` 保证幂等，但 `_command`（`authority.py:5103,5115`）仍会为每次重放执行 `BEGIN IMMEDIATE` + `commit()`，即每个已处理的 operator request 在每个 tick 都要抢一次 SQLite 写锁；`actions.append(f"operator_{...}")` 也会每轮写一条 telemetry。`invalid_operator_request`（symlink 或坏 JSON，`:399-409`）同样无限重复且不隔离。`_apply_operator_requests()` 位于 `tick()` 的 reconcile-interval 早退**之前**（`:69`），所以频率是主循环频率而非 reconcile 频率——acceptance config 的 `sync.scan_interval_seconds` 只有 0.1s。

**修复建议**：处理成功后把请求 publish 到 `scheduler_operator_requests/applied/`（或写一条 create-no-replace 的 disposition 并跳过已有 disposition 的文件），与 admission 的 `registration_dispositions_v4` 模式保持一致；无效请求移入 quarantine 目录只报一次。

**缺失测试**：断言同一个 operator request 连续两次 `tick()` 只产生一次 `operator_applied` action；断言 symlink/坏 JSON 只被报告一次并被隔离。

---

#### M-3 `capacity_observations.selected_contributors` 在生产路径恒为 0

**文件/行**：`fs_diloco/runtime/syncer_v4.py:226-230`（`selected_contributors=0` 硬编码）；schema 约束 `fs_diloco/storage/schema_v4_dynamic.sql:126`；写入 `fs_diloco/storage/authority.py:1939,1996`。

这是一个持久化到 authority 的具名 evidence 列，acceptance 分析会读它。恒 0 会让"选择宽度 vs 容量"这类判断得到错误结论。`eligible_contributors` 传的是真实值，两列语义不一致更容易误导。

**修复建议**：要么把上一轮 `merge_once` 的 `len(batch.candidates)` 缓存到 `MergeService` 并传进来，要么删除该列并同步 schema/文档，不要保留一个恒定占位值。

**缺失测试**：断言 `tick()` 之后 `capacity_observations[-1]["selected_contributors"]` 等于上一轮实际 selection 宽度。

---

#### M-4 boundary 门禁被永久放宽：`bound_mutators` 期望硬编码为空、`baselines/protocol.py` 退出字节冻结

**文件/行**：`scripts/miyabi/check_plan03.py:439-450`（`expected_counts.update(bound_mutators=0, ...)`、`("inventory.bound_mutators", actual[...], [])`）、`:40`+`:326-336`（`P5_BASELINE_PROTOCOL_MIGRATION` 从 baseline package manifest 中排除）；测试语义翻转 `tests/test_plan03_checker.py:104-118`（原 `assert verify_boundaries(protocol_drift, expected) == ["boundary_manifest_sha256"]` → 现 `== []`）。

`bound_mutators` 现在实测为空（其源文件在 base 已删除），把期望写死成 `[]` 是当前正确值，但把"与 frozen 集合比对"退化成了"必须为空"，未来重新引入 bound mutator 时只能靠新增检测。`fs_diloco/baselines/protocol.py` 被永久移出 §9 的 torch-baseline 字节冻结面——base 里把 `maybe_autocast` 迁到 `modeling/training.py` 是一次性事件，却换来了该文件此后任意漂移都不再报警。新加的 `retained_boundary = fs_diloco/baselines/artifacts.py` 反例（`:113-118`）只证明*别的*文件仍被守护。

我另外验证了删除类漂移仍能被捕获（把 `fs_diloco/baselines/artifacts.py` 从 inventory 里摘掉 → `['boundary_manifest_sha256']`），所以 `_boundary_manifest` 新增的 `if path in payload["manifest_sha256"]` 过滤没有制造漏洞。

**修复建议**：把 `P5_BASELINE_PROTOCOL_MIGRATION` 改成"只允许一次已审阅的 sha 迁移"（记录 P5 后的新 sha 作为期望值），而不是整文件豁免；`bound_mutators` 的期望改为从 frozen payload 派生的"P5 后应为空"投影并附注释，与 `fragment_enabled_configs` 的处理方式一致。

**缺失测试**：`assert verify_boundaries(<protocol.py 漂移>, expected) == ["boundary_manifest_sha256"]`（在给定期望新 sha 之后）。

---

#### M-5 新 runtime service 的关键策略分支缺测试

**文件/行**：`fs_diloco/runtime/services/terminal.py:58-75`（`launch_budget_exhausted`）、`:152-159`（`_all_declared_final_updates_visible` 超时路径）、`fs_diloco/runtime/services/merge.py` 全文。

- `admission_close_policy: global_target_or_launch_budget` 是 `TerminalSection` 的**默认值**，也是仓库两个 dynamic config 的实际取值，但 `terminal_close_reason` 的 launch-budget 分支没有任何测试（`tests/runtime/test_terminal_service.py:150-182` 只覆盖 global_target / deadline / manual）。
- `proposal_visibility_grace_seconds` 超时后仍有 declared-but-invisible final update 时的行为没有测试；我实测该路径下 `finalize_terminal` 会把 tokens 记为 `direct_dropped` 且 balance 守恒（不是 bug），但缺少断言把这条语义钉住。
- `MergeService` 无直接单测（见 M-1）。
- `admit_dynamic_incarnation` 的 bootstrap-slot 一次性消费（`authority.py:2580-2588`）只在 authority 层有覆盖，没有经由 `_admit_requests` 的 syncer 级测试。

**修复建议**：按上述四点补测试；`launch_budget_exhausted` 至少要覆盖"budget 用尽但仍有未释放 reservation → 不关闭"与"全部释放且容量仍低 → 关闭"两个方向。

---

### Low

#### L-1 `MergeService` 的 `command_id` 含 `uuid4`，command journal 不再可重放/可复现

`fs_diloco/runtime/services/merge.py:57-64`：`f"select-{purpose}-e{epoch}-n{sequence}-{uuid.uuid4().hex[:12]}"`。command journal 的设计目的就是"同一逻辑命令重放时 dedup"，随机后缀让这条性质失效，也让 audit 重建不可复现。`purpose` + 单调 `sequence` + epoch 已经足够唯一，建议去掉 uuid；如果担心同 epoch 内进程重启导致序号复用，改用从 DB 读回的 `MAX(selection_seq)+1` 而不是随机数。

#### L-2 pre-close admission grace 静默跳过 `created_at` 非法的 request

`fs_diloco/runtime/syncer_v4.py:322-331`：`created_at` 缺失/为 bool/非数值时直接 `continue`，既不发 telemetry 也不发 rejection，与同一函数上方 `admission_request_deferred`（`:315-321`）的处理风格不一致。建议至少记一条 `admission_request_deferred` 事件。

#### L-3 manual close request 的失败路径对 operator 不友好

`fs_diloco/storage/terminal_request.py:70-92`：run_id/descriptor_sha256 不匹配、字段多余、request_id 不自洽时一律返回 `None`，syncer 侧不产生任何 telemetry——operator 发了 close request 却看不到"为什么被忽略"。`fs_diloco/tools/request_terminal_close.py:26-31` 也没有像 `tools/authorize_static_replacement.py:40-55` 那样把 `publish_immutable_bytes` 的 `FileExistsError` 转成 `parser.error(...)`，第二次不同内容的请求会以裸 traceback 结束。建议 reader 侧发 `manual_terminal_request_rejected` telemetry，tool 侧给出可操作的错误文案。

#### L-4 query 工具现在硬依赖可打开的 authority DB，且重复打开

`fs_diloco/tools/eval_lm_harness.py:163-178` 的 `validate_query_manifest_output` 每次都 `query_run_protocol(source_run_root)`，而 `open_query_only_database`（`fs_diloco/legacy/reader.py:32-33`）用 `resolve(strict=True)`。结果：(a) 从没有 `control/syncer_metadata.sqlite3` 的归档/拷贝 checkpoint 目录导出模型，现在会以 `FileNotFoundError` 失败，而 base 不需要 DB；(b) `export_checkpoint` 在一次调用里会打开 DB 两次并比对分类结果（`:212-249`）。建议把分类结果在 manifest 生命周期内缓存一次，并对"无 authority DB"给出显式的 `unclassified` 语义而不是崩溃。

（同一处的 URI 转义与 `timeout=60.0` 修复是对的，`tests/legacy/test_legacy_v1_v3_reader.py:148-155` 用含 `#`/`?` 的路径覆盖，符合上一轮 L-2 的建议。）

#### L-5 文档与实际能力的残余漂移

- `docs/07-operations.md:17` 仍写"dynamic replacement 必须带 current instance ID、stream ID 和明确 launch request ID"，但 operator 已无工具生成 launch request ID（见 H-3）。
- `docs/06-configuration.md:63` / `docs/08` 记录了 authority schema 7 无 6→7 in-place migration，这点是对的；但 `runs/fs_diloco/` 下仍存有大量 schema 6 的 P4 run，配合 H-2 会让"保持原提交 evidence"实际上变成"用当前代码完全读不了"。建议在 docs/08 明确：schema 6 run 的 query-only eval 需要 checkout 对应 commit。

---

## 3. 未发现问题的检查项

以下项目经检查未发现 finding，记录以界定审查边界：

- **上一轮 Critical/High 的修复有效性**：`tests/test_plan03_checker.py:86-96` 改为合成 `fragments:\n  enabled: true\n` drift，不再读被删除的 tracked config；`pytest tests/test_plan03_checker.py` 在 target 上全绿。`verify_p3_operational_contracts()` 重定位到 `storage/authority.py` + `runtime/services/dynamic_capacity.py` + `schema_v4_dynamic.sql` 并新增 `scheduler.deleted-candidate-outbox-remains` 反向断言；`verify_boundaries()` / `verify_p4_migration_contracts()` 增加 P5 投影后 differences=[]；三者都有 current-tree 断言（`tests/test_plan03_checker.py:120-125,140-144`）。`run_plan03_phase5_tests.pbs:73-79` 已把 `--verify-boundaries --verify-p3-operational-contracts` 纳入门禁，`:20` 的 TEST_TARGET 也加入了 `tests/test_plan03_checker.py`。
- **上一轮 M-2..M-5 / L-1..L-5 的处置**：P5 三条 requirement 的 `implementation`/`tests` owner 已补齐（checker 差异消失）；dead surface 全部落定（`candidate_launch_outbox` DDL 与两个 mutator 删除、`phase1_performance.py` 删除、`validate_global_adoption_strategy` 删除、`pbs_scheduler.py`/`FakePBS` 现被 `dynamic_capacity` 与三个新测试消费）；adoption 校验去重到 `core/adoption_rules.py`（torch-free，`core/config.py:16,741` 与 `runtime/adoption.py:23,385,477` 共用）；`_tracked` 改名 `_repository_files` 并补 docstring；`observability/__init__.py` docstring 与 `docs/modules/observability.md` 同步；DDL 检查改为 `sqlite3` 内存库读 `sqlite_master`，config 检查补齐 `failure_sim`/`sync.stop_after_global_tokens`/`capture_terminal_predecessor_for_eval`；`runtime/services/` 按 §10.3 落地并在 `docs/05-code-structure.md:15-18` 如实描述。我做的全仓 AST dead-module 扫描只剩 `python -m` shim。
- **legacy 降级判据收紧的正向效果**：`tests/test_validation_eval.py:209-236` 的两条反例（未知 `coordination.leader` 子键、`lease_duration_seconds=0`）确认 `load_query_config_snapshot` 与 `load_resolved_config_snapshot` 抛出**同一个** `ValueError`，base 的 M-1 静默降级已消除。合法 v4 snapshot 仍由 `core/config.py:431-436` 的 v4 envelope 路由正常加载（回归见 H-2，只影响 P4-era 含 removed-key 的历史 snapshot）。
- **terminal ack 早于 proposal 可见性**：`authority.py:4064-4068` 把"declared final update 尚未 ingest"从硬拒改成放行，看似是 bounded-state 风险，我实测 token ledger 仍精确守恒：receipt 后 `direct_outstanding=6`，finalize 后 `direct_dropped=6`、`direct_outstanding=0`、`hard_crash_gap_tokens_upper_bound=0`，`adjudicated_processed = local_discarded + direct_dropped` 成立。final update 与 receipt 的 `planned_update_id` 仍强绑（`:4054-4057`），因此不构成 TOK-05 破损，反而比旧的 hard-crash 上界更精确。正向测试见 `tests/storage/test_authority_p3_operational.py:1046-1091`。
- **dynamic admission 的凭据收紧**：`admission.py:296-311` 的"bootstrap_slot 与 launch_request_id 恰好二选一"、`bootstrap_slot == stream_id`、bootstrap 不得带 `replace_instance_id`；`authority.py:2536-2588` 的 replay identity 全量比对（hostname/pid/pbs_job_id 前缀）、launch authorization 的 stream/replacement/job-id exact match、bootstrap slot 一次性消费。`fs_diloco/tools/launch_independent_run.py:122` 的 `BOOTSTRAP_SLOT={slot}` 与 `scripts/miyabi/run_dynamic_learner.pbs:63-78` 的三分支授权解析一致；PBS 变量注入经 `_pbs_variable_value`（`pbs_scheduler.py:22-26`）过滤 `,=\n\r`，queue 名走字符白名单。`validate_identity` 允许 `.`/`-`，普通 PBS job id（`NNNN.host`）合法；dynamic bootstrap 走 `BOOTSTRAP_SLOT` 单作业而非 job array，因此不会遇到 `1234[0].host` 这类非法 identity。
- **replacement 的证据门槛**：`_confirmed_lost_instance`（`dynamic_capacity.py:180-200`）要求 progress 超过 `heartbeat_dead_after_seconds` **且** live 或 historical `qstat` 为 `finished`；`plan_dynamic_launch_request`（`authority.py:2129-2145`）要求 `stream.current_instance_id == replace_instance_id`、instance 仍 `admitted`、`expected_scheduler_job_id` 与 `instance.pbs_job_id` 完全一致，并在同一事务内 retire 旧 incarnation。`suspended`/`unknown` 不授权替换，有反例覆盖（`tests/runtime/test_dynamic_capacity_service.py:340-369`）。
- **scaling 配置的交叉校验**：`core/config.py:638-703` 已覆盖 `max_pending <= max_total`、`launch_request_ttl >= 2×reconcile`、`uncertainty_timeout >= 3×reconcile`、`low_threshold < desired`、`consecutive_low_windows >= 2`、`retention >= consecutive_low + productive_window`、`productive_upload_grace_min <= max`，因此不存在"retention 太小导致 scale-out 永不触发"的静默死区。新增的 `walltime >= 00:10:00`（`:683`）与仓库 `AGENTS.md` 第 3 条一致，并有反例测试（`tests/test_config.py:220-229`）；两个 dynamic config 的对应改动被 `verify_p4_migration_contracts` 的 `p5_dynamic_walltime_updates` 精确投影，其他 scaling 字段漂移仍 BLOCKED（`tests/test_plan03_checker.py:171-190`）。
- **schema 6→7**：`versions.py:8`、`schema_v4.sql:4` CHECK、`tests/storage/test_schema_v4.py:41-42` 三处同步；`scheduler_operator_requests` 从 base schema 移到 dynamic feature schema（static 不再创建假的 scheduler 表），`launch_requests` 增加 `stream_id NOT NULL REFERENCES streams` 与 `replace_instance_id REFERENCES learner_instances`；`docs/06`/`docs/08`/`docs/modules/storage.md` 均已声明无 in-place migration。
- **legacy query-only 不变量**：`open_query_only_database` 保持 `mode=ro` + `PRAGMA query_only=ON` 回读校验，新增 URI quote 与 `timeout=60.0`；`validate_query_output_path` 统一了 export/manifest/CSV/validation 四类输出的 outside-root 策略，`validation_eval` 对 legacy 源强制显式 `--output` 且不再写回 `summary.json`；反例覆盖 `tests/legacy/test_legacy_v1_v3_reader.py:177-226`。
- **静态门禁**：`compileall`、`ruff check`（全绿）、`bash -n`、`git diff --check` 全部 PASS；`run_plan03_phase5_tests.pbs` 保持 literal group ID `xg24i002`、`walltime=00:10:00`、`CUDA_VISIBLE_DEVICES=""`；format scope 与新增/修改文件同步扩充（含 `fs_diloco/runtime/services`、`storage/terminal_request.py`、`tools/request_terminal_close.py` 及 6 个新测试）。
- **架构边界**：`runtime/services/*` 只依赖 `core`/`protocol`/`storage`/`runtime`，未 import `legacy`；`protocol/` 仍无 `pathlib`/`storage`/`runtime` 依赖；entrypoint 不拼 SQL、不直接调 qsub/qstat（qsub/qstat 只在 `runtime/pbs_scheduler.py` 这一个窄 adapter 内）；`cli.py` 新增的 `close` 子命令为惰性 import。`tests/architecture` 全绿。
- **删除面**：`--verify-p5-contracts` differences=[]，`tests/architecture/test_p5_removed_runtime.py` 全绿；本 commit 未复活任何 classic/fragment surface。

---

## 4. 建议的修复顺序

1. **C-1**：把 manual close 的 `command_id` 与自由文本 reason 解耦，补端到端 finalize 测试。
2. **C-2**：stream 选择排除已有 reservation + `_plan()` 吞掉 planning-time fence 竞态 + `tick()` 在 syncer 主循环中做错误分层。
3. **H-1**：让 `_transition` 的 `command_id` 对每次观察唯一（或在 dedup 命中时校验状态一致性），补 flapping 反例。
4. **H-2**：`_project_legacy_payload` 无条件剥离 `coordination`/`maintenance`，补 P4-era snapshot fixture。
5. **H-3**：决定 P4 dynamic replacement 场景的 successor 并更新 PBS 与 `docs/07`；同时确认该脚本在 scaling 变活后是否应关闭 scaling。
6. **M-1..M-5**：三态 merge 结果、operator request 归档、`selected_contributors`、boundary 门禁的精确化、缺失测试补齐。
7. **H-4**：以上完成后在 clean 的新 target 上重跑 `run_plan03_phase5_tests.pbs`，产出带 `source_commit`/`git_dirty=false` 的 evidence，并收敛 matrix status 与 `artifact_contract`。
8. **L-1..L-5**：可与上述任一轮合并。

---

## 5. 最终判定

# CHANGES_REQUIRED

阻塞项：**C-1**（manual terminal close 100% 打死 syncer）、**C-2**（默认 pending budget 下第二个 scale-out reservation 打死 leader，crash loop）、**H-1**（qstat 抖动把健康 job 误升级 manual_review 并永久拒绝其 admission）、**H-2**（仓库现存 P4-era v4 run 的 query-only eval 全部失效）。

上一轮的 Critical/High（测试套件红、三条 Checker 门禁硬损坏）以及全部 Medium/Low 都已得到实质修复，删除面、架构边界、legacy query-only 语义与静态门禁在 target 上均为绿。但本 commit 首次把 dynamic capacity/scheduler 与 manual terminal close 变成活的 production 路径，这两块新代码引入了四条可复现的运行时缺陷，其中两条会直接终止 leader syncer。在 C-1/C-2/H-1/H-2 修复、H-3 的 P4 场景 successor 明确、并在 clean review target 上重跑完整门禁产出 source-bound evidence 之前，`P5-delete-classic-refactor` 不应按 §10.5 / §11.13 关闭。
