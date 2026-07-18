# B9：adaptive ETA 改单一时钟源

## 1. 元信息

- 来源：review B9（低）。`fastest_next_upload_eta_seconds`（syncer.py:673-702）以 learner 节点写入的 `committed_at`（learner wall clock）加 cycle 估计，减 syncer 节点 `time.time()`——跨节点时钟差直接进入秒级 deadline 决策（run_analysis 观测的 ETA 收紧幅度为 1.5–9.9s，与 NTP 偏差同量级边缘）。run_analysis 中 commit→selection age 类指标同样混用两端时钟。
- 性质：**语义修正**（ETA 参考起点从 learner 时钟改为 syncer 时钟）+ 文档标注。adaptive 模式默认关闭的配置不受影响。
- 影响文件：`fs_diloco/runtime/syncer.py`、测试、docs/metrics 字段说明。
- 前置依赖：无。

## 2. 规格

### 2.1 单一时钟源方案（进程内存，不新增 schema）

- 现 schema 已有由 syncer 写入的 `ingested_at`，所以原计划中“DB 加 ingested_at 列”的备选基于过时盘点。它仍是 wall clock，且 resume 可能换 syncer 节点，不适合作为进程内 deadline 的单调时钟。实施采用进程内 registry：syncer 在 `insert_*_update_metadata` 首次成功时记录 `update_id → (first_seen_monotonic, first_seen_wall)`；重复摄取不刷新。摄取函数增加可选 observer/registry 参数，不额外扫描文件；
- ETA 估计改为：`first_seen + cycle_seconds - now_monotonic`（全部 syncer 单时钟）。语义差异：起点从 learner 的 commit 时刻变为 syncer 的观察时刻，两者相差一次 scan 间隔（≤ `scan_interval_seconds`，2s）——该偏移是**保守方向**（高估剩余时间→少收紧 grace），可接受并写入 docstring；
- resume/进程重启后映射为空：缺 `first_seen` 的 update 跳过估计（与现实现对 `step_seconds is None` 的处理一致），效果是 resume 后第一轮 grace 不收紧——保守且正确；
- registry 使用容量上限 `max(64, 4 * num_learners * max(1, num_fragments))` 的 insertion-ordered/LRU 映射；成功应用、明确 drop 的 update 主动移除，容量淘汰只会让某次估计缺失并保守地不收紧，不影响正确性；
- **不删除** `committed_at` 字段本身（研究证据仍有价值），但所有跨节点时钟字段在 metrics/docs 中标注 `cross-node wall clock` 语义（review 建议的最低要求）。

### 2.2 否决的备选

- 直接用既有 DB `ingested_at`：它能持久化且适合离线分析，但仍是 wall clock；进程重启后若换节点，拿它与当前 `time.time()` 相减仍回到跨节点/时钟跳变问题。deadline 决策只使用 monotonic registry，离线分析继续保留 `committed_at`/`ingested_at` 并标注时钟域。

## 3. 目标与完成谓词

1. `fastest_next_upload_eta_seconds`（或其替代）不再读取 `committed_at` 参与时间差计算（ETA-04 静态检查）；
2. 单元测试：注入 learner 时钟偏移 ±60s，ETA 结果不变（ETA-01——本修复的定义性测试）；
3. first_seen 生命周期正确：重复 ingest 不刷新首见时间；supersession（同 learner 更新 proposal）建立新条目；映射有界（随 update 行清理或以 LRU/上限封顶，SPECIFY 冻结）（ETA-02）；
4. adaptive tiny run（`fs_diloco_tiny_adaptive_global_stop.yaml`）行为正常：grace 收紧事件仍发生、run 正常结束（ETA-03）；
5. 全量 pytest；跨节点时钟字段的文档标注完成。

## 4. 范围与非目标

- **范围内**：ETA 时钟源、first_seen 映射、字段文档标注。
- **非目标**：run_analysis 历史指标的重算（分析侧只加标注）；grace 窗口算法本身（收紧策略不变）；NTP/时钟同步的基础设施假设。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 语义冻结 | 冻结 first_seen 生命周期规则（§3-3）与映射上界；基线 commit；基线 adaptive tiny run 留档 | 无实现 | 决策入 progress.md |
| L1 时钟注入单元 | ETA-01 先 RED：现实现下注入 learner 时钟偏移 → ETA 随偏移漂移（RED 证据即缺陷证明） | first_seen 映射 + ETA 改造 | ETA-01/02 GREEN |
| L2 集成与文档 | ETA-03 场景定义 | metrics/docs 字段标注 | adaptive tiny run 通过；全量 pytest；ETA-04 静态检查 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| ETA-01 | 时钟偏移免疫 | learner `committed_at` 偏移 ±60s → ETA 不变（修复前漂移，RED） |
| ETA-02 | first_seen 生命周期 | 重复 ingest 不刷新；supersession 新条目；映射大小有上界 |
| ETA-03 | adaptive 集成 | adaptive tiny run：grace 收紧事件出现、`input_exhausted`/目标停止正常 |
| ETA-04 | 静态检查 | ETA 计算路径无 `committed_at` 参与减法 |

progress.md 每条记录必须列出覆盖的 ETA ID（P8）。

## 7. 验证阶梯

1. **登录节点**：ETA-04 grep、lint。
2. **1 节点 compute**：单元 → 全量 pytest → adaptive tiny run。
3. 2/9 节点：不需要——时钟偏移用注入模拟，比真实双节点更能覆盖极端值。下一次 adaptive 9 节点实验的 run_analysis 可对比修复前后 grace 收紧统计（被动观察，非门禁）。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B9/`。关键证据：ETA-01 修复前 RED 输出（量化时钟偏差对决策的影响）、adaptive tiny run 日志。无 Checker 变更。

## 9. 停止与升级规则

按 AGENTS.md。若 SPECIFY 发现摄取点无法可靠承载 first_seen（如存在绕过摄取的 update 读取路径），先记录再评估 DB 方案，不得混用两种起点。

## 10. 文档同步

- metrics 字段表：`committed_at` 等跨节点字段标注时钟语义；新增 first_seen 相关字段说明；
- docs 的 adaptive grace 章节更新起点定义与保守偏移说明；review 报告 B9 条目标注 commit。
