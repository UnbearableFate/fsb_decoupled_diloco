# N5/N6：旧 metadata 清理分支移除与 fragment final-wait heartbeat

## 1. 目标与边界

本计划处理两个不改变训练数学的低风险生命周期问题：

- N5：`collect_runtime_artifacts()` 仍遍历
  `updates/payloads/learner_*/*.meta.json`，但当前 full/fragment 发布协议只在固定
  `updates/latest/` pointer 写 metadata；
- N6：fragment learner 的 finally final wait 最长可达 no-progress timeout，循环内不写
  heartbeat，使一个仍在正常收尾的进程被观测为 dead。

N5 选择删除旧分支，不把它保留为隐式迁移器。当前协议/identity 已不承诺 pre-E4 run
兼容；旧 run 的离线迁移另行处理。N6 只修正观测与 shutdown 诊断，不改变 input-closed
判定（仍只认 stopped）、final adoption 或等待 deadline。

## 2. 不变量

| ID | 不变量 |
| --- | --- |
| INV-MNT-01 | runtime payload 目录只含不可变 tensor 与短暂 tmp；生产代码不生成或扫描历史 `.meta.json` |
| INV-MNT-02 | proposal tensor 的 GC 仍只按 DB/pointer 引用、terminal 集合和 orphan grace 决定 |
| INV-FWAIT-01 | fragment final wait 未结束时 learner 以 `active, phase=final_fragment_wait` 周期心跳；不能因等待本身变 dead |
| INV-FWAIT-02 | heartbeat 频率不高于配置间隔；更新 heartbeat 不延长 final-wait deadline |
| INV-FWAIT-03 | finally 最终仍写一次 `stopped, phase=process_exit`；正常/异常退出语义不变 |

## 3. 范围与影响文件

范围内：`fs_diloco/storage/maintenance.py`、`fs_diloco/runtime/learner.py`、retention、
fragment learner/liveness 测试，以及对应 module docs。

非目标：删除 `tests/test_fragment_pointer_discovery.py` 中用于证明固定发现面不受历史
junk 影响的数据；为 pre-E4 run 增加迁移；改变 heartbeat schema；把 dead 当 stopped；
改变 fragment final wait 的 timeout 或 adoption 规则。

## 4. Requirement 与测试矩阵

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| MFW-01 | 静态搜索生产代码的 payload `.meta.json` glob/写入 | 除明确的离线迁移工具（若存在）外为零；maintenance 分支消失 |
| MFW-02 | current full/fragment proposal 的 active、terminal、orphan、tmp 组合 | tensor GC、grace、gc_pending 行为与修复前一致 |
| MFW-03 | fixed discovery 测试旁置 100 个历史 `.meta.json` junk | discovery 读取面仍固定；该测试不被误删或改成较弱断言 |
| MFW-04 | fragment final wait 持续超过 `dead_after_seconds`，无 newer latest/stop | 每个 heartbeat 间隔均写 active final-wait heartbeat；liveness 不到 dead |
| MFW-05 | final wait 中出现 newer latest | 正常 adopt；heartbeat 携带更新后的 merge event/fragment versions；deadline 不重置 |
| MFW-06 | final wait 中出现 stop 或达到 deadline | 循环及时结束；随后恰好落一份 stopped process-exit heartbeat |
| MFW-07 | final wait adoption 抛异常 | 保留 `final_fragment_adoption_failed` 诊断；finally 仍尝试 stopped heartbeat；不得把异常吞成训练成功证据 |

## 5. Loop Engineering 实施循环

| Loop | SPECIFY / RED | IMPLEMENT / GREEN | HARDEN / CHECK / PERSIST |
| --- | --- | --- | --- |
| L0 协议搜索 | MFW-01：列出所有 generator/reader；确认 runtime 无生产者 | 无实现 | 搜索结果入 artifacts；冻结 pre-E4 非兼容边界 |
| L1 maintenance 清理 | MFW-02/03 保持绿色基线 | 删除 payload metadata 遍历，不改 tensor/tmp/DB GC | retention 全组合、fixed surface、1000-cycle bounded 测试 |
| L2 final wait RED | 受控 monotonic/sleep 让等待超过 dead_after；捕获当前仅一份最终 heartbeat | 无实现 | RED 日志区分“进程仍活”与“DB 判 dead” |
| L3 heartbeat keepalive | MFW-04/05/06 | 按 heartbeat interval 写 active `final_fragment_wait`；复用完整 heartbeat 字段；deadline 独立 | 调用次数上界、phase、版本字段、最终 stopped 顺序全断言 |
| L4 异常与 pipeline | MFW-07 + fragment tiny | 只补必要错误传播/日志，不扩大语义 | 全量 pytest、fragment Checker、终态目录人工检查 |

实现可在 finally 中维护独立 `next_final_heartbeat`/last-write monotonic 值，或提取一个
可测 helper；不得用每个 poll 都写 heartbeat 的方式制造共享存储压力。若一次
`handle_fragment_latest` 超过 heartbeat interval，应在完成后立即补 heartbeat，但不需要
另起 writer 线程。

## 6. 验证、报告与文档

登录节点执行 INDEX G1 和 MFW-01。compute 节点运行 retention、fragment pointer、
liveness/learner 聚焦组、全量 pytest 与一次 fragment tiny；无需 2/9 节点。

报告目录：
`reports/imp_plans/20260719-second-review/N5-N6-maintenance-and-final-wait/`。
MFW-04 artifact 必须包含受控时钟、heartbeat timestamps/status/phase 序列、
dead_after/heartbeat interval 和 liveness 结果。

同步：

- `docs/04-data-flow.md`、`docs/modules/storage.md`：payload 目录无 metadata 扫描；
- `docs/03-runtime-flow.md`、`docs/modules/runtime-learner.md`：fragment final wait active
  heartbeat 与最终 stopped 顺序。

## 7. 失败升级

若 heartbeat 测试三连败，不先放宽 dead_after 或缩短 poll；在 `code_review.md` 检查
monotonic 与 wall time 的职责、`handle_fragment_latest` 阻塞边界、finally 异常路径和
原子 heartbeat 替换。若 MFW-02 暴露仍有 runtime `.meta.json` 生产者，暂停删除分支，
先确定其协议身份和 live-set，不能只加注释掩盖双布局。

