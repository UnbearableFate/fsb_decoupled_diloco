# 模块参考：storage

## Authority

`authority.py` 提供 `initialize_authority_v4`、`LeaderAuthority`、只读 model 和绑定 token 的 `LeaderSession`。公开 mutation 是有限命名 command；command journal 绑定 canonical request digest。每个业务事务取得 SQLite write lock 后重新检查 leader 和 contributor fence。

schema 7 分为 `schema_v4.sql`（static）和 `schema_v4_dynamic.sql`（dynamic）。两者共享 proposal/receipt/token/selection/publication/terminal/audit 域，fresh DDL 不含 Fragment V0 表。dynamic feature 另外保存 stream-bound `launch_requests`、capacity observations 和 one-use bootstrap reservation；static schema 不创建假的 scheduler 表。

## Lease 与 object I/O

- `leader_lease.py`：acquire/renew/release/fail、wall/monotonic safety 和 stale token 错误。
- `object_store.py`：regular-file/no-symlink/size/digest/tensor schema typed verification。
- `atomic_io.py`：atomic JSON、immutable bytes、fsync 和 safe reads。
- `tensor_codec.py` / `tensor_identity.py`：global/update/outer tensor serialization 与 theta identity。

## Filesystem control

- `admission.py`：static/dynamic request、operator authorization、response/rejection/disposition archive。request identity 与 committed command identity分开；hot inode 在处理前后精确核对。
- `control.py`：epoch heartbeat/latest/admission/receipt ack/drain/terminal publisher/reader。fixed cache 不授权 learner。
- `terminal_request.py`：descriptor-bound、create-no-replace manual close request reader/publisher。
- `paths.py`：唯一 current run path definition；不包含 fragment directory/method。

## 初始化、审计和清理策略

- `run_initializer.py`：same-parent staging、sibling identity reservation、manifest-hashed hard links 和 `.complete` publication/explicit repair。
- `artifact_policy.py`：versioned artifact class、generic-cleanup allow/forbid 与 checksum。
- `audit_archive.py`：immutable batch/partition build/publish/verify，以及 symlink-safe GC。

publication orphan 和 audit source GC 必须先由 fenced authority claim；generic cleanup 永不删除 DB、sidecar、current control、publication intent、terminal 或 audit authority。
