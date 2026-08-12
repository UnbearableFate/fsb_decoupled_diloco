
### 4.2 何时使用外部多智能体 reviewer

仅在以下情况 materially beneficial 时使用：

- 用户或 plan 明确要求；
- 变更涉及 authority、并发、持久化格式、恢复、安全边界或高风险 fault oracle；
- 即将运行成本较高的多节点故障、soak 或性能验收；
- 三次有效失败后需要不同解释；
- coordinator 对 blocking finding 的处置缺乏足够反证。

通常把 test-design 和 current-state 问题合并到一次 preformal review。修缮后只有关键不变量再次变化时才做增量复审；不得因为 reviewer 不可用而重复提交相同 review。

外部 reviewer 是 best effort：不可用不等于 APPROVE，也不自动阻断；任何有效 finding 都必须处置。Codex/GPT 必须先保存自己的结论，再读取本轮外部报告。

所有外部 reviewer 必须通过 `scripts/miyabi/agent/run_multi_agent_review.pbs` 在 compute node 上运行。Runner 当前支持的 `REVIEW_KIND` 为：

```text
test-design | failure | phase | preformal-plan-complete |
final-evidence | critical-incremental
```

Review packet 保持紧凑，只包含 review kind、plan/requirement、冻结 snapshot 中需要审查的精确相对文件路径、关键不变量、相关证据路径、明确问题和期望 verdict。Reviewer 的工作接口是文件路径和 current-state 内容。需要增量复审时，由 coordinator 直接列出本轮必须检查的当前文件以及受影响的 caller、test、config、launcher、Checker 和文档文件。若有与 exact target 匹配且最新的 CodeGraph 索引，优先用 `impact`/`affected` 补全这些路径；索引身份不匹配时不得把其结果当作审查范围证据。

Runner 仍使用唯一 `TARGET_COMMIT` 和 tree hash 创建不可变 snapshot，并把它们写入 request/job summary 作为证据身份，但不把 commit ID 当作 reviewer 的导航或范围定义。提交时必须提供：

```text
TARGET_COMMIT
REVIEW_PROMPT_FILE + REVIEW_PROMPT_SHA256
REVIEW_PATHS_FILE + REVIEW_PATHS_SHA256
```

Runner 只通过 OpenCode 调用固定模型 `opencode-go/deepseek-v4-flash`，不接受模型选择参数，也不调用 Claude Code 或其他 OpenCode 模型。Reviewer 使用固定 ID `opencode-deepseek-v4-flash`；request/job summary 同时记录 reviewer ID 与完整 requested model ID。

`REVIEW_PATHS_FILE` 每行必须是 `TARGET_COMMIT` 中一个精确、tracked、repository-relative 文件路径；不接受目录、glob、重复项、绝对路径或 `..`。PREFORMAL 应列出全部 current source，并按需要列出测试、配置、PBS、Checker 和文档；FINAL 只列 evidence review 实际需要的文件。Runner 将这份列表复制为 snapshot 内的 `.review-scope/paths.txt`，reviewer 可以读取直接耦合文件来确认契约和影响，但 finding 的主范围必须由路径列表定义。

Review 前必须验证：

- target 是完整 commit ID，来自 clean candidate，且 tree identity 已记录；
- prompt 和 review-path list 位于规定 review 目录；
- 每个 review path 都是 target 中的精确 tracked 文件，且列表完整覆盖本轮声明的审查范围；
- reviewer snapshot 只读，结果记录 requested/actual model、PBS job、状态和路径；
- reviewer 不修改仓库、不提交 job、不读取其他 reviewer 输出。

Finding 处置只使用：

```text
fixed | rejected-with-evidence | deferred-with-justification
```

Critical/High 必须修复或以强反证拒绝。Medium 在当前 scope 内修复，或写清影响和延期理由。Low 只在有后续价值时保留。