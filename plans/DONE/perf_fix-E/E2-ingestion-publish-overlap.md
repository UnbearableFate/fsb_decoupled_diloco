# E2：事件化 ingestion 与发布流水线化

## 1. 元信息

- 来源：review E2（中）。runs B–H 的系列证据表明 supersession/staleness 的结构性根因是节拍失配：syncer 按"收齐一批→合并→发布"节拍，learner 按自身 cycle 节拍，相位差由 grace 吸收，吸收不了即浪费。报告建议停止扫 grace 参数，把两个结构性候选列入对照矩阵：
  1. **事件化 ingestion**：scan_interval 从 2s 降到 0.2s 量级（8 个固定 pointer 的 stat 成本极低；G run 27/50 次靠 quorum_max 提前结束，检测延迟直接转化为吞吐）；
  2. **发布 I/O 期间摄取**：E1 的 checkpoint I/O future 未完成时，主线程可继续摄取 heartbeat/pointer 元数据，减少 publish 后首次发现延迟。
- 性质：L1 为低风险配置/参数实验；L2 为**架构变更实验**（并发引入协调核心）。
- 影响文件：`fs_diloco/runtime/syncer.py`、配置、metrics。
- 前置：L0 telemetry 必须先行（P5）；若 E1 的并行写已落地，其线程化基建可复用。

## 2. 目标与完成谓词

1. telemetry：global interval 分解字段（`grace_seconds`、`merge_seconds`、`publish_seconds`、`idle_seconds`、quorum 触发类型）落地并在基线 run 中留档（OVL-01）；
2. 事件化 ingestion：scan=0.2s 的成对对照 run 完成，量化 quorum_max 提前触发率与 interval 变化；Lustre stat 压力有实测数据（OVL-02/03）；
3. 发布 I/O 期间摄取：checkpoint worker 只写临时/目标文件，主线程在等待 future 时执行 heartbeat/pointer ingest；随后仍由主线程按原顺序完成 DB commit → latest。单 writer、proposal 状态机、crash matrix 全部复验，成对对照量化 publish 后发现延迟（OVL-04/05）；
4. 两项结论（含"不值得做"的负结论）写入 run_analysis 与 00 计划 §5.3/5.4 对照矩阵。

## 3. 范围与非目标

- **范围内**：telemetry、scan 缩短实验、发布线程化实验。
- **非目标**：改变 merge/quorum/staleness 语义；learner 节拍调整（DyLU 类，归研究矩阵）；grace 参数再扫描（报告明确不做）。
- **审计修正**：原计划声称 interval 下限可从 `grace+publish` 变成 `max(grace,publish)`，但 learner 只有在 DB commit 后的 latest 发布后才能基于新版本训练；在不改变 staleness/训练语义时，下一轮 grace 不能与当前 publish 完全重叠。该主张删除，不作为验收指标。
- **并发边界（L2 的 SPECIFY 核心）**：DB 写事务仍全部发生在主循环线程；worker 只做 weight/outer 文件 I/O。等待 worker 时的 ingestion 仍由主线程串行写 metadata DB，禁止 selection/maintenance/版本 commit；两个 worker 成功后才 commit 当前版本并切 latest。这样不会出现第二 SQLite writer，也不会让 GC 删除正在发布的 checkpoint。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 telemetry | 以单调时钟定义 cycle 起点和 `discovery/grace/read/merge/publish/maintenance/residual`，quorum trigger 冻结 | interval 分解上报 | 每行各分量不重叠；总和与 interval 残差 <5%（首轮/terminal 单列） |
| L1 短 scan | 成对实验设计（仅 scan_interval 差异，P6）；stat 压力测试先行（1 节点对共享 FS 以 0.2s 轮询 8 pointer，量化开销） | 无代码（配置） | 成对 9 节点 run；quorum 触发类型分布对比入报告 |
| L2 并发规格 | §3 冲突清单 + 先 RED：I/O 等待中摄取、worker 失败、stop 可见、禁止 selection/maintenance | E1 future wait loop 中按配置启用主线程 ingest | crash matrix；tiny；1000-cycle；无第二 writer |
| L3 对照与决策 | — | — | 三方案（scan=2/无摄取、scan=.2/无摄取、scan=.2/I/O 等待摄取）按相邻单变量比较；发现延迟与 interval 表入报告 |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| OVL-01 | telemetry | 单调时钟分解字段完备且不重叠，各分量之和 ≈ interval（正常非首轮残差 <5%） |
| OVL-02 | stat 压力 | 0.2s×8 pointer 轮询的共享 FS 开销实测 <1% syncer CPU/可忽略 I/O |
| OVL-03 | 短 scan 对照 | 成对 run：quorum_max 提前触发率、interval、supersession 率对比数据齐备 |
| OVL-04 | 并发正确性 | 冲突清单每项有对应测试；crash matrix 全绿；无 SQLITE_BUSY/locked |
| OVL-05 | I/O 期间摄取对照 | 成对 run：publish 期间摄取数、publish 后到下一次 quorum 可判定的延迟、interval 数据齐备；不宣称 grace 完全被隐藏 |

## 6. 验证阶梯

1. 登录节点：静态检查。
2. 1 节点：pytest + OVL-02 压测 + tiny run。
3. 2 节点：L2 完成后一次跨节点冒烟（并发 + 共享 DB 压力路径变化）。
4. 9 节点：L1/L3 对照实验（G7/G8 纪律）。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/perf_fix-E/E2/`。L2 若三次并发测试失败即升级 code_review（AGENTS.md），且升级审查必须重新评估"是否值得引入并发"——L0 数据若显示 publish 占比不高，负结论同样是合格交付。

## 8. 文档同步

00 计划 §5.3/5.4 对照矩阵登记两个候选与实验结果；docs 若描述 syncer 单线程模型，按 L2 结果更新。
