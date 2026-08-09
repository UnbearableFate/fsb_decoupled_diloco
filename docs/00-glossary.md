# 术语

- **Full Protocol v4**：当前唯一可写、可启动的 filesystem DiLoCo 协议族。
- **proposal**：`FullUpdateProposalV2`；learner 对一次有效训练 segment 发布的完整参数载荷元数据。proposal wire version 2 不代表“Protocol v2”。
- **cycle receipt**：`CycleReceiptV1`；记录一次 learner cycle 的 cursor、hash chain、token fate 和可选 proposal identity。
- **authority**：run 内的 v4 SQLite 数据库及其 schema identity。它是 selection、publication、membership、terminal 和 accounting 的唯一写权威。
- **leader lease**：SQLite 中的当前 `(epoch, owner_id)`；所有业务命令在同一写事务内重新验证它。
- **candidate**：尝试取得 leader lease 的 syncer 进程。candidate 身份不等于 leader 权限。
- **contributor fence**：static binding generation 或 dynamic instance/placement/stream generation 的 typed identity。
- **stable contributor key**：公平选择和 receipt chain 使用的稳定键；static 为 learner ID，dynamic 为 stream ID。
- **attempt ID**：一次进程/授权尝试的身份。发生不可变 authorization collision 时必须使用新的 attempt ID。
- **publication intent**：权威 DB 中先于 weight/optimizer I/O 建立的发布意图；提交时再次核对两个对象及 theta identity。
- **current control**：某个 leader epoch 下的 heartbeat、latest head、admission、receipt ack、drain 或 terminal 文件。learner 验证其 epoch/owner/hash，不把 fixed cache 当权威。
- **fixed cache**：`control/latest.json`、`stop.json`、`summary.json` 等可修复视图；不能替代 SQLite/current epoch control。
- **direct-weight tokens applied**：直接进入成功 global weight merge 的有效 token。它不等同旧版宽泛的 `total_seen_tokens`。
- **hard-crash gap upper bound**：terminal freeze 后未能精确确认的最多一 cycle token 上界；与 token ledger balance 分开报告。
- **query-only legacy**：只读打开已完成 v1-v3/full 或 Fragment V0 的 DB/config/artifact，禁止 bootstrap、迁移、resume、repair 或 GC。
- **Fragment V0**：已归档且不受支持的分片 writer/runtime。当前只保留历史 index/summary 解码。
- **classic full**：v4 cutover 前的无统一 authority full writer。只存在于归档 tag。
- **fresh attempt**：新的 run/attempt identity。v4 的不可变配置 SHA、换行和稳定对象身份承诺只适用于 fresh v4 run，不用于原地升级旧 run。
