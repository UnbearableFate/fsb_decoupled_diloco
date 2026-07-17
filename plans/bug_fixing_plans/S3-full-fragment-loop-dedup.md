# S3：full/fragment 双份收集循环与 adoption 块去重

## 1. 元信息

- 来源：review S3（中）。四处结构性重复：
  1. `collect_with_grace_window` vs `collect_fragment_with_grace_window`（syncer.py:725-882）逐行平行；
  2. `drop_missing_update_files` vs `drop_missing_fragment_update_files`（syncer.py:787-812）;
  3. full/fragment 两个 syncer 主循环共享约 70% 结构（selection→read→merge→outer→publish→mark→maintenance→metrics）；
  4. learner 侧 `run_fragment_learner` 内 fragment adoption 块出现三次（learner.py:1096-1132、1263-1306、1332-1381）。
  已被证实的代价：terminal drain 修在 full 循环，fragment 循环未同步获得（B4）。
- 性质：**行为保持重构**。fragment 循环的 drain 语义**不**在本轮启用。
- 影响文件：`fs_diloco/runtime/syncer.py`、`fs_diloco/runtime/learner.py` 及相应测试。
- 前置依赖：S2 的轨迹等价工具。与 S1 互相独立可并行；若 S1 已完成，第 4 项重复应基于策略类而非独立函数提炼（开工时确认并在 progress 记录选择）。

## 2. 目标与完成谓词

全部满足才可声明完成：

1. grace-window 收集与缺文件降级在 syncer.py 中各只有**一份**参数化实现；`collect_fragment_with_grace_window`、`drop_missing_fragment_update_files` 标识符删除（LDU-06 静态检查）。
2. input-closed / terminal-drain **判定谓词**提炼为与 full/fragment 无关的共享函数；full 循环行为不变，fragment 循环仍不接线（显式非目标，见 §3）。
3. learner 侧 fragment adoption 三块收敛为单一函数（LDU-03）。
4. full 与 fragment 各一个 tiny run 的归一化轨迹与基线等价（LDU-04/05）。
5. 全量 pytest 通过；一次 2 节点 fragment debug 冒烟通过（§7）。

## 3. 范围与非目标

- **范围内**：上述 1、2（谓词提炼部分）、4 的去重；参数化接口设计。
- **非目标**：
  - **B4 修复本身**（fragment 循环接入 input-closed 分支并获得 `input_exhausted` 停止）——这是语义变更，需要自己的 RED 测试与管线证据；本计划交付后 B4 应只剩"接线 + 测试"，作为独立小计划或 R2 条目执行；
  - 第 3 项重复（两个主循环骨架的完全统一）——收益/风险比最差，仅当 1、2 完成后骨架差异已缩小到一屏时才考虑，本轮明确不做；
  - E4（fragment 发现面 O(history) glob）——效率问题另行处理。
- 兼容性：无配置、无磁盘布局变化。

## 4. 规格

### 4.1 参数化收集接口（SPECIFY 阶段先做差异盘点）

L1 的第一步是对两对函数做**逐行 diff 并把全部差异点列成清单**（候选枚举方式、eligibility 判定、staleness 键、事件名前缀、降级动作），清单入 progress.md。设计约束：

- 共享骨架接受一个"提案源"参数对象（候选枚举 + 元数据读取 + 降级回调），full/fragment 各提供一个实例；
- 事件名与字段保持现状（轨迹等价是硬门槛），骨架不得为了统一而重命名事件；
- 若 diff 清单暴露两侧**无意的行为分歧**（同 S1 §10 的处理方式）：先记录，经确认后要么保持分歧（参数化表达出来），要么定义为 bug 另行修复，不得在重构中静默抹平。

### 4.2 input-closed 谓词

- 把 full 循环中"全部预期 learner 已 stopped 且最终 ingestion 完成"的判定提炼为纯函数（输入：learner 状态集合、配置），full 循环调用点行为不变；
- fragment 循环**不**调用该谓词（本轮），但函数签名不得依赖 full 专有结构，B4 接线时不应需要再改签名。

### 4.3 learner fragment adoption 函数

- 三块（inner poll / upload 后 / final wait）先 diff 盘点差异（事件字段、等待语义），收敛为单一函数 + 少量调用点参数；
- `reset_inner_optimizer_on_fragment_adopt` 语义与事件顺序不变。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 基线冻结 | 记录 commit；差异盘点：两对 syncer 函数与三块 learner adoption 的逐行 diff 清单 | 基线上跑 tiny full（`fs_diloco_tiny_local.yaml`）与 tiny fragment（`fs_diloco_tiny_fragment_local.yaml`）run | 轨迹 + diff 清单入 artifacts/progress |
| L1 收集/降级去重 | LDU-01/02 先 RED：grace 窗口到期、缺文件降级在 full/fragment 两源下行为逐项一致于 diff 清单 | 参数化骨架落地，删除 fragment 变体函数 | LDU-06 静态检查；相关单元测试（tests/test_syncer_selection.py 等）全通过 |
| L2 input-closed 谓词 | 谓词单元测试先行：full 现语义全组合 | 提炼纯函数，full 循环切换调用 | full tiny run 轨迹等价（LDU-04）；fragment 不接线的事实写入 progress |
| L3 learner adoption 去重 | LDU-03 先 RED：单函数在三种调用语境下事件与状态转换一致于 diff 清单 | 提炼函数，三处调用点改写 | fragment tiny run 轨迹等价（LDU-05）；`tests/test_fragment_pipeline_smoke.py` 通过 |
| L4 集成验证 | — | — | 全量 pytest；2 节点 fragment debug 冒烟（§7）；证据归档 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| LDU-01 | 参数化收集：full 源 | grace 到期、quorum 满足/不满足、staleness 边界行为与基线一致 |
| LDU-02 | 参数化收集：fragment 源 + 缺文件降级 | 候选缺 payload 时降级动作与事件与基线一致 |
| LDU-03 | fragment adoption 单函数 | 三种调用语境（inner poll/upload 后/final wait）事件与 optimizer reset 语义一致 |
| LDU-04 | full 轨迹等价 | tiny full run 归一化轨迹与基线一致 |
| LDU-05 | fragment 轨迹等价 | tiny fragment run 归一化轨迹与基线一致 |
| LDU-06 | 静态检查 | `collect_fragment_with_grace_window`、`drop_missing_fragment_update_files` 在 `fs_diloco/` 出现 0 次 |
| LDU-07 | input-closed 谓词单元 | full 语义全组合正确；签名不含 full 专有类型 |

progress.md 每条记录必须列出覆盖的 LDU ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、`git diff --check`、LDU-06 grep。
2. **1 节点 compute**：LDU 单元测试 → 全量 pytest → tiny full/fragment 轨迹等价。
3. **2 节点 compute**（建议保留）：`run_2node_fragment_debug.pbs` 一次冒烟——收集循环与跨节点提案可见性直接相关，这是本目录唯一建议 2 节点验证的计划。检查项：committed merge 数、DB/latest 一致、无 error 事件。
4. 9 节点：不需要。

## 8. 报告、证据与 Checker

- 报告目录：`reports/imp_plans/bug_fixing/S3/`，规则按 [plans/AGENTS.md](../AGENTS.md)。
- L0 的 diff 差异清单是本计划的关键 SPECIFY 产物：后续每个 LDU 测试都应能回指清单中的一行；清单中标注"疑似无意分歧"的条目必须在完成前逐条关闭（保持/立案）。
- tiny run 与 2 节点冒烟对 run 目录执行 `check_plan01_invariants.py`，维持 PASS。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。轨迹不等价的处理规则同 S1 §10：不得用放宽归一化 profile 吸收真实行为差异。

## 10. 文档同步

- 完成后在 review 报告 B4 条目处标注："S3 已交付共享 input-closed 谓词，B4 剩余工作 = fragment 循环接线 + drain 语义测试"，并把 B4 立为独立小计划（一页即可，语义变更性质，含 1-hour no_progress 场景的 RED 复现）。
