# Codex phase review: P6-acceptance-final-review

- Reviewer: `gpt-5.6-sol`
- Base commit: `7f797e47d983878e25f9c48c1fddbeb9f0b2ea4f`
- Target commit: `e3a89c784005acb6c92603d6fea24170bea4daf5`
- Review mode: complete `git diff` of the base/target pair
- Ancestry: verified; base is an ancestor of target
- Excluded working-tree state: the user's uncommitted `plans/AGENTS.md`, the post-target ancestry-preflight entry in `failures.md`, and untracked post-target artifacts are not part of the frozen target

## Scope checked

I inspected the complete changed-file inventory and the production changes in the authority schema/commands, audit archive and receipt fallback, publication prepare/publish/commit ordering, artifact/audit GC, cleanup ownership filtering, syncer pre-Torch admission and takeover hooks, learner target-aware close behavior, dynamic capacity bootstrap suppression, tensor serialization, terminal telemetry, configuration, launch/PBS scripts, P6 checker/acceptance/quality/validation harnesses, regression tests, documentation, requirement bindings, and retained structured evidence.

The concurrency and persistence review specifically followed: publication intent creation through immutable-object verification and commit; predecessor reconciliation through identity-checked GC; hot authority rows through immutable audit batches, command receipts, partitions, and audit-object deletion; learner/syncer admission through Torch import; and terminal close/acknowledgement. I also checked raw-SQL placement, removed-runtime/import scans, PBS group IDs and topology declarations, exact P6 requirement inventory, completed-checker output, and the final G0-G10 evidence set.

## Findings

### Low — P6 commits resumability partials after their terminal aggregate evidence exists

Evidence:

- The target tracks 19 hidden `*.partial.json` files for incomplete early G5 attempts under `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/`, including the `20260809-235600`, `20260810-000100`, `20260810-000500`, `20260810-002300`, `20260810-004200`, and `20260810-005200` prefixes.
- `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/progress.md:729` says later six-part G5 partials remain only as resumability evidence pending final evidence compaction. The frozen target contains the terminal combined G5 artifact `20260810-035500_p6-g5-final-pass.json`; failures have their own retained `*_fail.json` artifacts and chronological records.
- `plans/AGENTS.md` requires retaining the smallest representative successful evidence and deleting redundant intermediate artifacts after an experiment reaches a terminal state.

Impact: no runtime correctness impact, but the evidence tree violates its declared terminal compaction policy, makes the phase diff noisier, and leaves partial files that can be mistaken for current/resumable state.

Recommendation: after recording this finding, delete only the exact tracked/untracked G5 `*.partial.json` files whose terminal combined PASS/FAIL evidence is retained; delete the superseded untracked G6 calibration review when the final tracked calibration artifact covers it; record the exact inventory and disposition in `progress.md`. Do not remove the three primary G5 failure artifacts or the final combined G5 artifact. A source/runtime retest is not needed because this is report-only cleanup; rerun `git diff --check` and the completed requirement checker to prove evidence references remain intact.

Missing test: none for runtime behavior. A future report-hygiene check could reject terminal evidence directories that retain hidden partials not referenced as unresolved failure evidence.

## No additional findings

No Critical, High, or Medium correctness finding was identified. In particular, the target's prepare-before-publish lifecycle, transactional merge fence revalidation, archived command replay, dependency-closed authority pruning, claimant-epoch GC, non-symlink immutable deletion checks, bounded pre-Torch admission overlap, sticky learner await-close state, terminal current-only authority validation, and exact eight-row P6 requirement inventory are internally consistent with the reviewed tests and retained evidence.

The frozen evidence reports PASS for the 747-test full suite (2 skipped, zero failures/errors), G3 state machines, 18-by-10 publication crash matrix, all six G5 pipelines, 10,000-cycle boundedness, two-node SQLite/takeover coverage, formal 9-node static/dynamic runs, both 20-pair performance comparisons, documentation review, cleanup manifests, quality manifest, and staged/completed requirement checkers. These results support but do not replace the source review above.

## Verdict

`CHANGES_REQUIRED` solely for the Low report-retention cleanup finding. No implementation or public-interface remediation is required.
