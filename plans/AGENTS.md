# 实施记录与失败升级规则

本目录中的实施计划在执行时，需要同步维护可供人工审阅的实施记录。所有记录都应位于仓库根目录的 `reports/DOING/` 下；其中本文件规定的 phase/plan 双模型审查报告使用 `reports/DOING/code_review/`。不要写回计划正文，也不要依赖终端滚屏作为唯一证据。

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

本节是执行 `plans/DOING/` 下计划时的完成门禁。Phase 或 plan 在以下流程结束前只能视为完成候选；门禁通过前，不得宣布完成或开始下一 phase。

连续三次失败后产生的 `reports/DOING/<plan-id>/code_review.md` 是失败诊断记录，不能替代完成门禁要求的 `reports/DOING/code_review/<plan-id>/<phase_id>/` 双模型独立报告；完成门禁报告也不能替代失败诊断记录。

### 冻结审查目标

1. 每个新 plan 必须在独立 Git branch 上执行。
2. 实现和初始关联测试组通过后，创建 review-target commit。审查覆盖的源代码和测试树必须与该 commit 一致；记录并排除审查范围外的既有改动。
3. 将 `git rev-parse HEAD` 的完整输出记录为 `<commit_id>`。comparison base 使用上一 phase-final commit；第一 phase 使用 plan branch point。plan-completion 审查覆盖从 plan branch point 开始的累计 diff。
4. `<plan-id>` 使用不含 `.md` 的计划文件名；`<phase_id>` 使用计划中稳定的 phase 标识；整个 plan 的审查使用 `plan-complete`。

### 使用 `claude -p` 运行双模型独立审查

从仓库根目录把占位符替换为实际值后执行：

```bash
claude -p \
  --model claude-opus-5 \
  --session-id <new-uuid> \
  --permission-mode bypassPermissions \
  --dangerously-skip-permissions \
  --output-format json \
  '审核当前仓库 commit id <base-commit> 到 <target-commit> 的代码修改，覆盖相关源代码、测试、配置、PBS脚本、launcher、Checker和文档。将审核结果写入 <absolute-repo>/reports/DOING/code_review/<plan-id>/<phase-id>/claude-opus-5_<target-commit>.md。按 Critical/High/Medium/Low 列出finding、证据、文件行号、修复建议和缺失测试，检查correctness、回归风险、错误处理、并发/持久化不变量、测试覆盖及plan验收条件，最后给出 APPROVE 或 CHANGES_REQUIRED；没有finding时列明检查范围。你是只读reviewer，除上述报告外不得修改任何文件或Git状态，不得qsub/qdel、删除run数据、commit、push或创建PR，也不得在报告中写入secret、token、凭据或完整环境变量。'
```

Codex同时独立审查同一`<base-commit>..<target-commit>`，并在读取Claude报告前保存自己的报告到同一目录。完成报告不可覆盖；重跑使用`-retryN`新文件。核验JSON中的实际模型和session ID；明确的Claude会话额度耗尽记为`skipped-session-limit`，不重试、不伪造报告且不阻断后续任务，Codex审查仍为必做门禁。其他模型/session/fallback/认证/权限错误均为`blocked`。

### 处置问题并验证

1. 通常只有两份报告都完成后，Codex 才能一起读取它们并修改代码；若 Codex 之外的 reviewer 已按上文记为 `skipped-session-limit`，Codex 报告完成后即可继续。Codex 必须在对应的 `reports/DOING/<plan-id>/progress.md` 或 `failures.md` 中，将所有实际产生的 finding 处置为 `fixed`、`rejected-with-evidence` 或 `deferred-with-justification`，并记录被跳过 reviewer 的调用身份、可核验限额原因和不阻断结论。
2. `Critical` 和 `High` findings 阻止完成，必须修复。`Medium` findings 必须修复，或在证据、影响和后续负责人明确的情况下延期。`Low` findings 可以记录为 follow-up。
3. 对每个接受的行为缺陷，Codex 必须新增或更新一个在修复前会失败的测试，然后重新运行覆盖所有修复改动的测试。若修复触及 phase 的关键不变量，重新运行该 phase 的完整关联测试组。在 `reports/DOING/<plan-id>/` 下记录准确命令、解析后的配置、结果和保留的 artifact 路径。
4. 修复和测试通过后，创建 phase-final 或 plan-final commit。审查修复 commit 不会递归触发另一轮完整双模型审查；但若修复改变公共接口、持久化格式、并发协议、安全边界或其他关键不变量，应创建新的 review-target commit，并针对它重复本门禁。
5. 失败或不完整的测试会使 phase 或 plan 保持未完成，并遵循本文件中的失败记录和清理规则。

## Test Artifact Retention and Cleanup

After each test or experiment reaches a terminal state, reduce the run output before starting the next work unit:

1. Persist the core evidence first in the applicable `reports/DOING/<plan-id>/` record. Keep the exact command and resolved configuration, source identity, run ID, PBS job ID, structured Checker result, final or summary metrics, and paths needed to audit the result.
2. For a successful test, retain only the smallest representative logs and artifacts needed to prove the tested invariant. For a failed test, retain the complete error log, the minimal reproduction evidence, and any artifact still needed for root-cause analysis.
3. Delete redundant files produced solely by that completed test, including duplicate or intermediate checkpoints, temporary/staging files, caches, superseded raw telemetry, repeated successful per-rank logs, orphan payloads, and other run-generated files whose information is already captured by the retained summary or manifest.
4. Resolve and inventory the exact cleanup targets before deletion. Limit cleanup to the completed test's known run directory; never delete files from a live, queued, or resumable run, the current database/checkpoint needed for recovery, source/configuration files, reports, unresolved failure evidence, pre-existing user data, or any path whose ownership is uncertain.
5. Write, use, and extend fs_diloco/tools/clean_run.py to better clean up generated runs.
