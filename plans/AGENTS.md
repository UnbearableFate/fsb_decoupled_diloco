# 实施记录与失败升级规则

本目录中的实施计划在执行时，需要同步维护可供人工审阅的实施记录。所有记录都应位于仓库根目录的 `reports/DOING/` 下；其中仓库根目录 `AGENTS.md` 规定的 phase/plan 双模型审查报告使用 `reports/DOING/code_review/`。不要写回计划正文，也不要依赖终端滚屏作为唯一证据。

## 报告路径与文件名

每份计划使用计划文件名作为稳定标识：

```text
plans/DOING/<plan-id>.md
    → reports/DOING/<plan-id>/
```

例如，`plans/DOING/01.md` 对应：

```text
reports/DOING/01/
├── progress.md
├── failures.md
├── code_review.md
└── artifacts/
```

- `progress.md`：记录已经通过的关联测试组、实验结果以及本轮新增或修改内容的简报。
- `failures.md`：按时间顺序记录每次失败、初步或确认的原因，以及下一次修改计划。
- `code_review.md`：记录连续三次失败后启动的全面代码审查、重新推导的实现逻辑和修订方案。
- `artifacts/`：保存需要长期保留的原始日志、结构化结果和检查输出。文件名采用 `YYYYMMDD-HHMMSS_<experiment-id>_<result>.<ext>`，其中 `result` 使用 `pass`、`fail` 或 `review`。

同一文件应持续追加，不得覆盖此前记录。体积较大的训练产物、模型和重复日志保留在原 run 目录中，报告只记录其绝对路径、run ID、PBS job ID 和必要摘要。不得在报告中写入 token、凭据或其他秘密信息。

## 关联测试通过后的记录

一组围绕同一改动或同一不变量的关联测试全部通过后，在继续下一工作单元前向 `progress.md` 追加一条记录。记录至少包括：

- 时间、工作单元或 experiment ID；
- 本轮验证的目标和范围；
- 新增或修改内容的简要说明；
- 实际执行的测试命令、关键配置和运行环境；
- 通过结果、关键指标和对应 artifact 路径；
- 尚未覆盖的风险、后续工作或明确的非目标。

不得以单个偶然通过的测试替代关联测试组结论。只有能够共同证明目标行为的相关测试均通过后，才将该工作单元记为通过。

## 测试失败后的记录

任何测试或实验失败后，在进行下一轮针对性修改前向 `failures.md` 追加一条记录。记录至少包括：

- 时间、experiment ID 和该实验的连续失败次数；
- 完整命令、配置、run ID、PBS job ID 和环境信息；
- 预期行为、实际行为和最小可复现症状；
- 原始日志或结构化结果的路径；
- 已确认的失败原因；若尚未确认，应明确区分事实与假设；
- 下一轮准备修改的逻辑、预期影响和用于证伪该修改的新测试。

失败记录不得只写“测试失败”或只保留异常末行。应保留足够信息，使没有参与当次运行的人能够复现问题并理解下一步计划。

## 连续三次失败后的升级

同一实验连续失败三次后，停止继续进行局部试错式修改，并在 `code_review.md` 中启动一次全面代码审查。完成审查和实施逻辑重写前，不得提交同一实验的第四次运行。

全面代码审查至少覆盖：

- 三次失败的共同模式、差异和现有证据；
- 从输入、状态转换、持久化、恢复到输出的完整数据流和控制流；
- 相关不变量、SQLite transaction、文件发布顺序、GC 引用关系和进程生命周期；
- 与问题相关的实现、测试、配置和 launcher，而不是只检查最后修改的函数；
- 当前测试是否验证了正确的不变量，是否存在错误假设或遗漏的反例；
- 至少一个与原实现思路不同的候选解释或实现方案；
- 重新整理后的根因判断、修订实施逻辑、影响范围和新的 RED 测试；
- 下一次实验的明确通过条件，以及为什么新方案能够避免前三次失败。

“同一实验”指验证目标、核心配置和目标不变量相同的测试或运行；仅修改随机种子、超时、日志级别或无关参数，不视为新实验。连续失败计数在该实验通过后归零。若确实改变了核心假设或验证目标，应先在 `code_review.md` 或 `failures.md` 中记录边界变化和理由，再使用新的 experiment ID。

## 记录质量

所有报告使用清晰、可审阅的 Markdown。事实、推断和后续计划应分开表述；测试结果应能追溯到命令和 artifact；代码已修改但尚未验证时，不得在 `progress.md` 中记为完成。实施代码、测试、计划与报告之间出现不一致时，应先修正记录或说明差异，再继续扩大实验规模。

## Git 分支与完成门禁

每个新 plan 必须在独立 Git branch 上执行。Phase 或 plan 的实现和初始关联测试通过后，先创建 review-target commit，再执行仓库根目录 `AGENTS.md` 中的 `Plan and Phase Completion Review Gate`。只有双模型审查、问题处置、修订测试和 phase-final/plan-final commit 全部完成后，才能宣布完成或进入下一 phase。

连续三次失败后产生的 `reports/DOING/<plan-id>/code_review.md` 是失败诊断记录，不能替代完成门禁要求的 `reports/DOING/code_review/<plan-id>/<phase_id>/` 双模型独立报告；完成门禁报告也不能替代失败诊断记录。

## Test Artifact Retention and Cleanup

After each test or experiment reaches a terminal state, reduce the run output before starting the next work unit:

1. Persist the core evidence first in the applicable `reports/DOING/<plan-id>/` record. Keep the exact command and resolved configuration, source identity, run ID, PBS job ID, structured Checker result, final or summary metrics, and paths needed to audit the result.
2. For a successful test, retain only the smallest representative logs and artifacts needed to prove the tested invariant. For a failed test, retain the complete error log, the minimal reproduction evidence, and any artifact still needed for root-cause analysis.
3. Delete redundant files produced solely by that completed test, including duplicate or intermediate checkpoints, temporary/staging files, caches, superseded raw telemetry, repeated successful per-rank logs, orphan payloads, and other run-generated files whose information is already captured by the retained summary or manifest.
4. Resolve and inventory the exact cleanup targets before deletion. Limit cleanup to the completed test's known run directory; never delete files from a live, queued, or resumable run, the current database/checkpoint needed for recovery, source/configuration files, reports, unresolved failure evidence, pre-existing user data, or any path whose ownership is uncertain.
5. Write, use, and extend fs_diloco/tools/clean_run.py to better clean up generated runs.
