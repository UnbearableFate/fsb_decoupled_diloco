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
