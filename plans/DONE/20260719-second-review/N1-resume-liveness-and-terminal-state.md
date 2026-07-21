# N1/N8：resume liveness 代际隔离与 terminal 状态机消歧

## 1. 问题、决策与完成定义

当前 `resume_run()` 会回滚 selected、重建 latest，却保留上一进程代际的
`learners.status=stopped` 和固定 heartbeat 文件。full 主循环第一次
`sync_liveness_and_metadata()` 后即可把输入判为 closed；terminal selector 又以空列表
同时表示“输入已重新打开”和“输入闭合但无 proposal”，调用方因此错误发布
`input_exhausted`。full/fragment 在 input-closed 分支还会额外执行一遍结果不使用的常规
discovery（N8）。

本计划采用两项明确设计：

1. **resume heartbeat fence**：不删除固定 heartbeat，也不单靠跨节点 wall-clock
   大小比较；resume 事务记录当时每个 heartbeat pointer 的内容签名，并把 DB learner
   行重置为本代 `unknown`。摄取层忽略与 fence 完全相同的旧 pointer；learner 原子替换
   为任何新 heartbeat 后即被接受。这样避免 unlink 与并发 rename 竞态，也避免节点时钟
   轻微偏差。
2. **显式 terminal decision**：selector 返回带状态的结果，而不是裸 list。状态至少为
   `open`、`closed_empty`、`closed_selected`；只有 `closed_empty` 能停止。

长期的 learner/syncer incarnation token 与 fragment resume 不在本计划范围内。若恢复
目标已经存在一套有效 `stop.json + summary.json` 完成证据，本计划不得静默删除 stop
并继续训练；保持 fail closed，另立“重开已完成 run”的协议计划。

完成定义：同一 shared root 上先形成旧代 stopped DB/heartbeat，再执行 full resume；
主循环首轮不得发布 stop，必须在新代 active heartbeat 出现后进入常规 quorum 路径，
并至少从权威版本 `vN` 提交到 `vN+1`。

## 2. 权威关系与恢复提交点

```text
SQLite max committed global row ───────→ resume 的模型/outer/版本权威
旧代 learner DB 行 + heartbeat pointer ─→ 仅作为 fence 输入，不证明本代 input closed
resume preparation transaction ────────→ selected 回滚 + learner reset + fence/run_state
新 heartbeat 原子替换 ─────────────────→ 本代 learner liveness 的可摄取事实
```

建议在 `SQLiteStore` 提供单一 `prepare_full_resume(...)` 事务接口，原子完成：

- `selected → pending`；
- 预期 learner 行改为 `status=unknown`、`status_reason=resumed`，清空本代含义的
  `last_seen/last_heartbeat_path/pid/hostname`；历史 proposal 与 committed version 不动；
- 在 `run_state` 写入本次 resume ID、时间、旧 heartbeat 内容签名映射；
- 返回 reset 数与 fence，供日志和当前 syncer 进程使用。

事务提交是 resume liveness 切代点。事务前崩溃仍属于旧代；事务后崩溃允许重复
resume，新一次事务生成新 fence，不能重复应用 selected。latest 仍在事务之后由 DB
重建；latest 写失败不回滚 DB，下一次 resume 必须可再次修复。

heartbeat fence 只匹配 resume 切代时完整读取到的固定 pointer 内容。实现必须使用
稳定的内容摘要或等价的完整字段签名，不能只匹配文件名、mtime 或 learner ID。未通过
JSON/run/learner 校验的文件不进入 fence，也不能被摄取。

## 3. terminal decision 状态机

定义不可歧义的返回对象（名称可在实现时调整，语义不得改变）：

```text
TerminalDrainDecision
├── state = open             selected = []
├── state = closed_empty     selected = []
└── state = closed_selected  selected = [1..quorum_max]
```

full 与 fragment 循环按同一顺序执行：

1. 常规摄取后计算 input-closed；
2. 首次 closed 时执行一次 terminal grace；
3. grace 后再次摄取并**重新判定** input-closed；
4. 若变为 `open`，清除/重置本次 `terminal_grace_complete`，回到常规 discovery；未来
   真正闭合时必须重新获得完整 grace；
5. 若仍 closed，按既有 future/staleness/每 learner 一个/quorum_max 规则选择；
6. `closed_selected` 才 merge，允许低于 quorum_min；`closed_empty` 才发布
   `input_exhausted`；
7. input-closed 分支不再执行第二遍常规 `eligible_updates → drop_missing →
   select_one_per_learner`。这同时闭合 N8。

sticky stopped 语义只在同一 resume 代际内保留；本计划不把 dead/stale 当 stopped，
也不放宽 terminal drain 的 future/staleness 规则。

## 4. 范围与影响文件

范围内：

- `fs_diloco/storage/sqlite_store.py`：原子 resume preparation；
- `fs_diloco/protocol/liveness.py`：heartbeat fence 过滤；
- `fs_diloco/runtime/syncer.py`：resume 接线、typed terminal decision、full/fragment 分支
  重排和 N8 重复 discovery 删除；
- `tests/test_resume.py`、`tests/test_liveness.py`、`tests/test_syncer_selection.py`、
  必要的 full/fragment pipeline 测试；
- `scripts/miyabi/check_plan01_invariants.py` 与可复用的 resume 回归 launcher；
- resume、terminal drain 相关 docs。

非目标：fragment resume、重开已完成 run、修改 quorum/selection policy、把 heartbeat
提升为训练权威、按 PID 杀旧进程、9 节点性能或质量实验。

## 5. Requirement 与测试矩阵

| ID | RED/检查 | 通过条件 |
| --- | --- | --- |
| RSM-01 | DB 中全部预期 learner 为 stopped，固定 heartbeat 也是旧 stopped；调用 resume preparation | expected learner 全变 unknown；selected 全 pending；committed rows 不变；fence 完整持久化 |
| RSM-02 | resume 后反复摄取未变化的旧 heartbeat | 旧 heartbeat 被计为 fenced/ignored，不覆盖 unknown；`all_expected_learners_stopped == false` |
| RSM-03 | fence 后 pointer 原子替换为新 active，再替换为新 stopped | active 被摄取并打开输入；同代 stopped 最终仍保持 sticky 并可闭合 |
| RSM-04 | resume preparation 在事务前、事务内、事务后/latest 前注入失败，再重复 resume | 只能观察旧代或完整新代；无部分 reset；selected 不重复应用；latest 可修复 |
| RSM-05 | terminal grace 中由 stopped 变 active | selector 返回 `open`，不产生 stop；grace 状态复位；常规 quorum 路径继续 |
| RSM-06 | 输入始终 closed 且无合法 proposal | full/fragment 均返回 `closed_empty`，且只在该状态发布一次 `input_exhausted` |
| RSM-07 | 输入 closed 且有低 quorum/future/stale/缺文件混合 proposal | 只选择合法集合；返回 `closed_selected`；低 quorum 可 merge；非法项不被绕过 |
| RSM-08 | input-closed 分支调用计数 | full/fragment 每轮只执行 terminal selector 所需 discovery，不再执行未使用的常规 discovery |
| RSM-09 | 真实 full `vN` 崩溃/看门狗停止后原地 resume | stop 不先于新 commit；DB/latest 到 `vN+1`；新代 learner active→stopped 生命周期完整 |
| RSM-10 | 已有一致 stop+summary 的 completed run 尝试 resume | fail closed 且错误可操作；不得删除终态证据或继续训练 |

RSM-10 若当前产品契约明确禁止 resume completed run，应只补契约测试与文档；不得顺手
增加“force reopen”配置。

## 6. Loop Engineering 实施循环

| Loop | SPECIFY / RED | IMPLEMENT / GREEN | HARDEN / CHECK / PERSIST |
| --- | --- | --- | --- |
| L0 基线复现 | 构造 RSM-01/02/05：保存当前空列表误停事件轨迹与 DB/heartbeat 快照 | 不改实现 | RED 输出、旧代文件签名、权威 vN 入 artifacts；冻结 completed-run 边界 |
| L1 resume 原子切代 | RSM-01/04 先 RED，断言事务内不得部分更新 | 实现 `prepare_full_resume`、run_state fence 和结构化 `run_resumed` 字段 | 重开 DB、重复 resume、integrity/selected/committed 对账；记录 progress |
| L2 heartbeat fence | RSM-02/03 先 RED；含损坏 JSON、错误 run_id、缺文件和 atomic replacement | 摄取接口加入 fence；旧签名跳过，新签名正常 upsert | liveness 全真值表、sticky stopped 同代回归；不得用 unlink 消除测试 |
| L3 terminal 消歧 | RSM-05/06/07 先 RED | full/fragment selector 返回显式 decision；grace 后重算 closure；open 路径复位 | full/fragment 对称状态轨迹；future/stale/缺文件/低 quorum 组合全绿 |
| L4 删除重复 discovery | RSM-08 先用 spy 证明当前 closed 分支调用两次 | 把常规 discovery 完整放入 open 分支 | 调用数与事件序列稳定；`rg` 确认无第二条 closed 热路径 |
| L5 真实 resume | RSM-09/10；先 1 节点再 2 节点 | 只修复真实 pipeline 暴露的协议缺口，不放宽断言 | Checker、DB/latest/log/目录人工对账；完整命令与 job/run ID 入 progress |

## 7. 验证阶梯与 Checker

1. 登录节点：INDEX G1，加静态搜索确认旧裸-list terminal 契约已消失；
2. compute 聚焦组：`test_resume.py`、`test_liveness.py`、`test_syncer_selection.py` 及
   full/fragment terminal pipeline；
3. compute 全量：`pytest -q`；
4. 1 节点：受控 crash/watchdog/resume，两代事件必须可从日志区分；
5. 2 节点：共享 DB/heartbeat 路径上重复 RSM-09，至少观察 `vN+1`；
6. Checker 增加可选 resume-progress 模式，验证：
   - `run_resumed(vN)` 后出现 `outer_step_applied/global_published(v>N)`；
   - 二者之间没有 `input_exhausted/stop_published/error`；
   - DB/latest/checkpoint 一致，selected 无重复 applied，旧 fence 不计作本代 stopped；
   - stdout 契约不变，版本与事件细节写 JSON artifact。

若新增/修改 PBS，提交前必须 `bash -n scripts/miyabi/*.pbs` 并确认 literal group ID。
PBS `R` 或 `Exit_status=0` 不是通过证据；日志须非空，并含 phase A 的 vN、watchdog
stopped、phase B 的 resume 和 vN+1 commit。

## 8. 报告与文档同步

报告目录：
`reports/imp_plans/20260719-second-review/N1-resume-liveness-and-terminal-state/`。
RSM-09 的 structured artifact 至少含 baseline commit/fingerprint、run ID、job ID、shared
root、旧/新 heartbeat 签名、resume transaction 字段、vN/vN+1、stop/event 顺序和
Checker 结果。

稳定语义同步到：

- `docs/02-architecture.md`：stopped 的代际边界；
- `docs/03-runtime-flow.md`：resume preparation 与三态 terminal decision；
- `docs/04-data-flow.md`：heartbeat fence 为恢复辅助状态，不是训练权威；
- `docs/07-operations.md`、`docs/modules/runtime-syncer.md`、
  `docs/modules/storage.md`：操作步骤、completed-run fail-closed 与 store 接口。

## 9. 停止与升级规则

同一 RSM experiment 三连败后停止修改 timeout、sleep 或 grace；在 `code_review.md`
重画 heartbeat publication、DB upsert、liveness classification、terminal grace、selector、
stop publication 全链，并至少比较“内容 fence”“timestamp watermark”“显式 incarnation
token”三种方案。没有证据不得把失败归因于共享文件系统。

