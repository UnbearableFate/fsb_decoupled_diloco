# 2026-07-19 第二轮审查问题修复计划索引

## 1. 计划依据与当前基线

本计划集用于闭合
[`reports/20260719_fs_diloco_second_review.md`](../../../reports/20260719_fs_diloco_second_review.md)
第 2 节确认的 N1–N8。编写方式遵循
[`plans/ref/实施计划制定与 Agent 执行经验.md`](../../ref/实施计划制定与%20Agent%20执行经验.md)
的权威关系、稳定不变量、逐级门禁和 Loop Engineering 循环，并受
[`plans/AGENTS.md`](../../AGENTS.md) 的实施记录与三连败升级规则约束。

计划制定时对当前源码做了静态复核，N1–N8 所指实现仍存在；本轮只制定计划，
没有修改运行时代码或测试。工作树中已有与本计划无关的未提交改动，执行 agent
必须在 G0 重新记录并保留其所有权，不得为了获得干净基线而回退或覆盖。

## 2. 总体完成定义

只有以下条件全部满足，才可把本计划集从 `DOING` 移入 `DONE`：

1. N1–N8 均有稳定 requirement/test ID，且每个 ID 在对应 `progress.md` 中回引
   命令、环境与 artifact；
2. full DB-first resume 不会重放上一进程代际的 stopped 心跳，resume 后至少提交
   一个严格更大的 global version，且此前不发布 `input_exhausted`；
3. terminal drain 能区分“输入重新打开”“输入闭合且为空”“输入闭合且已选择”，
   full/fragment 均不把空列表歧义解释为正常终止；
4. predict reconcile 的 stop 竞态走正常退出，predict/rebase 在 stop 已出现时不再
   创建即将丢弃的 reference，同时仍允许采纳已经可见的新版；
5. `sync.ingest_during_publish=false` 时 publish-ingest 四个字段严格为零，历史污染
   口径已写入报告；
6. fragment final wait 期间 heartbeat 按配置间隔推进，不会把仍在收尾的 learner
   误观测为 dead；
7. terminal predecessor 的“checkpoint 已写、manifest 未写”崩溃窗口可幂等恢复，
   不会给错误内容提交 manifest；
8. payload 目录旧 metadata 清理分支和 input-closed 重复 discovery 已移除，相关
   静态约束、聚焦测试、全量测试与 tiny pipeline 全部通过；
9. 独立 Checker 输出 `PASS`；若仅正式长作业仍在运行，可输出
   `PASS_WITH_FOLLOWUPS`，但不得用它掩盖任何已知失败。

## 3. 权威链与共同不变量

```text
不可变 weight / outer / proposal payload
    ↓
SQLite committed version 与 proposal 状态（训练权威提交点）
    ↓
latest / heartbeat / stop 等固定路径控制文件（可验证、可重建或代际受限的视图）

terminal predecessor checkpoint
    ↓
manifest 原子发布（研究证据包提交点；不进入训练权威链）
```

| ID | 共同不变量 | 机检位置 |
| --- | --- | --- |
| INV-AUTH-01 | resume 版本只由 SQLite 最大 committed 行决定；latest 不能反向覆盖 DB | resume 单测、plan-01 Checker |
| INV-LIVE-01 | `learners.status=stopped` 只证明当前 syncer 代际输入闭合；上一代 DB 行和固定 heartbeat 不得证明本代闭合 | N1 单测、2 节点 resume 回归 |
| INV-DRAIN-01 | 只有 `closed_empty` 可产生 `input_exhausted`；`open` 即使 selected 为空也必须回到常规循环 | full/fragment 状态机测试 |
| INV-STOP-01 | stop 是正常控制信号；它优先于等待超时，且 stop 后不得创建新预测/重基 reference | adoption 策略测试、predict tiny |
| INV-METRIC-01 | `publish_ingest_passes` 只统计真实调用摄取 callback 的轮数；关闭开关时所有 publish-ingest 字段为 0 | publication 单测、tiny CSV |
| INV-EVID-01 | predecessor manifest 是证据提交点；manifest 存在后内容冲突 fail closed，manifest 前残留 checkpoint 可校验修复 | capture crash matrix |
| INV-BOUND-01 | 运行热路径不扫描 payload 历史 metadata；input-closed 每轮不重复常规 discovery | 静态搜索、调用计数测试 |

## 4. 文件清单与执行顺序

| 顺序 | 计划 | 覆盖问题 | 性质 | 前置关系 |
| --- | --- | --- | --- | --- |
| 1 | [N1-resume-liveness-and-terminal-state.md](N1-resume-liveness-and-terminal-state.md) | N1、N8 | 高严重度恢复语义 + 状态机消歧 | 阻塞独立重启实验；最先执行 |
| 2 | [N2-N4-adoption-stop-races.md](N2-N4-adoption-stop-races.md) | N2、N4 | 正常停止竞态 + 收尾成本修复 | N1 后执行，便于复用稳定 stop/terminal 语义 |
| 3 | [N3-publish-ingest-telemetry.md](N3-publish-ingest-telemetry.md) | N3 | telemetry 正确性 | 可独立；必须早于后续性能数据采集 |
| 4 | [N5-N6-maintenance-and-final-wait.md](N5-N6-maintenance-and-final-wait.md) | N5、N6 | 死分支清理 + liveness 观测修复 | N1 后执行，避免两批同时改 heartbeat 断言 |
| 5 | [N7-terminal-predecessor-recovery.md](N7-terminal-predecessor-recovery.md) | N7 | 研究证据崩溃恢复 | 独立；最后合并以降低与主训练链的耦合 |

N8 与 N1 同时实施，因为二者修改 full/fragment 的同一 input-closed 分支；拆成两次
改动会重复改写并重复证明 terminal state machine。N2 与 N4 同时实施，因为两者
共同约束 adoption hook 在 stop 到达后的生命周期。其余问题保持独立 loop，任何一组
失败都不应阻塞不相交的低风险修复。

## 5. 共同验证阶梯

### G0：范围、环境与证据初始化

- 读取根目录、`plans/AGENTS.md`、本索引与目标子计划全文；
- 记录 `git rev-parse HEAD`、`git status --short`、hostname、PBS 上下文和 Python 环境；
- 为每份计划创建对应 `reports/imp_plans/20260719-second-review/<plan-id>/`，包含
  `progress.md`、`failures.md`、`code_review.md`、`artifacts/`；
- 冻结上述 INV 映射与非目标；计划外 dirty 文件只读保留；
- 需要运行 Miyabi/PBS/torch/pytest 时按触发规则加载 `miyabi-development` skill；
  纯静态编辑阶段不加载。

通过条件：源码基线、所有权、运行位置和测试责任无歧义。

### G1：登录节点静态门禁

- `git diff --check`；
- `python -m compileall -q fs_diloco tests scripts/miyabi`；
- 各子计划列出的 `rg` 负向/正向检查；
- 若修改或准备提交任何 PBS 脚本，先执行 `bash -n scripts/miyabi/*.pbs`，并确认每个
  `#PBS -W group_list=` 都是有效字面 group ID；未满足不得提交。

登录节点不运行 pytest、torch/model import 或训练。

### G2：聚焦测试组

在 compute 节点按子计划 ID 分组运行正例、反例、竞态和 rollback 测试。预期 RED
只作为实现前复现 artifact，不计入三连败；实现后的意外失败必须先追加
`failures.md` 再修改。

### G3：全量与真实 tiny pipeline

- 全量 `pytest -q`；
- full replace、predict 各一次 tiny 2-process smoke；
- fragment 一次 tiny smoke；
- 检查非空日志、实际 metrics 行、DB/version 变化、stop/summary/latest 一致和无
  `error`/`no_progress_timeout`，不能只记录进程或 PBS 的退出码。

### G4：恢复与跨节点门禁

- 1 节点受控“提交后 syncer 失败 → learner watchdog stopped → 原地 resume”回归；
- 2 节点共享文件系统重复同一状态机，resume 后至少新增一次 commit；
- 在稳定 commit 边界运行扩展后的 `scripts/miyabi/check_plan01_invariants.py`，stdout
  仍只能是 `PASS`、`PASS_WITH_FOLLOWUPS` 或 `BLOCKED`，细节写 structured artifact。

### G5：最终复核与文档同步

- 重跑 G1、所有聚焦组和全量测试；
- 按每份子计划指定位置同步稳定语义到 `docs/`，把 run/job/耗时等事实写入
  `reports/`；
- 审核 N1–N8 requirement → implementation → test → artifact 的完整映射；
- 本计划不要求 9 节点作业。若后续确实以超过 50-local-step × 10-global-step 基线的
  9 节点实验验证这些改动，必须按仓库指令再同步相应文档与实验结果。

## 6. 报告、失败与停止规则

报告根目录固定为：

```text
reports/imp_plans/20260719-second-review/
├── N1-resume-liveness-and-terminal-state/
├── N2-N4-adoption-stop-races/
├── N3-publish-ingest-telemetry/
├── N5-N6-maintenance-and-final-wait/
└── N7-terminal-predecessor-recovery/
```

每个关联测试组全部通过后、进入下一 loop 前追加 `progress.md`。任何非预期失败先写
`failures.md`，明确事实、假设、根因候选和下一次证伪测试；同一 experiment 连续失败
三次后停止局部试错，在 `code_review.md` 完整重画输入、状态转换、持久化、恢复与输出
链路，完成审查前不得发起第四次同类实验。

外部阻塞、队列等待或在途长作业不自动构成 `BLOCKED`。只有相同阻塞连续三轮且已
穷尽安全替代时才升级；不得取消仍在运行的长作业，除非计划或用户另行授权。

## 7. 实施完成记录（2026-07-21）

N1–N8 已按本索引顺序完成。最终静态门禁、95 条聚焦组合、357 条全量测试、full/
predict/fragment tiny、fragment final-wait liveness 诊断和 2-node 原地 resume 均通过；
job `2421684.opbs` 的扩展 Checker 为 `PASS`。完整 requirement/evidence 映射见
[`reports/imp_plans/20260719-second-review/FINAL_AUDIT.md`](../../../reports/imp_plans/20260719-second-review/FINAL_AUDIT.md)。

本轮没有运行 9-node 作业；完成后将本计划目录整体移入 `plans/DONE/`。
