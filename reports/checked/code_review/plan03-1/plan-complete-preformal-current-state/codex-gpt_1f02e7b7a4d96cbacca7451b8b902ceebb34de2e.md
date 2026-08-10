# Codex preformal plan-complete current-state review

- Plan: `plan03-1`
- Review kind: `preformal-plan-complete`
- Review base: `e1f76a85f77c33765ebaaddb4828e40cc45d4d24`
- Frozen target: `1f02e7b7a4d96cbacca7451b8b902ceebb34de2e`
- Review scope: the complete tracked current repository, not only the P4 diff
- External review: skipped by explicit user direction; untracked external
  output is excluded from evidence and findings

## Inputs inspected

I inspected the complete tracked product and harness surface: all modules under
`fs_diloco`, the three current configs, every Miyabi PBS/shell/Python launcher
and Checker, the complete test tree and exact retained-surface manifest,
packaging and CLI entry points, README/docs, the plan workflow state, requirement
matrix, P1--P3 phase evidence, the P4 candidate artifact and its raw/JUnit
evidence, the candidate formal-ladder manifest, artifact schema, test ladder,
and cleanup ownership.

The review also traced the current authority and recovery flow through receipt
ingestion, contributor progress, static and dynamic replacement admission,
learner resume, terminal acknowledgement, hot-history archival, run identity,
fresh schema bootstrap, publication, terminalization and Checker projection.
Static repository scans found one unversioned product surface, three current
configs, one canonical schema family, no tracked generation-suffixed product
path or spelling, no legacy/baseline/fragment/compatibility path, and an exact
one-to-one test owner for all 82 retained executable/config/schema surfaces.

The recorded candidate validation is internally consistent for source
`1393f38ab51ea78d193b457501947d4095070eab`: Ruff passed, the changed-surface
rerun passed 104 tests, the focused JUnit suite passed 168 tests, and the full
JUnit suite passed 526 tests with no failures, errors or skips on one confirmed
Miyabi compute node. The evidence-only commits through the reviewed target do
not change the captured formal source fingerprint.

## Current-state assessment

- Public product naming, config, schema filenames, run paths and CLI surfaces
  are consistently unversioned. Removed designs are deleted rather than
  adapted or retained.
- Protocol types are independent of runtime/storage, storage does not import
  runtime, and mutation authority remains explicit and leader-fenced.
- Fresh-run initialization binds source, config, descriptor, schema and
  immutable filesystem identity and fails closed on collisions or mismatches.
- P3 evidence proves the normal, static replacement and leader-takeover
  behaviors from one clean source fingerprint with durable terminal/accounting
  oracles; it is suitable historical candidate evidence but is not substituted
  for the final same-target ladder.
- The final ladder has the right five execution gates and two Codex-internal
  review gates, exact topology/workload identities and a staged/completed
  aggregation design. Three blocking defects below prevent freezing the final
  common target.

## Findings

### P4-R2-F1 — High — contributor resume update identity has the wrong durable owner

The frozen target adds `last_update_id` to `ContributorProgress` and
`ContributorResumeState`, but `AuthorityReadModel.contributor_progress()` and
dynamic admission recover it by joining `contributor_progress.last_receipt_id`
back to `cycle_receipts.planned_update_id`. Receipt archival is intentionally a
separate history lifecycle, while contributor progress is the durable current
resume authority. This makes a current progress field disappear when its
receipt history is no longer hot, and makes the same logical state have two
owners.

The fix must store the planned update identity directly in the
`contributor_progress` row in the same receipt-ingestion transaction, read that
row directly for both static and dynamic admission, bump the incompatible fresh
authority schema revision everywhere, and add a direct SQLite assertion plus
replacement/terminal regression coverage. No migration or fallback is allowed.

### P4-R2-F2 — High — formal supporting evidence can be unrelated to artifact evidence

`check_plan_completion.py` validates that registered `supporting_evidence`
files are tracked and hash-correct, and separately validates that a gate
artifact's `evidence_paths` exist. It does not require the registered files to
be among those named by the gate artifact. A manifest can therefore register
an arbitrary tracked file while leaving the real raw/JUnit/runtime evidence
unhashed and mutable, yet still pass aggregation.

The aggregate must require every registered supporting file to be named by the
corresponding gate artifact. U1 must hash-bind its complete raw log and both
JUnit XML files. Add a mutation that replaces a runtime gate's support binding
with an unrelated tracked file and requires rejection.

### P4-R2-F3 — Medium — runtime cleanup is authorized before completed checking

The formal G1 manifest currently permits cleanup after final evidence review
and staged completion. Completed mode still revalidates every gate artifact's
raw `evidence_paths`; following the documented cleanup order can therefore
delete required inputs before the final Checker invocation. The runtime
retention contract must preserve complete gate inputs through completed
checking and plan archive, and only then authorize evidence-bound cleanup.

## Required remediation and evidence

Implement P4-R2-F1 through F3 as one continuous remediation. Because F1 changes
the authority schema and recovery semantics and F2 changes the formal evidence
acceptance boundary, the existing P4 candidate validation is invalidated for
promotion. Freeze a new source target, run all affected focused tests and the
complete U1 producer on a confirmed Miyabi compute node, then perform a Codex
critical-incremental rereview before freezing the final common target.

No other Critical, High, Medium or Low current-state finding was identified.

Verdict: CHANGES_REQUIRED
