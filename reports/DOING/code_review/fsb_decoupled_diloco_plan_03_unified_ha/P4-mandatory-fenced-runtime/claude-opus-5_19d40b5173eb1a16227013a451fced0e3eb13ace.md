# Plan 03 / P4-mandatory-fenced-runtime 独立代码评审报告

## 1. 评审元信息

| 项 | 值 |
|---|---|
| 仓库 | `/work/xg24i002/x10041/fsb_decoupled_diloco` |
| Commit range | `d18fae055b5beec1887f38c3f2070f0bf6ec901b..19d40b5173eb1a16227013a451fced0e3eb13ace` |
| Range 内提交 | `352318f` → `27f1a03` → `07d7ea1` → `19d40b5` |
| Base 关系 | `d18fae0` 为 `19d40b5` 的祖先 |
| 评审范围 | 完整 diff + P4 目标态（protocol/runtime/storage 生产码、Checker、tests、PBS、需求矩阵、证据与 finding disposition） |
| 实际模型 | **claude-opus-5**（Claude Opus 5） |
| Session | `175cc4fb-6814-4917-b567-1d712b8a6bcf` |
| 独立性 | 未读取本目标已有 Codex/Claude review report；仅按任务范围检查了 finding disposition artifact，结论由源码和证据独立推导 |
| 运行约束 | 仅源码审阅、Git 元数据和证据摘要静态检查，未在登录节点运行 pytest、脚本或实验 |

**最终结论：CHANGES_REQUIRED**

## 2. 严重度分组的可执行发现

### 【高】H-1 static contributor 缺少运行中 liveness/terminal 通路，§9.2/§9.4 的“PBS rerun 复用 logical ID 取得新 generation”在生产路径上不成立

证据：

- `fs_diloco/storage/authority.py:978-985`：当前 row 为 `active` 且请求带新 attempt 时，`bind_or_replace_static_attempt` 要求 `expected_generation` 和 `replacement_reason`。
- `fs_diloco/runtime/syncer_v4.py:449-465`：`replacement_reason` 只能来自 operator authorization 文件。
- `fs_diloco/storage/authority.py:1048` 的 `mark_static_attempt_terminal()` 无生产调用者，只有测试引用。
- `static_contributor_bindings` 其余 terminal 写点位于 run terminal-close 路径，不是运行中 lost 检测。
- `scripts/miyabi/run_v4_allocation.sh:132-143` 每 rank 只启动一次 learner，无重启循环；P4 static rerun PBS 必须先调用 `authorize_static_replacement`。

与计划文本的冲突：§9.2 把“同 logical ID rerun”与“新 logical job 显式 replacement”分开，§9.4 要求 static learner 同 logical launch rerun 恢复；实现中 active 同-logical rerun 也需要人工授权。

影响：static learner 中途崩溃后 binding 保持 active，自动重启不能获得新 generation，只能由 operator 显式授权；这使当前实现与 gate 文本表达的自动恢复预期不一致。

修复建议（二选一）：

1. 基于可靠的 learner/scheduler terminal evidence 调用现有 fenced `mark_static_attempt_terminal()`，随后允许同 logical ID 新 attempt 自动 generation+1；或
2. 如果“任何 active attempt rerun 都必须人工授权”是安全策略，修订 §9.2/§9.4 和需求矩阵，明确 paused/stale process 故障模型下不能仅凭 heartbeat 超时自动替换，并为 operator-authorized same-logical rerun 留证。

缺失测试：没有“production terminal evidence → same logical new attempt generation+1”的集成测试，也没有生产调用者/显式 disposition 的架构检查。

### 【中】M-1 learner 回读刚发布的 request 文件计算 sha256，与 leader 归档存在竞态

`fs_diloco/runtime/learner_entrypoint.py:113-114` 在请求发布后通过 `request_path.read_bytes()` 重读并计算 SHA，而 publish API 只返回路径。syncer 可在发布与回读之间完成 admission 并通过 `_remove_hot_request()` 删除热文件，导致 learner 在已经被 authority admit 后因 `FileNotFoundError` 退出。static 下这还会留下 active binding，dynamic 下会暂时占用 stream。

修复建议：publish API 返回 path 与内存 payload/摘要，entrypoint 不再回读热文件；或对 `FileNotFoundError` 转入轮询补偿。增加“发布后立即被归档”的竞态测试。

### 【中】M-2 `_admit_requests` 的 deferral 处理器过宽，吞掉 lease/fencing 与不变式违例信号

`fs_diloco/runtime/syncer_v4.py:370-378` 捕获所有 `(OSError, RuntimeError)` 并降级为 `admission_request_deferred`。这包含 `StaleLeaderTokenError`、`AuthoritySchemaError` 和 `syncer_v4.py:556` 的“command returned a fence that is no longer current”不变式违例。

影响：事务内 token fence 仍防止双写，后续 renew/select 也可能使旧 leader 退出，但关键 fencing/schema 故障被稀释并静默重试。

修复建议：只捕获明确可重试的 filesystem/publication 错误，令 stale token、schema error 和不变式违例传播。增加注入 `StaleLeaderTokenError` 必须传播的负例。

### 【中】M-3 永久不可读 hot entry 从不隔离，导致每轮 append+fsync 的无界遥测增长

- `syncer_v4.py:361-369` 对 unreadable observation 每轮只写 `admission_request_deferred` 后继续，无隔离、计数或退避。
- `_read_hot_request` 对目录/FIFO等非普通文件永久返回 OSError。
- `ActorTelemetryWriter.event` 每次 append 并 fsync。
- 新测试反向固化了 poison 目录永久保留。

修复建议：按路径/观察指纹做有界诊断与指数退避；在安全分类后移出热发现根，或者在 P6 G6 明确证明事件/元数据操作的有界性。增加 N 轮扫描下事件数远小于 N 的测试。

### 【中】M-4 跨 epoch 重放 rejected disposition 不在当前 epoch 重新发布 rejection，learner 无法观测终态

`_validate_admission_disposition` 用 disposition 内旧 epoch/owner 的 rejection 校验；新 leader 在发现 disposition 后直接归档热请求，不在当前 epoch 发布 rejection；learner 却只查询 current epoch rejection。因此 epoch 1 写 disposition/rejection、归档前崩溃后，epoch 2 会删除热请求，而 learner 只能超时。

修复建议：当前 leader 验证旧 rejection 后，在自己的 epoch 下幂等发布等价 request-specific rejection 再归档；补充与 admitted replay 对称的 rejected 跨 epoch 测试，并断言 learner 在新 epoch 看到终态。

### 【中】M-5 static 快路径 `binding = prior` 绕过 fenced command，却仍执行全局副作用

`syncer_v4.py:441-447` 在当前 binding 的 logical/attempt 相同时直接使用只读 `prior`，完全跳过 `LeaderSession._command` 的 token 校验和 `expected_generation` 校验，随后仍发布全局 disposition 并删除 hot request。

失败场景：被 fence 的旧 leader 恢复后命中此路径，可写旧 epoch disposition并删除全局热请求。该快路径也让 fresh request 重用当前 attempt ID 时绕过 generation fence。

修复建议：只有在 exact content-addressed command record 已提交且结果等于 current binding 时才走恢复路径，并先验证 current token；否则调用正常 fenced command。增加 stale-token 不得发布 disposition/删除 hot request，以及 same-attempt fresh request 被拒绝的测试。

### 【低】L-1 `_read_hot_request` 无字节上限

`admission_v4.py:962-983` 无界读取 learner 可写的请求；协议层已有 `DEFAULT_MAX_JSON_BYTES` 但未使用。本项已在 disposition 中合理延期到 P6/G6，不作为本次单独阻塞项；P6 必须完成 size limit、流式硬上限、bounded diagnostic 和边界测试。

### 【低】L-2 §9.4 的“CUDA allocation counter 为 0”缺少直接证据

P4 PBS 将 `CUDA_VISIBLE_DEVICES` 置空，仓库无 CUDA allocation counter。现有 gate 只用 pre-torch import sentinel；逻辑上它是更早的强条件，但计划文字明确列出 counter。建议在可见 GPU 下直接验证，或修订计划说明 import sentinel 是 production torch/CUDA allocation 的证明口径。

### 【低】L-3 Checker 证据等价性判定的语义问题

`scripts/miyabi/check_plan03.py:442-473` 的祖先 + 相关树无 diff 设计合理，但：

1. 缺少顶层 `source_commit` 时回退到 `checks.current_migration_boundaries.source_commit`，后者是冻结边界 ref 而非证据产出 commit；
2. 不拒绝 `git_dirty=true` 的 runtime evidence；
3. 相关树遗漏根目录 `main.py`。

建议只接受显式 evidence source commit，要求 structured runtime evidence 明确为 clean，并补入根 shim；增加 dirty evidence 必须 BLOCKED 的测试。

### 【低】L-4 `_repair_current_admission_controls` 每轮为每个 contributor 重发不可变对象

每轮都执行临时写、fsync、link 冲突和既有文件重哈希。建议只在 epoch 初始化、pointer/response 缺失或失配时 repair，或缓存本 epoch 已修复 fence。该项可与 P6 G6 合并关闭。

### 【低】L-5 resume 字段去掉 `int()` 后 admission 层不再做类型校验

`_decode_admission_response_control` 和 `_existing_admission_resume` 直接透传字段，`ContributorResumeState` 无 `__post_init__`。类型错误会较晚在 cursor 使用处失败。建议做 strict type validation 但不转换，以兼顾原字节幂等与早失败。

### 【低】L-6 admission request SHA/command ID 变更不兼容进行中 run root

SHA canonicalization 加入 newline，command ID 去掉 epoch。P4 cutover 不承诺在途升级，因此影响有限；应在 P5 compatibility 文档登记。

## 3. 正面观察

- response path 加入完整 fence namespace，rejection path 加入 request SHA，修复 immutable collision；attempt-ID 复用与多 rejection 测试覆盖良好。
- `27f1a03` 的“未处理请求保持 pending”语义正确，并仍在第二次 admission 校验时 fail closed。
- valid history 的 canonical bytes 与 digest 同源。
- disposition 重放与 consumer 复用严格 response/rejection decoder，并验证 exact current pointer/response SHA。
- runtime `assert` 已替换为显式分支。
- 稳定跨 epoch command ID 与 admitted replay 回归覆盖了 disposition 前崩溃恢复。
- 6 份最终 PBS evidence 的 stdout SHA、clean target identity、exit status 和 walltime 记录可复核；`27f1a03..19d40b5` 的相关源码树无差异。
- failure artifacts、MODE-02 P4 ownership、tracked evidence 与 requirement binding 记录完整。

## 4. 最终裁决

**CHANGES_REQUIRED**

阻塞项：H-1 必须通过安全的 terminal-evidence实现或明确修订计划/矩阵契约。M-1、M-2、M-4、M-5 直接涉及 admission/fencing/recovery边界，建议同轮修复并补 RED/GREEN。M-3/L-1/L-4 应绑定 P6 G6 的有界资源验收；其余 Low 可在 P5/P6 收敛。
