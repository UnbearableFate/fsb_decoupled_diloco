# 文档索引

当前文档只描述主线 **Full Protocol v4** 和明确标注的 legacy query-only 能力。

1. [术语](00-glossary.md)
2. [概览](01-overview.md)
3. [架构与故障模型](02-architecture.md)
4. [运行流程](03-runtime-flow.md)
5. [数据流与持久化](04-data-flow.md)
6. [代码结构](05-code-structure.md)
7. [配置](06-configuration.md)
8. [运维](07-operations.md)
9. [兼容与迁移](08-compatibility-and-migration.md)

模块参考：

- [core](modules/core.md)
- [protocol](modules/protocol.md)
- [storage](modules/storage.md)
- [learner runtime](modules/runtime-learner.md)
- [syncer runtime](modules/runtime-syncer.md)
- [modeling](modules/modeling.md)
- [observability](modules/observability.md)
- [tools](modules/tools.md)
- [scripts](modules/scripts.md)

稳定文档不记录具体 PBS job ID、单次性能数字或阶段性实验结果；这些证据位于 `reports/`。归档代码的权威定位同时写 tag 和完整 commit，避免依赖本地 tag 状态。
