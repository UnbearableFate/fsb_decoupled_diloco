# plan05 PREFORMAL checkpoint 证据补充审查

- Review kind：`critical-incremental` 前的 coordinator current-state 审查
- Target commit：`18f26ae428deb02e063dd4d06665d0231717fb8b`
- Target tree：`b47c816ad5e06bcde1e7c53b04a9829025369893`
- Source fingerprint：`sha256:fc61dbdb13d8ecbdb855c41f01487bd4ad058da319b9d258a0620daaaa4c5974`
- 验证：PBS `2531884.opbs`，`mg0856`；Ruff、focused `261 passed`、full `592 passed`、website lint、14 项 rendered-site test 全部通过。

本结论由 coordinator 在读取仍在运行的外部 reviewer 结果前独立完成。它补充 `coordinator_remediation_af1eb61ed678d7c30017da4eebe78a3a00335a74.md`，后者对 PF-01 至 PF-06 的处置继续有效。

## CP-01 — `fixed`

前一版 independent formal supervisor 虽然验证了 `global_versions` 的连续版本、merge 和 token identity，却没有把每个 committed version 绑定到实际 weight/outer-optimizer checkpoint。损坏、缺失、可写、非 regular 或 authority identity 指向非 canonical 路径的 checkpoint 仍可能被正式实验误判为 PASS。

当前 `_publication_object_evidence()` 对 v0 至 v10 的 22 个对象逐一验证：

- predecessor 连续；
- path 精确等于 epoch、owner、version 和 publication ID 推导出的唯一 canonical path；
- 所有父级均为真实目录，叶子为不可写 regular file；
- 实际 byte size 和流式 SHA-256 精确等于 durable `global_versions` identity。

证据被写入正式 authority result 的 `publication_objects`。现有 `tests/harness/test_plan04_experiment.py` 是该 formal oracle 的唯一 test owner；健康 fixture 生成全部 22 个不可变对象，并以 checkpoint SHA mutation 证明该检查能改变 acceptance。

## Current-state 结论

Coordinator 已重新检查 formal supervisor 的 workload、token ledger、hard-crash、replacement、attestation 和 checkpoint object 路径，以及对应 config、launcher 和 mutation tests。没有发现新的 blocking finding。外部旧 target 的任何有效 finding 仍须在读取后逐项处置；关键不变量已改变，因此正式实验前必须在当前 target 上完成固定模型 `opencode-go/deepseek-v4-flash` 的 `critical-incremental` 复审。

Verdict: APPROVE
