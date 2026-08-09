# 模块参考：syncer runtime

## `syncer_entrypoint.py`

验证 descriptor 后才打开 authority。candidate 轮询取得 lease，启动独立 renewer 并发布 epoch heartbeat；dynamic pool 初始化后，entrypoint 在最多 5 秒的窗口中处理 initial admission，再进入会 import Torch/model 的 runtime。这让 admitted learner 与 syncer model/v0 初始化重叠；窗口不是 barrier，late request 继续由主 loop 扫描。renewer 失败会使主 loop fail。异常路径发布 error control 并调用 `fail_leader`，正常路径 release。entrypoint 不拼 SQL、不直接 qsub/qstat。

## `syncer_v4.py`

主 loop：

1. reconcile predecessor publication；必要时初始化 global v0；
2. 扫描 admission request，每个 observation 独立错误边界；
3. 摄取 current contributor 的 receipt/proposal并发布 receipt ack；
4. 检查 outer/direct-token target；
5. fenced selection、stable tensor reduction、outer step；
6. immutable weight/outer publication、intent、commit和 latest control；
7. `MaintenanceService` 执行有界 audit archive/partition compaction 和 identity-checked GC；
8. close/drain/final acknowledgement 与 terminal publication。

selection conflict 返回 typed result并重新加载 committed authority head，不消耗 failed batch service credit。旧 classic/fragment writer、shared CSV、recovery submission 和 launch-outbox loop 已删除。

## `pbs_scheduler.py`

提供 scheduler adapter 和 observation normalization；operator/scheduler identity 不直接授予 authority。runtime entrypoint不调用 qsub/qstat，scheduler uncertainty通过明确 tool/request和 leader command处理。
