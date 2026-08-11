# plan05 PREFORMAL coordinator 修缮后审查

- Review kind：`preformal-plan-complete` 修缮后 current-state 审查
- Target commit：`af1eb61ed678d7c30017da4eebe78a3a00335a74`
- Target tree：`dcd2b5560759fd76eba8f539aa1f08050cc93dde`
- Source fingerprint：`sha256:e37b8ce55e85ab2a9270d9f5fbe5ab79a675f3b8eee4b5dec4c1c2931f3ac24d`
- 验证：PBS `2531812.opbs`，`mg0865`；Ruff、focused `260 passed`、full `591 passed`、website lint、14 项 rendered-site test 全部通过。
- 范围：`execution.md` 注册的全部 formal source scopes，以及本 review packet 中的配置、PBS、Checker、实验 oracle、测试和当前文档。

本结论由 coordinator 在读取本轮外部 reviewer 结果前独立完成。仓库级清理搜索只命中旧字段的 strict rejection 或 absence assertion；当前实现中没有旧 membership 类型、旧 schema、旧配置、兼容 alias 或双 admission 路径。

## 初始 findings 处置

### PF-01 — `fixed`

`tools/summarize_runs.py` 现在接受 `acked` 或带正数 gap bound 的 `hard_crash` terminal fence。硬崩溃 stream 的步数从 durable `contributor_progress` 推导；无 telemetry 时只省略该 stream 的 loss coordinate。正常 ack 仍要求唯一 telemetry 和 proposal loss。现有 summary test owner 覆盖真实 current-schema hard-crash 行为。

### PF-02 — `fixed`

正式 authority oracle 使用统一 archive-aware reader 读取 receipts、updates、token fates 和 versions，并交叉验证：每周期 workload、receipt predecessor/cursor chain、proposal 一一对应、每版四个 applied proposal、每类 direct fate、token rollup、terminal direct tokens 和零余额。hard crash 只允许其冻结 fence 上一个已承诺但缺失的 proposal按 dropped 终结；未把 gap upper bound 计作 processed token。rollup mutation test 能改变 acceptance。

### PF-03 — `fixed`

Authorized replacement oracle 已串联同一 request/stream 的 capacity snapshot、scheduler-confirmed loss reason、唯一 qsub receipt transition、launch request 与 PBS job、旧/新 instance、stream epoch、receipt predecessor/cursor、admission history和旧 fence 后续 durable effect。正式 topology 同时要求 replacement job 的不可变 attestation 精确绑定 admitted instance。capacity action 必须与 durable productive/reserved counters 一致，不再把 startup-grace 内的 scheduler-confirmed replacement 错误限定为 low-capacity launch。

### PF-04 — `fixed`

`LeaderSession.admit_incarnation()` 强制 `bootstrap_slot` 与 `launch_request_id` exact-one。Bootstrap authorization 不接受 replacement 字段；replacement instance/reason 必须成对出现。最终 ownership writer 不再把 `stream_id` 隐式解释为 bootstrap slot。现有 launch-authorization owner 覆盖 missing、both 和 explicit bootstrap 行为。

### PF-05 — `fixed`

现有 `tests/harness/test_plan04_experiment.py` 已扩展为正式 oracle 的唯一 test owner，覆盖 no-failure exact merge、token rollup drift、bounded hard crash、authorized replacement capacity/qsub/cursor/fence boundary、actor attestation identity/topology和 mutation rejection；未新增重叠测试文件。

### PF-06 — `rejected-with-evidence`

Plan05 正式 supervisor 只调用 `summarize_runs.py` 生成单 run row，不调用可选的 DDP/Periodic Average comparison API。Plan04 在其最新 target 上按用户要求停止，明确没有完成可与 plan05 当前 source 严格匹配的 Dynamic Full baseline；现有诊断 run 的 source、completion contract 和 formal status 均不满足预注册 comparable 条件。Plan05 FINAL 必须输出 `incomparable`，不计算 20% threshold，也不以 DDP、Periodic Average 或旧 diagnostic data 替代。通用 baseline comparison 属于独立的 baseline 工具 surface，不参与 P05-R10，因此不会被当作 plan05 结论来源。

## 修缮后新增检查

- Formal workload oracle 精确冻结 8-stream、8-bootstrap、quorum 4、200 inner step、10 global version、seed 1337，以及 model、tokenizer、dataset、staleness、optimizer、dtype、completion 和 learner adoption identity。
- Actor topology 对每个 scheduler job 校验 immutable regular file、exact schema、self hash、run/descriptor/source/lock/model/data identity、canonical path、runtime PBS allocation、authority actor identity和初始 9 个不同 host；重复 scheduler attestation 会 fail closed。
- Capacity observation 被 launch request 引用后不会被 hot-history retention 删除；formal reader 因而能在终态可靠重建 observation→launch 链。
- U1 validation 为所有子进程显式传递当前 `sys.executable` 作为 `PYTHON_BIN`，避免不同 compute node 的系统 Python 版本改变网站 reference gate。

## 结论

全部 Critical/High finding 已修复，Medium finding 已修复或以不进入 plan05 evidence path 的直接证据拒绝。当前未发现会使三组正式实验接受 malformed state、拒绝注册合法状态或绕过唯一 admission/replacement 语义的 blocking finding。

Verdict: APPROVE
