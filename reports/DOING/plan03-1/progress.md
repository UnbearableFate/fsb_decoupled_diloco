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

## 2026-08-10T16:35:16+09:00 — EXTERNAL_TEST_REVIEW

- Pre-submit checks passed: shell syntax for every Miyabi PBS/shell script,
  literal `group_list=xg24i002`, no group placeholder, prompt hash match and
  branch-point ancestry for target
  `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`.
- Submitted reviewer job `2518445.opbs` to `debug-g`: one `mg0004` compute
  node, four reviewer processes, requested walltime `00:30:00`, prompt SHA-256
  `0c5ed443950e3650ff884781a43aca1661bc75a95412a0ce21765cfe78ddcec1`.
- Initial `qstat -f` confirmed running state, full PBS job ID, literal group,
  one-node topology and the registered base/target/prompt variables.
- Transition: `TEST_REVIEW_TARGET` → `EXTERNAL_TEST_REVIEW`; only monitoring
  and evidence collection are allowed until the job reaches terminal state.

## 2026-08-10T17:00:15+09:00 — TEST_REMEDIATION

- Reviewer job `2518445.opbs` reached a valid orchestration terminal state on
  `mg0004` with PBS exit status 0 and immutable snapshot digests unchanged.
- No external invocation produced a structurally valid, model-verifiable
  report: Claude failed authentication, GLM and DeepSeek were invalid output,
  and Kimi timed out. None is counted as APPROVE.
- The mandatory Codex findings C1-C4 remain accepted. Two independently
  corroborated harness gaps from the invalid DeepSeek raw transcript are also
  accepted: allocation failure must still publish a structured artifact, and
  the real syncer fault seam needs a focused unit test. Its proposed hostname
  aliasing is rejected because exact scheduler/attestation identity is safer.
- The current matrix has no dynamic multi-node requirement, so the obsolete
  `F-DYNAMIC-CAPACITY` runtime-scenario registration will be deleted rather
  than extended with a second Checker mode.
- Transition: `EXTERNAL_TEST_REVIEW` → `TEST_REMEDIATION`. Runtime execution
  remains blocked until the remediation is frozen and critically re-reviewed.

## 2026-08-10T17:11:43+09:00 — TEST_REVIEW_TARGET remediation freeze

- Continuous remediation target:
  `af54925a4d0487a37c20f298f2027003cb079d20`; base is the prior reviewed
  implementation `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`.
- C1-C4 were implemented with an aggregate valid terminal fixture plus eight
  mutations, explicit FAIL/BLOCKED separation, exact normal/takeover epoch
  lifecycles and an exact 80-surface coverage manifest.
- The PBS early-exit artifact and real syncer pause seam are now covered. The
  obsolete dynamic multi-node registration was removed without removing the
  current dynamic product mode or its unit tests.
- Login-safe syntax, group, JSON, manifest inventory and diff checks passed. No
  project Python or runtime test ran on the login node.
- The mandatory critical-incremental Codex report was saved before requesting
  or reading any external conclusion for this round and found no new blocking
  design issue. Runtime tests remain blocked pending the external review job.

## 2026-08-10T17:12:09+09:00 — EXTERNAL_TEST_REVIEW remediation

- Pre-submit shell syntax, literal group, prompt hash and continuous ancestry
  checks passed for target
  `af54925a4d0487a37c20f298f2027003cb079d20`.
- Submitted critical-incremental reviewer job `2518777.opbs` with one
  `debug-g` node (`mg0006`), four parallel reviewer invocations and
  `00:30:00` walltime. Prompt SHA-256 is
  `78675261a99237b706cfa95476e50bbc7a2f024be91a33fc2af8acd340293888`.
- Initial scheduler metadata confirms the exact base/target/prompt identities,
  literal group `xg24i002` and running state. Only monitoring is allowed until
  the job reaches terminal orchestration state.

## 2026-08-10T17:37:12+09:00 — TEST_REMEDIATION terminal-control closure

- Reviewer job `2518777.opbs` reached terminal orchestration state on `mg0006`;
  all four read-only snapshot digests remained unchanged. No invocation
  produced a valid report: Claude authentication was expired, while GLM,
  DeepSeek and Kimi reached the registered 1500-second timeout without output.
- The mandatory remediation review remains the controlling conclusion for
  C1-C4/A1/A2/A4. No unavailable external invocation is counted as approval or
  as a finding.
- A subsequent coordinator inspection found two acceptance-boundary gaps in
  the frozen target: the aggregate fixture hand-builds terminal controls that
  the current publisher would never emit and the Checker does not validate
  their exact schemas; the PBS early-exit test checks source text but does not
  execute the EXIT trap.
- Transition: `EXTERNAL_TEST_REVIEW` -> `TEST_REMEDIATION`. Runtime execution
  remains blocked until C5-C6 are implemented and the new continuous target
  completes another critical-incremental review.

## 2026-08-10T17:43:57+09:00 — TEST_REVIEW_TARGET terminal-control closure

- Frozen target: `219abe663025adc7ff8f731f65d90fb27c42c0fe`; continuous
  base: `af54925a4d0487a37c20f298f2027003cb079d20`, with ancestry verified.
- C5 now uses the product terminal publisher for the aggregate fixture and
  requires exact stop/summary authority projections plus a non-writable epoch
  publication. Three focused mutations cover both schemas and immutability.
- C6 now executes the tracked PBS wrapper against a failing allocation stub and
  requires exit-status preservation plus blocked-evidence publication.
- Login-safe `bash -n`, literal-group and current-worktree diff checks passed.
  No project Python or runtime test ran on the login node.
- The mandatory Codex critical-incremental report was saved before any external
  request and returned `APPROVE` for test-design promotion. Runtime remains
  blocked pending external reviewer terminal state and finding disposition.
