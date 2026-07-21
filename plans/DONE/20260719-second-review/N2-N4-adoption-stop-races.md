# N2/N4：adoption 策略的 stop 竞态与收尾 reference 生命周期

## 1. 目标与完成定义

本计划统一修复两个同源问题：

- N2：predict 策略在 `on_cycle_end()` 等待 reconcile 时看到 stop，
  `wait_for_latest_if_newer()` 返回 `None`，却被当成真正 timeout 抛异常；
- N4：learner 已看到 stop、完成最后一份 partial proposal 后，predict/rebase 的
  `on_after_publish()` 仍创建马上丢弃的 prediction reference 或 rebase snapshot。

完成后，stop 必须是 adoption 状态机的一等正常输入：等待期间到达 stop 不抛
`TimeoutError`，predict state 被显式放弃；最后 proposal 发布后仍先做一次非阻塞 newer
latest 检查，有新版则直接采纳，无新版且 stop 在场则不创建任何新 reference。replace
策略行为保持不变。

## 2. 状态与优先级规格

```text
predict active
  ├─ newer latest → reconcile → inactive
  ├─ wait budget exhausted 且无 stop → TimeoutError，保留 state 供诊断
  └─ wait 返回 None 且 stop 存在 → abandoned_on_stop → inactive → 正常退出

after final publish
  ├─ newer latest 已可见 → direct adoption（即使 stop 同时存在）
  ├─ 无 newer latest + stop → skip reference creation
  └─ 无 newer latest + 无 stop → 按 predict/rebase 原语义创建 reference
```

`stop.json` 优先级只覆盖“None 的原因判断”和“是否创建未来才会使用的 reference”；
不得把所有 None 都吞成正常停止，也不得因 stop 存在跳过已经可见的 final global
adoption。runner 仍可发布当前 cycle 已完成训练产生的最后 proposal，syncer 的 shutdown
final ingestion/terminalization 负责处理它；本计划不改变该协议。

`PredictGlobalAdoptionStrategy.on_stop()` 应把 prediction state 清空，并保证
`global_prediction_abandoned_on_stop` 每个 active prediction 最多一次。rebase 若没有 active
anchor 无需额外 abandon 事件，但 stop 后不得新建 anchor。

## 3. 范围与非目标

范围内：`fs_diloco/runtime/adoption.py`、必要的 `runtime/learner.py` hook 接线、
`tests/test_adoption_strategy.py`、runner/真实 predict tiny 回归和对应 docs。

非目标：改变 prediction/rebase 数学、reconcile timeout 配置、删除 final partial
proposal、修改 syncer shutdown/terminal drain、对三种 adoption 策略做质量比较。

## 4. Requirement 与测试矩阵

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| AST-STOP-01 | predict active；fake wait 在等待中原子写 stop 并返回 None | 不抛异常；返回 no-op action；state inactive；abandoned 事件恰好一次 |
| AST-STOP-02 | predict active；wait 返回 None，stop 不存在 | 仍抛原 `TimeoutError`；state 保留；诊断事件不被折叠 |
| AST-STOP-03 | runner 在 cycle-end 初始检查后、wait 内收到 stop | 最终 `process_exit` exit=0/stopped；无 `error`；syncer shutdown 不超时 |
| AST-STOP-04 | predict after-publish：无 newer latest，stop 已存在 | `prepare_prediction` 调用数为 0；不创建 state；不 reset optimizer |
| AST-STOP-05 | rebase after-publish：无 newer latest，stop 已存在 | `snapshot_model` 调用数为 0；不保存 anchor |
| AST-STOP-06 | stop 与 newer latest 同时存在 | predict/rebase 均直接采纳新版；不创建 reference；版本与 tokens/reset 语义保持原契约 |
| AST-STOP-07 | stop 不存在的普通 after-publish | predict reference、rebase anchor 的现有事件与状态轨迹完全不变 |
| AST-STOP-08 | `global_only + predict` tiny run 达到最后 global target | learner 正常 stopped；syncer 完成 summary/finalize；无 error/shutdown timeout/残留 active payload |

## 5. Loop Engineering 实施循环

| Loop | SPECIFY / RED | IMPLEMENT / GREEN | HARDEN / CHECK / PERSIST |
| --- | --- | --- | --- |
| L0 竞态复现 | AST-STOP-01/03：让 fake wait 在调用中创建 stop；保存当前 TimeoutError 与缺失 stopped heartbeat 证据 | 无实现 | RED 是预期基线，不计三连败；冻结 stop/newer-latest 优先级 |
| L1 predict stop 消歧 | AST-STOP-01/02 先行 | None 后复检 stop；stop 分支复用单一 abandon 逻辑并清 state；无 stop 才 timeout | 正常 stop 与真 timeout 两条反例共同通过；事件只发一次 |
| L2 after-publish 收尾 | AST-STOP-04/05/06/07 先行，spies 统计重操作 | newer-latest poll/direct adopt 保留；仅在“无新版且无 stop”时 prepare/snapshot | 三策略事件/状态轨迹回归；显式断言重操作未调用 |
| L3 runner 集成 | AST-STOP-03/08 先 RED 或以当前代码复现 | 仅补必要 hook 接线，不在 runner 复制策略判断 | full predict tiny + replace/rebase smoke；终态目录、DB、heartbeat 人工复核 |

## 6. 验证与证据

登录节点执行 INDEX G1，并静态确认 stop 分支没有靠延长 timeout 实现。compute 节点先跑
adoption/learner 相关聚焦组，再跑全量 pytest 与真实 tiny。

AST-STOP-08 的通过证据必须同时包含：

- stop 发布与 reconcile wait 的事件顺序；
- learner `process_exit` 与最终 stopped heartbeat；
- syncer 无 `learner_shutdown_timeout`、`error`；
- summary/latest/DB version 一致；
- proposal payload 终态为零；
- plan-01 Checker `PASS`。

不需要 2 节点或 9 节点作为本计划门禁。若竞态只在真实并发下可复现，可追加一次
2 节点 debug，但不得以增加 wait/timeout 作为修复。

## 7. 报告与文档

报告目录：`reports/imp_plans/20260719-second-review/N2-N4-adoption-stop-races/`。
每条 progress 回引 AST ID；保存策略事件序列和 spy 调用计数，避免只记录“pytest passed”。

同步 `docs/03-runtime-flow.md` 与 `docs/modules/runtime-learner.md`：说明 reconcile wait
返回 None 的 stop/timeout 区分，以及 stop 后 after-publish 的“先看已可见新版、再跳过新
reference”语义。具体 tiny run ID 和耗时只写 reports。

## 8. 失败升级

同一竞态三连败后，在 `code_review.md` 逐步列出 runner stop 检查、strategy
`on_cycle_end`、wait helper、`before_publish/on_after_publish`、finally heartbeat 和 syncer
shutdown 的完整时序。必须检查测试是否真正让 stop 在 wait 内出现，不能只测试“调用前
已经有 stop”的较弱场景。

