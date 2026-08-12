# plan04 进度

## 2026-08-12 — INIT 与当前设计更新

- Source：branch `new_plan04`；branch point 与 workflow pin 均为 `f2ec3e886ce77b93497ab6cd3e306e5de13ef6a4`。
- 重新冻结当前 workload：固定版本 GPT-2/WikiText-2，Full Protocol 为 200 local steps × 25 global steps、quorum 4；DDP 与 periodic-average baseline 均为 5,000 optimizer steps。
- Baseline 与 normal 的初始 PBS walltime 调整为 40 分钟；每项实验只运行 1 seed，normal 相对两种 baseline 的 loss/time 阈值调整为 30%。
- 删除旧 Full Protocol baseline/timed 配置和 2,000-step baseline 入口；统一 one-line submission、summary 与 comparison 路径。
- 回滚全部 learner runtime-attestation 启动屏障。Terminal admission 不要求八个提交 job 全部进入 runtime；learner fault 在 60 秒边界只从当前 admitted bootstrap learner 中选择目标。

## 2026-08-12 — IMPLEMENT 与 focused 验证

- Miyabi compute jobs `2540105.opbs`、`2540108.opbs`、`2540120.opbs` 使用 1-node `interact-g` 验证当前变更。
- 最终 focused 命令覆盖 plan04 harness、syncer composition、summary tool 和 standalone baseline config/artifact/protocol tests，结果为 `49 passed`。
- 完整 pytest 在未提交 source 上得到 `585 passed, 5 failed`；五项失败均由 harness 正确拒绝 dirty validation source，分类为 `source-invalid`，不作为产品失败。当前实现提交并冻结 clean candidate 后重跑完整测试。
- Login node 上 `git diff --check`、修改 Python 文件 compile、全部适用 PBS/Bash `bash -n` 检查通过；literal PBS group 均为 `xg24i002`。
- 当前 `qstat` 无 active/queued job。下一步为提交实现、执行 clean-candidate 全量测试和 PREFORMAL 审查。

## 2026-08-12 — Clean candidate 全量验证

- Candidate commit：`4e905f31a8b136dd2a6f210944552ff6bbaa5aff`。
- Miyabi compute job：`2540212.opbs`，1-node `interact-g`，默认模块 `nvidia/25.9` 与 `nv-hpcx/25.9`。
- 命令：`.venv/bin/python -m pytest -q`。
- 结果：`591 passed in 34.74s`；PBS 使用 walltime 约 1 分 50 秒。
- 下一步：完成 PREFORMAL current-state 审查并冻结正式 target；随后提交两种 5,000-step baseline。

## 2026-08-12 — PREFORMAL 完成

- `FINAL_COMMON_TARGET`：`d0acc5f9f95cb3d3885baf947319b93d84caeff1`；source fingerprint `sha256:b5b51b507d339a2a61d8b937023a61131cb35fb588dc5131ecf814ae63c5b8c7`，13 个 formal source scope 均 clean。
- Current-state 审查关闭 terminal admission、baseline source 绑定、replacement latest-version oracle、baseline `PROJECT_ROOT` 传递和 supervisor walltime 余量问题；审查结论见 `reports/DOING/code_review/gpt-5_d0acc5f9f95cb3d3885baf947319b93d84caeff1_260812-2101.md`。
- Miyabi compute job `2540413.opbs`：focused tests `30 passed`；clean target 上 Ruff format/check 通过，完整 pytest `594 passed in 31.87s`。
- Login control-plane 上全部适用 PBS/Bash 语法和 literal group 检查通过；`qstat` 无 unfinished job。下一步提交同一 target 的两个正式 5,000-step baseline。

## 2026-08-12 — 首轮 normal 根因处理与 target 重冻结

- 旧 target 的 baseline `2540481.opbs`/`2540482.opbs` 均健康完成；training time 为 `1129.11s`/`1058.37s`。Normal supervisor `2540602.opbs` 的产品 workload 和 durable oracle 全部完成，training time `604.33s`，loss 相对 baseline 仅升高 `8.93%`/`6.63%`。
- Comparison checker 错把更短 `42.90%–46.48%` 的 training time 当成回退。根因是 quorum-4 Full Protocol 的 applied contributor workload 为 20,000 optimizer steps，而八 rank baseline 共执行 40,000 steps；详情见 `failures.md`。
- Checker 已收敛为 signed regression gate：仅 identity mismatch 或 loss/time 升高超过 30% 才失败。Miyabi compute `2540684.opbs` 上 focused `25 passed`，clean full suite `595 passed in 35.81s`。
- 新 `FINAL_COMMON_TARGET`：`7145197f3209fa67727bb0d458d0db38a81eb86d`；fingerprint `sha256:fcecd04f3ca5720cc3cd576889d8c20ad5d3f9dd9dd5725c7415b0549cdffa35`。复审结论见 `reports/DOING/code_review/gpt-5_7145197f3209fa67727bb0d458d0db38a81eb86d_260812-2140.md`。旧 target 正式证据失效，下一步从 baseline 重跑。

## 2026-08-12 — 当前 target 的 baseline 与 normal 完成

- DDP baseline `2540694.opbs` 完成 5,000 steps：run `20260812_214048_torch_ddp_gpt2_wikitext2_8n_5000`，最终 loss `2.935435`，training time `1127.79s`。
- Periodic-average baseline `2540695.opbs` 完成 200×25 steps：run `20260812_214048_torch_periodic_average_gpt2_wikitext2_8n_5000`，最终 loss `2.998504`，training time `1040.56s`。
- Normal supervisor `2540861.opbs` 完成：run `plan04_e1_normal_20260812_220108`，artifact `reports/DOING/plan04/artifacts/20260812_220108_e1_normal.json` 为 PASS；最终 loss `3.196270`，training time `605.51s`。
- Normal loss 相对 DDP/periodic-average 分别升高 `8.89%`/`6.60%`，training time 分别降低 `46.31%`/`41.81%`，均未触发 30% regression gate。三个 run 均绑定当前 target，并已写入 `runs/summary.csv`。
- 按并行策略提交 Experiments 2–7。首次并行提交暴露当前用户作业并发容量限制，导致 Experiments 4、5、7 的关键 actor 未及时运行；这些 run 已记为 `infra-invalid`，详见 `failures.md`，不修改产品代码或 oracle。

## 2026-08-12 — 两个 stagger 场景完成

- Experiment 2 supervisor `2541158.opbs`：run `plan04_e2_stagger_4_4_20260812_223229`，artifact `reports/DOING/plan04/artifacts/20260812_223229_e2_stagger_4_4.json` 为 PASS。两批 learner 首次提交间隔 `30.0001s`；version 25、100 个 200-step applied update、八个 acknowledged terminal fence 和全部 owned actor 终态均通过。Training time `617.38s`，最终 loss `3.197345`。
- Experiment 3 supervisor `2541231.opbs`：run `plan04_e3_stagger_3_3_2_20260812_224411`，artifact `reports/DOING/plan04/artifacts/20260812_224411_e3_stagger_3_3_2.json` 为 PASS。三批 learner 首次提交 offset 为 `0.07s`、`32.02s`、`60.00s`；只有前三个 learner 时 authority 已初始化但尚无 global version，随后完成 version 25。Training time `647.61s`，最终 loss `3.167654`。
- 两项 run 均绑定当前 target 并写入 `runs/summary.csv`。首次并行 run 的失败不作为正式证据；当前正式 artifact 使用资源空闲后的 fresh run root。

## 2026-08-12 — 切换至 debug-g 与 30 分钟预算

- `qstat --rscuse` 显示 `debug-g/interact-g` 使用率约 `31%–33%`，低于 `regular-g` 的约 `69%–70%`。按用户指示，plan04 supervisor、actor、scheduler-authorized replacement 和 baseline 统一改为 `debug-g`、walltime `00:30:00`。
- 新 target commit：`d2117d0c83f205eabd2246feec82a77c2e0230c0`；source fingerprint `sha256:2c651ee95fda24539fa597b85753706f16d40b847cde4cbeedba208f01a7998d`。变更只涉及 scheduler policy，没有修改训练 workload、协议行为或 oracle。
- Static PBS/Bash syntax、literal group、Python compile 和 `git diff --check` 通过。Miyabi compute `2541387.opbs` 上 Ruff check、55 项 focused tests、完整 `595 passed in 35.97s` 和 Ruff format check 均通过。
- PREFORMAL 复审结论见 `reports/DOING/code_review/gpt-5_d2117d0c83f205eabd2246feec82a77c2e0230c0_260812-2311.md`。此前 target 的实验保留为结果与运行时参考；最终 manifest 从本 target 选取共同 source identity 的正式证据。
- 用户随后明确收敛证据策略：queue/wall-time 预算切换不得废弃既有结果，只有功能逻辑变化才需要重跑。因此立即取消了误提交的 baseline `2541412.opbs`/`2541413.opbs`，继续从此前失败的 Experiment 4 开始。最终 manifest 将显式记录 `7145197...` 到 `d2117d0...` 仅为 scheduler policy 差异。

## 2026-08-12 — learner fault oracle 与 terminal contract 对齐

- Experiment 4 `2541414.opbs` 完成 version 25、replacement admission 和全部应用 workload，但 artifact 因三个已裁决 `hard_crash` terminal fence 被旧 oracle 拒绝。产品与统一 summary contract 均允许 `acked`/`hard_crash`，详情见 `failures.md`。
- Oracle 已收敛为只接受这两个协议状态，同时继续拒绝外部 stream；hard-crash bounded-gap 仍由统一 summary parser 强校验。修复提交 `a064ef8837ca033db883fe60879c4578b921b09c`，fingerprint `sha256:a0479788d97f5418cbc8e35c1816712e7795fe8ce05a2adce9291a16b251d296`。
- Miyabi compute `2541627.opbs`：Ruff format/check、26 项 focused tests 和完整 `596 passed in 40.09s` 均通过。复审结论见 `reports/DOING/code_review/gpt-5_a064ef8837ca033db883fe60879c4578b921b09c_260812-2335.md`。

## 2026-08-12 — Experiment 4 完成并收敛通过标准

- Experiment 4 supervisor `2541648.opbs`：run `plan04_e4_learner_failure_simultaneous_20260812_233703` 完成 global version 25。最终 Syncer `2541649.opbs` 的 PBS `Exit_status` 为 `0`，最终平均 loss 为 `3.288647`。
- 该 run 满足最新的三个通过条件：最终 Syncer 正常退出、训练达到 25 个 global step、最终平均 loss < 3.5。因此 Experiment 4 记为通过。
- 旧 supervisor artifact `reports/DOING/plan04/artifacts/20260812_233703_e4_learner_failure_simultaneous.json` 因 replacement 专用诊断报 FAIL。该诊断不再影响通过结果。`tools/summarize_runs.py` 已将 run 追加到 `runs/summary.csv`。
- 实验 supervisor 已改为只使用三个终态条件决定 PASS/FAIL。批次、replacement、authority、terminal fence 和 baseline comparison 继续写入诊断信息，但不再改变实验状态。

## 2026-08-13 — 新通过标准完成验证，Experiment 5 已提交

- 判定规则提交：`319081c8d57411a3e1b8ee724c82f810f25a3228`；source fingerprint `sha256:5ab9a68956dc718db713938957a0ab390cd00b0b5956e1740c6547154d772ebf`。该提交只修改实验 supervisor、测试和文档，不修改训练或协议功能逻辑。
- Miyabi compute `2541809.opbs`：Ruff format/check、focused pytest `271 passed`、完整 pytest `599 passed`。Validation artifact `reports/DOING/plan04/artifacts/validation_319081c.json` 为 PASS。
- Experiment 5 supervisor `2541824.opbs` 已提交至 `debug-g`。Run ID 为 `plan04_e5_learner_failure_staggered_20260813_000324`。提交前 `qstat --limit` 显示项目无运行作业，`qstat --rscuse` 显示 `debug-g/interact-g` 使用率为 31%。
- Experiment 5 已通过：最终 Syncer `2541825.opbs` 的 PBS `Exit_status` 为 `0`，global version 为 `25`，最终平均 loss 为 `3.305553`。Artifact `reports/DOING/plan04/artifacts/20260813_000324_e5_learner_failure_staggered.json` 为 PASS。Replacement succession oracle 仍报告诊断，但不影响最新通过结论。
- Experiment 6 supervisor `2541871.opbs` 已提交至 `debug-g`。Run ID 为 `plan04_e6_syncer_failure_20260813_002004`。提交前项目无运行作业，`debug-g/interact-g` 使用率为 10%。
- Experiment 6 已通过：最终接管训练的 Syncer `2541896.opbs` 的 PBS `Exit_status` 为 `0`，global version 为 `25`，最终平均 loss 为 `3.243430`。Artifact `reports/DOING/plan04/artifacts/20260813_002004_e6_syncer_failure.json` 为 PASS。
- Experiment 7 supervisor `2541938.opbs` 已提交至 `debug-g`。Run ID 为 `plan04_e7_dual_syncer_20260813_003704`。提交前项目无运行作业，`debug-g/interact-g` 使用率为 12%。
- Experiment 7 已通过：最终接管训练的 Syncer `2541950.opbs` 的 PBS `Exit_status` 为 `0`，global version 为 `25`，最终平均 loss 为 `3.245163`。Artifact `reports/DOING/plan04/artifacts/20260813_003704_e7_dual_syncer.json` 为 PASS。
- `qstat --limit` 显示项目运行作业为 `0/16`，`qstat` 显示无 unfinished job。七项 Full Protocol 实验均已完成，下一步为 formal manifest、FINAL 证据审查和归档。

## 2026-08-13 — FINAL 证据审查通过

- `reports/DOING/plan04/formal_manifest.json` 记录两种 baseline、七项 Full Protocol gate、三个精确 source identity，以及 queue/wall-time 和 harness-only 变更不废弃既有训练结果的证据策略。
- 完成逐项 machine audit：七项最终 Syncer 的 PBS `Exit_status` 均为 `0`；七项 terminal authority 均为 version `25`；七项最终平均 loss 均低于 `3.5`；每项只保留一个最终 model weight 和一个 outer optimizer state。
- Repository-wide static audit、PBS/Bash 语法、literal group、formal source clean 检查均通过。`qstat --limit` 为 `0/16`，`qstat` 无 unfinished job。
- Coordinator FINAL evidence review 结论为 `APPROVE`，见 `reports/DOING/code_review/gpt-5_319081c8d57411a3e1b8ee724c82f810f25a3228_260813-0103.md`。全部 requirement 已验证，下一步只执行 plan/report 归档。
