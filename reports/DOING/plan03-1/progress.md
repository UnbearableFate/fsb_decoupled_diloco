# plan03-1 progress

## 2026-08-10T14:55:19+09:00 — PLAN_INIT

- Plan: `plans/DOING/plans/plan03-1.md`.
- Workflow: version 3 at commit `7d4a607b753744d9b57b54fe0400d1267b13cc40`.
- Dedicated branch: `plan03-1`; branch point
  `7d4a607b753744d9b57b54fe0400d1267b13cc40`.
- Host classification: Miyabi control plane (`miyabi-g1`, no `PBS_JOBID` or
  `PBS_NODEFILE`). Only source inspection, editing, Git/PBS control operations,
  and static shell checks are allowed here.
- Frozen startup artifacts: `inventory.md`, `requirement-matrix.csv`,
  `identity-authority.md`, `test-ladder.md`, and `artifact-schema.md`.
- Observation: the branch point has no tracked tests or runnable configs, while
  the package and documentation still expose versioned Full Protocol names,
  legacy readers, a second runnable baseline, stale entry points, and references
  to deleted scripts/configs.
- Decision: implement only the unversioned Full Protocol. No old API, config,
  schema, run-layout, legacy reader, migration, baseline, or compatibility path
  is retained.
- Next action: run the plan-init static self-check and freeze this startup state
  in Git before product implementation.

## 2026-08-10T14:55:19+09:00 — startup freeze self-check PASS

- Command: strict shell assertions over the branch/head, globally unique plan
  path, ten required startup artifacts, twelve unique requirement rows, all
  workflow-state keys, reviewer PBS shell syntax, absence of group placeholders,
  and `git diff --check`.
- Environment: Miyabi control plane `miyabi-g1`; no project runtime imported.
- Result: `plan-init static self-check: PASS`.
- Transition: `PLAN_INIT` → `IMPLEMENT_AND_DRAFT_TESTS`, phase
  `P1-current-protocol`, work unit `P1-W1-unversioned-surface`.

## 2026-08-10T16:34:16+09:00 — TEST_REVIEW_TARGET

- Frozen implementation target:
  `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`; base remains the branch point
  `7d4a607b753744d9b57b54fe0400d1267b13cc40`, verified as an ancestor.
- Implemented one unversioned Full Protocol surface, strict current config,
  fresh unversioned schema paths, query-only operator authority, canonical
  source identity, current-only cleanup, static/functional/formal configs,
  current PBS launchers, fault injection and structured Checker.
- Login-safe static commands passed on the target: `git diff --check`,
  `bash -n scripts/miyabi/*.pbs`, `bash -n scripts/miyabi/*.sh`, literal group
  checks, path/reference scans and obsolete-product-name scans. No project
  Python import or runtime test was executed on the login node.
- Mandatory Codex/GPT test-design review was saved before requesting or reading
  any external conclusion at
  `reports/DOING/code_review/plan03-1/test-P2-full-protocol-harness/`.
- Review verdict: `CHANGES_REQUIRED`. Open findings cover the missing aggregate
  Checker acceptance/mutation fixture, incorrect FAIL/BLOCKED classification,
  incomplete takeover epoch-state oracle and missing retained-module coverage
  inventory.
- External review prompt SHA-256:
  `0c5ed443950e3650ff884781a43aca1661bc75a95412a0ce21765cfe78ddcec1`.
- Transition: `IMPLEMENT_AND_DRAFT_TESTS` → `TEST_REVIEW_TARGET`. The only next
  action is the external test-design reviewer job; runtime tests remain blocked.
