结论：当前实现已经超过“最小 fresh-only 原型”，大致处于“Stage 1 主体完成 + Stage 2/3 部分基础能力”的位置；但还不能说完成了 Stage 2–5，更不能支撑计划中的 C2–C5 论文主张。

最值得继续的是：有界存储、真实 crash recovery、性能基线和代表性长跑。完整异步保存、stale 多种子大矩阵和多 syncer 暂时不应排在前面。

## 一个前置问题

[05-RESEARCH_PLAN-v1.4-draft.md](/work/xg24i002/x10041/fsb_decoupled_diloco/plans/05-RESEARCH_PLAN-v1.4-draft.md:1) 自己声明是“非权威草案”，依赖的 `STAGE0-4_SPEC.md`、v1.3 source、`PROGRESS.yaml` 和 `reports/stage*` 在当前 checkout 中都不存在。因此：

- 下述结论是对“这份草案”和“当前工作区代码”的实质对照；
- Stage 0/1 的历史验收只能说“计划记录为完成”，当前仓库无法独立复核；
- 正式继续前，应先产出一份反映现状的 v1.5/ADR，否则后续很容易继续实现互相矛盾的目标。

## 当前实现覆盖情况

| 阶段 | 当前状态 | 已实现 | 主要缺口 |
|---|---|---|---|
| Stage 0 | 计划称完成，但不可审计 | 草案记录了仿真、FS 微基准、数学 oracle 结果 | 引用的报告、原始 CSV、Spec 和 Checker 证据不在当前仓库；S0A-03 仍未完成 |
| Stage 1 | 主体已实现，验收未完全闭合 | FS-only 数据面、payload-first/meta-last、原子 rename、safetensors、SQLite 状态机、quorum/grace、心跳、外层优化器、full/fragment 运行、1/2/9 节点验证 | ≥1B token 长跑未完成；当前实验不是严格 fresh-only；部署仍是单个 `mpirun` 作业而非真正独立 PBS jobs |
| Stage 2 | 部分实现 | 完整耗时 telemetry、CPU/GPU 资源指标、BF16 上传、fragment 直接抽取、W&B/CSV 分析 | 没有后台 publish/adopt pipeline、latest-wins backpressure、慢 FS 注入、no-communication goodput 基线 |
| Stage 3 | 基础能力部分实现 | 临时文件+fsync+rename、full-mode resume、DB dump、learner sleep/skip/crash 开关 | fragment resume 明确未实现；没有自动重启、kill -9 矩阵、恢复时间证据；发布与 DB 状态之间仍有 crash window |
| Stage 4 | 只有表面接口，不是计划中的算法 | 有 `max_staleness_versions` 和 `tokens/(1+λs)` 权重 | 没有 base-relative displacement、Q_fresh、base payload 保留、future/missing-base 拒绝和完整拒绝归因；当前实现不能算计划中的 stale-aware |
| Stage 5 | 基本未开始 | 做了 full vs fragment、FP32 vs BF16 的小型 50x10 工程对照；实现了 balanced-tensor | 无网络基线、naive FS 基线、延迟扫描、对象存储微基准、容量曲线、RDA、1000+ outer updates |
| Stage 6 | 工具骨架较完整 | 文档、PBS 脚本、分析器、fragment 自动断言、checkpoint 导出/lm-eval 接口 | 无论文、claims→证据表、冻结主实验、一键复现主图和完整 artifact |

## 已经做得比较扎实的部分

### 1. FS 通信协议核心

当前已经具备：

- 临时文件写入、`fsync`、`os.replace` 原子发布：[atomic_io.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/storage/atomic_io.py:23)
- learner 先写 tensor，再写 metadata 作为提交点；
- syncer 本地 SQLite 管理 pending/selected/applied/dropped 状态；
- global/fragment 权重和外层优化器状态版本化；
- `latest.json` 作为唯一可见性指针；
- quorum、grace window、staleness window、liveness；
- Nesterov/AdamW 等显式外层优化器。

所以 C1 的“共享 FS 可以作为数据面”已经有不错的原型级证据。

### 2. Fragment 主路径

已经实现：

- `balanced_tensor` 分片和完整覆盖校验：[fragment_index.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/protocol/fragment_index.py:37)
- per-fragment version、权重和 optimizer state；
- round-robin fragment 调度；
- learner 增量采纳；
- fragment materialization；
- 最新修改后的直接参数切片抽取和 BF16 上传。

两种 9 节点 50x10 已经稳定完成。但它们只有 10 次 merge、约 65.5M accepted tokens，而且 fragment run 的实际 staleness 全为 0：[analysis_summary.json](/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/codex_bf16_fragment_50x10_20260716_1724/analysis_summary.json:236)。因此它们证明了工程主路径，不证明 stale 或长跑有界性。

### 3. 可观测性和分析工具

当前 metrics 已覆盖 learner 写入、tokens/s、cycle step time、syncer read/aggregate/outer/publish、fragment staleness、资源利用率等：[metrics.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/observability/metrics.py:27)。

这部分足以作为后续性能实验的基础，只需要补：

- snapshot/staging 时间；
- adopt pause；
- proposal discovery latency；
- accepted-token efficiency；
- 分原因 discard rate；
- adoption lag；
- bytes/token；
- 显式 syncer duty cycle。

## 当前最重要的设计偏差

### 1. 当前 stale 不是计划中的 stale

计划要求旧 base 上的 displacement 重建和 Q_fresh 锚定：[研究计划](/work/xg24i002/x10041/fsb_decoupled_diloco/plans/05-RESEARCH_PLAN-v1.4-draft.md:304)。

当前做法则是直接平均 stale learner 的绝对参数：

```text
p̄ = weighted_average(local_params)
grad = current_theta - p̄
```

对应代码在 [syncer.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:1448)。这不等价于把旧 base 上的更新 displacement 搬到当前 base。

而且 eligibility SQL 没有限制 `base_version <= current_version`，[sqlite_store.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/storage/sqlite_store.py:288) 中 future-base 也会满足条件，随后 staleness 又被截为 0。

建议：在完整 Stage 4 语义实现前，正式实验先把 `max_staleness_versions` 设为 0。当前 50x10 配置设为 2，与计划“Stage 1–3 使用 S_max=0”矛盾。

### 2. Fragment 状态当前不是有界的

这是 C1/C5 前最大的工程缺口：

- fragment learner 不执行 update retention；
- applied/dropped fragment tensors 和 meta 不会移动或删除；
- fragment 权重和 optimizer 历史版本没有对应 retention；
- syncer 每轮重新 glob 全部 pending metadata：[syncer.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:470)
- SQLite 历史行也不会压缩。

因此 fragment 模式的文件数、存储量和 discovery 成本会随 update 数线性增长，无法通过计划中的 1000+ update/C5 验收。

### 3. Crash recovery 还不是论文级能力

full mode 可以从 global weight、outer state 和 DB dump 恢复：[syncer.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:388)，但：

- fragment resume 直接抛 `NotImplementedError`：[syncer.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:899)
- global `latest.json` 在 updates 被标为 applied、DB dump 之前发布：[syncer.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/runtime/syncer.py:1465)
- 若在这个窗口 kill，latest、DB dump 和 proposal 状态可能不同步；
- 当前 resume 单测只覆盖初始化版本恢复，没有注入 publication 中断：[test_resume.py](/work/xg24i002/x10041/fsb_decoupled_diloco/tests/test_resume.py:10)
- `mpirun` 中一个 learner 非零退出通常会终止整个作业，不能证明独立 learner 崩溃后其余节点继续。

所以 C3 目前只有实现基础，没有证据。

### 4. 实际部署与计划场景不同

计划设想：

- 独立批作业；
- 进程之间只能共享 FS；
- syncer 使用 CPU node；
- 默认 layer-aligned fragment。

当前 9 节点脚本则是一个 co-allocated MPI 作业，rank 0 使用 GPU 跑 syncer：[PBS 脚本](/work/xg24i002/x10041/fsb_decoupled_diloco/scripts/miyabi/run_9node_fragment_gpt2_wikitext2_50x10.pbs:104)，分片默认 `balanced_tensor`：[配置](/work/xg24i002/x10041/fsb_decoupled_diloco/configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml:97)。

数据面确实没有使用 MPI 通信，但还没有证明“独立作业、不同启动时间、无法建长驻网络服务”的主场景。

### 5. 当前训练栈不能执行计划中的质量主实验

当前模型始终通过 `from_pretrained()` 加载：[hf_model.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/modeling/hf_model.py:58)，没有 from-scratch 初始化模式。

数据路径会把整个 shard 的文本和 token 全部装入 Python list，再无限循环：[hf_data.py](/work/xg24i002/x10041/fsb_decoupled_diloco/fs_diloco/modeling/hf_data.py:75)。这适合 WikiText-2 冒烟，不适合 FineWeb/C4 的 3–8B token 实验，也没有 held-out eval loss/matched-token 多种子执行器。

因此目前只能做系统功能实验，不能验证 H3b 或其他质量主张。

## 哪些值得继续

### P0：必须继续

1. **冻结一份符合现状的权威计划**

   明确选择：

   - fresh-only 默认还是立即做 stale；
   - layer-aligned 还是 balanced-tensor；
   - CPU syncer 还是 GPU syncer；
   - 独立 PBS jobs 还是 co-allocated launcher；
   - 论文保留哪些 claims。

2. **先关闭错误/未完成的 stale 路径**

   正式配置设 `S_max=0`；同时补 future-base 拒绝。若继续 Stage 4，再实现完整 displacement/Q_fresh 语义。

3. **实现 fragment 有界存储和有界 discovery**

   包括 applied/dropped artifact GC、fragment checkpoint retention、DB 压缩或分区、避免每轮全目录扫描。这是 C1/C5 和长跑的必要条件。

4. **完成 Stage 3 recovery**

   - fragment resume；
   - syncer publication crash window；
   - 独立 learner 重启；
   - syncer/learner kill -9 矩阵；
   - 明确只主张进程级 crash，不主张节点断电。

5. **补代表性训练数据路径**

   支持 from-scratch、流式/预分词数据、held-out eval loss、matched-token 和多种子控制。否则后续 GPU 长跑没有论文价值。

### P1：论文必须，但可以在 recovery 后做

1. no-communication 本地训练基线；
2. 网络版 decoupled baseline；
3. naive full-checkpoint FS baseline；
4. discovery/adoption/goodput/bytes-token 完整 telemetry；
5. 两个模型规模、多个 fragment 大小和 M/Q 的 syncer 容量曲线；
6. 1000+ outer updates 长跑。

当前 BF16 优化使 payload 减半，但端到端只改善约 1–2%：[实验记录](/work/xg24i002/x10041/fsb_decoupled_diloco/docs/07-operations.md:116)。这说明应先测量完整 goodput，再决定是否建设复杂异步管线。

### P2：条件性继续

- **后台异步 save/adopt pipeline**：当前规模不是首要瓶颈。只在 410M/1B 或 1/10 慢 FS 下测得 pause 超过 2% 后实施。
- **Stage 4 stale 多种子矩阵**：先恢复 S0A-03 原始数据并完成零算力归因。仿真已经预测收益接近 0，不建议立刻投入 3–8B token × 多种子的完整矩阵。
- **对象存储微基准和可见性延迟扫描**：只有保留“协议可一般化到对象存储/跨区域”的主张时才必须。
- **RDA、layer-aligned/balanced-tensor 消融**：作为论文补充，不是系统闭环前的阻塞项。

### 现在不建议继续

- **多 syncer Phase 2**：当前 50x10 数据中，粗略的 `read+aggregate+outer+publish` 占 interval 约为 fragment 2.8%、full 10.1%，还没有单 syncer 饱和证据。应等待正式容量曲线。
- **直接跑大规模 stale 质量矩阵**：语义和数据管线都未就绪，算力投入会产生不可用结果。
- **继续堆短跑配置**：现在缺的是可证伪实验和恢复/有界性证据，不是更多 10-step smoke run。

总体上，我建议把后续主线收敛为：

```text
权威设计冻结
→ S_max=0 + 有界存储
→ 独立 PBS jobs
→ fragment crash recovery
→ 三类性能基线
→ 1000+ update 容量/长跑
→ 再决定 async pipeline、stale 和多 syncer
```

本次仅做了当前工作区的只读审查，没有修改文件或启动运行任务。