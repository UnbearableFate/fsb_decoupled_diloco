# B6：GC 竞态防护推广到全部权重加载点

## 1. 元信息

- 来源：review B6（中）。current-only GC 下 "读到 latest → 加载 weight 文件" 存在文件被新一轮 merge+GC 回收的竞态。`prepare_prediction_or_find_newer_latest`（learner.py:702-782）已为 prediction 实现了 bounded retry；其余加载点未防护：
  - `adopt_global`（learner.py:435-444，内部 `load_global_weights_into_model`）；
  - rebase 的 `load_global_weights_flat`（learner.py:488-493）；
  - resume/初次加载 `wait_for_json(paths.latest_json)` 后的 `adopt_global`（learner.py:1437-1443）——learner 启动慢于两次 merge 时可命中。
  当前窗口约一个 merge interval（~20s），风险低；adaptive grace 缩短 interval、存储变慢或 learner 停顿时窗口闭合。
- 性质：**语义加固**（FileNotFoundError 从直接崩溃变为有界重试后再崩溃）。正常路径行为不变。
- 影响文件：`fs_diloco/runtime/learner.py`、测试。
- 前置依赖：建议先完成 [bug_fixing_plans/S2](../bug_fixing_plans/S2-prediction-reconcile-dedup.md)（等价工具在手，且 prediction 路径已是可迁移形态）。若 S1 已完成，helper 挂到策略类共享的加载入口。

## 2. 规格：`load_or_refresh_latest` helper

单一通用入口，所有由可推进 latest pointer 引用的权重加载走它（full 与 fragment）：

- 输入：`paths`、当前持有的 `latest` 记录、加载回调（`load_global_weights_into_model` 或 `load_global_weights_flat` 的闭包）、重试策略（次数与等待上限，取值对齐 prediction 现实现的常量/配置，SPECIFY 时冻结）；
- 行为：加载回调抛 `FileNotFoundError` → 有界等待**更新的** latest（版本号 > 当前持有值）→ 用新 latest 重试加载；重试耗尽仍失败 → 抛出并保留原始异常链（fail-closed，不静默降级）；
- 返回：实际加载所用的 latest 记录（调用方据此更新 `last_loaded_global_version` 等状态——**竞态恢复后 adopt 的是更新的版本，调用方状态必须一致更新**，这是本 helper 语义的关键，也是过去各调用点手写时最容易漏的部分）；
- prediction 的 `prepare_prediction_or_find_newer_latest` 重构为该 helper 的调用方，其专属逻辑（预测分支选择、outer state 与 weight 必须属于同一 latest）保留在外层；
- rebase 路径注意：竞态恢复拿到更新版本时，rebase 数学（reference 与新 global 的差）语义不变——rebase 本就是"搬到更新的 global 上"；但事件中的版本字段必须反映实际加载版本。
- fragment 初次 materialize 与增量 adoption 同样存在竞态：同一 fragment 推进后旧片文件可被 GC。helper 需以 `global_merge_event` 判新旧并对整份 latest 重新执行加载，禁止只替换单个缺失片而混用两个 latest 快照。

## 3. 目标与完成谓词

1. full 的 direct adoption/rebase/初次加载，以及 fragment 的初次 materialize/增量 adoption 全部经由 helper；GCR-05 以调用点清单与针对每类入口的注入测试为准，不使用容易被 callback 定义误伤的纯文本“只出现一次”断言；
2. 竞态注入测试通过：加载时文件缺失、更新 latest 随后出现 → adoption 成功且版本状态一致（GCR-02）；无更新 latest → 有界超时后抛原始异常（GCR-03）；
3. prediction 路径重构后行为等价（GCR-04：tiny predict run 轨迹与基线等价；S2 的 REC 矩阵回归通过）；
4. 正常路径无回归（GCR-01：tiny replace/rebase run 轨迹等价）；
5. 全量 pytest 通过。

## 4. 范围与非目标

- **范围内**：helper、全部已盘点的 full/fragment latest 权重加载点接入、prediction 路径重构为调用方、竞态注入测试。
- **非目标**：
  - GC 侧的发布宽限调整（GC 语义不动，防护做在读侧）；
  - fragment proposal payload 的 syncer 读取竞态（已有 selected/reset/drop 处理，协议不同）；fragment learner 的 latest 权重加载明确在范围内；
  - 加载性能优化（`load_global_weights_into_model` 的流式语义保持）。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 盘点与基线 | 枚举全部权重加载调用点（含 fragment 侧）成清单：已防护/未防护/排除理由；冻结重试参数来源；基线 commit + 三策略 tiny run 轨迹 | 无实现 | 清单入 progress.md |
| L1 helper 单元 | GCR-02/03 先 RED：假 run 目录 + 缺失文件 + 延迟出现的新 latest（注入时钟/轮询间隔） | helper 实现 | 单元全绿；异常链保留断言 |
| L2 full/fragment 接入 | 各点竞态注入测试先 RED（当前直接 FileNotFoundError 崩溃即 RED 证据） | adopt_global、rebase、resume 初次加载、fragment 初次/增量加载接入 helper | GCR-01/06/07；全量 pytest |
| L3 prediction 收编 | REC 矩阵作为回归护栏 | `prepare_prediction_or_find_newer_latest` 改为调用方 | GCR-04 轨迹等价；GCR-05 静态检查；S2 REC 全绿 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| GCR-01 | 正常路径无回归 | replace/rebase tiny run 轨迹与基线等价 |
| GCR-02 | 竞态恢复 | 文件缺失 + 更新 latest 出现 → 加载成功；返回的版本 = 实际加载版本；调用方状态一致 |
| GCR-03 | 重试耗尽 | 无更新 latest → 在界内抛出，异常链含原始 FileNotFoundError 与重试统计 |
| GCR-04 | prediction 等价 | tiny predict run 轨迹与基线等价；REC 矩阵通过 |
| GCR-05 | 静态检查 | 调用点清单中的每类入口均有 helper 接入和注入测试；callback 内的实际 tensor load 不被误判为绕过 |
| GCR-06 | resume 场景 | 初次加载时 latest 指向已 GC 文件、新 latest 出现 → learner 正常启动 |
| GCR-07 | fragment 一致快照 | 初次或增量片加载中缺文件且 fragment latest 前进 → 整份新 latest 重试成功，返回的各片版本与实际模型来源一致 |

progress.md 每条记录必须列出覆盖的 GCR ID（P8）。

## 7. 验证阶梯

1. **登录节点**：GCR-05 grep、lint。
2. **1 节点 compute**：GCR 单元 → 全量 pytest → 三策略 tiny run 轨迹等价 → GCR-06 集成。
3. 2/9 节点：不需要。竞态本身用注入复现，不依赖真实多节点时序。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B6/`。关键证据：L0 调用点清单（含 fragment 侧结论）、GCR-02/03 注入日志、轨迹对比输出。无 Checker 变更。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 L3 中 prediction 行为无法在保持等价的前提下收编（说明其重试语义与通用 helper 有真实差异），停止收编、保留两条路径，并把差异写回 review 报告勘误——不得为了统一而改变 prediction 已验证的行为。

## 10. 文档同步

- docs 中 current-only GC 章节补"读侧竞态防护"语义（所有全量加载可容忍一次 GC 回收并前滚到更新版本）；
- review 报告 B6 条目与 S2 计划的"缝"声明标注闭合 commit。
