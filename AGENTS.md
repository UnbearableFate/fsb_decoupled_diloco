# Repository Agent Instructions

## Skill Usage

Follow the standard skill-loading rules. Invoke the `miyabi-development` skill only when the user request or an applicable scoped instruction explicitly requires testing or experimentation. Do not invoke it for work limited to static source inspection, documentation analysis, or file-only editing.

## Project Context

This repository implements a filesystem-based Decoupled DiLoCo prototype.

## PBS Script Validation

Before submitting any PBS script:

1. Run `bash -n scripts/miyabi/agent/*.pbs` in a safe static-validation environment.
2. Replace every `#PBS -W group_list=<group_id>` placeholder with a valid, literal group ID.
3. Estimate runtime from the workload and prior evidence, then request the shortest practical `walltime` that still has enough safety margin for startup variance, runtime variance, and orderly teardown. The objective is to improve scheduling while preserving a high probability that the job finishes successfully; do not shave the margin so tightly that the test is likely to time out. At least give 10 mins. When a script's default is materially longer than this evidence-based estimate, override it explicitly in `qsub -l walltime=...`.
4. Do not submit the script until these checks are complete.

## Test Organization

- Follow the existing `tests/` organization: group test files by production module or coherent functional area.
- Create a new test file only when adding a new production module or a genuinely new functional area that has no existing test owner.
- When changing an existing module or existing behavior, extend or revise its corresponding existing test file. Do not create a new test file merely to isolate a bug fix, refactor, regression, edge case, or implementation phase.
- Keep each production module or functional area with one clear test-file owner. If ownership is unclear, identify the closest existing owner before editing; consolidate overlapping test files instead of adding another one.
- Near every added or changed test, include a concise English comment explaining the behavioral or regression reason for the test. The test function's docstring may serve as this comment. Describe the enduring reason the assertion exists, not edit history or an issue narrative, and do not repeat what the assertion already says.

## Python Documentation

- All new or modified handwritten Python code, including production modules, tests, and repository scripts, must use English documentation comments.
- Every new or modified Python file must have a module docstring. Every new or modified class, method, and function, including private helpers and test functions, must have an English docstring that states its responsibility and any non-obvious invariant, side effect, or failure behavior.
- Class and instance data members must have a concise English declaration or inline comment when introduced. For dataclass fields and annotated attributes, place the comment next to the field; for attributes initialized inside methods, document them at the initialization site.
- Comments and docstrings must explain intent, contracts, or rationale rather than restating the code. Keep them synchronized with behavior, and delete stale comments when the associated design is removed.

## Subagent Usage

Use subagents only when the task materially benefits from parallel execution. Avoid delegation for routine sequential work, and do not run more than two subagents concurrently within the same task.

## 最简设计&实现原则
请始终以“当前最新设计的最简洁、最直接、最一致的实现”为唯一目标。

本任务不要求、也不允许保留任何向后兼容性。旧代码、旧设计、旧接口、旧配置、旧数据格式以及历史行为，只要不再属于当前设计，都应彻底删除，而不是兼容、包装、保留或标记 deprecated。

Do not optimize for minimizing the diff. Optimize for minimizing the complexity of the final repository.

具体要求：

1. 只实现当前最新设计
    * 以当前明确的设计和需求为唯一 source of truth。
    * 不要因为仓库中已有旧实现、旧测试或旧配置而限制新设计。
    * 如果旧设计与当前设计冲突，直接以当前设计替换旧设计。
2. 不保留 backward compatibility
    * 不要保留旧 API、旧函数签名、旧 CLI 参数、旧配置项、旧环境变量、旧文件格式或旧行为。
    * 不要添加 compatibility layer、adapter、shim、migration wrapper、fallback path 或 legacy mode。
    * 不要为了让旧调用方式继续工作而增加额外分支。
3. 主动删除 obsolete 内容
    * 删除已经没有必要的旧功能代码。
    * 删除只服务于旧设计的 helper、wrapper、abstraction 和 compatibility code。
    * 删除针对旧行为、旧接口、旧配置的测试。
    * 删除 obsolete fixtures、mock、test utilities。
    * 删除旧配置项、配置模板、示例配置、CLI 参数和环境变量。
    * 删除已经不适用于当前实现的文档、注释和示例。
    * 删除 dead code、unused imports、unused dependencies 和不再需要的 feature flags。
4. 测试只验证当前设计
    * 不要修改新实现去满足旧测试。
    * 如果测试验证的是已经废弃的行为，应删除或重写该测试。
    * 测试应描述当前系统应该如何工作，而不是过去如何工作。
5. 配置和接口保持唯一
    * 对同一个概念只保留一种当前推荐的表达方式。
    * 不要同时支持 old_name/new_name、old_format/new_format、old_path/new_path。
    * 不要通过 alias 或 fallback 同时维持两套接口。
6. 优先降低复杂度
    * 如果删除兼容性代码可以显著简化架构，应直接删除。
    * 优先选择更少的状态、更少的分支、更少的抽象层和更明确的数据流。
    * 不要为了“以后可能有用”而保留当前不需要的 abstraction 或 extension point。
    * 不要为了最小化 diff 而保留不合理的旧结构；允许进行必要的重构。
7. 不进行历史迁移支持
    * 除非当前任务明确要求，否则不需要支持旧 checkpoint、旧数据库 schema、旧 metadata、旧配置文件或旧运行目录。
    * 可以假设用户会从当前版本的干净环境开始运行。
8. 完成后进行 repository-wide cleanup
    * 搜索整个仓库，确认没有遗留对旧接口、旧配置、旧名称和旧设计的引用。
    * 确认代码、测试、配置、脚本、文档和示例彼此一致。
    * 如果发现某段内容存在的唯一理由是兼容历史版本，应删除它。

判断原则：

当你在“保留旧实现以兼容历史行为”和“删除旧实现以得到更简单、更一致的当前实现”之间做选择时，始终选择后者。

不要把 backward compatibility 当成优点。在这个项目当前阶段，历史兼容性属于不必要的技术债务。

最终代码应看起来像“这个项目从一开始就是按照当前设计实现的”，而不是“在旧设计上不断打补丁演化到当前状态”。

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
