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

## 2026-08-10T21:03:18+09:00 — preformal remediation rereview

- Rejected base: `1f02e7b7a4d96cbacca7451b8b902ceebb34de2e`.
- Reviewed evidence target: `272fa81331a110f815a52d871c2fd61f7d1c3abb`;
  clean compute-validation source:
  `2b0c9a004e04af0907ce7766d4d9df47b29cf545`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/plan-complete-preformal-remediation/codex-gpt_272fa81331a110f815a52d871c2fd61f7d1c3abb.md`.
- P4-R2-F1, P4-R2-F2 and P4-R2-F3 are closed. One-node evidence proves 54
  affected tests, 169 focused tests and 527 complete tests with all registered
  format/lint/JUnit gates passing.
- Verdict: `APPROVE`. External review remains skipped by user direction.

## 2026-08-10T21:16:51+09:00 — R2 final-target preformal review

- Final source target: `5b474d5c1735beb8cca922fd6cc7b6304926df2c`.
- Mandatory report:
  `reports/DOING/code_review/plan03-1/plan-complete-final-target-r2/codex-gpt_5b474d5c1735beb8cca922fd6cc7b6304926df2c.md`.
- Scope: complete tracked current repository, documentation-complete final
  design and exact final ladder; requirements SURFACE/CONFIG/SCHEMA/CLEAN/ARCH.
- All prior preformal findings remain closed. Verdict: `APPROVE`.
- External review remains skipped by explicit user direction and is not a gate.

## 2026-08-10T21:30:00+09:00 — current review disposition re-audit

The user explicitly added
`reports/DOING/code_review/plan03-1/critical-current-fs-diloco/finding-dispositions_4ebee6339fb76f63127874c655d7b109b2ec0b39.md`
to the remediation inputs. This is a disposition audit of already-produced
evidence, not a resumption of external reviewer execution.

Codex checked every finding against current target
`5b474d5c1735beb8cca922fd6cc7b6304926df2c` rather than assuming that the
older reviewed target still described the worktree:

- FSD-H1 is already closed: U1 publishes and validates nonzero JUnit test
  totals with zero failures, errors and skips, and hash-binds both XML files.
- FSD-H2 is already closed: the fault scenario is the sole registered input;
  the Checker unconditionally pins binding generations and exact history.
- FSD-H3 is already closed: contributor progress directly owns
  `last_update_id`, admission/resume carries it, and the learner uses it for a
  graceful terminal acknowledgement.
- FSD-H4 is already closed: valid normal/replacement/takeover fixtures execute
  the fault branches and targeted mutations prove rejection.
- FSD-M1 through M7 remain real in the current state: syncer admission/ingest
  composition lacks a behavioural owner test; the multi-node fault text
  overclaims stale-writer evidence; config discovery is CWD-relative; the
  generation scan is path-only and vacuous; integrity exceptions are broadly
  swallowed; the takeover boundary is duplicated; and unused console/facade
  surfaces remain.
- FSD-L1 through L6 also remain real current-only cleanup defects: duplicated
  owner-path derivation, an unwritten schema state, contradictory Python floor,
  stale defaults, swallowed leader release failure, and repeated/inert static
  config fields. FSD-L7 is not a defect and is already explicitly bounded to
  unit evidence in `identity-authority.md`.
- The recommendation to rename independently versioned `CycleReceiptV1` and
  `FullUpdateProposalV2` remains rejected. Their wire-format identity is
  current protocol data, not a Full Protocol product-generation suffix; the
  repaired content oracle will encode this precise distinction.

The completed final U1 and normal F1 artifacts remain valid historical results
for their exact source but are invalidated for promotion because the accepted
current-source remediations change formal scopes. The final ladder will restart
only after implementation, compute-node validation and Codex rereview.

## 2026-08-10T21:51:14+09:00 — multi-agent review restored

- The user's temporary Codex-only direction is now superseded. External
  multi-agent review is restored for the remaining remediation, preformal and
  final-evidence review gates under the workflow's best-effort availability
  rule; every valid finding remains mandatory input to Codex disposition.
- The four current reviewer lanes are Claude Opus 5, GLM-5.2, DeepSeek V4
  Flash and MiniMax M3. The obsolete Kimi-named runner slot and environment
  variable are removed instead of retained as aliases.
- Formal Codex review artifacts advance to schema 2 and no longer encode the
  temporary `skipped-by-user` policy. External invocation summaries and
  dispositions remain separate tracked inputs so an outage cannot be
  represented as approval.
- The current remediation will be frozen and reviewed by Codex before the
  external job is submitted. Runtime validation remains blocked until any
  valid external findings are disposed.

## 2026-08-10T21:57:09+09:00 — current-review remediation Codex pass

- Continuity base: `5b474d5c1735beb8cca922fd6cc7b6304926df2c`.
- Frozen target: `74ecd4fb64311c69ae0d758d8c1d99b27a9c5572`.
- Formal source fingerprint:
  `sha256:df143611ba42181cdfea90c3b205b2c758997dc99817d345256ecea4d9bef078`.
- Mandatory Codex-first report:
  `reports/DOING/code_review/plan03-1/critical-current-remediation/codex-gpt_74ecd4fb64311c69ae0d758d8c1d99b27a9c5572.md`.
- Codex rechecked every accepted current-review finding and the restored
  reviewer workflow. A stale deleted test path and missing proposal-branch
  conflict test were fixed before target freeze. No open finding remains in
  the frozen source/test design. Verdict: `APPROVE` for external review and,
  after disposition, compute validation.
- This report was saved before submitting or reading any external result from
  this review round.

## 2026-08-10T22:29:50+09:00 — external remediation review disposition

- PBS job `2520922.opbs` finished on `mg0011` with scheduler exit 0 after
  20:14. Every reviewer snapshot digest was unchanged.
- Claude Opus 5 produced a valid model-verified `CHANGES_REQUIRED` report with
  FSD-R1 through R9. MiniMax M3 produced a valid `APPROVE` report. GLM-5.2 and
  DeepSeek V4 Flash returned `invalid-output`, not approval.
- Dispositions are tracked in
  `reports/DOING/code_review/plan03-1/critical-current-remediation/finding-dispositions_74ecd4fb64311c69ae0d758d8c1d99b27a9c5572.md`.
  R1 and R3 through R9 are accepted. R2 is accepted only for retryable SQLite
  contention; its proposed catch-all is rejected because it would swallow
  unexpected storage/integrity failures again.
- Target `74ecd4f` is not promotable. Transition:
  `EXTERNAL_TEST_REVIEW` -> `TEST_REMEDIATION`; close the accepted findings and
  run compute-node validation before rereview.
