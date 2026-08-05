# Repository Agent Instructions

## Skill Usage

Follow the standard skill-loading rules. Invoke the `miyabi-development` skill only when the user request or an applicable scoped instruction explicitly requires testing or experimentation. Do not invoke it for work limited to static source inspection, documentation analysis, or file-only editing.

## Project Context

This repository implements a filesystem-based Decoupled DiLoCo prototype.

## PBS Script Validation

Before submitting any PBS script:

1. Run `bash -n scripts/miyabi/*.pbs` in a safe static-validation environment.
2. Replace every `#PBS -W group_list=<group_id>` placeholder with a valid, literal group ID.
3. Do not submit the script until both checks are complete.

## Documentation Synchronization

When a code change has been verified by a 9-node experiment whose workload exceeds the 50-local-step × 10-global-step baseline, update the relevant documentation to reflect the verified behavior and experimental result.

## Plan and Phase Completion Review Gate

This is a repository-wide completion gate for work executed from a plan under `plans/DOING/`. A phase or plan is only a completion candidate until the workflow below finishes; do not announce it as complete or start the next phase before the gate passes.

### Freeze the review target

1. Execute each new plan on a dedicated Git branch.
2. After the implementation and its initial related test group pass, create a review-target commit. The source and test tree covered by the review must match that commit; unrelated pre-existing changes must be recorded and excluded from the review scope.
3. Record the full output of `git rev-parse HEAD` as `<commit_id>`. Record the comparison base as the previous phase-final commit, or the plan branch point for the first phase. A plan-completion review covers the cumulative diff from the plan branch point.
4. Use the plan filename without `.md` as `<plan-id>`. Use the plan's stable phase identifier as `<phase_id>`; for a whole-plan review, use `plan-complete`.

### Run two independent reviews

Start both reviews against the same review-target commit. They must inspect the complete in-scope diff, including affected `fs_diloco` code, tests, configuration, PBS scripts, launchers, Checkers, and documentation.

1. In Herdr, create a new independent Claude Code session, select and verify **Opus 5**, enable its full-permission/bypass mode, and instruct it to perform the review. Record the Herdr agent/session identity and the verified model in its report. Claude Code is reviewer-only throughout this workflow: it may inspect the repository and write its assigned report, but it must not modify implementation, tests, configuration, plans, or other reports. If Opus 5 or full-permission mode is unavailable or cannot be verified, mark the gate blocked; do not silently substitute another model or permission mode.
2. At the same time, Codex performs its own review. Codex must finish and save its report before reading the Claude report so that the conclusions remain independent.
3. Full-permission mode is a tool permission setting, not additional task authorization. It does not authorize either reviewer to submit or delete scheduler jobs, delete run data, commit, push, open a PR, mutate remote services, use credentials outside the task, or make any non-report code change during review.

Write the completed reports to:

```text
reports/DOING/code_review/<plan-id>/<phase_id>/<model_name>_<commit_id>.md
```

Use `claude-opus-5` for Claude and a stable slug identifying the actual Codex model for Codex. A completed report is immutable. If the same model must review the same commit again, add `-retryN` to `<model_name>` and create a new file; never overwrite or append to the completed report. Keep these immutable multi-model review reports under `reports/DOING/code_review/`, separate from the append-only per-plan progress and failure records.

Each report must include:

- review-target and comparison-base commit IDs, source identity, review scope, and relevant diff;
- the actual model used and, for Claude Code, its Herdr agent/session identity and permission mode;
- findings classified as `Critical`, `High`, `Medium`, or `Low`, with evidence and file/line locations;
- correctness and regression risks, error handling, concurrency and persistence invariants, test coverage, and agreement with plan acceptance criteria;
- concrete fixes and missing tests, plus what was inspected when no findings are reported;
- a final `APPROVE` or `CHANGES_REQUIRED` decision, with facts, inferences, and recommendations clearly separated.

Do not include secrets, tokens, credentials, or unnecessary sensitive environment data in either report.

### Remediate and verify

1. Only after both reports are complete may Codex read them together and edit the code. Codex must disposition every finding as `fixed`, `rejected-with-evidence`, or `deferred-with-justification` in the applicable `reports/DOING/<plan-id>/progress.md` or `failures.md` record.
2. `Critical` and `High` findings block completion and must be fixed. `Medium` findings must be fixed or explicitly deferred with evidence, impact, and follow-up ownership. `Low` findings may be recorded as follow-ups.
3. Codex must add or update a test that would fail for each accepted behavioral defect, then rerun the tests covering all remediation changes. If a fix touches a phase's key invariant, rerun that phase's full related test group. Record exact commands, resolved configuration, results, and retained artifact paths under `reports/DOING/<plan-id>/`.
4. After remediation and tests pass, create the phase-final or plan-final commit. Review-remediation commits do not recursively trigger another full dual review. However, if remediation changes a public interface, persistence format, concurrency protocol, safety boundary, or another key invariant, create a new review-target commit and repeat this gate against that commit.
5. A failed or incomplete test leaves the phase or plan incomplete and follows the failure-recording and cleanup rules in `plans/AGENTS.md`.


## Subagent Usage

Use subagents only when the task materially benefits from parallel execution. Avoid delegation for routine sequential work, and do not run more than two subagents concurrently within the same task.
