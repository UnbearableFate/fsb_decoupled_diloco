# Plan 03 `P3-operational-robustness` 增量代码审查（claude-opus-5）

- **Base commit ID**：`225db163ee5bbfbf16bba3d59e06c4fbd6d789f8`（`plan03: complete P2 correctness measurement`，P2 phase-final commit）
- **Target commit ID**：`de3d27879fdef188afa03a233acd4b40d90e5feb`（`plan03: implement P3 operational robustness`）
- **审查范围**：`git diff 225db163ee5bbfbf16bba3d59e06c4fbd6d789f8 de3d27879fdef188afa03a233acd4b40d90e5feb`，共 **72 个文件、+7401 / −213 行**
- **Base 是 Target 的 ancestor**：是（`git merge-base --is-ancestor` 通过；`base` 即 `target^`）
- **工作树状态**：`M plans/AGENTS.md`（**不属于 target，明确排除在审查范围外**）；`?? reports/DOING/code_review/.../P3-operational-robustness/`（本报告目录）。`fs_diloco/`、`tests/`、`scripts/`、`plans/DOING/plans/`、`reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/` 与 target 完全一致（`git diff --stat HEAD -- fs_diloco tests scripts` 为空）
- **Reviewer 角色**：只读。除本报告外未修改任何文件、未变更 Git 状态、未 qsub/qdel、未删除任何 run 数据、未 commit/push/PR。本报告不含任何 secret / token / 凭据 / 完整环境变量
- **审查模型 / 日期**：claude-opus-5 / 2026-08-09

---

## 1. 检查范围

### 1.1 逐文件覆盖

| 类别 | 文件 | 状态 |
|---|---|---|
| 协议（新增） | `fs_diloco/protocol/{data_cursor,scheduler,selection,token_accounting}.py` | 逐行审查 |
| 协议（修改） | `fs_diloco/protocol/{__init__,authority,merge}.py` | 逐行审查 |
| 存储（新增） | `fs_diloco/storage/{artifact_policy,audit_archive,run_initializer}.py` | 逐行审查 |
| 存储（修改） | `fs_diloco/storage/{authority.py(+1621),fenced_store,leader_lease,paths,atomic_io,schema_bootstrap}.py` | 逐行审查 |
| Schema | `fs_diloco/storage/schema_v4.sql`、`schema_v4_dynamic.sql` | 逐行审查 |
| Core | `fs_diloco/core/{config,config_v4,run_descriptor}.py` | 逐行审查 |
| Runtime | `fs_diloco/runtime/launch_outbox.py` | 逐行审查 |
| Observability | `fs_diloco/observability/logging_utils.py` | 逐行审查 |
| Tools | `fs_diloco/tools/{check_workload_equivalence,clean_run,init_run,resolve_scheduler_uncertainty}.py` | 逐行审查 |
| Checker | `scripts/miyabi/check_plan03.py`（+99/−12） | 逐行审查 |
| PBS | `scripts/miyabi/run_plan03_phase3_tests.pbs`（新增 99 行） | 逐行审查，`bash -n` 通过 |
| 新增测试 | `tests/{observability,protocol,storage,tools}/…_p3_*.py`（7 个文件，共 +1997 行） | 逐行审查 |
| 修改测试 | `tests/{test_plan02_phase1_ha,test_plan02_phase2_dynamic,test_plan03_p0_red,test_plan03_checker,test_clean_run,storage/test_schema_v4}.py` | 逐行审查 |
| Fixture | `tests/fixtures/golden/unified_v4_trace.json`（新增） | 逐行审查并与 P0 fixture 交叉核对 |
| 需求矩阵 | `plans/DOING/plans/..._unified_ha-requirement-matrix.csv`（40 条 P3 行） | 全部解析核对 |
| 报告/证据 | `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/{progress,failures,code_review}.md` + 24 个 artifact | 全部通读 |

### 1.2 只读验证动作

- `git diff --stat` / `git show <target>:<path>` 逐文件读取 target 内容；`git merge-base --is-ancestor` 确认 ancestry。
- `bash -n scripts/miyabi/run_plan03_phase3_tests.pbs` 通过；PBS 头部 `group_list=xg24i002` 为字面量、`walltime=00:10:00` 满足 `AGENTS.md` §PBS 第 3 条最短 10 分钟规则。
- 用 `csv.DictReader` 解析需求矩阵：P3 共 **40 条**、`invariant_id` 无重复、全部 `status=complete`；与 `check_plan03.py --verify-phase-requirements` 的判定口径逐条对照。
- 静态交叉引用：`grep` 确认 `TrainingSegmentAccumulator` / `IndexedBlockCursor` / `PersistentFairSelector` / `ActorTelemetryWriter` / `write_actor_attestation` / `actor_metrics_path` 在 `fs_diloco/runtime/**` 内**零调用点**；确认 `AUTHORITY_SCHEMA_VERSION` 在 base 与 target 均为 `4`。
- **动态复现（本机 CPU，`/tmp/p3repro` 隔离目录，未写入仓库、未占用 GPU、未 qsub）**：
  - 复现 F-01（run root 发布后 `load_run_descriptor` 失败）；
  - 复现 F-02（uncertainty deadline 永不到期）；
  - 复现 F-04（`finalize_terminal` 永久阻塞）；
  - 测量 F-05（启动扫描随 audit 对象线性增长：13.8 ms → 98.5 ms @ 5000 objects）。
- 回归确认：`.venv/bin/python -m pytest -q tests/protocol tests/storage tests/observability tests/tools tests/test_plan03_p0_red.py tests/test_clean_run.py tests/test_plan03_checker.py` → **235 passed in 33.23s**（与 artifact `20260809-052846_..._pass.json` 记录的 focused 285 / full 794 口径一致，本次只跑了其中的 CPU 子集）。
- **合规声明**：本次在登录节点 `miyabi-g1` 上执行了约 40 秒纯 CPU 的 pytest 与 4 段秒级 Python 复现脚本（无 torch 训练、无 GPU 分配、无 MPI）。未提交任何 PBS 作业。

### 1.3 明确不在本次范围

- `plans/AGENTS.md` 的未提交工作树修改。
- P4/P5/P6 的 runtime cutover、config 迁移与正式 9-node 性能测量（计划已显式后置）。
- base commit 之前已存在的实现（仅在与本次增量产生交互时评估）。

---

## 2. Findings

严重度定义：**Critical** = 使正常运行路径不可用或破坏持久化正确性；**High** = 违反本 phase 明确 Gate 条款或造成运行期不可恢复/放大故障；**Medium** = 不变量与实现/门禁不一致、回归检测能力下降；**Low** = 契约不清、死代码、可运维性缺口。

---

### Critical

#### F-01（Critical，correctness / 回归）`load_run_descriptor` 在 run 发布后必然失败：initializer manifest 的 `mutable_prefixes` 没有覆盖 runtime 写入的 `control/*.json`

**证据**

`fs_diloco/core/run_descriptor.py:139` 在每次加载 descriptor 时新增了全量自检：

```python
    validate_completed_run(paths.shared_root)
```

`fs_diloco/storage/run_initializer.py:267-290`（`_validate_completed_run`，`strict_initial=False` 分支）把 run root 下**任何**不在 `.complete` manifest、也不在 `mutable_prefixes` 白名单内的条目判为致命错误：

```python
    for path in final_root.rglob("*"):
        ...
        extras = (actual_files - expected_files) | (actual_dirs - expected_dirs)
        prefixes = tuple(str(item).rstrip("/") for item in manifest["mutable_prefixes"])
        forbidden = sorted(... if not any(item == prefix or item.startswith(prefix + "/") ...))
        if forbidden:
            raise RuntimeError(f"run root contains protocol-external entries: {forbidden}")
```

而 `fs_diloco/storage/run_initializer.py:101-114` 的白名单只有：

```python
        "mutable_prefixes": [
            "audit", "control/scheduler_operator_requests", "control/syncer_epochs",
            "control/syncer_launch_claims", "control/registration_requests",
            "eval_checkpoints", "heartbeats", "logs", "metrics", "optim", "updates", "weights",
        ],
```

`control/` 下由 runtime 产生的授权/缓存文件全部缺席，而它们确实会被写：
`fs_diloco/runtime/syncer.py:586, 717`（`paths.latest_json`）、`:770, 835`（`paths.param_index_json`）、`:1006-1007`（`stop.json` / `summary.json`）。
`fs_diloco/tools/request_dynamic_close.py` 写的 `control/dynamic_close_request.json` 同样不在白名单。
`atomic_write_bytes`（`fs_diloco/storage/atomic_io.py:48`）在 `control/` 内建立的 `.<name>.*.tmp` 临时文件、以及 `journal_mode=DELETE` 下事务期间的 `control/syncer_metadata.sqlite3-journal`，也会命中同一条 `forbidden` 判定（后者是竞态型偶发失败）。

**失败场景（已复现）**

```
descriptor 加载 OK（新 run）
syncer 发布 control/latest.json
→ load_run_descriptor(root)
  RuntimeError: run root contains protocol-external entries: ['control/latest.json']
```

调用点为生产入口：`fs_diloco/runtime/learner.py:2260`、`fs_diloco/runtime/syncer.py:2905`。因此**只要 syncer 完成第一次发布，之后任何 learner 重启、syncer takeover、以及分析工具（`phase1_matched_performance.py:390`、`phase2_chaos_evidence.py:83`、`phase2_matched_evidence.py:42-43`）都会硬失败**。`fs_diloco/tools/init_run.py:63` 的 `.complete` 恢复路径同样会因此失效，使幂等 re-init 在 run 跑过之后不可用。

**修复建议**

1. 把 `control/latest.json`、`control/stop.json`、`control/summary.json`、`control/param_index.json`、`control/dynamic_close_request.json` 以及 SQLite sidecar/临时文件模式纳入 manifest 的 mutable 白名单（建议改为“显式 mutable 路径集合 + 显式 temp 模式集合”，并与 `artifact_policy.build_artifact_policy()` 的 `cache` / `temporary` 分类保持单一真源，避免两处白名单二次分叉）。
2. 把 `load_run_descriptor` 的默认校验降级为 identity/manifest 自检（`.complete` + `.identity` + descriptor/config/source hash），把“协议外条目穷举”留给 `strict_initial=True` 的发布时自检与显式 `verify-run` 工具（另见 F-05）。

**缺失测试**

- 缺“初始化 → 写入 `control/latest.json` / `param_index.json` / `stop.json` / `summary.json` → `load_run_descriptor` 仍成功”的用例。
- 缺“authority 事务进行中（存在 `-journal`）时并发 `load_run_descriptor` 不失败”的用例。
- `tests/storage/test_run_initializer_p3.py` 全部用例都只在**刚发布、零 runtime 写入**的 run root 上断言，正是这一盲区导致缺陷未被发现。

---

### High

#### F-02（High，correctness / plan Gate 未达成）scheduler uncertainty deadline 每次 reconcile 都被刷新，`manual_review` 在真实时钟下不可达

**证据**

`fs_diloco/runtime/launch_outbox.py:442-476`：每一轮对同一 uncertain 分类都会先读 deadline 再**重写**一个新的 deadline：

```python
                    deadline = request.get("uncertainty_deadline")
                    timeout = float(getattr(self.config, "scheduler_uncertainty_timeout_seconds", ...))
                    if deadline is not None and now >= float(deadline):
                        ... state="manual_review" ...; continue
                    results.append(store.update_launch_request(..., state="terminal_uncertain",
                            first_uncertain_at=now, uncertainty_deadline=now + timeout, ...))
```

`fs_diloco/storage/fenced_store.py:2508` 让**新值获胜**：

```python
                    uncertainty_deadline=COALESCE(?, uncertainty_deadline),
```

（对照同一条 UPDATE 的 `first_uncertain_at=COALESCE(first_uncertain_at, ?)` 是“旧值获胜”，两者语义相反，说明 deadline 这一侧是笔误而非设计。）

`fs_diloco/core/config.py:78-85` 的 validator 又强制 `scheduler_uncertainty_timeout_seconds >= 3 × scheduler_reconcile_interval_seconds`，因此 `now + timeout` 恒大于下一次 reconcile 的时刻——deadline **在结构上**永远追不上。

**失败场景（已复现）**

以 `interval=10s`、`timeout=300s`、时钟随 reconcile 前进模拟 200 轮（2000 s）：

```
first: (1010.0, 'terminal_uncertain', 1310.0)
last : (3000.0, 'terminal_uncertain', 3300.0)
→ 全程停留在 terminal_uncertain，manual_review 不可达
```

现有用例 `tests/test_plan02_phase2_dynamic.py:1219-1243` 之所以通过，是因为它在两次 `outbox.reconcile(store)` 之间**冻结了 wall clock**（`now[0]` 不变），deadline 恰好没被推后；把时钟按 reconcile 间隔推进后立即失效。

**影响**：直接违反计划 §8.8 Gate “scheduler duplicate admission=0，uncertainty 在 deadline 内有明确状态”与需求 `SCHED-04`（矩阵中已标记 `complete`）。同时 `reservation_released_at` 永远为 NULL，request 永久驻留于 `active_only` 集合。

**修复建议**

- `uncertainty_deadline` 改为 `COALESCE(uncertainty_deadline, ?)`（首次进入 uncertainty 时锚定，之后只由 positive evidence 显式清除）；
- 或在 `launch_outbox` 侧只在 `state != "terminal_uncertain"` 时传 `uncertainty_deadline`，已在 uncertain 态时只更新 `scheduler_state` / `last_error`。

**缺失测试**

- 缺“时钟按 reconcile 间隔单调前进、经过 `timeout` 后必须进入 `manual_review`”的用例（现有用例必须改为推进时钟，否则它反而把缺陷固化成了预期行为）。
- 缺 `submitting/submission_unknown` 无 job_id 分支（`launch_outbox.py:517-541`）落入 `terminal_uncertain` 后**再也不会被重新评估** deadline 的用例——该分支后续只能靠 `expires_at` 兜底，`uncertainty_deadline` 在那条路径上是纯粹的死字段。

---

#### F-03（High，并发/资源不变量）新增的 `terminal_uncertain` 未加入 reservation/budget 状态集合，scaler 会为同一缺口重复下发 launch request

**证据**

新状态被加进了两处“活跃集合”：`fs_diloco/storage/fenced_store.py:343`（`FencedSQLiteStore.launch_requests(active_only=True)`）与 `:3160`（`ReadOnlySQLiteStore` 同名方法），以及 `schema_v4_dynamic.sql` / `schema_v4.sql` 的 CHECK 约束。

但容量与预算核算用的是另一份**未同步**的常量表，`fs_diloco/storage/fenced_store.py:2168-2176`：

```python
            reserved_states = (
                "planned", "submitting", "submitted", "started",
                "external_submitted", "submission_unknown", "retryable",
            )
```

它同时喂给 `reserved`（`:2178-2186`）和 `pending_scale`（`:2288-2298`），而 `:2311-2320` 的扩容判据是：

```python
                and productive + reserved < int(desired_contributors)
                and pending_scale < int(max_pending_launch_requests)
                ...
                and active_count + reserved < int(stream_pool_size)
```

**失败场景**：一个 `scale_out` 请求的 PBS job 进入 `terminal_uncertain`（job 可能仍在排队/运行）。因为该状态不计入 `reserved` / `pending_scale`，下一个 low-capacity 观测窗口就会认为“没有在途 launch”，从而新建一个 `scale_out` request。叠加 F-02（该状态永不退出），每个后续窗口都会再加一个，直到 `max_total_launch_requests` 耗尽；`active_count + reserved < stream_pool_size` 同样被低估，可能超额下发到 stream pool 之外。这与计划 §8.4“uncertain/manual_review 保留 anti-duplicate tombstone”和 Gate“scheduler duplicate admission=0”的意图相悖（`admitted_instance_id` 唯一约束仍能阻止重复 *admission*，但阻止不了重复 *launch* 与资源浪费）。

**修复建议**：把 `terminal_uncertain`（以及必要时 `manual_review`）加入 `reserved_states`，或把 `reserved` / `pending_scale` 的判据统一改为 `reservation_released_at IS NULL AND admitted_instance_id IS NULL`，从状态字符串列表切换到已有的 tombstone 字段，从根上消除“状态集合三处各自维护”的漂移。

**缺失测试**

- 缺“request 处于 `terminal_uncertain` 时 `record_capacity_observation` 不得再生成新的 `scale_out` launch request”的用例。
- 缺一个把 `launch_requests` CHECK 允许值、`active_only` 集合、`reserved_states` 三者做集合一致性断言的结构性测试（这是最能防住此类漂移的低成本测试）。

---

#### F-04（High，持久化不变量 / liveness）`acknowledge_terminal_contributor` 不校验最终 receipt 的 `proposal_expected`，可使 `finalize_terminal` 永久阻塞

**证据**

`fs_diloco/storage/authority.py:3496-3533`（`acknowledge_terminal_contributor` 的非 hard-crash 分支）只在 `final_update_id is not None` 时校验 update；`final_update_id=None` 时直接置 `state='acked'`，**不**检查该 cycle 的 receipt 是否已声明 `proposal_expected=True`：

```python
                if final_update_id is not None:
                    update = connection.execute("SELECT * FROM updates WHERE update_id=?", ...)
                    ...
                state = "acked"
```

而 `ingest_cycle_receipt`（`fs_diloco/storage/authority.py:1102`）对 `proposal_expected=True` 的 receipt 写入 `direct_fate='outstanding'`，`finalize_terminal`（`:3576-3585`）新增的门禁要求：

```python
            if awaiting or (token_outstanding is not None and int(token_outstanding[0]) != 0):
                raise RuntimeError("terminal finalization requires drained contributor/token state")
```

一旦 fence 变成 `acked`，`acknowledge_terminal_contributor` 的入口检查 `frozen["state"] != "awaiting_ack"` 会拒绝任何后续 ack（含 hard-crash 路径），也就无法再触发 `_terminalize_fenced_updates`（`:3876`）里新增的 orphan-receipt 清扫。

**失败场景（已复现）**：dynamic run，learner 上报 `proposal_expected=True` 的 receipt 后崩溃、proposal 从未发布；operator 以 `final_cycle_seq=1, final_update_id=None` 完成 drain ack：

```
ack state: acked
direct_outstanding: 6  balance: 0
finalize_terminal → RuntimeError: terminal finalization requires drained contributor/token state
```

此后没有任何 API 能把该 receipt 的 fate 从 `outstanding` 迁走，run 无法进入 terminal 状态。

**修复建议**

- 在 ack 时读取 `cycle_receipts.proposal_expected`：若为真而 `final_update_id is None`，要么 `MembershipFenceError` 拒绝（强制 operator 走 hard-crash ack），要么在同一事务内 `_transition_token_fate(..., fate="dropped", reason="terminal_ack_without_declared_proposal")`。
- 相应地，`terminal_contributor_fences` 可增加“ack 与 receipt 的 `proposal_expected` 一致”的应用层断言。

**缺失测试**

- 缺“最终 receipt 承诺 proposal 但 proposal 未到达 → ack → `finalize_terminal` 仍可完成或 ack 被明确拒绝”的用例。现有 `test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens`（`proposal_expected=False`）与 `test_terminal_close_accepts_only_one_contiguous_current_cycle_and_matching_update`（proposal 已 commit）恰好绕开了这个组合。

---

### Medium

#### F-05（Medium，plan Gate 未达成）启动期校验会遍历整个 run root（含 `audit/**`），与 Gate“audit 增长不进入启动扫描”直接冲突

**证据**：`fs_diloco/storage/run_initializer.py:267` 的 `for path in final_root.rglob("*")` 会 `lstat` run root 下每一个条目，包括 `audit/batches/**`、`audit/partitions/**`。`mutable_prefixes` 里的 `"audit"` 只让这些条目**被允许**，并不让它们**被跳过**。该函数经 `run_descriptor.py:139` 挂在每个 actor 的启动路径上。

**测量**：同一 run root，`load_run_descriptor` 耗时 **13.8 ms**（空 audit）→ **98.5 ms**（5000 个 audit batch 对象），即 O(audit 对象数)。计划 §8.8 Gate 与需求 `AUDIT-04`（矩阵标 `complete`）要求“immutable audit history 可线性增长但**不参与启动扫描**”。

**修复建议**：在 `rglob` 之前对 `mutable_prefixes` 做目录级剪枝（改用 `os.walk` 并在进入 `audit/`、`logs/`、`metrics/`、`updates/payloads/` 等前缀时 `dirnames.clear()`），只穷举协议受控前缀；或按 F-01 的建议把穷举移出启动路径。

**缺失测试**：缺“audit 对象数量增长 N 倍后，descriptor 加载所遍历的路径数保持常量”的结构性断言（可用 monkeypatch 计数 `Path.lstat` / `os.scandir` 调用次数，不必依赖计时）。

---

#### F-06（Medium，测试覆盖 / 矩阵与实现不一致）`AUDIT-05` 标记 complete，但 runtime 仍存在多进程共享 CSV append，`ActorTelemetryWriter` 零调用

**证据**：矩阵 `AUDIT-05` 的 test_contract 明写“static/dynamic 多 learner 扫描无 shared append”。实际 runtime 仍在每个 learner 进程里追加同一个共享文件：
`fs_diloco/runtime/learner.py:1949-1950` 与 `:2823-2824` → `paths.metrics / "learner_metrics.csv"`；`:1978-1979` 与 `:2843-2844` → `paths.metrics / "update_manifest.csv"`（`append_csv_row` 定义于 `fs_diloco/observability/metrics.py:12`）。
新增的 `ActorTelemetryWriter`（`fs_diloco/observability/logging_utils.py:48`）与 `RunPaths.actor_metrics_path`（`fs_diloco/storage/paths.py:216`）在 `fs_diloco/runtime/**` 内**没有任何调用点**（全仓 grep 确认）。唯一的测试 `tests/observability/test_p3_operational_evidence.py:15` 只验证这个未被使用的原语本身。

**说明与建议**：把 telemetry 切换放到 P4 cutover 是可以接受的工程排序，但那样 `AUDIT-05` 在本 phase 就应记为 `partial` / `deferred-to-P4` 并在矩阵中写明 disposition，而不是 `complete`。若坚持在 P3 关闭，则需要补上 runtime 接线与“扫描 run root 下不存在多 writer 共享 CSV”的实测用例。同一判断适用于 `TOK-01/02/03`（`TrainingSegmentAccumulator` 未接入 learner）与 `DATA-01`（`IndexedBlockCursor` 未接入 data loading）——这三者的 implementation_contract 描述的是 learner 侧行为，当前只有纯模型与单元测试。

**缺失测试**：`tests/architecture` 下缺一条“`fs_diloco/runtime/**` 不得调用 `append_csv_row` 写入 run 内共享路径”的静态断言。

---

#### F-07（Medium，测试覆盖）unified v4 golden 的 “no-replace/full-quorum 必须与 P0 逐位相同” 锚点是自指的，无法检出 v4 管线漂移

**证据**：`tests/fixtures/golden/unified_v4_trace.json` 中 `cases.no_replace_full_quorum.semantic_projection` 与 `classic_full_v1_trace.json` / `static_ha_v1_trace.json` 的 `semantic_projection` **逐字段完全相同**（含 `merged_float32_le_hex`、`theta_float32_le_hex`、`outer_momentum_float32_le_hex`）。而 `tests/protocol/test_p3_unified_v4_golden.py:20-32` 只做 fixture 之间的字典相等断言：

```python
    assert unified["semantic_projection"] == classic == static
```

全仓 grep 确认，**没有任何测试或工具执行 v4 merge/apply 管线来产生这份 `semantic_projection`**（`_semantic_projection` 只存在于 `tests/reference/test_plan03_classic_static_oracle.py:78`，且只服务 P0 的 classic/static 两条 oracle）。

**影响**：计划 §8.7 与 §8.8 要求“无 replace、无 quorum 截断 case 必须与 P0 `torch.equal`”。当前实现下，即使 v4 authority + merge 路径产生了不同的张量，这条断言仍会通过——它比较的是两个静态 fixture，不是两条实现。`P3-REBASE` 与 `SEL-06` 的 Gate 因此并未被真正把守。

**修复建议**：新增一个用 v4 `LeaderAuthority` 走完 `initialize_v0 → ingest_receipt → submit_proposal → select → commit_merge` 的用例，用与 P0 相同的 `_semantic_projection` 口径计算投影，并与 `classic_full_v1_trace.json` 做 `torch.equal` / 逐 hex 断言；`unified_v4_trace.json` 应由该用例（或一个显式 regenerate 工具）产出，而非手工从 P0 复制。

---

#### F-08（Medium，回归检测能力下降）Checker 把 `fs_diloco/storage/fenced_store.py` 移出 boundary manifest

**证据**：`scripts/miyabi/check_plan03.py:268-278` 删除了

```python
-        "fs_diloco/storage/fenced_store.py",
```

新增用例 `tests/test_plan03_checker.py:109-119` 把这一放宽固化为预期：`implementation_only["manifest_sha256"]["fs_diloco/storage/fenced_store.py"] = "0"*64` 仍必须返回 `[]`。

**影响**：本次 diff 恰好修改了 `fenced_store.py`（+19 行，含 F-03 涉及的 `active_only` 状态集合与 `update_launch_request` 的 5 个新字段）。移除该文件后，boundary gate 只剩 `inventory.bound_mutators` 名单，**内容漂移不再被检出**。这是为了让本次改动通过门禁而放宽门禁本身，方向上应当引起注意。

**修复建议**：保留 hash 边界，改用“允许清单 + 明确记录本次授权变更 sha256”的方式；或至少把 `fenced_store.py` 的 boundary 从 whole-file hash 降级为“公共 mutator 签名 + 状态集合常量”的结构化指纹，而不是整体退出。

**缺失测试**：缺“`fenced_store.py` 的状态字符串常量集合与 schema CHECK 保持一致”的替代性守卫（同 F-03）。

---

#### F-09（Medium，门禁强度）`verify_phase_requirements` 只验证“声明存在”，不验证“不变量成立”

**证据**：`scripts/miyabi/check_plan03.py:320-346`（`_declared_requirements`）通过 AST 抓取模块顶层的 `PLAN03_REQUIREMENTS` 字面量集合；`:348-397`（`verify_phase_requirements`）对每条需求仅检查四件事：`status == "complete"`、`artifact_contract == f"checker requirements.{id}"`、存在至少一个 implementation owner 与 test owner、evidence 路径存在于磁盘。

**影响**：只要在任意模块加一行 `PLAN03_REQUIREMENTS = frozenset({"X"})`、在任意测试文件加同名声明、并放一个 evidence 文件，需求 `X` 即判 PASS。F-06 就是这一机制的直接产物：`AUDIT-05` 的 owner 是一个 runtime 从不调用的类。40/40 PASS 因此不能作为“Gate 已达成”的独立证据。

**修复建议**：在 `verify_phase_requirements` 之外，为每条 Gate 级需求补一个可执行断言（结构性测试或行为测试），并让 checker 校验该断言的 nodeid 出现在矩阵的 `test_contract` 中；同时对 implementation owner 增加“该模块的公共符号至少被 `fs_diloco/runtime` 或 `fs_diloco/storage` 引用一次”的弱可达性检查。

---

#### F-10（Medium，错误处理 fail-open）`clean_run` 在 artifact policy 缺失时静默退回旧的宽松规则

**证据**：`fs_diloco/tools/clean_run.py:238-244`，`_load_artifact_policy` 在文件不存在时返回 `None`；`:415-420` 因此整体跳过策略校验与 authority live-reference 校验：

```python
    if artifact_policy is not None:
        _validate_policy_candidates(run_root=run, policy=artifact_policy, candidates=candidates)
```

`_authority_live_paths`（`:251`）只在该分支内被调用。

**影响**：`AUDIT-03`（“cleanup 不删除不可重建 authority/audit 或 live run 数据”）只对**由新 initializer 创建、且 policy 文件未被移除**的 run 生效。对 P0–P2 期间创建的 run root，或 policy 被删除/损坏的场景，清理工具的行为与 base 完全一致，即无 live-reference 保护。这与本文件其余部分一致采用的 fail-closed 风格（符号链接、sidecar、hash 全部拒绝）不一致。

**修复建议**：改为 fail-closed —— policy 缺失时 `raise CleanupRefusedError("artifact policy is required to prove cleanup safety")`，并为历史 run 提供显式 `--allow-legacy-run-without-policy` 开关（默认关闭、写入 manifest）。

**缺失测试**：缺“policy 文件缺失时 `build_cleanup_plan` 必须拒绝”的用例；现有 `test_clean_run_refuses_symlinked_policy_or_authority_database` 只覆盖了符号链接与非法内容。

---

#### F-11（Medium，回归风险）`schema_v4.sql` 发生不兼容变更但 `AUTHORITY_SCHEMA_VERSION` 未 bump

**证据**：`fs_diloco/core/versions.py:8` 在 base 与 target 均为 `AUTHORITY_SCHEMA_VERSION = 4`。而 `schema_v4.sql` 本次新增 5 张表（`terminal_contributor_fences`、`archive_partitions`、`audit_partition_batches`、`audit_gc_candidates`、`scheduler_operator_requests`）、新增列（`controller_state.hard_crash_cycle_token_budget`、`archive_batches.record_kind`、`candidate_launch_outbox.{first_uncertain_at,last_positive_evidence_at,manual_reason}`）、重写了 `candidate_launch_outbox.state` 的 CHECK 值域，并**移除**了 `cycle_receipts.previous_receipt_id` 的外键：

```sql
-    previous_receipt_id TEXT REFERENCES cycle_receipts(receipt_id),
+    previous_receipt_id TEXT,
```

**影响**：由 P2 创建的 v4 authority DB 打开时版本校验会通过，随后在第一次触碰新表/新列时抛出 `sqlite3.OperationalError`（而非清晰的 schema 版本不匹配错误）。外键的移除本身是可以理解的（为了让 archive prune 能删除历史 receipt），且 `ingest_cycle_receipt` 有应用层链式校验作为补偿，但它同时降低了 DB 层的链完整性保证，值得在计划/矩阵中显式记录为已接受的取舍。

**修复建议**：bump `AUTHORITY_SCHEMA_VERSION` 到 5，并在 `open_existing` 的不匹配分支给出“需要重新 init run”的明确诊断；在矩阵中为“移除 receipt chain FK”登记一条 accepted-finding disposition。

**缺失测试**：缺“P2 schema 的 DB 被 P3 代码打开时必须以明确的版本错误 fail closed”的用例。

---

#### F-12（Medium，并发/持久化）`audit_gc_candidates` 的 `claimed` 状态没有回收路径，进程在 claim 与 delete 之间崩溃会永久泄漏

**证据**：`fs_diloco/storage/authority.py:3218-3243`（`claim_audit_gc`）只选取 `state='pending'` 并原子置为 `'claimed'`；`:3245-3280`（`complete_audit_gc`）只接受 `state='claimed'`。文件删除由 `fs_diloco/storage/audit_archive.py:250`（`delete_claimed_audit_batch_object`）在**事务之外**执行。

**失败场景**：leader 在 `claim_audit_gc` 提交后、`delete_claimed_audit_batch_object` 完成前崩溃或丢失 lease。重启后的 leader 无法重新 claim（状态已是 `claimed`），也无法 complete（对象仍存在，`complete_audit_gc` 的存在性检查会 `RuntimeError("audit GC object still exists")`）。这些 batch 对象与索引行将永久留存，与 `AUDIT-04` 的“hot set 有界”目标相悖。`AuthorityReadModel.audit_archive_summary`（`:553`）暴露了 `claimed_gc` 计数，说明作者意识到该状态可能滞留，但没有配套的 requeue 命令。

**修复建议**：为 `audit_gc_candidates` 增加 `claimed_by_epoch` / `claimed_at`，并提供一个 fenced 的 `requeue_stale_audit_gc(command_id, older_than)`，把非当前 epoch 的 `claimed` 项在校验对象仍存在且 hash 匹配后退回 `pending`。

**缺失测试**：缺“claim 之后模拟 leader 更替 → 新 leader 能重新推进该 GC 候选”的用例。

---

### Low

| ID | 位置 | 问题 | 建议 |
|---|---|---|---|
| F-13 | `fs_diloco/tools/init_run.py:71, 104` | 全新初始化的返回值也是 `{"recovered": True}`，与“recovered”语义相反；调用方无法区分首次发布与幂等重放。现有测试（`test_plan02_phase1_ha.py:551`、`test_run_initializer_p3.py:95`）只断言重放路径为 `True`，未固定首次路径 | 首次发布返回 `False`，并补一条断言 |
| F-14 | `fs_diloco/storage/authority.py:1078-1092` vs `:4284` | `ingest_cycle_receipt` 仍在写 `streams.resume_cursor` / `streams.last_receipt_id`，但 `_dynamic_admission_result` 已改为从 `contributor_progress` 读取。两份 cursor 真源并存且无一致性断言，属于可漂移的死写 | 删除该 UPDATE，或在 admission 时断言两者相等 |
| F-15 | `fs_diloco/tools/resolve_scheduler_uncertainty.py:112-124` | `--expected-state-sha256` 是必填项，但仓库内**没有任何 operator 可用的只读工具**能产出该 hash（`scheduler_state_sha256` 仅被 `authority.py:1943` 内部与测试调用）。而工具本身按设计禁止连接 authority DB，operator 处于死锁 | 新增一个只读 `dump-scheduler-state` 子命令/工具（走 `ReadOnlySQLiteStore`），输出 `request_id → scheduler_state_sha256` |
| F-16 | `fs_diloco/storage/authority.py:1913-2020` | `apply_scheduler_operator_request` 绕过了 `transition_candidate_launch_request:2036-2052` 的状态机（例如可从终态 `manual_review` 回到 `submitted`），两套转移规则并存 | 让 operator 路径复用同一张 transition 表，或显式注释“operator 覆盖”并在 audit 行中标记 |
| F-17 | `fs_diloco/storage/authority.py:3292-3336` | `_transition_token_fate` 的 `applied_version=?` 无条件写入（默认 `None`），任何从 `applied` 迁出的转移都会静默清空 `applied_version` 审计字段 | 未显式传入时保留原值（`applied_version=COALESCE(?, applied_version)`） |
| F-18 | `fs_diloco/storage/run_initializer.py:298-311` | `repair_identity_reservation` 是计划 §8.5 明确要求的“显式 repair”，但没有任何 CLI / console script 暴露它，实际不可运维 | 增加 `fs_diloco/tools/` 下的显式 repair 入口（默认 dry-run） |
| F-19 | `fs_diloco/storage/authority.py:3374-3395`（`_audit_history_records` 的 `update_rows` 子查询） | 该子查询对 `selected_batch_id IS NULL` 的 `dropped` update 不施加任何 `cutoff_version` 约束，`archive_audit_batch(cutoff_version=0)` 会一次性归档全部历史 dropped update，`cutoff` 语义在不同表之间不一致 | 统一按 `cutoff_version` 过滤，或在 docstring 中明确“dropped 行不受 cutoff 约束”这一有意设计 |

---

## 3. 值得肯定的部分

- `run_initializer.py` 的 same-parent staging → parent-sibling hard-link identity reservation → exclusive `mkdir` → per-object create-no-replace → `.complete` marker 顺序，以及 `claim_identity_reservation` 中“新建 reservation 后发现 final 已存在则先删除 reservation 再 fail closed”的处理（`:190-196`），与计划 §8.5 的要求逐条对应；`tests/storage/test_run_initializer_p3.py` 对 5 个 crash point 与每一次 `fsync_directory` 失败都做了参数化重试验证，是本次质量最高的一组测试。
- `initialize_authority_v4` 新增的 `_path_entry_exists`（`authority.py:229`，用 `lstat` 取代 `exists()` 以正确拒绝断链符号链接）与 `except BaseException` 回滚（`:351-355`），修掉了一类真实的半发布窗口，并有 `test_fresh_v4_schema_rejects_broken_symlink_collisions_without_partial_publish` 覆盖。
- `leader_lease.py` / `authority.py` 把 `now = float(self._wall_clock())` 移到 `BEGIN IMMEDIATE` 之后（共 6 处），是对 M-01 的正确修复。
- `PersistentFairSelector` 的“选择不消耗 credit、只有 `commit` 消耗”设计与 `commit_merge` 内同事务更新 `selection_state`（`authority.py:2690-2696`）配合得当，Group B 门禁（1000 轮、偏差 ≤1、max wait ≤ ⌈N/3⌉+1、Jain ≥ 0.95）由 `test_persistent_fair_selector_meets_frozen_1000_round_gate` 实测覆盖，并额外验证了 replay 确定性。
- `normalized_update_weights` 的加固（拒绝 bool/非 int token、重复 update_id、future base、非有限权重，改用 `math.fsum`）在 classic 路径上是安全的：`sqlite_store.py:954` 已过滤 `base_global_version <= ?`，`learner.py:1883` 的 `if not losses: continue` 保证 `interval_tokens > 0`，因此不构成运行期回归。
- `check_workload_equivalence.py` 明确输出 `clipping_applied: False` 并用有符号中位数（而非 median-absolute）做 20% 审计阈值，符合 §8.6 “不得用 clipped ratio 作为 gate”。
- PBS 脚本 `bash -n` 通过、group ID 为字面量、walltime 满足最短 10 分钟规则；`set -eEuo pipefail` + ERR trap + 完成标记齐备。

---

## 4. 与 Plan §8.8 Gate 的逐条对照

| Gate 条款 | 判定 | 依据 |
|---|---|---|
| H-02/H-05/H-07/H-08/H-09 与 M-01..M-03/M-05..M-12/M-15 均有 GREEN 测试或明确 disposition | **部分** | 有 disposition JSON 与测试，但 M-06/M-07（AUDIT-05）实际未在 runtime 落地（F-06）；H-07 的 SCHED-04 未真正成立（F-02） |
| authority 连续裁决的 receipt ledger terminal balance=0 | **通过（有例外）** | `TokenLedgerSummary.balance` 在 `__post_init__` 强制为 0；但 `direct_outstanding` 可能永远无法清零（F-04） |
| static/dynamic 每个 lost incarnation replay ≤ one cycle，run 级上界逐 incarnation 求和 | **通过** | `hard_crash_cycle_token_budget` + `terminal_contributor_fences.hard_crash_gap_tokens_upper_bound` + `test_terminal_hard_crash_gap_is_summed_per_lost_incarnation` |
| scheduler duplicate admission=0，uncertainty 在 deadline 内有明确状态 | **未通过** | F-02（deadline 不可达）、F-03（重复 launch） |
| wall-clock jump 不改变 process timeout | **通过** | `test_process_elapsed_safety_is_monotonic_despite_wall_clock_jumps`（±1h 与 −2h 双向） |
| init 每个 crash point 都不会让 reader 接受半成品、也不覆盖既有 final；同 identity retry 可完成；descriptor logical path 在 complete 后有效 | **部分** | crash point / identity collision 覆盖充分；但 “complete 后 logical path 有效” 只在零 runtime 写入的 run 上成立（F-01） |
| recovery hot DB/files 在 rollup/archive/prune 后有界，audit 增长不进入启动扫描 | **未通过** | F-05（启动扫描 O(audit 对象数)）；另见 F-12 的 GC 泄漏 |
| unified v4 trace 和归因报告完成 | **部分** | 文件与归因字段齐备，但 P0 锚点断言自指、不具备漂移检出能力（F-07） |
| torch baseline data/optimizer/protocol tests 保持通过 | **通过** | 本地 CPU 子集 235 passed；artifact 记录 full suite 794 passed |

---

## 5. 结论

## CHANGES_REQUIRED

P3 的架构方向、initializer 崩溃恢复设计、token ledger 守恒模型与 fair-selection 模型质量都很高，测试量也很扎实。但存在 **1 个 Critical + 3 个 High** 的阻塞项：

1. **F-01（Critical）**：`load_run_descriptor` 在 syncer 发布 `control/latest.json` 之后必然抛 `RuntimeError`，使 learner 重启、syncer takeover、`init_run` 幂等恢复与全部离线分析工具在真实 run 上不可用。已本地复现。
2. **F-02（High）**：scheduler uncertainty deadline 每轮被刷新，`manual_review` 在真实时钟下不可达，`SCHED-04` 与对应 Gate 未成立；现有用例因冻结时钟而把缺陷固化为预期。已本地复现。
3. **F-03（High）**：`terminal_uncertain` 未加入 `reserved_states`，与 F-02 叠加会持续下发重复 `scale_out` launch request。
4. **F-04（High）**：drain ack 不校验 `proposal_expected`，可使 `finalize_terminal` 永久阻塞且无恢复路径。已本地复现。

此外 F-05 / F-07 使两条 Gate 条款在当前测试下无法被实际把守，F-06 / F-09 表明 40/40 需求 PASS 不足以作为 phase 完成的独立证据，建议在修复上述阻塞项的同时，把 `AUDIT-05` / `TOK-01..03` / `DATA-01` 的矩阵状态改为显式 deferred-to-P4 并记录 disposition，或补齐 runtime 接线。

建议按 F-01 → F-02 → F-04 → F-03 → F-05 → F-07 的顺序修复，每项都先补上文列出的 RED 测试，再以同一 base / 新 target 重新提交本 phase 审查。
