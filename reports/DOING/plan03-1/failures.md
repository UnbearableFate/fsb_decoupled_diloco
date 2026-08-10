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

## 2026-08-10T17:50:16+09:00 — P2 one-node static attempt 1

- Experiment: `p2-one-node-validation-01`; domain: `harness`.
- Category: `valid-test-failure`; consecutive count: 1.
- Allocation: `2519128.opbs`, one `interact-g` node `mg0012`, held by the main
  agent with a one-hour walltime.
- Command: `ruff format --check . && ruff check .`.
- Result: format check failed because 26 current Python files would be
  reformatted; lint did not run because the first command failed.
- Evidence:
  `artifacts/20260810-175016_p2-one-node-ruff_fail.log`.
- Root cause: the implementation target was statically reviewed before the
  repository-wide formatter gate had ever run on a compute node. The drift
  spans retained current source and tests, including the two C5/C6 files; it is
  mechanical formatting rather than a runtime semantic failure.
- Remediation: run the configured formatter across the only current Python
  surface, inspect the mechanical diff with mandatory Codex review, then rerun
  format/lint and the focused tests in the same allocation. The frozen target
  `219abe663025adc7ff8f731f65d90fb27c42c0fe` is superseded for subsequent test
  evidence.

## 2026-08-10T17:54:29+09:00 — P2 one-node focused attempt 2

- Experiment: `p2-one-node-validation-01`; domain: `harness`.
- Category: `valid-test-failure`; consecutive count: 2.
- Allocation: retained `2519128.opbs`, node `mg0012`.
- Static result: all 121 Python files formatted and Ruff lint passed.
- Focused pytest result: collection stopped with two import errors because
  `tests/support/__init__.py` imports deleted module `tests.support.performance`.
  The harness and terminal-service modules both import the support package and
  therefore expose the dead re-export before any test executes.
- Evidence: `artifacts/20260810-175429_p2-one-node-focused_fail.log`.
- Root cause: `performance.py` was deleted with the obsolete performance path,
  but its `PairedPerformanceResult` and `paired_noninferiority` re-exports were
  left in the package initializer. Repository-wide search finds no current
  caller; restoring the deleted helper would reintroduce obsolete test surface.
- Remediation: delete the two dead imports/exports, verify no repository
  reference remains, save a mandatory Codex-only incremental review, then rerun
  the same static and focused command. A third valid failure would trigger the
  workflow's comprehensive failure review.
