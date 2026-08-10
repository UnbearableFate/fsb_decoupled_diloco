# plan03-1 failures

## 2026-08-10T17:00:15+09:00 — external reviewer availability

- Experiment: `test-design-full-protocol-harness-01`.
- Category: `reviewer-unavailable`; valid test attempt: no; consecutive test
  failure count: unchanged.
- PBS job: `2518445.opbs`; node `mg0004`; orchestration exit status 0;
  walltime `00:25:02`.
- Claude: `failed-command` after an expired OAuth session could not refresh.
- GLM: `invalid-output` (incomplete transcript, no exact verdict).
- DeepSeek: `invalid-output` (substantive transcript, unverifiable actual model,
  verdict wrapped in backticks instead of the exact required final line).
- Kimi: `timed-out` at the registered 1500-second invocation limit.
- Snapshot digests before/after were identical for all four invocations. Raw,
  stderr, request and runner summary evidence are retained in
  `reports/DOING/code_review/plan03-1/test-P2-full-protocol-harness/`.
- Decision: no automatic retry. Workflow v3 makes external availability best
  effort; proceed with mandatory-review remediation while preserving all valid
  coordinator findings.

## 2026-08-10T17:37:12+09:00 — remediation reviewer availability

- Experiment: `test-design-full-protocol-harness-remediation-01`.
- Category: `reviewer-unavailable`; valid test attempt: no; consecutive test
  failure count: unchanged.
- PBS job: `2518777.opbs`; node `mg0006`; terminal orchestration state;
  reviewer walltime `00:25:00`.
- Claude: `failed-command` because the expired OAuth session could not refresh.
- GLM, DeepSeek and Kimi: `timed-out` at the registered 1500-second invocation
  limit, with no stdout beyond their CLI startup headers.
- Snapshot digests before/after were identical. Raw output, stderr, request and
  the runner summary are retained under
  `reports/DOING/code_review/plan03-1/test-P2-full-protocol-harness-remediation/`.
- Decision: external availability is best effort. Preserve the mandatory
  review conclusion, remediate the newly identified C5-C6 acceptance gaps, and
  critically re-review their continuous diff before any runtime test.
