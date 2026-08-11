# plan05 唯一 stream-pool 协议实验报告

## 结论

Plan05 已在同一 clean source target 上完成实现、验证和三组正式实验。`FINAL_COMMON_TARGET` 为 commit `288f0c9d13e90ce597ddf0502e631aa509b53081`，source fingerprint 为 `sha256:d2e3b725d16de5c8d9768386cfabd42f94dbaf865156bb334e2c67ccf6fed3e4`。配置、descriptor、protocol、authority、admission、runtime、launcher、Checker、测试与当前文档现在只保留 stream/instance admission 模型；不再支持 static learner mode、双 schema、旧 fence 或旧 run root。

最终 U1、两个 functional harness 和三个 fresh-root formal scenario 均为 `PASS`。正常正式场景以 8 个独立 learner job 加 1 个独立 syncer job 在 9 个不同 compute host 完成 GPT-2/WikiText-2、200 local × 10 global workload。两种故障场景分别证明不替换的 bounded hard crash，以及真实 capacity observation 和 qsub receipt 授权的 replacement。

与 plan04 的性能比较为 `incomparable`。Plan04 没有在其 latest target 上留下完成且与 plan05 严格匹配的 Dynamic Full formal baseline，因此未执行预注册的 20% loss/wall-time threshold，也没有用 DDP、Periodic Average 或 diagnostic run 代替。

## 最终验证

| Gate | PBS | 拓扑 | 关键结果 | Artifact |
|---|---:|---|---|---|
| U1 | `2533273.opbs` | 1 compute node | Ruff；focused `264 passed`；full `595 passed`；website lint/test | `artifacts/validation_candidate30.json` |
| Functional no-failure | `2533382.opbs` | 4 learner + 1 syncer，5-node co-allocation | v4；12 applied、4 dropped proposal；ledger balance 0 | `artifacts/functional_no_failure_final_288f0c9.json` |
| Functional syncer takeover | `2533383.opbs` | 4 learner + 2 sequential syncer epoch | primary fenced；successor 完成 v4；无 stale commit | `artifacts/functional_syncer_takeover_final_288f0c9.json` |

## 正式工作量与输入

- Model：`gpt2` revision `607a30d783dfa663caf39e06633721c8d4cfcd7e`，BF16。
- Dataset：`Salesforce/wikitext` / `wikitext-2-raw-v1` revision `b08601e04326c79dfdd32d625aee71d232d685c3`，train split。
- Seed：`1337`；block size `1024`；micro batch `2`；gradient accumulation `8`。
- Membership：8 个 fixed stream、8 个 bootstrap instance；每个 actor 是独立 PBS scalar job。
- Optimization：200 inner step；10 global version；每版 exact quorum 4；terminal extra merge 为 0。
- Accounting：每个有效 cycle 处理 `3276800` token；每个 global version 应用 4 个 update，总 direct applied token 为 `131072000`。

## 正式场景

| 场景 | Supervisor | Run ID | Final loss | Artifact-to-terminal time | Durable 结果 | 状态 |
|---|---:|---|---:|---:|---|---|
| no-failure | `2533369.opbs` | `plan05_no_failure_20260812_050805` | `3.2378166584` | `275.48s` | 9 个 initial actor 分布于 9 host；40 applied update；8 个 ack fence；ledger balance 0 | PASS |
| failure/no-replacement | `2533281.opbs` | `plan05_failure_no_replacement_20260812_045342` | `3.2268198312` | `402.10s` | stream 2 在首个 receipt 前 hard crash；无 replacement；其余 stream bounded completion；ledger balance 0 | PASS |
| failure/authorized-replacement | `2533326.opbs` | `plan05_failure_authorized_replacement_20260812_050107` | `3.2325158224` | `366.90s` | stream 4 的 capacity→qsub→admission chain 完整；stream epoch 1→2；旧 fence 无 late effect | PASS |

三场均完成 terminal v10 和 40 个 applied update；每份正式 artifact 都包含 source/config identity、scheduler history、actor attestation、archive-aware authority evidence、receipt/cursor chain、token fate、checkpoint publication identity 和 cleanup ownership。no-replacement 的 victim 在 admission 后、首个 durable receipt 前被终止，因此该 stream 的 durable optimizer step 为 0；这是预注册 hard-crash boundary 的合法结果，不是丢失 evidence。

## Checkpoint 与归档证据

每场正式 oracle 都检查 v0 至 v10 的 weight 和 outer-state publication，共 22 个 identity。仍存在的 object 必须是 canonical path 上的 immutable regular file，并精确匹配 size 和 SHA-256。只有已经进入 archive、且 durable GC candidate 不再处于 pending/claimed 的旧 object 才允许记录为 `garbage_collected`；hot object 或未完成 GC 的 object 缺失会 fail closed。

## 审查

Coordinator PREFORMAL review 最初发现 hard-crash summary、token ledger、replacement chain、bootstrap authorization、mutation coverage、baseline 可比性和 checkpoint object identity 问题；全部 blocking finding 已在正式运行前关闭。外部 reviewer 始终只使用 OpenCode exact model `opencode-go/deepseek-v4-flash`，没有使用 Claude Code 或其他 OpenCode 模型。old target 的完整 review 超时且无输出；三个后续增量审查因各自 target 被 fresh formal failure 推翻而终止；最终 target 的结果与 coordinator disposition保存在 `reports/DOING/code_review/plan05/`。

## 证据索引

- 正式 target 与 gate：`formal_manifest.json`
- Requirement 验收：`requirements.csv`
- 里程碑：`progress.md`
- 有效失败与失效 target：`failures.md`
- U1、functional 和 formal artifacts：`artifacts/`
- PREFORMAL/FINAL review：`reports/DOING/code_review/plan05/`
