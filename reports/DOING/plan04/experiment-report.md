# plan04 Dynamic Full 实验报告

## 结论

本任务已于 2026-08-11 21:23 JST 按用户要求停止，未完成全部正式 gate，不能形成最终实验结论。停止时冻结目标为 commit `12ae38993d94cce8d15b1e842c9123d22d5148b3`，source fingerprint 为 `sha256:73614127d64bf8ae1dd23b763c5d8c883ba3084e7d2e97dfb5af1bf8591765ee`，source scope 为 clean。最终 source 已通过 focused `136 passed`，以及 Ruff、49 个 plan04 focused tests和完整测试集 `592 passed`；此前诊断数据证明 loss 比较方法和已定位的性能根因，但不能替代 latest target 的正式证据。

latest target 尚未运行两个 2,000-step baseline、normal 及六个故障或错峰场景。因此 `formal_manifest.json` 的 gate artifact 保持 `null`，`requirements.csv` 保持 `pending`，FINAL 审查和归档均未执行。停止时无未完成的已知自有 PBS 作业。

## 正式工作量

- 输入：固定 revision 的 GPT-2 与 WikiText-2，BF16 模型，micro batch 2，gradient accumulation 8，block size 1,024。
- Baseline：8 个节点、每节点 1 个 GPU rank、每 rank 2,000 optimizer steps。Periodic Average 每 200 steps 平均一次参数，共 10 次。
- Dynamic Full：8 个独立 learner job、8 个固定 stream、每轮 200 local steps、10 个 global versions；syncer 每次精确合并 4 个 learner parameter。
- 指标：baseline loss 为 8 个 rank 最后 5 个已记录 optimizer coordinate 的均值；Dynamic Full loss 为 8 个 terminal stream 最后一次 proposal loss 的均值。时延使用 artifact 创建至 terminal 的 wall time，并显式保留两类拓扑和协议差异。

## 已失效的性能诊断

| 模式 | PBS job | 完成状态 | 工作量 | 同步行为 | Scheduler 时延 | 诊断 artifact |
|---|---:|---|---|---|---:|---|
| DDP | `2529632.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | 每 step gradient sync | 8 分 17 秒 | `logs/torch_ddp_baselines/20260811_193646_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json` |
| Periodic Average | `2529633.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | steps 200、400、…、2,000，共 10 次 | 7 分 25 秒 | `logs/torch_ddp_baselines/20260811_193646_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json` |

两个 baseline 的 run manifest、resolved config、8 个 rank 的 metrics/logs、checkpoint 与 source identity 均已保存，但因后续 payload verification 性能修正而不再构成最终 gate。`bace678` normal 的 proposal ingest 间隔约为 28 秒；修正后独立 compute benchmark 验证同一正式大小 payload 仅需 0.436 秒。新 target 的 baseline 和 normal 将重新运行，统一 loss 和 wall-time 仍由 `tools/summarize_runs.py` 一次性严格生成。

Archive-aware 汇总器已对这组失效但完整的同 target 数据完成诊断重放：Dynamic Full final mean loss 为 `3.167263`，相对 DDP 的 `2.965163` 高 `6.82%`，相对 Periodic Average 的 `3.039461` 高 `4.20%`，均在 20% 阈值内；旧 scalar finite scan 使 Dynamic Full 时延为 `1,326.09` 秒，相对两个 baseline 分别高 `176.50%` 和 `207.79%`。该数据只用于证明性能根因与 loss 比较方法，不替代 current target 的正式 evidence。

## 已失效的第二组 Baseline

| 模式 | PBS job | 完成状态 | 工作量 | 同步行为 | Health artifact |
|---|---:|---|---|---|---|
| DDP | `2529911.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | 每 step gradient sync | `logs/torch_ddp_baselines/20260811_202130_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json` |
| Periodic Average | `2529912.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | 每 200 steps，共 10 次 | `logs/torch_ddp_baselines/20260811_202130_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json` |

两个 baseline 均绑定 clean target `5b4dab6`，且各自正确完成 2,000 steps；但同 target 的 normal 暴露 `global_only` 会在每 learner 仅 1,400 steps 时关闭。因 source 必须修正，该组 baseline 不再构成正式 gate。严格统一 loss、artifact wall time 和 20% 比较只对下一个 current target 重新生成。

## 正式 Baseline

| 模式 | PBS job | 完成状态 | 工作量 | 同步行为 | Health artifact |
|---|---:|---|---|---|---|
| DDP | `2530049.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | 每 step gradient sync | `logs/torch_ddp_baselines/20260811_204941_torch_ddp_gpt2_wikitext2_8n_2000/final_health.json` |
| Periodic Average | `2530050.opbs` | `FINISH` / `PASS` | 8 ranks × 2,000 steps | 每 200 steps，共 10 次 | `logs/torch_ddp_baselines/20260811_204941_torch_periodic_average_gpt2_wikitext2_8n_2000/final_health.json` |

两者均绑定 clean target `0f7d3aa`，但 exact-workload normal 随后暴露 v10 后重复等待不存在的 v11；该组 baseline 因 source 修正而失效，不构成最终 gate。

## Dynamic Full 场景

| 场景 | PBS supervisor | Run ID | 关键 oracle | 结果 artifact | 状态 |
|---|---:|---|---|---|---|
| normal | `2530098.opbs` | `plan04_normal_20260811_210535` | v10 merge fence 成立；v10 后不得等待不存在的 v11 | telemetry 与保留 run root | 失效：重复 120 秒空等 |
| staggered_4_4 | 待提交 | 待生成 | normal oracle；精确 4+4 bootstrap timeline | 待生成 | 待执行 |
| staggered_3_3_2 | 待提交 | 待生成 | normal oracle；精确 3+3+2 bootstrap timeline | 待生成 | 待执行 |
| learner_loss | 待提交 | 待生成 | qdel receipt；一个 replacement；fence 前进；旧 fence 无越界 effect | 待生成 | 待执行 |
| staggered_learner_loss | 待提交 | 待生成 | 精确 4+4 timeline；一个 replacement；terminal oracle | 待生成 | 待执行 |
| syncer_loss | 待提交 | 待生成 | qdel 前 active lease；旧 epoch expired；新 epoch released | 待生成 | 待执行 |
| dual_syncer | 待提交 | 待生成 | 第一 syncer 存活时第二个被 fenced；删除后完成 takeover | 待生成 | 待执行 |

## 性能比较

正式统一 CSV 和 20% 比较 artifact 待 normal gate 完成后生成。任何 loss 或 wall time 的绝对相对差异超过 20% 时，将保留根因证据与处置结论，不修改阈值。

## 实现修正

正式实验前后的有效运行暴露并修正了七项执行问题：proposal scan 过去会在单次 merge decision 中验证全部可见大 payload；accepted command replay 过去无法与首次 accepted 区分；terminal drain 过去会重复验证已 terminalized fence 的 payload；正式大小 BF16 payload 过去以 Python scalar loop 执行 finite scan；终态 evidence consumer 过去只读取 hot authority table；global v10 过去会在每 learner 未达到 2,000 steps 时提前关闭；joint horizon 后 learner 过去仍等待不存在的 v11。当前实现分别采用单个新 proposal 的有界接纳、`exact_replay` 结果、payload read 前 terminal snapshot 过滤、向量化 finite check、统一的受验证 logical hot+archive view、`local_and_global` 双 horizon和 final-horizon non-blocking adoption，并继续保留 authority 事务内 fence 校验与 payload identity/digest/schema 校验。完整失败事实和失效 target 映射见 `failures.md` 与 `formal_manifest.json`。

## 验证与审查

- 精确输入预热：PBS `2528984.opbs`，完成 2,048-token BF16 GPU forward/backward micro-step，loss `3.7773797512054443`。
- 最终 source target focused tests：`136 passed`。
- 最终 source target 门禁：Ruff、49 个 plan04 focused tests、完整测试集 `592 passed`。
- 已归档真实 run 重放：scenario terminal oracle 与 unified diagnostic summary 均通过。
- 按本任务指示跳过 multi-agent review。PREFORMAL 和 FINAL 均只由当前 Codex 对 current state 与正式证据执行审查。

## 证据索引

- 正式 gate 注册：`formal_manifest.json`
- Requirement 验收：`requirements.csv`
- 里程碑：`progress.md`
- 有效失败与失效证据：`failures.md`
- PREFORMAL 审查：`reviews/preformal.md`
- 最终证据审查：待生成 `reviews/final.md`
