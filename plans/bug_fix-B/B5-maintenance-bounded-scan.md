# B5：maintenance 热路径去掉 O(history) 归档扫描

## 1. 元信息

- 来源：review B5（中）。`collect_runtime_artifacts`（maintenance.py:140）每次 merge 后调用 `_archived_terminal_paths(paths.update_history_jsonl)`（定义 maintenance.py:29-42），逐行读取**整个**归档 JSONL 重建 terminal payload 路径集合。归档每 merge 约 +8 行 → 第 N 次 merge 的 maintenance 成本 ~O(N)，累计 O(N²)。直接违反 plan 01 不变量"单次操作成本不依赖历史 update 数"；BND-10/11 只测了 SQLite 行数/页数，文件扫描成本从测试矩阵漏出。
- 性质：**行为保持**（GC 决策结果不变），移除的是实现方式；附带补齐 BND 断言与 telemetry（P5）。
- 影响文件：`fs_diloco/storage/maintenance.py`、`fs_diloco/storage/sqlite_store.py`（如采用 gc_pending 表）、`tests/test_retention.py`、`tests/test_bounded_1000_cycles.py`。
- 前置依赖：无。review R0 项。

## 2. 现状语义盘点（SPECIFY 的对象）

归档路径集合当前在 GC 中的两个用途（maintenance.py:150-172）：

1. 让**已归档但文件尚未删除**的 payload/metadata 跳过 orphan grace 立即删除（`terminal` 判定）；
2. 与 `consumed_pointer_payloads`、`live_payloads` 共同构成删除决策。

关键观察：`run_maintenance`（:185-209）在同一轮先调 `archive_and_prune` 再调 `collect_runtime_artifacts`，且 `archive_and_prune` 的返回值已带本轮 `terminal_paths`（:58-62）并经 `extra_terminal_paths` 传入（:203）。因此全量重读归档**只**为覆盖一种情形：某轮归档成功但对应文件删除在完成前崩溃/失败，路径需要在后续轮次仍被识别为 terminal。

## 3. 设计决策：持久 gc_pending 集合替代全量重读

在 SQLite 增加 `gc_pending` 表（列：`file_path` 主键、`archived_at`）：

- `archive_and_prune` 在**同一事务**内：写入归档 JSONL 前后按现有顺序不变，`delete_archived_rows` 的同一事务中把本轮 terminal 路径插入 `gc_pending`（幂等 upsert）；
- `collect_runtime_artifacts` 的 terminal 集合 = `gc_pending` 全表（行数 = 尚未删除成功的文件数，正常为 0–本轮增量）∪ `extra_terminal_paths`；
- 文件删除成功（或已不存在）后，同轮删除对应 `gc_pending` 行；
- 不再读取 `update_history_jsonl`；`_archived_terminal_paths` 删除。

备选（若不愿加表）：只用本轮 `terminal_paths` + "文件不存在即幂等跳过"。否决理由：崩在"归档后、删除前"的 payload 将永远不被识别为 terminal，只能等 orphan grace 兜底——而 pointer 已消费的 payload 不落入 `consumed_pointer_payloads` 集合时会残留，违反 BND-14 类终态断言。gc_pending 以 O(pending) 成本保住崩溃安全，语义最干净。schema 变更与 plan 01 的 DB 权威原则一致（新表由 syncer 独写）。

## 4. 目标与完成谓词

1. `_archived_terminal_paths` 与对 `update_history_jsonl` 的任何运行时读取从 maintenance 路径消失（MNT-05 静态检查；归档 JSONL 降级为纯 append-only 研究证据）；
2. GC 决策等价：现有 retention/GC 测试全数通过，crash 注入用例（MNT-03）证明"归档后删除前崩溃"的 payload 在恢复后仍被删除；
3. 有界性断言落地：1000-cycle 测试新增"maintenance 单轮扫描行数与耗时不随 cycle 增长"（MNT-04）；
4. telemetry：`maintenance` 事件/metrics 增加 `gc_pending_rows`、`maintenance_scan_seconds` 字段（先于断言实现，P5）；
5. 全量 pytest 通过；tiny run 上 `check_plan01_invariants.py` PASS。

## 5. Loop Engineering 实施循环

| Loop | SPECIFY/RED | IMPLEMENT/GREEN | HARDEN、CHECK、PERSIST |
| --- | --- | --- | --- |
| L0 语义冻结 | 把 §2 盘点写成测试可引用的清单；记录基线 commit；在基线跑一次 1000-cycle 测试记录 maintenance 耗时曲线（作为改进对照） | 无实现 | 耗时曲线入 artifacts |
| L1 telemetry 先行 | 字段名、采样边界、写入位置（metrics CSV/JSONL）先定 | `gc_pending_rows`（暂为归档集合大小）与 `maintenance_scan_seconds` 上报 | 字段在 tiny run 输出中可见 |
| L2 gc_pending 表 | MNT-01/02 先 RED：插入与清除的事务性、幂等 upsert、重启后可见 | 建表；`archive_and_prune` 事务内插入；删除成功后清行 | schema 变更走既有 store 初始化路径；DB-03 类重开测试复跑 |
| L3 切换 GC 数据源 | MNT-03 先 RED：注入"归档提交后、unlink 前"崩溃 → 恢复后该 payload 仍被删除、gc_pending 清空 | terminal 集合改读 gc_pending；删除 `_archived_terminal_paths` | 全部 retention/GC/终态测试通过；MNT-05 静态检查 |
| L4 有界性断言 | MNT-04 先 RED（在旧实现上应 FAIL 或标记 xfail 证明断言有效） | — | 1000-cycle 下 `gc_pending_rows` 有上界、`maintenance_scan_seconds` 无线性趋势；对照 L0 曲线写结论 |

## 6. 测试矩阵与通过条件

| ID | 测试 | 通过条件 |
| --- | --- | --- |
| MNT-01 | gc_pending 事务性 | 插入与 `delete_archived_rows` 同事务：事务内注入异常 → 两者均未发生 |
| MNT-02 | 幂等与重启 | 重复归档同一路径不产生重复行；重开 DB 后 pending 行可见 |
| MNT-03 | crash 恢复 | "归档后、删除前"崩溃 → 下一轮 maintenance 删除该文件并清除 pending 行 |
| MNT-04 | 有界性 | 1000-cycle：单轮扫描行数与耗时不随 cycle 增长；gc_pending 行数有上界 |
| MNT-05 | 静态检查 | `_archived_terminal_paths` 出现 0 次；maintenance 运行路径无 `update_history_jsonl` 读取 |

progress.md 每条记录必须列出覆盖的 MNT ID（P8）。

## 7. 验证阶梯

1. **登录节点**：MNT-05 grep、lint。
2. **1 节点 compute**：storage 相关 pytest → 全量 pytest → 1000-cycle（MNT-04）→ tiny run + invariant Checker。
3. 2/9 节点：不需要（单 writer 语义未变）。注意：本改动含 DB schema 变更，**不兼容既有 run 目录的 resume**——SPECIFY 阶段确认建表走"缺表即建"的幂等初始化即可兼容；若做不到则在计划中显式声明不兼容边界（经验文档 §1.3）。

## 8. 报告、证据与 Checker

报告目录 `reports/imp_plans/bug_fix-B/B5/`。关键证据：L0 与 L4 的 maintenance 耗时对照、MNT-03 crash 注入日志、1000-cycle telemetry 曲线。`check_plan01_invariants.py` 若检查表清单硬编码，需同步认识 gc_pending 表（终态时应为空——这本身可作为新的终态不变量加入 Checker）。

## 9. 停止与升级规则

按 AGENTS.md 三连败升级。若 L2/L3 中发现现 GC 存在**依赖全量归档集合的未记录行为**（例如历史路径用于防误删），先记录 failures.md 并回写 review 勘误，重新评估 §3 决策后再继续。

## 10. 文档同步

- docs 中关于 maintenance/GC 的描述更新为 gc_pending 语义；归档 JSONL 定位改为"纯研究证据，运行时不读取"；
- 终态不变量"gc_pending 为空"补入 Checker 说明。
