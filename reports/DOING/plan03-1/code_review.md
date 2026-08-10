# plan03-1 code review

No review target has been frozen.

## 2026-08-10T17:56:48+09:00 — P2 three-failure escalation

The `harness:p2-one-node-validation-01` domain reached three consecutive valid
failures. Attempt 1 found repository-wide formatter drift. After mechanical
AST-equivalent remediation, attempt 2 found a dead import from deleted test
support. After current-only deletion, attempt 3 ran 93 focused cases and found
one invalid workload mutation that SQLite rejected before Checker execution.

The attempts differ in immediate symptom but share one process gap: the first
compute-node execution occurred only after the original test-design freeze, so
cheap repository and harness preconditions were discovered serially. No
production runtime path has failed. The third failure specifically invalidates
the claimed RED/mutation evidence for the exact-workload Checker oracle.

Local retry is stopped. The frozen failure target must receive a comprehensive
Codex review covering inputs, authority/schema transitions, persistent rows,
filesystem controls, process/PBS lifecycle, Checker output and alternative
mutation design. The user has temporarily disabled external reviewers, so that
review stage is recorded as skipped-by-user rather than fabricated as approval.

### Comprehensive review result

- Frozen target: `a701570a5762c05dd892b10599a27a793e6d1549`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/failure-P2-one-node-validation-round1/codex-gpt_a701570a5762c05dd892b10599a27a793e6d1549.md`.
- Verdict: `CHANGES_REQUIRED`; one High harness finding F1 accepted.
- Root cause: the token mutation violates a current SQLite equality constraint
  and rolls back before the Checker runs.
- Chosen rewrite: atomically change processed and effective tokens to 15, prove
  the row committed, then require the exact workload error from the aggregate
  Checker. Constraint disabling and weaker/different cursor/config probes are
  rejected.
- External failure review is skipped by explicit user direction. No fourth
  attempt is allowed before implementing and reviewing the complete rewrite.

## 2026-08-10T18:18:37+09:00 — P2 phase review

- Base: `7d4a607b753744d9b57b54fe0400d1267b13cc40`.
- Frozen target: `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/P2-phase-code-and-evidence-review/codex-gpt_5f3d61400fa9fa3c6ee469fa80a75d58558e5c87.md`.
- Verdict: `CHANGES_REQUIRED`.
- Blocking findings: P2-F1 (group-level manifest can falsely claim per-module
  coverage) and P2-F2 (ignored ad-hoc logs are absent from the frozen target and
  do not satisfy the structured gate artifact contract).
- External phase review is skipped under the user's Codex-only direction.

## 2026-08-10T18:37:00+09:00 — P2 phase closure review

- Remediated evidence target:
  `11290da38f12520ead2c9488662cfa573526fa91`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/P2-phase-closure-review/codex-gpt_11290da38f12520ead2c9488662cfa573526fa91.md`.
- Verified evidence: tracked structured PASS from clean source, 127-file Ruff
  gate, 118 focused tests and 504 complete tests on one PBS compute node.
- P2-F1 and P2-F2 are closed; `UNIT-01` and `HARNESS-01` are complete.
- Verdict: `APPROVE`. External review remains skipped by user direction.

## 2026-08-10T19:40:27+09:00 — P3 phase closure review

- Base: P2 phase-final `9398e822ebe6cf9755e55567b18916802b93162f`.
- Frozen target: `a133a98a431566dbd1aef1af6a7f496f2c301d38`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/P3-phase-closure-review/codex-gpt_a133a98a431566dbd1aef1af6a7f496f2c301d38.md`.
- Verified one-node 120/506 PASS and three exact-source five-node PASS
  artifacts, including binding replacement, leader takeover and five-way
  terminal applied-token equality after hot-table archival.
- `FUNC-4L1S-01` and `FAULT-4L1S-01` are complete. Verdict: `APPROVE`.
  External review remains skipped under user direction.

## 2026-08-10T20:51:53+09:00 — preformal current-state review

- Base: P3 phase-final
  `e1f76a85f77c33765ebaaddb4828e40cc45d4d24`.
- Frozen target: `1f02e7b7a4d96cbacca7451b8b902ceebb34de2e`.
- Mandatory full-current-state report:
  `reports/DOING/code_review/plan03-1/plan-complete-preformal-current-state/codex-gpt_1f02e7b7a4d96cbacca7451b8b902ceebb34de2e.md`.
- Verdict: `CHANGES_REQUIRED`.
- Blocking findings: P4-R2-F1 (resume update identity is derived from archival
  receipt history instead of owned by durable contributor progress), P4-R2-F2
  (hash-registered supporting evidence need not be named by its gate artifact),
  and P4-R2-F3 (cleanup may precede completed evidence checking).
- External review is skipped by explicit user direction; untracked external
  output is not read as a conclusion or accepted as evidence.
