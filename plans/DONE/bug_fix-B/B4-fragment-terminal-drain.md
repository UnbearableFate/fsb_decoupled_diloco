# B4：fragment syncer 接入 input-closed / terminal drain

## 1. 元信息

- 来源：review B4（中）。`run_fragment_syncer`（syncer.py:1161-1557）主循环只有 `fragment_quorum_wait + no_progress_timeout`，缺少 full 路径的 `all_expected_learners_stopped → terminal drain → input_exhausted` 分支（该分支仅存在于 full 循环，syncer.py:1642-1662）。fragment learner 全部到达 local horizon 后，syncer 空等满 `no_progress_timeout_seconds`（正式配置 3600s）——plan 01 在 full 上修掉的 "A run 4872s" 问题在 fragment 上原样存在。plan 01 只把 fragment resume 排除出范围，terminal drain 属于范围缝隙。
- 性质：**语义新增**（fragment 循环获得 `input_exhausted` 正常结束语义）。full 循环不动。
- 影响文件：`fs_diloco/runtime/syncer.py`、fragment 相关测试、（可能）`check_plan01_invariants.py`。
- 前置依赖：**建议先完成 [bug_fixing_plans/S3](../bug_fixing_plans/S3-full-fragment-loop-dedup.md)**——S3 交付与 full/fragment 无关的 input-closed 谓词后，本计划的 L1 退化为接线。若 S3 未执行，本计划 L1 需自行提炼该谓词（做法按 S3 §4.2，提炼后 S3 对应工作量核减；两份计划不得各自维护一份谓词实现）。

## 2. 规格：fragment 的 input-closed 语义

复用 full 的既有定义（plan 01 terminal drain 语义，经验文档 §2.2），fragment 特有部分需在 SPECIFY 阶段冻结：

- **input-closed 判定**：全部预期 learner 已 stopped（最终心跳）——与 full 相同，判定与 fragment 无关；
- **terminal drain**：input-closed 后等待一次已配置 grace 并再次 ingestion；之后按 round-robin 的当前目标片反复 merge，每轮仍只取每 learner 一份且不超过 `quorum_max`，但允许 `1..quorum_min-1` 的尾部 merge。每次 merge 后按推进后的 global event 选择下一目标片；若该目标片无合法 proposal，则立即 `input_exhausted`，不得跳过调度目标去消费其他片，剩余 proposal 由终态化处理；
- **终态化**：drain 后不再消费的 pending/selected fragment proposal 走既有 `finalize_unconsumed_updates(fragment_mode=True)` 路径终态化（SPECIFY 确认该函数 fragment 分支已可用，review 未标记问题）；
- **stop reason**：与 full 一致使用 `input_exhausted`；
- **不变量**：drain 不绕过 future/staleness 规则；终态 fragment proposal tensor 为零。

## 3. 目标与完成谓词

1. fragment learner 全停后，syncer 在 terminal grace（而非 no_progress_timeout）内结束，stop reason 为 `input_exhausted`（FDR-01，本计划的核心 RED 复现）；
2. drain 语义测试通过：尾部低 quorum merge（若采纳）、迟到 pointer、future base 拒绝（FDR-02/03）；
3. 终态断言：fragment run 结束目录满足与 full 对应的终态不变量（proposal tensor 为零、DB/latest/stop/summary 一致，FDR-04）；
4. full 循环轨迹等价（未动，FDR-05）；
5. 全量 pytest；tiny fragment run 上 Checker PASS。

## 4. 范围与非目标

- **范围内**：fragment 主循环的 input-closed 分支、drain、终态化接线与测试。
- **非目标**：fragment resume（plan 01 已显式出范围）；E3（物化策略）与 E4（fragment 发现面）；full/fragment 主循环骨架统一（S3 的非目标同样在此维持）。
- **对照污染声明（P6）**：本修复显著改变 fragment run 的完整训练时间（消除最长 1 小时尾部空等）。合入后 fragment 耗时与历史 run 不可直接对比，run_analysis 引用需注明 commit。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 复现与语义冻结 | FDR-01 先 RED：tiny fragment 配置 + 全 learner 到达 horizon → 复现 syncer 空等 no_progress_timeout（缩小超时以便测试）；§2 已冻结 grace、低 quorum、多轮和调度目标耗尽语义 | 无实现 | 复现日志与语义清单入 artifacts/progress |
| L1 谓词就位 | （S3 已完成则跳过）谓词单元测试先行 | 提炼/复用 input-closed 谓词 | 谓词单元全绿；full 侧无 diff 或轨迹等价 |
| L2 drain 接入 | FDR-02/03 先 RED：尾部 merge、迟到 pointer、future base 场景 | fragment 循环加入 input-closed 分支：最终 ingestion → drain → 终态化 → `input_exhausted` | FDR-01 GREEN；事件顺序与 full 对应分支可对照 |
| L3 终态与集成 | FDR-04 先 RED（旧行为下终态断言失败或不可达） | 终态化与 summary/metrics 接线 | 全量 pytest；tiny fragment run + Checker；FDR-05 full 轨迹等价 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| FDR-01 | 核心复现→修复 | 全 learner 停止后 syncer 于 terminal grace 内以 `input_exhausted` 结束（修复前：空等满 no_progress_timeout） |
| FDR-02 | 尾部 drain 规则 | 低 quorum 尾部 merge 行为符合 §2 冻结清单；future/staleness 不被绕过 |
| FDR-03 | 迟到输入 | learner 在看到 stop 前发布的最后 proposal 被最终 ingestion 捕获并按 drain 规则处理或终态化 |
| FDR-04 | 终态不变量 | 结束目录：终态 fragment proposal tensor 为零；DB/latest/stop/summary 一致 |
| FDR-05 | full 无回归 | tiny full run 轨迹与基线等价 |

progress.md 每条记录必须列出覆盖的 FDR ID（P8）。

## 7. 验证阶梯

1. **登录节点**：lint、静态检查。
2. **1 节点 compute**：谓词/drain 单元 → 全量 pytest → FDR-01 集成（tiny fragment，缩小超时）→ Checker。
3. **2 节点 compute**（建议）：`run_2node_fragment_debug.pbs` 一次，确认跨节点心跳可见性下 input-closed 判定正确（learner 心跳经共享 FS/DB，与 1 节点路径不同）。
4. 9 节点：不需要作为门禁；下一次 fragment 5000-step 实验自然验证（其完整时间数据将首次可信）。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B4/`。FDR-01 修复前后的耗时对比是核心证据。`check_plan01_invariants.py` 若其终态检查只覆盖 full，需扩展 fragment 终态断言（FDR-04 的机检形态）。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 L0 对照发现 full drain 分支存在 fragment 无法复用的隐含假设（如依赖 full 专有 DB 视图），先记录 failures.md，必要时回到 S3 界面重新设计，不得在 fragment 侧复制一份变体逻辑（那正是 B4 的病因）。

## 10. 文档同步

- docs 的 terminal drain 章节标注 fragment 已覆盖；
- plan 01 的范围缝隙（"fragment terminal drain 未显式排除也未实现"）在 review 报告 B4 条目标注闭合 commit；
- run_analysis 侧记录对照污染声明（§4）。
