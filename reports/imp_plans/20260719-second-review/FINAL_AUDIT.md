# 20260719 second-review 实施终审

## 结论

N1–N8 的 implementation、focused regression、真实 tiny/cross-node evidence、Checker、
静态门禁和稳定语义文档均已闭合。最终门禁为 `PASS`；没有 9-node 作业，也没有触发
“超过 50-local-step × 10-global-step 的 9-node 结果”文档条件。

## Requirement → evidence

| 范围 | 结果 | 主要证据 |
| --- | --- | --- |
| RSM-01–04 | PASS | `tests/test_resume.py`、`tests/test_liveness.py`：resume transaction、rollback、fence、new pointer takeover |
| RSM-05–08 | PASS | `tests/test_syncer_selection.py`、`tests/test_terminal_state_machine.py`：三态、reopen、closed-empty、full/fragment 单次 discovery |
| RSM-09 | PASS | PBS `2421684.opbs`，`N1.../artifacts/20260721_rsm09_2node_resume_evidence.json`：old stopped v1 → fenced resume → active on second node → v2 |
| RSM-10 | PASS | completed run fail-closed 与 error marker archive tests |
| AST-STOP-01–07 | PASS | adoption focused tests；stop/timeout 消歧、direct adoption 优先、stop 后不建 reference |
| AST-STOP-08 | PASS | `second_review_predict_20260721`，v3，Checker PASS，`global_prediction_start_skipped_on_stop` → process_exit |
| PUB-TEL-01–04/06 | PASS | publication + metrics focused tests |
| PUB-TEL-05 | PASS | `second_review_full_20260721` 非空 metrics，false 模式四字段严格为零，Checker PASS |
| MFW-01–03 | PASS | production static search、retention/fixed-pointer tests |
| MFW-04–06 | PASS | `20260721_mfw04_real_finalwait_evidence.json`：3.53s wait > 1.5s dead-after，0.505s max heartbeat gap，dead=0，最终 stopped |
| MFW-07 | PASS | final adoption injected exception：诊断 → stopped heartbeat attempt → re-raise；focused 4 PASS |
| TCP-01–08 | PASS | hardlink/copy/crash/retry/manifest conflict matrix |
| TCP-09 | PASS | terminal partial selector→capture integration；DB/latest bytes 不变、无 tmp |

## 最终门禁

- static：`bash -n scripts/miyabi/*.pbs`；全部 PBS `group_list=xg24i002` literal；
  compileall、Ruff、`git diff --check`、payload metadata scan negative check 全 PASS。
- final focused：`95 passed in 6.31s`；MFW finalization follow-up：`4 passed in 1.37s`；
  publication lint follow-up：`6 passed in 1.48s`。
- final full：`357 passed in 13.67s`。
- 真实 pipelines：full replace、predict、normal fragment 均完成并通过对应 Checker/assertion；
  final-wait run 是故意不可达 target 的 MFW-04 诊断分支，不作为正常 smoke 结果。
- 2-node RSM-09：job `2421684.opbs`，Exit_status=0，walltime 50s；扩展 Checker stdout
  `PASS`，structured artifact 亦为 `PASS`。

## 已知但不阻塞的失败证据

- 初次 1-node failpoint 命中 v0 initialization，未建立 resume 前置状态；已记录并改用
  外部精确 PID 控制。
- 1-node immediate post-commit kill 留下合法 committed v1、但缺失非权威 phase-A metric
  行，因此完整 Checker 正确 `BLOCKED`；没有放宽门禁。更强的 2-node run 等待 metric 与
  stopped DB/heartbeat 成立后再 kill，最终 Checker PASS。
- 两次 focused command/test fixture 失误与一次 Ruff E731 均先写 failures，再做窄修复；
  无同一 experiment 三连败。

## 文档同步

稳定语义已同步 architecture、runtime flow、data flow、configuration、operations、
runtime-syncer、runtime-learner、storage 和 `reports/run_analysis.md` 历史 telemetry 口径。
具体 job/run/checksum 只保存在本实施报告，不写入系统设计文档。
