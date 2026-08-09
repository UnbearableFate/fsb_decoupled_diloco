# 数据流与持久化

## Run root

```text
<run>/
├── .identity                         # initializer reservation 的同 inode identity
├── .complete                         # immutable manifest；可见性提交点
├── run_config.resolved.yaml
├── control/
│   ├── run_descriptor.json
│   ├── run_source_manifest.json
│   ├── run_config.resolved.yaml
│   ├── artifact_policy.json
│   ├── bootstrap_complete.json
│   ├── syncer_metadata.sqlite3       # 唯一业务 authority
│   ├── syncer_epochs/e*/             # epoch heartbeat/latest/admission/ack/terminal
│   ├── registration_requests/
│   ├── registration_history_v4/
│   ├── registration_dispositions_v4/
│   ├── static_replacement_requests/
│   ├── latest.json                   # 可修复 cache
│   ├── stop.json                     # 可修复 cache
│   └── summary.json                  # 可修复/完成视图
├── updates/
│   ├── latest/                       # bounded pointer/cache surface
│   └── payloads/<contributor>/       # immutable proposal tensors
├── weights/epochs/e*/                # immutable global weights
├── optim/epochs/e*/                  # immutable outer state
├── heartbeats/                       # 非 authority 兼容/观测文件
├── metrics/
│   ├── <actor-kind>/<actor>/<attempt>.jsonl
│   └── attestations/<kind>/<actor>/<attempt>.json
└── audit/
    ├── batches/
    ├── partitions/
    └── command_receipts/
```

SQLite 的 WAL/SHM/journal 是 authority sidecar，任何清理都必须 fail closed，而不是按临时文件处理。

## Proposal 和 receipt

`FullUpdateProposalV2` 绑定 run、stable contributor、完整 contributor fence、cycle sequence、base global version、effective token、payload relative path/size/SHA 和训练 cursor。路径必须在 run root 允许的 immutable payload namespace 内。

`CycleReceiptV1` 另外绑定 previous receipt hash、数据 cursor、processed/effective/local-discarded token fate 和可选 proposal ID/digest。authority 只允许连续 sequence/hash/cursor；receipt-only cycle 也必须入账。

## Publication

每次 merge 的顺序是：

```text
selection batch
  → compute next theta/outer state
  → publish immutable weight + outer objects
  → prepare publication intent
  → fenced commit verifies both objects/theta
  → publish epoch latest pointer/head
```

文件路径包含 epoch、owner、version 和 publication ID，避免 successor 与 predecessor 复用同名对象。fixed `control/latest.json` 可以损坏或落后；learner 的授权路径是 live epoch heartbeat → head → version pointer → immutable object identity。

## SQLite 域

v4 schema 的主要域包括：

- identity/schema/command journal/leader epochs；
- static binding 或 dynamic stream/instance/launch state；
- proposal observation、visibility、quarantine、frontier；
- cycle receipt、contributor progress、token fate/rollup；
- selection batch、service credit、publication intent/artifact/global version；
- controller、terminal contributor fence 和 terminal record；
- audit batch/partition/GC 索引。

static 与 dynamic schema 可以有不同的物理 membership 表，但共享 proposal、accounting、selection、publication 和 terminal 语义。fresh v4 DDL 不创建 `fragments`、`fragment_versions`、`fragment_updates` 或 `fragment_proposal_frontiers`。

## Token 数据流

一次 local segment 结束时，token 只进入一个 fate：retained effective、local discarded，或之后由 authority 转为 applied、dropped、quarantined/conflicted、unpublished/outstanding。authority summary 满足：

```text
processed = local_discarded + applied + dropped
          + quarantined/conflicted + unpublished + outstanding
```

carried ancestry 与 hard-crash gap upper bound 单独报告，不能混入 direct applied token。旧 `total_seen_tokens` 只有 legacy 标签，不自动解释为 v4 direct-weight tokens。

## Audit 与 telemetry

active leader 的 bounded maintenance pass 在 commit 后和 terminal close 执行。authority history 的不可变 audit batch 先发布并校验，DB 才删除 dependency-closed 的精确 source rows；多个 hot batch 合并成带 manifest 的 partition 后，source batch 才可通过 fenced claim/complete GC 删除。启动与恢复只扫描有界 hot authority，不全量读取历史 partition。

被 DB history prune 的 command 仍以 immutable `audit/command_receipts/` 保存 request digest 和 result；相同 command 重放会验证 receipt 后返回原结果，digest 冲突仍 fail closed。artifact/audit GC 在 authority 中先 claim，再核对 regular-file、path、size 和 SHA-256 identity 后删除，不能把未知对象当成 orphan。

actor telemetry 是每 attempt 单写者 JSONL，可以丢失或清理，但不能改变 authority summary。

## Legacy 数据

旧 completed run 用 SQLite URI `mode=ro` 并设置 `PRAGMA query_only=ON`。Fragment V0 的四张表和 index JSON 只在 `legacy/`/analysis 中识别。导出 summary 必须写到旧 run root 之外；旧 root 不做 schema migration、sidecar repair、resume 或 GC。
