# 独立代码审核 — P4 mandatory fenced runtime remediation

- Base commit: `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4`
- Target commit: `d18fae055b5beec1887f38c3f2070f0bf6ec901b`
- 审核关系：base 是 target 的祖先；本报告覆盖完整增量 `git diff 0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4 d18fae055b5beec1887f38c3f2070f0bf6ec901b`
- 区间内 commit：`97b9868`（fix，唯一含源码/测试改动）、`52a0980`（evidence + requirement matrix）、`d18fae0`（tracked evidence gate 记录）
- 已核实：`git diff --stat 97b9868 d18fae0 -- fs_diloco tests scripts configs` 为空，即 target 与 `97b9868` 的源码树完全一致
- Reviewer：Claude，实际模型 `claude-opus-5`
- 角色：只读 reviewer（未修改除本报告外的任何文件、未改动 Git 状态、未 qsub/qdel、未删除 run 数据）
- 结论：**CHANGES_REQUIRED**

---

## 审核范围

### 源代码（`git diff` 全部 11 个源文件）
- `fs_diloco/protocol/admission_v4.py`（+651/-…，新增 rejection / current-pointer / disposition / history / 静态替换授权协议）
- `fs_diloco/protocol/control_v4.py`（heartbeat 改为 committed lease 快照；heartbeat / latest head / latest payload 严格字段校验；anchored pointer 读取）
- `fs_diloco/runtime/syncer_v4.py`（`_admit_requests` 重写、`_repair_current_admission_controls`、`_existing_admission_resume`、`_resume_from_progress`）
- `fs_diloco/runtime/learner_entrypoint.py`（删除 `FS_DILOCO_ALLOW_LOGICAL_REPLACEMENT`；torch import 前二次校验）
- `fs_diloco/runtime/syncer_entrypoint.py`（`V4ControlPublisher` 去掉 lease_duration；renew 返回 committed lease）
- `fs_diloco/storage/authority.py`（`committed_leader_lease`、`renew_leader` 返回值）
- `fs_diloco/storage/leader_lease.py`（`CommittedLeaderLease`）
- `fs_diloco/storage/paths.py`（6 个新路径 helper + 3 个新 control 目录）
- `fs_diloco/storage/artifact_policy.py`（3 条新 authority glob）
- `fs_diloco/tools/authorize_static_replacement.py`（新 operator CLI）
- `fs_diloco/tools/migrate_config_v3_to_v4.py`（flock + publication-boundary CAS + create-no-replace）

### 测试 / 脚本 / 配置 / 文档
- `tests/runtime/test_p4_mandatory_runtime.py`（+760，13 个新用例）
- `scripts/miyabi/run_plan03_phase4_static_rerun.pbs`（未授权 duplicate 拒绝段 + operator 授权段 + 诊断正则扩展）
- `plans/DOING/plans/…-requirement-matrix.csv`（AUTH-02/03/04/05/07/09/10、P4-MIGRATE 证据重绑）
- `reports/DOING/…/artifacts/**`（50 个新证据 JSON）、`failures.md`、`progress.md`
- 配置：本区间无 `configs/**` 改动（已用 `git diff --stat` 确认，与 `20260809-121500_p4-remediation-migration-delta_pass.json` 记录一致）

### 本地复现执行（只读，均在 `/tmp` 临时目录）
- `.venv/bin/python -m pytest tests/runtime/test_p4_mandatory_runtime.py` → `67 passed`
- `.venv/bin/python -m pytest -q`（全仓）→ `883 passed in 117.53s`
- `.venv/bin/ruff check fs_diloco tests scripts/miyabi` → `All checks passed!`
- `bash -n scripts/miyabi/run_plan03_phase4_static_rerun.pbs` → OK
- `scripts/miyabi/check_plan03.py --expect … --verify-phase-requirements P4-mandatory-fenced-runtime --require-tracked-evidence` → **BLOCKED**（详见 M5）
- 6 个针对 admission 热路径 / 替换路径的独立复现脚本（下文逐条给出实际输出）

---

## Critical

无。

---

## High

### H1 — 热路径中任何“读不出来”的条目会让每一个 leader candidate 在 `_admit_requests` 里无限崩溃循环

**证据（代码）**

- `fs_diloco/protocol/admission_v4.py:809-812`：`_read_hot_request()` 对非 regular file（目录、symlink、FIFO）直接 `raise OSError`。
- `fs_diloco/protocol/admission_v4.py:148-155`：`iter_admission_requests()` 只捕获 `(OSError, json.JSONDecodeError)`，把结果降级为 `payload=None`；`UnicodeDecodeError` 不在捕获集合内。
- `fs_diloco/runtime/syncer_v4.py:351-371`：`payload is None` 被判定为 `MalformedAdmissionRequest`，随即调用 `dispose_invalid_admission_request()`。
- `fs_diloco/protocol/admission_v4.py:526`：该函数**第二次**调用 `_read_hot_request()`，对同一个非 regular 条目再次抛 `OSError`，且此处没有任何 try/except。
- `fs_diloco/runtime/syncer_v4.py:160`：`_admit_requests()` 在主循环内被无保护调用，异常直接终止 syncer 进程。

**证据（实际复现输出）**

```
EXP1 iteration 0: OSError: admission request is not a regular file: .../registration_requests/static/learner_000/poison.json
EXP1 iteration 1: OSError: admission request is not a regular file: ...
EXP1 iteration 2: OSError: admission request is not a regular file: ...
EXP1 poison still present: True

EXP2: OSError: admission request is not a regular file: .../registration_requests/static/learner_000/link.json
EXP2 symlink still present: True

EXP4 invalid-utf8: UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 20: invalid start byte
```

（EXP1 = `control/registration_requests/static/learner_000/poison.json` 为目录；EXP2 = 同名 symlink；EXP4 = `b'{"format_version": "\xff\xfe\xfa"}'`。三者均为 `_admit_requests()` 直接抛出、未被消化。）

**失败场景**：共享文件系统上出现一个非 regular 的 `*.json` 条目（误建目录、残留 symlink、外部工具写入的非 UTF-8 字节）。当前 leader 立即死亡；successor 取得 lease 后在同一位置再次死亡；毒条目是 epoch 无关的，因此**跨所有 epoch 永久阻断**整个 run 的 admission，只能人工删除文件恢复。这与本 commit 明确宣称的设计（"Durably archive and remove one malformed or foreign hot request"，`admission_v4.py:524`）直接矛盾，也破坏 P4 failover 可用性目标。

**修复建议**
1. 把“读失败”和“读成功但内容非法”分成两类返回值（例如 `Unreadable` / `Invalid(bytes)`），`iter_admission_requests()` 返回三态。
2. `dispose_invalid_admission_request()` 只接受调用方**已经成功读到的字节**，不再自行二次读取；非 regular / 不可读条目走单独的隔离路径（记录 telemetry + 跳过，或写入一条 quarantine 记录），绝不允许异常逃逸出 `_admit_requests()`。
3. `iter_admission_requests()` 的 except 补上 `UnicodeDecodeError`（或统一捕获 `ValueError`）。
4. 无论如何，`syncer_v4.py:160` 的 `_admit_requests()` 应对单条请求做 per-request 异常边界，防止一条毒数据杀死整个 leader。

**缺失测试**
- 在 `registration_requests/**` 放入目录 / symlink / FIFO 各一个，断言 `_admit_requests()` 不抛异常、其余合法请求仍被正常 admit、且连续多轮调用行为稳定。
- 非 UTF-8 字节请求被记为**一条**持久 `MalformedAdmissionRequest` disposition 而不是抛 `UnicodeDecodeError`。
- 一个覆盖“毒条目存在时 leader 仍能完成 v0→v1 提交”的 runtime 级回归。

---

### H2 — 一次瞬时读失败会把**合法**注册请求永久销毁并记为 `MalformedAdmissionRequest`

**证据（代码）**

- `fs_diloco/protocol/admission_v4.py:153-155`：任何 `OSError`（EIO/ESTALE/共享 FS 抖动）都被折叠成 `payload = None`，与“文件内容非法”不可区分。
- `fs_diloco/runtime/syncer_v4.py:351-378`：`None` 被无条件当作 malformed，调用 `dispose_invalid_admission_request()`。
- `fs_diloco/protocol/admission_v4.py:526-551`：第二次读取成功后，把这份**完全合法**的请求以 raw-bytes SHA 归档、写 `outcome="rejected"` 的 disposition，并 `_remove_hot_request()` 删除热路径文件。

**证据（实际复现输出）**：注入**一次**性的 `OSError(5, "Input/output error")`：

```
events: [('admission_request_discarded', 'MalformedAdmissionRequest')]
binding: None
hot left: []
disposition: {..., 'error_type': 'MalformedAdmissionRequest',
              'message': 'request is not a JSON object',
              'outcome': 'rejected', ...}
```

**失败场景**：Lustre/NFS 上一次瞬时读错误 → 该 static learner 的注册请求被永久删除，authority 中没有任何 binding，learner 侧只能在 `learner_recovery_wait_seconds` 之后超时退出（`dispose_invalid_admission_request()` **不写** `epoch_admission_rejection_path`，所以 learner 连快速失败的诊断都拿不到）。在 static array job 中这等于整个 job 失败，且证据里只留下一条误导性的 "MalformedAdmissionRequest"。

**修复建议**
- 与 H1 同一处修复：只有在**成功读到完整字节**之后才允许做 malformed 判定；读失败一律 skip-and-retry，不产生任何持久 disposition，也不删除热路径文件。
- 建议在 disposal 前加一次独立复读（不同 fd）确认字节一致，进一步降低误判。

**缺失测试**
- 注入一次性 `_read_hot_request` `OSError`，断言：请求仍在热路径、无 disposition/history 产生、下一轮 poll 正常 admit、binding 生成。
- 注入持续性读失败，断言 leader 不崩溃且不产生 disposition。

---

## Medium

### M1 — admission 路径上的 immutable publication 冲突以 `FileExistsError` 逃逸，卡死 leader 且热请求永久滞留

**证据（代码）**

- `fs_diloco/protocol/admission_v4.py:305-306`：rejection 走 `paths.epoch_admission_rejection_path(epoch, owner, actor_id, attempt_id)` 的 immutable 发布，**路径不含 request SHA**，因此同一 `(epoch, actor, attempt)` 的两次不同 rejection message 必然字节冲突。
- `fs_diloco/protocol/admission_v4.py:456-457 / 497-508`：合法请求的 history 用 canonical JSON SHA 命名，但归档的是**原始字节**；语义相同、编码不同的两个请求会命中同一路径而字节不同。
- `fs_diloco/runtime/syncer_v4.py:466-483`：这两处发布都在 `except` 分支或循环体内，`FileExistsError` 不在 `syncer_v4.py:460-465` 捕获集合中，直接逃逸出 `_admit_requests()`。

**证据（实际复现输出）**

```
EXP7 events so far: [('learner_admitted', None), ('admission_rejected', 'static binding generation changed')]
EXP7 RAISED: FileExistsError immutable target collision:
  .../syncer_epochs/e000001_391887cbcf92/membership/admissions_v4/rejections/learner_000/attempt-1.json
EXP7 hot left: ['attempt-1.json']
```

```
EXP5(不同编码同语义请求) : FileExistsError immutable target collision:
  .../control/registration_history_v4/26a3537f…c4fc.json
EXP5 hot left: ['attempt-1.json']
```

**失败场景**
- 场景 A（EXP7）：launcher 复用同一 `--attempt-id` 重试。第一次因 `expected_generation` 不匹配被拒（message X），归档后热路径释放；第二次同 attempt-id 因 active-binding 规则被拒（message Y）→ rejection 路径字节冲突 → leader 崩溃，且该热请求永远留在扫描树中，后续每一轮都在同一点崩溃。
- 场景 B（EXP5）：任何非本仓库 writer（或未来换用别的 JSON 序列化）写出语义相同但编码不同的合法请求，第二份永远归档失败、永远滞留。

**修复建议**
- rejection 路径加入 `request_sha256`（例如 `rejections/<actor>/<attempt>/<request_sha>.json`），并保留一个可覆盖的 "latest rejection" 指针供 learner 读取；或让 rejection payload 只含确定性字段（error_type + request_sha），把 message 移到附属诊断文件。
- 合法请求的 history 归档 canonical 字节（与摘要同源），而不是原始字节；无效请求继续按 raw-bytes 内容寻址。
- 在 `_admit_requests()` 内为 `FileExistsError` / `RuntimeError` 建立 per-request 边界，保证单条请求的发布冲突不终止 leader。

**缺失测试**
- 同一 `(epoch, learner, attempt)` 先后产生两种不同 rejection 原因，断言两条 rejection 都持久、热请求被清空、leader 不抛异常。
- 同语义不同编码的两份合法请求顺序处理，断言两份都被移除且 history 只有一个不变对象。

---

### M2 — attempt-id 复用时 `_existing_admission_resume` 在 authority 已提交后抛 `RuntimeError`，同 epoch 内形成崩溃循环

**证据（代码）**

- `fs_diloco/runtime/syncer_v4.py:491-503`：`bind_or_replace_static_attempt()` 已经**提交**新 binding 之后才调用 `_existing_admission_resume()`。
- `fs_diloco/runtime/syncer_v4.py:623-624`：若同路径已存在旧 fence 的 response，直接 `raise RuntimeError("existing admission response does not match current fence")`，该异常不在 `syncer_v4.py:460-465` 的捕获集合内。

**证据（实际复现输出）**

```
EXP8 binding now: attempt-2 2
EXP8 RAISED: RuntimeError existing admission response does not match current fence
EXP8 hot left: ['attempt-1.json']
EXP8 binding after: attempt-1 3          <-- authority 已经推进到 generation 3
EXP8 retry RAISED: RuntimeError existing admission response does not match current fence
```

**失败场景**：operator 授权把 `attempt-2`(gen2) 替换回一个**曾经用过**的 attempt-id `attempt-1`。authority 事务成功推进到 gen 3，但 response 发布路径 `responses/learner_000/attempt-1.json` 上还留着 gen 1 的 immutable response → 每一轮 `_admit_requests()` 都在同一点抛 `RuntimeError`。持久状态被撕裂：binding 已是 gen3/attempt-1，却永远发不出对应 response，learner 只能超时；只有换 leader epoch（新 epoch 目录）才能自愈，而自愈之后又会退化成 M3 的"已 admit 却被记为 rejected"。

**修复建议**
- response 路径中加入 `binding_generation`（例如 `responses/<learner>/<attempt>/g<generation>.json`），或直接以 fence 的规范摘要命名，使 `(actor, attempt)` 不再是唯一键。
- 在调用 authority mutation **之前**先检测目标 response 路径是否已被不同 fence 占用，若占用则以 rejection 形式拒绝，而不是在提交后崩溃。

**缺失测试**
- 授权替换到一个已被使用过的 attempt-id，断言：要么整条请求在 mutation 前被拒绝、authority 不变；要么 response 成功发布且与新 fence 一致。当前两种都不成立。

---

### M3 — leader 切换后，已经生效的 admission 会被持久记录成 `rejected`，与同一次调用里发布的 response 自相矛盾

**证据（代码）**

- `fs_diloco/runtime/syncer_v4.py:384`：`command_id = f"admit-e{leader.token.epoch}-{request_sha}"`。base 版本是 `admit-{request_sha}`；`authority.py:4678-4720` 的 command journal 以 `command_id` 全局去重，因此加上 epoch 前缀等于**主动放弃了跨 epoch 的命令幂等性**。
- 跨 epoch 的唯一去重手段是 `syncer_v4.py:379-383` 的 disposition 文件，而 disposition 是在 authority 提交**之后**才写的（`syncer_v4.py:504-521`），存在必然的窗口。
- `fs_diloco/storage/authority.py:971-973`：`expected_generation` 检查在"attempt 完全一致则幂等返回"之前，因此重放会先命中 `MembershipFenceError`。

**证据（实际复现输出）**：epoch1 提交 gen2/attempt-2 后在 disposition 前崩溃，epoch2 接管：

```
EXP6 epoch2 events: [('admission_rejected', 'MembershipFenceError', 'static binding generation changed')]
EXP6 response exists: True | rejection exists: True
EXP6 rejection payload: {... 'error_type': 'MembershipFenceError',
                          'message': 'static binding generation changed',
                          'leader_epoch': 2, ...}
```

**失败场景**：同一 epoch 目录下同时存在该 attempt 的 **admission response**（由 `_repair_current_admission_controls()` 在 `syncer_v4.py:349` 发布）和 **admission rejection**；disposition 记录 `outcome="rejected"`；telemetry 发出假的 `admission_rejected`。learner 侧因为 `read_admission_response()` 先查 response（`admission_v4.py:331`）而侥幸不受影响——但这个安全性完全依赖 repair 与 reject 的执行顺序，属于隐式不变量，没有测试锁定；同时故障取证与运维审计被污染（"当前 active 的 attempt 被记为 rejected"）。

**修复建议**
- 在 command 重放前先查询当前 binding：若 `(learner_id, logical_launch_id, attempt_id)` 已与请求一致且 `status='active'`，直接走"已 admit"路径补齐 response/disposition/archive，不再重新执行 mutation。
- 或恢复跨 epoch 稳定的 `command_id`，并在 `fence not in current_contributor_fences()`（`syncer_v4.py:491`）处把陈旧重放结果转成显式 rejection 而不是 `RuntimeError`。
- 无论选哪种，都应把"response 与 rejection 不得同时存在于同一 `(epoch, actor, attempt)`"提升为显式断言。

**缺失测试**
- disposition 发布失败 → leader epoch 切换 → 断言不产生 rejection 控制文件、不发出 `admission_rejected`、disposition 最终为 `admitted`。
- 断言同一 `(epoch, actor, attempt)` 下 response 与 rejection 互斥。

---

### M4 — 三个新的 control 目录被归入 `authority` 类（永不清理）且无任何保留策略

**证据**

- `fs_diloco/storage/artifact_policy.py:126-128` 新增 `control/registration_history_v4/**`、`control/registration_dispositions_v4/**`、`control/static_replacement_requests/**` 到 `classes.authority`。
- `fs_diloco/storage/artifact_policy.py:27-29`：`GENERIC_CLEANUP_CLASSES` 只含 TELEMETRY/CACHE/PAYLOAD/TEMPORARY —— `authority` 类永不被通用清理。
- `fs_diloco/storage/maintenance.py` 中没有任何针对这三个目录的保留/归档逻辑（`registration_requests` 相关代码走的是 DB 侧 `registration_history_jsonl`，与新目录无关）。

**失败场景**：每一次 admission 请求（含 dynamic 模式下每次 instance 替换）都会在 `registration_dispositions_v4/` 和 `registration_history_v4/` 各留下一个永久小文件。长跑 dynamic run 会在共享 FS 上无界累积 inode，直接违背 P0-RETENTION 冻结的 "hot-row bounds" 口径，并把风险推到 P6 的 bounded-resource 门。

**修复建议**：为这三个目录定义显式保留窗口（例如与 `publication_orphan_grace_seconds` / terminal 状态挂钩），或改为按 epoch 分片以便随 epoch 归档；无论选哪种都需要更新 P0 冻结的 retention artifact 与 checker 阈值。

**缺失测试**：N 次 admission/替换后目录条目数受配置上限约束的回归；maintenance 不会误删仍被 hot request 引用的 disposition 的负例。

---

### M5 — P4 phase gate 在 target commit 上不通过；MODE-02 仍是 `pending/TBD` 且不在 P4 phase 内被检查

**证据**

- 在 target commit 上实际运行（默认 `--verification-target-ref HEAD`）：

```
$ .venv/bin/python scripts/miyabi/check_plan03.py --root . \
    --expect …/20260808-223500_p0-runtime-surface-inventory_review.json \
    --verify-phase-requirements P4-mandatory-fenced-runtime --require-tracked-evidence
status BLOCKED
differences [
 "requirements.AUTH-02.structured-checker-evidence",
 "requirements.AUTH-03.structured-checker-evidence",
 "requirements.AUTH-04.structured-checker-evidence",
 "requirements.AUTH-05.structured-checker-evidence",
 "requirements.AUTH-07.structured-checker-evidence",
 "requirements.AUTH-09.structured-checker-evidence",
 "requirements.AUTH-10.structured-checker-evidence",
 "requirements.P4-MIGRATE.structured-checker-evidence"
]
src d18fae055b5beec1887f38c3f2070f0bf6ec901b
```

- 原因：`scripts/miyabi/check_plan03.py:507-527` 要求证据的 `source_commit` 等于 verification target commit，而重绑后的全部 P4 证据都记着 `97b98689123e081117501bd26bd68058589b78f2`：
  - `20260809-121700_p4-remediation-target-requirements_pass.json` → `requirements_source_commit: 97b9868…`
  - `20260809-121510_p4-remediation-target-runtime_pass.json` → `source_commit: 97b9868…`
  - `20260809-121900_p4-final-tracked-evidence-gate_pass.json` 自己记录的命令即为 `--verification-target-ref 97b98689…`
- MODE-02（"static learner … 旧/重复 process 不能提交或占 GPU"，正是本 commit 的核心实现）在 matrix 第 24 行仍为 `phase=P1-typed-foundation`、`gate=P4 completed`、`status=pending`、`evidence=TBD`。`check_plan03.py:452-453` 按 `phase` 列筛选，因此 `--verify-phase-requirements P4-mandatory-fenced-runtime` **从不检查 MODE-02**，尽管 `20260809-121510_p4-remediation-target-runtime_pass.json` 的 `requirements_covered` 已经声明覆盖它。

**影响**：P4 的完成门实际上只对 `97b9868` 成立；在被声明为 phase 终态的 `d18fae0` 上重跑同一门直接 BLOCKED。同时该 phase 最核心的 MODE-02 不受门约束，证据栏仍是 TBD。这是 plan 验收条件层面的实质缺口（并非纯文档瑕疵）。

**修复建议**
1. 在最终 commit 上重跑 requirements/tracked-evidence 门并写入一份 `source_commit == d18fae0…`（或最终 commit）的证据；或把"证据 commit 可以是同源码树的祖先 commit"这一豁免显式实现进 checker（需同时校验 `git diff --stat <evidence_commit> <target> -- fs_diloco tests scripts configs` 为空）。当前是"人工用 `--verification-target-ref` 绕过"，没有任何自动约束。
2. 把 MODE-02 的 `phase` 迁到 `P4-mandatory-fenced-runtime`（或让 checker 按 `gate` 列而不是 `phase` 列聚合 P4 门），并把 status/evidence 绑到本次 static rerun 的真实证据。

**缺失测试**：`tests/test_plan03_checker.py` 需要一个用例断言"证据 commit 与 verification target 不一致时 BLOCKED"，以及一个断言"`gate=P4 completed` 的行必须被 P4 phase 门覆盖"。

---

## Low

### L1 — `_repair_current_admission_controls()` 在每一次 poll 都对每个 current fence 重发 immutable response
`fs_diloco/runtime/syncer_v4.py:349` 位于主循环 `syncer_v4.py:155-160` 内；`syncer_v4.py:581-588` 每轮都调用 `publish_admission_response()`，而 `publish_immutable_with_writer()`（`storage/atomic_io.py:100-168`）即使是幂等重放也会做 mkstemp + write + 2 次 fsync + chmod + sha256 + link 尝试 + 冲突后整文件重读。合并繁忙时循环不 sleep，N 个 contributor 会带来 O(N) 次共享 FS 元数据操作/轮。建议只在 epoch 首轮或检测到 pointer/response 缺失时才执行 repair。缺失测试：断言稳态下每轮 repair 的文件系统写次数为 0。

### L2 — 无效请求不产生 learner 可见的拒绝，actor 只能超时
`dispose_invalid_admission_request()`（`admission_v4.py:513-552`）只写 disposition/history，不写 `epoch_admission_rejection_path`。结合 H2，被误判的 learner 既拿不到诊断也拿不到快速失败，只能等满 `learner_recovery_wait_seconds`。建议：当 `actor_id/attempt_id` 可以从字节里安全解析出来时，同时发布一条 rejection。

### L3 — 热请求读取无字节上限
`admission_v4.py:809-827` 无界读入并 join；`admission_v4.py:526-535` 再做 base64 展开（约 1.33×）后 immutable 发布。建议按协议定义最大字节数，从 `fstat` 提前拒绝，超限只保留摘要与截断诊断。

### L4 — learner 二次校验把瞬时读失败当作"admission 变更"
`learner_entrypoint.py:150-159`：`read_admission_response()` 内部 pointer 读取用 `safe_read_json()`（`admission_v4.py:386`），任何 OSError 都被吞成 `None` → 返回 `None` → `current_admission != admission` → `RuntimeError("learner admission changed immediately before torch import")`，健康 learner 被瞬时 FS 抖动杀死。建议 pointer 读失败时做有限次重试后再判定。

### L5 — 用 `assert` 承载运行时不变量
`control_v4.py:236` 与 `control_v4.py:380` 的 `assert isinstance(...)` 在 `python -O` 下会被剥除。当前逻辑正确性由前置校验函数保证，建议改为显式 `if not isinstance(...): return None/continue` 以免优化模式下语义漂移。

### L6 — in-place migration 的锁文件不会被清理
`tools/migrate_config_v3_to_v4.py:95` 创建 `.{name}.migrate.lock`，`migrate()` 的 `finally`（第 173-176 行）只解锁并关闭 fd，不删除文件，会在 `configs/` 旁留下持久隐藏文件。建议在成功路径上删除（或明确文档化为设计选择）。

### L7 — operator 授权文件是 immutable，写错无法更正
`publish_static_replacement_authorization()`（`admission_v4.py:652-681`）以 0o444 immutable 发布到 `static_replacement_requests/<learner>/<new_attempt>.json`。若 operator 填错 `--old-binding-generation`，同一 `new_attempt_id` 再次发布会直接抛 `FileExistsError: immutable target collision`，且文件不可写。`tools/authorize_static_replacement.py` 没有任何补救路径或提示。建议在 CLI 中捕获并给出明确指引（换 attempt-id 或由 operator 显式清理）。

### L8 — static rerun PBS 新增的可接受诊断削弱了该门的证明强度
`scripts/miyabi/run_plan03_phase4_static_rerun.pbs:284` 把 `AdmissionSupersededError` 加入旧 learner 的可接受失败集合。由于 `learner_entrypoint.py:136-149` 的 admission signal 写入发生在 `:150` 的二次校验**之前**，PBS 在看到 signal 后立刻 `kill -STOP`（`:135`）很可能命中该窗口——`20260809-105900_p4-review-static-duplicate-rerun-attempt1_fail.json` 记录的正是这个结果。此时旧进程根本没有 import torch，也就不再检验"旧 attempt 在 fenced-write 边界被拒"。脚本没有任何断言强制旧进程到达 post-torch 阶段。建议：把 admission signal 移到二次校验之后，或增加一个显式区分"pre-torch supersede"与"post-torch fence rejection"的计数断言，使 MODE-02 的两条边界都被真实覆盖。

---

## 已确认正确 / 值得肯定的改动

- 删除 `FS_DILOCO_ALLOW_LOGICAL_REPLACEMENT` 自授权通道（`learner_entrypoint.py`、`admission_v4.publish_static_request`），改为 operator 侧 `authorize_static_replacement` 显式记录 + `read_static_replacement_authorization()` 精确绑定 `(old_fence, new_logical_launch_id, new_attempt_id)`，并把 authorization SHA 写进 `replacement_reason`（`syncer_v4.py:415-421`）。这是对 H1 类越权替换的正确修法，PBS 里也有真实进程级验证。
- heartbeat 改为只从 `CommittedLeaderLease` 发布（`control_v4.py:54-72`、`authority.py:779-818`、`leader_lease.py:37-56`），消除了 publisher 侧本地时钟推导 lease 的分叉；`publish_heartbeat` 对已过期 lease fail closed，`renew_leader` 在同一事务内回读行。这修正了 base 版本 lease 语义的实质缺陷。
- `_read_anchored_regular_file()`（`control_v4.py:538-577`）用逐段 `dir_fd` + `O_NOFOLLOW` + 读后 inode 复核，配合从 `(epoch, owner, version)` **推导**（而不是信任）pointer 路径，彻底关掉了 head 指针逃逸；`_valid_latest_head` / `_valid_latest_payload` 的精确字段集与已发布 schema 完全一致（我逐字段核对了 `publish_latest`，18 个字段一一对应）。
- 迁移工具的 flock + 发布边界 CAS 复核 + create-no-replace（`os.link`）+ 失败时回滚 link，是对并发覆盖与部分可见的正确处理；三个新测试覆盖到位。
- `_existing_admission_resume()` 复用已发布的 resume 快照，使同 epoch 的 response 重放字节幂等——这是对 `failures.md` 里两次真实 runtime 失败的正确根因修复。
- 全仓 883 测试、ruff、`bash -n` 在 target commit 上均通过；焦点模块 67 个用例全绿；本次新增的 13 个用例质量高（含 nonfinite timestamp、extra-field + 匹配哈希、disposition 截断、发布失败不落 rejection 等负例）。

---

## 结论

**CHANGES_REQUIRED**

阻塞项：**H1、H2**（热路径不可读条目导致 leader 永久崩溃循环 / 一次瞬时读失败销毁合法注册，二者共用同一处修复），**M1、M2**（admission 发布路径的键设计导致 `FileExistsError` / `RuntimeError` 逃逸并卡死 leader），**M3**（跨 epoch 重判把已生效 admission 记为 rejected），**M5**（P4 phase gate 在 target commit 上 BLOCKED，且 MODE-02 未被该 phase 门覆盖、证据仍为 TBD）。

建议处置：H1/H2/M1/M2/M3 先各写 RED 回归再改实现（与本 phase 既有的 RED→GREEN 流程一致），M5 需要在最终 commit 上重跑并落一份 target-bound 证据、同时修正 MODE-02 的 phase/status/evidence 绑定。M4 与 L1–L8 可在本 phase 修复，或显式带入 P6 并在 plan 中记录责任人与理由。协议不变量改动后需重新冻结 target 并再做一次增量独立审核。
