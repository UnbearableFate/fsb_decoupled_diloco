# bug_fix-B 索引

本目录为 `reports/20260717_plans_fs_diloco_review.md` 第 2 节（代码实现与逻辑问题）中的每个要点提供一份独立实施计划（B2 基于 DiLoCo 系文献调研后补入，吸收并取代 `plans/todo/cosine_scheduler_decoupling.md`）。计划风格遵循 `plans/ref/实施计划制定与 Agent 执行经验.md` §8 骨架与 §6.2 loop 循环，并落实报告第 4 节教训：显式完成谓词（P1）、基线 commit 与证据（P6）、稳定测试 ID 且 progress 回引（P8）、昂贵验证前先有 telemetry（P5）。

## 文件清单与推荐执行顺序

| 顺序 | 计划 | 一句话目标 | 性质 | 前置/关联 |
| --- | --- | --- | --- | --- |
| 1 | [B1-fragment-stop-json.md](B1-fragment-stop-json.md) | fragment learner 忽略 stop.json | 语义修复 | **委托给 [bug_fixing_plans/S4](../bug_fixing_plans/S4-stop-predicate-unification.md) 执行** |
| 2 | [B2-scheduler-decoupling.md](B2-scheduler-decoupling.md) | LR 调度解耦：累计步进度、独立 horizon、min_lr 下限 | **语义变更（高）** | 无；**阻塞所有后续质量消融**（quality_fix-Q/Q1 委托至此） |
| 3 | [B3-dead-config-fields.md](B3-dead-config-fields.md) | 删除三个无消费点的配置字段 | 配置变更 | 无（S5 的 L1 引用本计划） |
| 4 | [B5-maintenance-bounded-scan.md](B5-maintenance-bounded-scan.md) | maintenance 热路径去掉 O(history) 归档扫描 | 行为保持 + 补 BND 断言 | 无 |
| 5 | [B7-learner-syncer-watchdog.md](B7-learner-syncer-watchdog.md) | learner 对 syncer 死亡的对称 liveness 兜底 | 语义新增 | 无 |
| 6 | [B4-fragment-terminal-drain.md](B4-fragment-terminal-drain.md) | fragment syncer 接入 input-closed / terminal drain | 语义新增 | 建议先做 [S3](../bug_fixing_plans/S3-full-fragment-loop-dedup.md) |
| 7 | [B6-gc-race-load-helper.md](B6-gc-race-load-helper.md) | GC 竞态防护推广到全部权重加载点 | 语义加固 | 建议先做 [S2](../bug_fixing_plans/S2-prediction-reconcile-dedup.md) |
| 8 | [B8-shutdown-timeout-config.md](B8-shutdown-timeout-config.md) | learner shutdown 等待超时可配置化 | 配置化 | 无 |
| 9 | [B9-adaptive-eta-clock.md](B9-adaptive-eta-clock.md) | adaptive ETA 改单一时钟源 | 语义修正 | 无 |
| 10 | [B10-midcycle-adoption-metadata.md](B10-midcycle-adoption-metadata.md) | mid-cycle adoption 的 proposal 元数据标注 | 元数据新增 | 无 |

顺序依据：B1 严重级最高但已有执行载体（S4）；B2 是全部质量实验的前置，必须最先排期；B3/B5 是 review R0 立即修项；B7/B4 消除两类"整节点空烧/空等"风险；B6 是窗口性风险加固；B8/B9/B10 为低严重收尾。B4、B6 若在对应 S 计划之后执行，工作量显著缩小——INDEX 不强制，但两份计划内各自写明了两种起点的差异。

## 统一约束

- 记录规则以 [plans/AGENTS.md](../AGENTS.md) 为准；报告路径映射：`plans/bug_fix-B/<Bx>-*.md → reports/imp_plans/bug_fix-B/<Bx>/`。
- 开工前记录 `git rev-parse HEAD` 与 dirty 状态；语义变更计划必须在 progress 中声明"该变更前后的 run 不构成受控对照"（P6）。
- 登录节点只做静态检查；pytest、torch import、tiny pipeline 一律在 compute 节点执行。
- 除 B4 建议一次 2 节点冒烟外，全部计划验证阶梯止于 1 节点；**均不需要 9 节点作业**。
- 行为保持类改动的等价验收使用 S2 交付的事件轨迹等价工具（若 S2 未执行，等价验收退化为"关键事件序列人工对账"，需在 progress 记录）。
