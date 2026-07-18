# E4：fragment 固定发现面与 frontier 短路

## 1. 元信息

- 来源：review E4（低）。fragment ingestion glob `updates/payloads/learner_*/update_*.meta.json`（syncer.py:617-618），对比 full 模式的固定 pointer + proposal frontier 去重。review 已澄清实际风险比字面小：终态 metadata 会被 GC 删除（maintenance.py:161-172），稳态扫描面 ≈ 活跃 proposal 数；真正成本是**每轮对每个 meta.json 的重复 JSON 读取**（无 frontier 短路）。plan 01/00 均已把"fragment 固定 proposal surface"列为后续项——本计划就是那个后续项。
- 性质：**行为保持优化**（发现面机制对齐 full 模式，ingestion 结果不变）。
- 影响文件：`fs_diloco/runtime/syncer.py`、learner fragment 发布路径、测试。
- 前置：建议在 [S3](../bug_fixing_plans/S3-full-fragment-loop-dedup.md)（收集循环参数化）之后——共享骨架就位后本计划只动"候选枚举"参数对象。

## 2. 设计规格

审计确认 fragment 不能使用“每 learner 单 pointer”：round-robin 下不同 fragment 的 proposal 会等待各自目标轮，单 pointer 会让新 fragment 覆盖尚未消费的其他 fragment，改变 ingestion/利用率。冻结为每 `(learner_id, fragment_id)` 一份 pointer：

- pointer 路径固定为 `updates/latest/learner_XXX_fNNN.json`，总数上界 `num_learners × num_fragments`；先写不可变 tensor，再原子替换 pointer；payload 目录不再写独立 fragment meta；
- 新增持久 `fragment_proposal_frontiers`，主键 `(learner_id, fragment_id)`，记录最后观察的 update/pointer；`insert_fragment_update_metadata(..., pointer_path=...)` 在一个事务内完成重放短路、同 pair 旧 pending supersede、插入新行和 frontier 推进；selected 行不被覆盖；
- syncer 每轮只枚举配置推导的固定 pointer 路径，frontier update_id 相同时可在 JSON 解析前用轻量 pointer stat/signature 缓存短路；进程重启后至少需读一次 JSON 与 DB frontier 核对；
- 不保留 payload glob 作为第二权威/兜底。旧 run resume 与新协议不兼容，identity/protocol version fail closed；迁移仅补 schema，不假装发现旧 meta。

## 3. 目标与完成谓词

1. 稳态 ingestion 每轮工作量 = O(活跃 pointer 数)，且已摄取的 meta.json 不被重复解析（FDX-01/02）；
2. ingestion 结果等价：tiny fragment run 事件轨迹与基线等价（FDX-03）；
3. 发现面有界断言：fragment 版"discovery 面大小不随历史增长"进入 1000-cycle/长循环测试（FDX-04，对齐 BND 系列）；
4. 全量 pytest；tiny fragment run 上 Checker PASS。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 语义冻结 | 上述 per-pair pointer、frontier transaction、旧 run fail-closed 与 GC live-set 冻结 | 无实现 | 决策与依据入 progress.md |
| L1 learner 发布侧 | pointer 重放/latest-wins 测试先 RED（对齐 full 的既有测试模式） | 固定 pointer 发布 | 单元全绿 |
| L2 syncer 摄取侧 | FDX-01/02 先 RED：重复扫描计数断言 | frontier 短路 + pointer 枚举 | FDX-03 轨迹等价；全量 pytest |
| L3 有界性 | FDX-04 先 RED | — | 长循环断言通过；证据归档 |

## 5. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| FDX-01 | 扫描面 | 稳态每轮枚举路径数恒为 `M×K`（不存在的路径只 stat；注入历史 payload 不增加枚举） |
| FDX-02 | frontier 短路 | 同 signature/update 不再 JSON parse；重启后一次核对不重复入库 |
| FDX-03 | 等价 | tiny fragment run 轨迹与基线等价 |
| FDX-04 | 有界 | 长循环中 discovery 工作量与文件扫描数有上界 |

## 6. 验证阶梯

1. 登录节点：静态检查。
2. 1 节点：pytest → tiny fragment run → 长循环。
3. 2 节点：一次 fragment debug 冒烟（pointer 跨节点可见性）。9 节点不需要。

## 7. 报告、证据与升级规则

报告目录 `reports/imp_plans/perf_fix-E/E4/`。按 AGENTS.md 三连败升级。若 L0 发现 fragment 与 full 的 supersession 语义存在设计性差异（非疏漏），停止对齐并把差异写回 00 计划裁决。

## 8. 文档同步

docs fragment 协议章节更新发现面机制；plan 01/00 的"后续项"条目标注闭合 commit。
