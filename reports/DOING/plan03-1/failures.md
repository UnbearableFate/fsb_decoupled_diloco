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

## 2026-08-10T17:56:48+09:00 — P2 one-node focused attempt 3

- Experiment: `p2-one-node-validation-01`; domain: `harness`.
- Category: `test-harness-failure`; valid attempt: yes; consecutive count: 3.
- Allocation: retained `2519128.opbs`, node `mg0012`.
- Static result: format and Ruff lint passed.
- Focused result: `92 passed, 1 failed` in 8.48 seconds. The only failure was
  aggregate mutation `exact_workload` before Checker invocation.
- Symptom: the mutation changed only
  `cycle_receipts.processed_tokens_this_cycle` from 16 to 15. SQLite rejected
  the statement because the current schema requires processed tokens to equal
  effective plus locally discarded tokens.
- Evidence:
  `artifacts/20260810-175648_p2-one-node-focused-attempt3_fail.log`.
- Fact: the positive fixture and ten other mutations reached their assertions;
  this failure says nothing negative about product runtime or Checker workload
  classification because the intended corrupted input was never committed.
- Escalation: stop local patch/retry. Freeze the three-attempt evidence and run
  the workflow's comprehensive Codex failure review across the fixture,
  authority/schema, Checker, launcher and artifact flow. External review is
  omitted only under the user's explicit Codex-only directive. A fourth attempt
  is forbidden until the reviewed test logic has been rewritten.

## 2026-08-10T18:03:29+09:00 — P2 one-node full-suite attempt 1

- Experiment: `p2-one-node-full-suite-01`; domain: `harness`.
- Category: `test-harness-failure`; valid attempt: yes; consecutive count: 1.
- Allocation: retained `2519128.opbs`, node `mg0012`; source HEAD
  `4a09e41343f7524c79e41dc7d2894f49aacb23d0` was clean apart from branch
  tracking metadata.
- Result: `473 passed, 16 failed` in 23.37 seconds.
- Evidence: `artifacts/20260810-180329_p2-one-node-full-pytest_fail.log`.
- Failure clusters:
  1. The dead-surface test scans all filesystem files while calling them
     tracked, so ignored legacy `__pycache__/*.pyc` files pollute its result; two
     tracked protocol test filenames also retain `_v1`/`_v2` suffixes.
  2. Both startup-admission tests construct the removed `config.shared.*`
     nesting, while the current runtime correctly reads `loaded.config.sync`
     and `.membership`.
  3. Thirteen adoption tests still configure removed aliases `rebase_local` and
     `predict_global`; the only current names are
     `rebase_post_publish_delta` and `predict_post_publish_global` in both
     validation and strategy dispatch.
- Root cause: stale tests and filenames survived the current-only product
  convergence. Product code is internally consistent; adding aliases or a
  `shared` wrapper would violate the explicit no-compatibility requirement.
- Remediation: make the architecture oracle enumerate Git-tracked paths, rename
  the two version-suffixed test modules and their manifest references, flatten
  the startup fixture, and rewrite all adoption inputs to current names. Review
  the continuous test-only change with Codex before attempt 2.

## 2026-08-10T19:04:00+09:00 — P3 normal pre-execution queue routing

- Experiment: `p3-functional-normal-01`; domain: `product`.
- Category: `infra-invalid-run`; valid product attempt: no; consecutive product
  failure count: unchanged at 0.
- Three `regular-g` submissions were terminated before execution:
  `2519520.opbs` inherited the site default 100 GiB per node,
  `2519545.opbs` fixed memory but inherited the site default CPU count, and
  `2519576.opbs` used the exact bounded request
  `5:ncpus=8:mpiprocs=1:mem=16gb` but remained queued because it would conflict
  with a reservation or top job.
- None acquired an `exec_host`, created a run/log root, produced PBS stdout or
  published structured evidence. Historical state for `2519576.opbs` is
  `F/substate=91` after cancellation and has no execution exit status. These are
  scheduler/pre-execution events, not failed protocol runs.
- Live `qstat --rsc -x` shows `debug-g` is enabled and started, permits 1-16
  nodes and 30 minutes, while the registered scenario needs five nodes for ten
  minutes. Current resource use was 13/48 debug nodes versus regular backfill
  conflicts.
- Remediation: pre-register `debug-g` and the exact bounded select string in the
  P3 manifest, freeze and Codex-review that report-only design change, then
  submit a new create-only normal scenario identity. Runtime source scopes stay
  unchanged; no evidence from the cancelled submissions is promoted.
