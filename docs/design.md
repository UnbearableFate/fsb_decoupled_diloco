# 设计与数据流

## 唯一运行路径

Full Protocol 的依赖方向是：

```text
core + protocol
       ↓
storage + modeling + observability
       ↓
runtime services
       ↓
learner / syncer entrypoints
       ↓
operator tools and PBS scripts
```

`protocol/` 只实现纯值对象和确定性算法，不依赖 filesystem、SQLite、runtime 或 PBS。`storage/` 是共享文件系统和 SQLite 的唯一适配层。`runtime/learner.py` 和 `runtime/syncer.py` 组合这些能力，不定义第二套配置、merge、terminal 或 authority 行为。

## 初始化与身份

`tools.init_run` 在临时 staging root 中完成全部初始化，再原子发布完成标记。初始化内容包括：

- `control/run_config.resolved.yaml`
- `control/run_descriptor.json`
- `control/run_source_manifest.json`
- `control/artifact_policy.json`
- `control/syncer_metadata.sqlite3`
- `control/bootstrap_complete.json`

descriptor 绑定 run ID、规范化 shared root、配置哈希、Git commit、source fingerprint、membership mode、learner/stream scope、model/tokenizer/dataset identity 和 authority schema。actor 启动时必须重新验证 descriptor、source manifest、resolved config 和初始化完成标记。

## Learner cycle

1. learner 在 Torch import 前提交 admission request。
2. syncer leader 在 SQLite 事务中决定 admission，并发布带 fence 的不可变 response。
3. learner 验证 response 后加载 model 和 data，采用当前 global checkpoint。
4. learner 执行配置的 local optimizer steps，生成 cycle receipt 和完整参数 proposal。
5. proposal payload、pointer、receipt 和前驱哈希构成连续 contributor history。
6. learner 等待 selection/adoption/terminal control；任何旧 fence 的写入都会被拒绝。

## Syncer cycle

只有持有当前 leader token 的 syncer 可以打开 `LeaderSession`：

1. 摄取并验证 admission、receipt 和 proposal。
2. 每个 contributor 最多选一个 pending update，并以持久化 fairness credit 排序。
3. 按 direct tokens 和 staleness 计算确定性权重。
4. 先发布 immutable weight/outer-state objects，再以一个 fenced transaction 提交 publication intent、global version、selection credit 和 token fate。
5. 发布 latest control；重复命令通过 command receipt 幂等返回。

syncer 异常时会把当前 epoch 标为 failed。successor 获得更高 epoch 后先 reconcile prepared/committed publications，再继续推进；旧 token 不能提交后续版本。

## Token 与终态

`token_rollups` 将已裁决 processed tokens 精确分成 local discarded、direct applied、direct dropped、quarantined/conflicted、reported unpublished 和 outstanding。所有类别之和必须等于 adjudicated processed；终态要求 outstanding 为零。

达到 global target、direct-token target、deadline、manual request 或动态 launch budget 条件后，leader 冻结 contributor fence，停止新选择，处理有限的 terminal merge，等待 ack，并以同一 authority transaction 完成 terminal state。`control/stop.json` 和 `control/summary.json` 是 authority 终态的 filesystem 投影，不是第二权威。

## 文件保留与清理

`artifact_policy.json` 是每个 run 的必需不可变策略。`tools.clean_run` 只接受已完成 run、匹配终态的 PASS evidence 和精确 run root；默认只生成计划，显式执行才删除允许清理且不再被 authority 引用的对象。它从不猜测缺失策略，也不接受其他历史布局。
