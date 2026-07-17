# bug_fixing_plans 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 3 节（设计模式与代码结构，S1–S5）中的每个要点提供一份独立实施计划。计划风格遵循 `plans/ref/实施计划制定与 Agent 执行经验.md`（下称"经验文档"）§8 的骨架与 §6.2 的 loop 循环，并吸收了同报告第 4 节的教训：每份计划都显式声明完成谓词（P1）、基线 commit 与等价证据（P6）、稳定测试 ID 且 progress 记录必须回引 ID（P8）。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置 |
| --- | --- | --- | --- | --- |
| 1 | [S2-prediction-reconcile-dedup.md](S2-prediction-reconcile-dedup.md) | 消除 prediction reconcile 双拷贝；交付事件轨迹等价工具 | 行为保持 | 无 |
| 2 | [S4-stop-predicate-unification.md](S4-stop-predicate-unification.md) | 合并 `stop_requested`/`fragment_stop_requested`，同时修复 B1 | **含语义变更** | S2（轨迹工具） |
| 3 | [S1-adoption-strategy-refactor.md](S1-adoption-strategy-refactor.md) | `run_learner` 三策略提炼为 `GlobalAdoptionStrategy` 类 | 行为保持 | S2 |
| 4 | [S3-full-fragment-loop-dedup.md](S3-full-fragment-loop-dedup.md) | full/fragment 双份收集循环与 adoption 块去重 | 行为保持 | S2（工具） |
| 5 | [S5-config-strategy-grouping.md](S5-config-strategy-grouping.md) | 配置按策略分组、校验挪入策略类，删除死字段（B3） | **含不兼容配置变更** | S1 |

顺序依据：S2 先交付所有后续计划共享的等价性基础设施并降低 S1 风险；随后 S4 以该工具验证未改动的 full 路径并尽早修复确认 bug；S5 依赖 S1 的策略类存在。S3 与 S1 互相独立，但报告与 run 对照应分开。

## 实施状态（2026-07-17）

| 计划 | 状态 | 主要实现 commit | 验证记录 commit |
| --- | --- | --- | --- |
| S2 | 完成 | `19414a1` | `19414a1` |
| S4 | 完成 | `dab45e8` | `67b6da2` |
| S1 | 完成 | `4ce7262`, `f2c6961` | `1fedd4a` |
| S3 | 完成 | `777e913` | `77edc08` |
| S5 | 完成 | `c53f14d`（另含验证中发现的 maintenance race 修复 `a0eebcc`） | `04cf478` |

各计划的 RED/GREEN、tiny run、Checker 与失败诊断见 `reports/imp_plans/bug_fixing/<Sx>/`。S3 明确不修复的 B4 terminal-drain 语义变更已拆到 `plans/followups/B4-fragment-terminal-drain.md`，不属于本索引五项的未完成工作。

## 统一约束

- **记录规则**以 [plans/AGENTS.md](../AGENTS.md) 为准；经验文档为背景参考；冲突时 AGENTS.md 优先。
- 报告路径映射：`plans/bug_fixing_plans/<Sx>-*.md → reports/imp_plans/bug_fixing/<Sx>/`（progress.md / failures.md / code_review.md / artifacts/，规则同 AGENTS.md）。
- 除非计划明确标注"语义变更"，一律为**行为保持重构**：确定性路径以基线 commit 上的 tiny run 事件轨迹为等价基准（工具由 S2 交付）；异步 full/fragment 管线必须先做同代码重复运行，只有可重复的投影才可作为硬门禁，其余以受控测试和终态不变量为权威证据。
- 开工前记录 `git rev-parse HEAD` 与 dirty 状态；等价对照的两次 run 必须各自记录 commit。
- 登录节点只做静态检查；pytest、torch import 与 tiny pipeline 一律在 compute 节点执行。
- 本目录全部计划**不需要 9 节点验证**；验证阶梯止于 1 节点（S3 建议加一次 2 节点）。
