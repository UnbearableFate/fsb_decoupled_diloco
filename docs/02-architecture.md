# 架构与故障模型

## 分层

```text
config/descriptor
      │
      ▼
protocol objects ── pure validation/accounting/selection
      │
      ▼
storage adapters ── SQLite authority + immutable filesystem controls
      │
      ▼
runtime composition ── learner/syncer process loops
      │
      ▼
tools/PBS ── initialize, submit, inspect, evaluate, operate
```

`protocol/` 不 import `Path`、storage、runtime 或 PBS；`runtime/` 不 import `legacy/`；baseline 不 import runtime learner。filesystem admission/control 因为执行 I/O，位于 `storage/`。

## 权威和事务

SQLite 是唯一业务写权威。每个 command 使用稳定 `command_id` 和 canonical request digest；相同 ID/相同 request 可重放，不同 request 冲突。所有 mutation 在 `BEGIN IMMEDIATE` 后验证当前 epoch/owner，再修改并提交。raw connection、任意 SQL 和动态 `__getattr__` mutator 不向 runtime 暴露。

selection 只冻结 batch；成功 `commit_merge` 才更新 service credit、global version、proposal/token fate 和 publication state。失败 selection、I/O 或 stale fence 不能留下 partial commit。

## 文件发布

小型 JSON authority/control 使用原子或 create-no-replace 发布。大型 tensor 对象不可变，路径绑定 identity、size、SHA-256 和 theta identity。weight 与 outer optimizer 都存在并验证后，publication 才能提交；fixed cache 只在权威提交后更新。

initializer 在 final 同 parent 建 staging：先 fsync immutable inputs，再以 sibling hard-link reservation 绑定 identity，exclusive 创建 final，按 manifest hash hard-link对象，最后发布 `.complete`。`.complete` 之前 reader 视 run 不存在。普通 retry 不能接管另一个 inode 的 staging；缺 reservation 的已完成 root 只有显式 full self-check repair 才能修复。

## 故障模型

- candidate 崩溃：其 lease 最终过期或被标 error；successor 重新验证 DB、reconcile publication，并在新 epoch 发布 control。
- stale candidate：任何旧 token 的业务 command 在事务内失败。
- learner 崩溃：authority 只接受 current contributor fence；terminal 时最多记一 cycle hard-crash gap 上界。
- torn/malformed proposal：visibility state machine 区分 not-found、transient I/O、malformed 和 identity mismatch；不会因一个坏对象丢弃健康 proposal。
- scheduler 不确定：保留 reservation 和 deadline；operator 通过 expected-state CAS 明确 resolve，不自动 `qdel`。
- shared-FS collision：create-no-replace 的唯一 winner 可重放，different bytes/identity fail closed。
- telemetry 丢失：不影响 token ledger、terminal 或 publication authority。

## GC 与审计

publication orphan 只有在 successor reconcile、lease-safe grace 和 fenced claim 后才可删除。authority history 先发布不可变 audit batch/partition，再在单事务中删除其精确依赖闭包；latest、current control、pending/selected proposal、publication intent、terminal 和 audit 引用不能被 generic cleanup 删除。

## Legacy 边界

`legacy/` 只提供 SQLite `mode=ro` + `PRAGMA query_only=ON` reader、历史 config projection 和纯 Fragment V0 decoder。任何 runtime import legacy 都是架构错误。旧 run 的输出可以写到 run root 之外，但源 root 本身不得被 repair 或修改。
