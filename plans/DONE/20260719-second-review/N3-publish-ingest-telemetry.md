# N3：publish-ingest callback 接线与 telemetry 口径修复

## 1. 目标与字段契约

当前 full syncer 把条件表达式写进 lambda 体，导致
`sync.ingest_during_publish=false` 时仍向 `publish_global()` 传入 callable。checkpoint
等待循环因此空转并把 `publish_ingest_passes` 记为非零。

修复后的字段契约为：

| 字段 | 精确定义 | flag=false |
| --- | --- | --- |
| `publish_ingest_passes` | checkpoint futures 尚未完成时，主线程真实调用摄取 callback 的次数 | 0 |
| `publish_ingested_updates` | 上述 callback 各轮返回的 metadata 插入数之和 | 0 |
| `publish_ingested_heartbeats` | 上述 callback 各轮返回的 heartbeat 摄取数之和 | 0 |
| `publish_ingest_seconds` | 仅 callback 调用本身的 wall-clock 累计时间 | 0.0 |

callback 调用但本轮没有新 metadata 时，passes 可以增加而 inserted 数为 0；两者不能
混为同一指标。并行 checkpoint、DB 单 writer 和 commit 顺序不改变。

## 2. 范围与完成定义

范围内：`fs_diloco/runtime/syncer.py` 条件 callable 接线、
`tests/test_parallel_publication.py`、一个覆盖真实 full caller 的 tiny/集成断言、metrics
口径文档和历史数据说明。

非目标：重新执行 E2 性能对照、改变 poll interval、引入第二 SQLite writer、修改
serial publication、根据本修复推翻既有 E2 方向性结论。

完成条件：flag=false 的直接测试与真实 full metrics 每行四字段严格为零；flag=true
的受控 pending-futures 测试真实调用 callback 并保留当前计数语义；checkpoint 失败仍不
提交 DB/latest。

## 3. Requirement 与测试矩阵

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| PUB-TEL-01 | parallel=true、flag=false、checkpoint workers 人为阻塞超过 poll interval | callback 根本不存在/不调用；四字段精确为 0；workers 完成后正常 commit |
| PUB-TEL-02 | parallel=true、flag=true、workers pending，callback 返回 metadata=2/heartbeat=1 | callback 在主线程调用；passes=真实调用数；两个 inserted 累计正确 |
| PUB-TEL-03 | flag=true 但 futures 在首轮 poll 前完成 | callback 可为 0 次；不得为了制造非零指标额外调用 |
| PUB-TEL-04 | weight 或 outer worker 失败 | callback 行为不掩盖异常；DB/latest 不提交；指标不作为成功 artifact |
| PUB-TEL-05 | flag=false 的真实 full tiny | `syncer_metrics.csv` 每行四字段均为零，且至少有一行证明 workload 真执行 |
| PUB-TEL-06 | metrics 汇总工具消费修复后 CSV | totals 与逐行和一致；不得把修复前 passes 当作实际 ingestion 轮数 |

PUB-TEL-01 不能只直接调用 `publish_global(..., during_checkpoint_wait=None)`，因为那无法
捕获本次 caller 绑定错误；至少一条测试必须经过构造 callback 的 full syncer 调用位置，
或将该构造提炼成有真实使用者的可测 helper。

## 4. Loop Engineering 实施循环

| Loop | SPECIFY / RED | IMPLEMENT / GREEN | HARDEN / CHECK / PERSIST |
| --- | --- | --- | --- |
| L0 caller RED | PUB-TEL-01/05 先证明 flag=false 仍得到 callable/非零 passes | 无实现 | 保存 AST/调用捕获和旧 CSV；冻结四字段口径 |
| L1 接线修复 | 保持 PUB-TEL-02 作为正控 | 条件表达式放在 lambda 外；关闭时传 `None` | 关闭/开启/快速完成三组共同通过；不改变 publish commit 顺序 |
| L2 失败与汇总 | PUB-TEL-04/06 | 仅补必要测试/说明，不修改失败事务语义 | 单 worker failure、metrics aggregate、全量 pytest |
| L3 真实 tiny | PUB-TEL-05 | 无额外性能调参 | CSV 非空、version 增长、Checker PASS；artifact 入 progress |

## 5. 历史数据与文档规则

修复 commit/source fingerprint 之前的 `publish_ingest_passes` 含空转轮询，不能解释为
真实 ingestion。历史 `publish_ingested_updates` 与 `publish_ingested_heartbeats` 若为 0，
仍可作为“没有实际插入”的证据，但不得据旧 passes 推断 overlap 活跃度。

把该边界写入 `reports/run_analysis.md` 或对应 E2 审计报告；稳定字段定义同步到
`docs/04-data-flow.md`、`docs/modules/runtime-syncer.md`。不要把具体 run 数字写入系统 docs。

报告目录：`reports/imp_plans/20260719-second-review/N3-publish-ingest-telemetry/`。

## 6. 验证与停止规则

登录节点按 INDEX G1；compute 节点运行 publication 聚焦组、metrics 汇总组、全量 pytest
和一次 flag=false tiny。无需 2/9 节点或正式 E2 重跑。

若 PUB-TEL-01 三次仍不稳定，先确认测试确实让 futures pending，而不是继续增大 sleep；
改用 Barrier/Event 建立确定性调度，并在 `code_review.md` 审核 futures wait、callback、
exception 和 result 收集顺序。

