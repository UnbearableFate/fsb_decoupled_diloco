# B4：fragment input-closed 与 terminal drain

## 性质与边界

这是语义变更，不与 S3 行为保持重构混做。S3 已交付共享 `all_expected_learners_stopped`、参数化候选收集/缺文件降级；本计划只让 `run_fragment_syncer` 在全部预期 learner 明确 stopped 后完成一次终端宽限、消费仍合格的 fragment proposal，并在无剩余输入时以 `input_exhausted` 退出。fragment resume、发现面复杂度和 full 路径均不在范围内。

## RED 与完成谓词

1. B4-01：受控时钟下，fragment learner 全停且没有 pending proposal；现实现继续 `fragment_quorum_wait`，按正式 `no_progress_timeout_seconds=3600` 推导出一小时空等，RED 必须断言其未走 input-closed。
2. B4-02：全停但目标 fragment 尚有一个合格 proposal；terminal drain 必须允许低于 `quorum_min` 的最后一次 merge，且不得选择 future/stale/缺文件 proposal。
3. B4-03：一次终端宽限后重新 ingest heartbeat/metadata；宽限只执行一次，之后没有 proposal 即发布 `input_exhausted` stop。
4. B4-04：active/dead/缺失 learner 不证明 input closed；继续现 quorum/no-progress 语义。
5. B4-05：fragment SQLite、`latest.json`、summary、history 与 payload 清理一致，所有 learner stopped；1 节点 tiny 和 2 节点 fragment debug 通过。
6. full 受控测试、tiny publication 投影与全量 pytest 不变。

## 实施顺序

先给 fragment terminal selection 提取/补齐只读测试接口，复用 `all_expected_learners_stopped` 与 `UpdateProposalSource`；再在 `run_fragment_syncer` 的 quorum 判断前接入 `terminal_input_closed → one-time grace → terminal selection → input_exhausted`。terminal selection 的 fragment/version 上下文字段必须保留，低 quorum 只能在 input-closed 后启用。失败按 `plans/AGENTS.md` 逐次记录，三连败停止局部试错。

## 验证与证据

登录节点只运行 lint、shell syntax 和静态检查；pytest/tiny 在 compute 节点。证据目录建议为 `reports/imp_plans/followups/B4/`。fragment 不使用 full-only 的 `check_plan01_invariants.py`；以 `fs_diloco.analysis assert-fragment-smoke`、DB/latest/summary 对账以及无 `error`/`no_progress_timeout` 事件为权威门禁。无需 9 节点验证；下一次正式 fragment 作业前必须完成本计划。
