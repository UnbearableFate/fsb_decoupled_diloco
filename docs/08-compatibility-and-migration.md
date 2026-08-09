# 兼容与迁移

## 当前支持矩阵

| 输入/运行形态 | 当前主线能力 |
|---|---|
| fresh Full Protocol v4 static | 初始化、训练、接管、终态、分析、评估 |
| fresh Full Protocol v4 dynamic | 初始化、admission/replacement、训练、接管、终态、分析、评估 |
| torch DDP / periodic-average baseline | 独立训练与 evidence/checker |
| completed classic v1-v3 full | query-only inspect/export/eval |
| completed Fragment V0 | query-only inspect；用已物化 full checkpoint export/eval |
| in-progress classic/Fragment run | 不可 resume 或原地迁移 |

## 归档定位

classic full writer 和 Fragment V0 writer 只存在于：

- `archive/classic-full-v1-final`
- `archive/fragment-v0-final`
- 不可变完整 commit：`a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`

tag 用于历史复现，不应作为当前 run 的 fallback runtime。主线删除了旧 writer、schema/bootstrap、fragment config/PBS 和动态 mutator proxy。

## Config 迁移

迁移工具只转换能证明语义明确的配置，并在内存中做 v4 round-trip validation。`stop_after_global_tokens` 不能普遍自动映射为 `stop_after_direct_weight_tokens_applied`；存在 replace、旧计数定义不明或 fragment 语义时必须阻塞并由用户明确选择新 target。

repository-owned in-place 迁移需要原 SHA-256 fence，且 publication 前再次读取验证；普通使用应输出到新路径。无论哪种方式，都必须用迁移后的配置创建 fresh v4 run root。

## Old-run query-only contract

`LegacyRunReader` 以 SQLite `mode=ro` 打开既有 DB，并启用/回读 `PRAGMA query_only=ON`。`load_query_config_snapshot` 先尝试 strict current loader；只有识别到已知旧 runtime key 才进入 legacy projection。未知拼写不会触发宽松 downgrade。

legacy projection 只保留 model/data/run 等分析和评估需要的共享字段，丢弃 init/fragment/failure/旧 coordination runtime switch，并把已知 prediction timeout spelling 映射到当前只读结构。它不使 `Config` 能表达旧 runtime。

导出派生数据必须写到旧 root 之外。current code 不会为旧 DB 建表、迁移、repair sidecar、prune history 或发布 control。

## Fresh-v4 identity

resolved config SHA、source identity、descriptor、manifest newline/bytes 和 filesystem inode/link 规则是 fresh v4 publication contract。不要用这些规则“修复”不满足 v4 initializer 协议的历史 root。若旧 authorization/control 与新内容冲突，选择 fresh attempt ID；不要覆盖不可变对象。
