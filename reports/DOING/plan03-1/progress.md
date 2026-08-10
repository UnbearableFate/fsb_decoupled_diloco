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

## 2026-08-10T17:44:46+09:00 — EXTERNAL_TEST_REVIEW terminal-control closure

- Pre-submit checks passed for every PBS/shell script, literal group ID,
  continuous ancestry and prompt SHA-256
  `20901296696ef94597e712f25644bac576e774f2adf6d1399edb0ab1ad33de40`.
- Submitted job `2519093.opbs`; `qstat -f` confirms one `debug-g` compute node
  (`mg0009`), target `219abe663025adc7ff8f731f65d90fb27c42c0fe`,
  literal group `xg24i002`, requested reviewer timeout 1200 seconds and
  evidence-based walltime `00:25:00`.
- The shorter limit retains margin beyond the only previously substantive
  external response latency while avoiding another unused 30-minute request.
  Only monitoring and evidence collection are allowed until terminal state.

## 2026-08-10T17:48:52+09:00 — external review skipped by user

- The user directed that external reviewers be skipped temporarily and that
  review use Codex only. Running job `2519093.opbs` was therefore cancelled;
  `qstat` confirms no unfinished job remains.
- The partial external request/raw/stderr artifacts are retained for audit but
  contain no valid external report or finding. No external conclusion is used.
- The already saved mandatory Codex report for target
  `219abe663025adc7ff8f731f65d90fb27c42c0fe` is `APPROVE` and becomes the
  controlling test-design decision under this explicit override.
- Transition: `EXTERNAL_TEST_REVIEW` -> `STAGED_TEST_EXECUTION`, work unit
  `P2-W2-one-node-validation`. The next action is to acquire the main-agent-held
  one-node `interact-g` allocation and run the focused harness/static suite,
  then the full pytest suite in the same allocation.

## 2026-08-10T17:50:16+09:00 — TARGETED_REMEDIATION after static attempt 1

- Acquired main-agent-held allocation `2519128.opbs`: exact one-node topology
  on `mg0012` confirmed through `PBS_NODEFILE`; the worktree was clean and the
  expected NVIDIA/HPC-X modules were loaded.
- The first registered gate failed only the repository formatter check on 26
  current Python files. This is recorded as valid failure 1; lint and runtime
  tests have not yet run.
- Transition: `STAGED_TEST_EXECUTION` -> `TARGETED_REMEDIATION`. The retained
  allocation will be used to apply and verify the mechanical formatter change,
  followed by a mandatory Codex-only incremental review before retrying tests.

## 2026-08-10T17:53:39+09:00 — STAGED_TEST_EXECUTION retry target

- Ruff formatted all 26 files reported by attempt 1; the formatter completed
  successfully and left the other 95 Python files unchanged.
- Frozen remediation target:
  `bb0023184abd78b6e220c487967d9c245adf36d5`.
- Mandatory Codex review compared the complete formatter diff and independently
  verified AST equality for all 26 before/after files (`mismatches=[]`). Verdict:
  `APPROVE`; external review is skipped per the user's directive.
- Transition: `TARGETED_REMEDIATION` -> `STAGED_TEST_EXECUTION`. Retry format,
  lint and focused harness/runtime/architecture tests in allocation
  `2519128.opbs` before considering the full suite.

## 2026-08-10T17:54:29+09:00 — TARGETED_REMEDIATION after focused attempt 2

- The formatter remediation is verified: format check covered 121 files and
  Ruff lint passed.
- Focused collection exposed one repository-cleanup defect: the support package
  still exports two names from deleted `tests/support/performance.py`. There is
  no current consumer of either name.
- Transition: `STAGED_TEST_EXECUTION` -> `TARGETED_REMEDIATION`; valid failure
  count is now 2. Delete the obsolete re-export rather than restoring legacy
  performance support, review the continuous diff with Codex, and retry in the
  retained allocation.

## 2026-08-10T17:56:08+09:00 — STAGED_TEST_EXECUTION attempt 3 target

- Deleted only the stale performance-support import and two matching exports;
  repository-wide search confirms no current consumer or remaining active
  reference.
- Frozen target: `83e160d3ca6b21e7103adfc10dca7454bd661a0c`.
- Mandatory Codex incremental review verdict: `APPROVE`; external review remains
  skipped by user direction.
- Transition: `TARGETED_REMEDIATION` -> `STAGED_TEST_EXECUTION`. Retry the exact
  static/focused gate in retained allocation `2519128.opbs`. Another valid
  failure would trigger comprehensive failure review rather than a third patch.

## 2026-08-10T17:56:48+09:00 — FAILURE_REVIEW escalation

- Attempt 3 kept format/lint green and executed 93 focused cases; 92 passed.
  The remaining workload mutation violated a SQLite CHECK constraint before it
  could invoke the aggregate Checker.
- The harness-domain consecutive failure count reached 3. Local iterative
  patching and a fourth attempt are now blocked.
- Transition: `STAGED_TEST_EXECUTION` -> `FAILURE_REVIEW`. Freeze this evidence,
  perform the mandatory comprehensive Codex review, rewrite the mutation to a
  schema-valid but workload-invalid durable state with an explicit oracle, and
  review the completed rewrite before any retry. External reviewers remain
  skipped under the user's current directive.

## 2026-08-10T18:00:00+09:00 — LOGIC_REWRITE authorized by failure review

- Comprehensive Codex failure review completed on frozen target
  `a701570a5762c05dd892b10599a27a793e6d1549`; verdict
  `CHANGES_REQUIRED`, with one accepted High harness finding F1.
- The review traced fixture input through fenced authority persistence,
  terminal/filesystem publication, direct mutation and Checker artifact output.
  Attempt 3 rolled back at SQLite and never reached the Checker.
- The approved rewrite changes processed and effective receipt tokens together
  to a schema-valid value 15, asserts persistence, and requires the exact
  cycle-workload error. Product code, schema and acceptance requirements remain
  unchanged.
- Transition: `FAILURE_REVIEW` -> `LOGIC_REWRITE`. External failure review is
  skipped per user direction; the retained allocation remains available, but
  no retry may run until rewrite implementation and Codex review are frozen.

## 2026-08-10T18:02:00+09:00 — STAGED_TEST_EXECUTION post-rewrite attempt 4

- Frozen rewrite target:
  `1190f7ff1a92ae4daf8d224e0b00a428569283f9`.
- The exact-workload mutation now commits a schema-valid `(processed=15,
  effective=15)` receipt, proves the persisted values and requires the exact
  Checker workload error.
- Mandatory Codex rewrite review verdict: `APPROVE`; F1 is fixed pending runtime
  verification. External review remains skipped under the user's directive.
- Transition: `LOGIC_REWRITE` -> `STAGED_TEST_EXECUTION`. The comprehensive
  review authorizes one post-rewrite attempt of the exact static/focused gate in
  retained allocation `2519128.opbs`, followed by the full suite only on PASS.

## 2026-08-10T18:02:31+09:00 — P2 post-rewrite focused PASS

- Allocation: `2519128.opbs`, one `interact-g` node `mg0012`; source HEAD
  `7022c405fca8ea65ddd0dbe96cb8155fcaa41fa3`, clean worktree, expected
  NVIDIA/HPC-X modules.
- Ruff format checked all 121 Python files and lint passed.
- The registered harness/runtime/architecture/config/source/cleanup focused
  command passed all 93 tests in 8.49 seconds. The positive aggregate Checker,
  11 mutations, terminal-control projections, PBS exit trap, syncer fault
  boundary and retained-surface inventory all executed in this group.
- Evidence:
  `artifacts/20260810-180231_p2-one-node-post-rewrite-focused_pass.log`.
- F1 is runtime-verified and the harness failure counter resets to zero. The
  next action is the complete pytest suite in the same retained allocation.

## 2026-08-10T18:03:29+09:00 — TARGETED_REMEDIATION full-suite attempt 1

- The complete suite executed 489 tests: 473 passed and 16 failed.
- All failures are obsolete-test drift: filesystem enumeration includes ignored
  bytecode and two versioned test filenames; startup fixtures retain removed
  config nesting; adoption tests retain removed strategy aliases.
- No current product API/config/schema change is required. Under the project's
  current-only rule, the remediation deletes the stale spellings and updates
  tests/manifest rather than reintroducing compatibility.
- Transition: `STAGED_TEST_EXECUTION` -> `TARGETED_REMEDIATION` in the new
  `p2-one-node-full-suite-01` domain, count 1. The retained allocation remains
  available; Codex review is required before full-suite attempt 2.

## 2026-08-10T18:08:13+09:00 — STAGED_TEST_EXECUTION full-suite attempt 2 target

- Frozen remediation target:
  `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87`.
- Current-only test cleanup now enumerates Git-tracked surfaces, uses
  unversioned protocol test filenames and manifest entries, builds the sole
  current startup config shape, and supplies exact current adoption strategy
  names with their required settings. No compatibility path was added.
- Removed 278 ignored generated bytecode files, including stale legacy module
  caches; no tracked data was deleted.
- Mandatory Codex incremental review verdict: `APPROVE`; external review is
  skipped under user direction.
- Transition: `TARGETED_REMEDIATION` -> `STAGED_TEST_EXECUTION`. Run full-suite
  attempt 2 in retained allocation `2519128.opbs`.

## 2026-08-10T18:09:51+09:00 — P2 one-node validation PASS

- Allocation: `2519128.opbs`, main-agent-held one-node `interact-g` allocation
  on `mg0012`; source HEAD for the full run was
  `c1ab1305e372b277beb51187539644b2295e7a6f`, with a clean product worktree and
  the expected NVIDIA/HPC-X modules.
- Full-suite attempt 2 passed all 489 tests in 19.30 seconds.
- A final current-target gate then confirmed all 121 Python files formatted and
  Ruff lint clean.
- Evidence:
  - `artifacts/20260810-180907_p2-one-node-full-pytest-attempt2_pass.log`;
  - `artifacts/20260810-180951_p2-one-node-final-static_pass.log`;
  - focused prerequisite
    `artifacts/20260810-180231_p2-one-node-post-rewrite-focused_pass.log`.
- `qsub` reported job completion and login-node `qstat` confirms no unfinished
  allocation. The full-suite failure counter resets to zero.
- P2 implementation, harness and one-node candidate tests are complete. Freeze
  this evidence for mandatory Codex phase review; external review remains
  disabled by the user.

## 2026-08-10T18:18:37+09:00 — P2 phase remediation required

- The mandatory Codex phase review inspected the complete branch-point to
  target increment and returned `CHANGES_REQUIRED` for target
  `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87`.
- P2-F1: the group-level module manifest proves file inventory equality but can
  cite unrelated tests for an uncovered module; concrete inspection/CLI
  boundaries have no focused behavior test.
- P2-F2: the useful 93/489/Ruff PASS logs are ignored, absent from the frozen
  target and not structured gate artifacts with complete source/environment
  identity.
- Transition: `PHASE_REVIEW_TARGET` -> `PHASE_REMEDIATION_AND_RETEST`. Implement
  exact per-surface test ownership and an atomic P2 validation artifact
  producer, review the continuous target, then rerun the P2 gate on one compute
  node. External review remains skipped by user direction.

## 2026-08-10T18:30:16+09:00 — P2 remediation approved for runtime validation

- Frozen continuous target:
  `296b4cd595719b1b0f61ceb5fcbd97dd0585e76a`; prior phase target
  `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87` is a verified ancestor.
- P2-F1 remediation replaces group-level coverage claims with 81 exact current
  surface owners and adds focused CLI, inspection, manual-close,
  learner-admission and actor-identity behavior tests.
- P2-F2 remediation adds one create-only, atomic one-node validation producer
  that binds fixed commands, both P2 requirements, source identity,
  interpreter/packages, PBS topology, raw evidence and result classification.
- Login-safe Python syntax, JSON, selector/surface equality, Ruff on the changed
  files, shell syntax, literal group and diff checks pass.
- Mandatory Codex continuous review verdict: `APPROVE`. External review remains
  skipped under user direction.
- Transition: `PHASE_REMEDIATION_AND_RETEST` -> `STAGED_TEST_EXECUTION`. Acquire
  a main-agent-held one-node allocation and execute the new producer once from
  this clean source target.

## 2026-08-10T18:35:12+09:00 — P2 remediation validation PASS

- Main-agent-held allocation: `2519464.opbs`, one `interact-g` node `mg0012`,
  requested walltime `01:00:00`; exact PBS nodefile and default NVIDIA
  25.9/HPC-X 25.9 modules verified before execution.
- Source: clean commit `8d92bcbe2c16cc813fc5cdca6273e869617401ea`,
  scopes `fs_diloco`, `configs`, `scripts/miyabi`, `tests`, `pyproject.toml`,
  `README.md`, `docs`; fingerprint
  `sha256:07fdb8bf2ad92104b9ddb8de0fd2afd24ee7541cd7e5648ed58cb936c74ddaa0`.
- Producer result: `PASS`, no errors. Ruff format covered 127 files, Ruff lint
  passed, the explicit focused group passed 118 tests in 11.83 seconds, and the
  complete suite passed 504 tests in 22.63 seconds.
- Environment: Python 3.13.13, pytest 9.1.1, pytest-timeout 2.4.0, Ruff 0.15.21
  and Torch 2.13.0+cu132 from the project `.venv`.
- Evidence:
  - `artifacts/20260810-183244_p2-phase-remediation-validation_pass.log`
    (`sha256:d74ed3f323865a92ca8360a3c8edc6ed74ca9c5de4bde7520a6db0879b375f7a`);
  - `artifacts/20260810-183244_p2-phase-remediation-validation_pass.json`
    (`sha256:12bbc2bd669bfc858db3ce59d9a84ce496995fbe2e6197773dcfd7ab4dbec6a1`).
- `qsub` reported normal job completion and login-node `qstat` confirms no
  unfinished allocation. P2-F1 and P2-F2 are runtime-verified.
- Transition: `STAGED_TEST_EXECUTION` -> `PHASE_REVIEW_TARGET`. Freeze the
  tracked evidence and perform Codex-only P2 phase closure review.

## 2026-08-10T18:37:00+09:00 — P2 PHASE_FINAL

- Codex phase closure review approved tracked target
  `11290da38f12520ead2c9488662cfa573526fa91` after independently checking its
  source ancestry/scope equality, artifact schema, raw-log hashes, PBS identity,
  exact command results and requirement ownership.
- `UNIT-01` and `HARNESS-01` are complete. Both phase findings are fixed and
  verified; no blocking finding remains.
- External review was not invoked, following the user's Codex-only directive.
- P2 is complete. The next phase is P3 functional/fault testing: freeze the
  exact 4 learner + 1 syncer workload/scenarios and complete a Codex test-design
  review before the first five-node allocation.

## 2026-08-10T18:40:00+09:00 — P3 test-design implementation

- P2 phase-final commit: `9398e822ebe6cf9755e55567b18916802b93162f`.
- P3 now has one exact functional/fault manifest covering the normal,
  learner-replacement and syncer-takeover scenarios.
- The manifest fixes the 5-node topology; synthetic model/data identity; 20
  local steps, 4 global steps and exact 5120 direct applied tokens; every fault
  layer, injection boundary, authority oracle, timeout, PASS formula, output
  ownership and cleanup rule.
- Evidence-based walltime is `00:10:00` per scenario: the minimum allowed by
  repository policy and still more than twice the expected bounded workload,
  including takeover lease expiry and orderly evidence teardown.
- Next action: freeze this report-only design target and perform the mandatory
  Codex P3 test-design review. No five-node job may be submitted before approval.

## 2026-08-10T18:41:21+09:00 — P3 test design APPROVE

- Frozen design target: `4688bedebda2cee94137bf943425ca3d9c31ed17`;
  P2 phase-final `9398e822ebe6cf9755e55567b18916802b93162f` is its
  ancestor and the runtime source scopes are identical.
- Mandatory Codex review inspected topology, workload arithmetic, both fault
  layers, launcher mapping, Checker durable oracles, result classification,
  walltime, source invalidation and cleanup. Verdict: `APPROVE`.
- External reviewers remain skipped under user direction.
- Promotion is serial: submit only `functional-normal` after PBS script syntax,
  literal group, clean-source and create-only path checks. Fault scenarios stay
  blocked until the normal artifact is terminal PASS.

## 2026-08-10T19:04:00+09:00 — P3 candidate queue-routing target

- The first three normal-scenario submissions never entered execution and
  produced no run or evidence. The first two exposed site resource defaults;
  the third carried the exact bounded five-node request but could not backfill
  ahead of a reservation/top job.
- Live queue discovery reports `debug-g` as enabled/started for 1-16 nodes with
  a 30-minute maximum and available capacity. The P3 scenario is a brief
  five-node functional validation with an already reviewed ten-minute budget,
  so the manifest now explicitly selects `debug-g` and
  `5:ncpus=8:mpiprocs=1:mem=16gb`.
- This is a pre-execution test-design correction. Runtime source, topology,
  workload, fault boundaries, durable oracles, walltime and cleanup policy are
  unchanged. Freeze this target and complete a Codex critical-incremental
  review before submitting a new normal scenario identity.

## 2026-08-10T19:06:00+09:00 — P3 queue routing APPROVE

- Frozen continuous target:
  `debbcdf10267c5fb786dded33c17d2caba08f198`; the approved P3 design target is
  its ancestor and the complete runtime source scopes are byte-identical.
- Mandatory Codex critical-incremental review verified live queue limits,
  explicit PBS resources, unchanged topology/workload/oracles, pre-execution
  failure classification and source invalidation rules. Verdict: `APPROVE`.
- External review remains skipped under user direction. The next action is one
  new `functional-normal` submission to `debug-g`; fault scenarios remain
  blocked until its structured terminal artifact is PASS.

## 2026-08-10T19:06:26+09:00 — P3 normal targeted remediation

- The revised queue route started immediately and exercised the complete
  five-node Full Protocol in 14 seconds. Topology, source, exact applied work,
  publications, terminal authority, token balance and integrity all reached
  their registered durable values.
- The structured artifact is still `FAIL`, so no P3 requirement is promoted.
  Its only error is a Checker-only exact-total-cycle assertion: asynchronous
  learners produced one additional adjudicated cycle each, with all four extra
  proposals terminal/supersession-dropped and fully balanced.
- Transition: `STAGED_TEST_EXECUTION` -> `TARGETED_REMEDIATION` in the harness
  domain, count 1. Preserve the complete failed run, strengthen the Checker to
  validate exact applied work plus lawful dropped overshoot, add positive and
  negative aggregate tests, complete Codex review, then rerun one-node
  validation before another five-node normal attempt.

## 2026-08-10T19:12:35+09:00 — P3 Checker remediation target

- The normal-run Checker now separates committed work from asynchronous local
  attempts. It still requires exact applied proposals/tokens, and additionally
  proves one-to-one receipt/proposal identity, exact per-cycle workload,
  applied-or-dropped-only terminal fates, exact dropped-token accounting and
  zero local discard/quarantine/unpublished/outstanding work.
- The aggregate positive fixture now contains one fully adjudicated terminal
  overshoot proposal. A new mutation converts that drop to quarantine while
  preserving ledger balance and must be rejected, so the weaker false-PASS
  interpretation is covered independently.
- The P3 manifest and artifact description explicitly distinguish processed
  attempts from applied work; the exact module-coverage selector follows the
  renamed positive boundary test. Login-safe formatting, lint, Python syntax,
  JSON and diff checks pass.
- Transition: `TARGETED_REMEDIATION` -> `TEST_REVIEW_TARGET`. Freeze this
  continuous target and complete mandatory Codex critical-incremental review
  before any compute test.

## 2026-08-10T19:13:32+09:00 — P3 Checker remediation APPROVE

- Frozen continuous target:
  `a040cb9c9a073b87a90cdc467dc1c04f72e16ca0`; base is the exact clean source
  used by failed job `2519662.opbs`.
- Mandatory Codex review traced learner attempts through receipt/proposal
  ingestion, supersession, terminal adjudication, token rollup and Checker
  projection. It confirmed the failure was a total-attempt/committed-work
  ontology error and that the replacement oracle remains false-PASS resistant.
- Verdict: `APPROVE`; external review remains skipped under user direction.
  Acquire a main-agent-held one-node allocation and run the fixed validation
  producer before submitting another five-node scenario.

## 2026-08-10T19:15:47+09:00 — P3 Checker remediation validation PASS

- Main-agent-held allocation `2519748.opbs` ran on exact one-node
  `interact-g` host `mg0018` with `1:ncpus=8:mem=16gb`; requested walltime was
  `01:00:00`, terminal exit status is 0, and actual walltime was 3:17.
- Clean source commit:
  `44fe476898cd89e9b4fbb13930aa338e56cbd87f`; fingerprint
  `sha256:87e6223c81c49839673b949eccb540f95fcd22d75e4aece3f9824aad124a0314`.
- The structured validation producer returned `PASS` with no errors: Ruff
  format covered 127 files, lint passed, the focused group passed 119 tests in
  12.38 seconds, and the complete suite passed 505 tests in 23.89 seconds.
- Environment: Python 3.13.13, pytest 9.1.1, pytest-timeout 2.4.0, Ruff 0.15.21
  and Torch 2.13.0+cu132 from the project `.venv`.
- Evidence:
  `artifacts/20260810-192000_p3-checker-remediation-validation.json` (SHA-256
  `a9b3adf8d491aad576f615ee171d36e51c38be0a3cb7a67b7de13aef4b723ecd`)
  and matching raw log (SHA-256
  `aff0c9d740de5fafd89b81f19aefbf2927d1e0d59471246420f820b896d90c14`).
- The harness failure counter resets to zero. The interactive allocation was
  closed normally and no unfinished job remains. Freeze this evidence, then
  submit a new normal scenario from the same post-review source scopes.

## 2026-08-10T19:19:14+09:00 — P3 functional-normal PASS

- PBS job `2519796.opbs` ran for 14 seconds on exact five-node `debug-g`
  topology `mg0020`, `mg0024`, `mg0025`, `mg0026`, `mg0028` with one syncer,
  four descriptor learners, five GPUs and the registered bounded resources.
- Source is clean commit `4ebee6339fb76f63127874c655d7b109b2ec0b39`,
  fingerprint
  `sha256:87e6223c81c49839673b949eccb540f95fcd22d75e4aece3f9824aad124a0314`.
- Structured Checker result is `PASS`, errors empty. Versions are exactly 0-4;
  all four contributors have four applied proposals and credit four; exact
  direct applied work is 5120 tokens. Twenty-three exact-workload proposals
  were processed, with 16 applied and seven lawfully dropped (2240 tokens);
  local discard/quarantine/unpublished/outstanding totals are zero and the
  ledger balance is zero.
- All actor/node attestations, the released syncer epoch, acknowledged terminal
  fences, immutable publication hashes and SQLite integrity passed. Scheduler
  terminal state has exit status 0.
- Evidence:
  `artifacts/20260810-191842_p3-functional-normal_result.json` (SHA-256
  `87f1e97dcbfcd02faeeb841f02994213ab08a3cd3b00e3a29e3179d6238a1d44`)
  and PBS log (SHA-256
  `c4c4601670c5ce62b9c3b061505029aed1ccabdf5a7f46715b9984be64c6f3f2`).
- Promotion remains serial. Do not commit or edit formal source scopes; submit
  learner replacement next from the same commit/fingerprint.

## 2026-08-10T19:20:42+09:00 — P3 learner-replacement remediation required

- Job `2519816.opbs` successfully exercised the replacement fence and all
  replacement-specific durable oracles, but its structured `PASS` is rejected
  by coordinator audit and is not promoted.
- Terminal authority/control reports only 2560 directly applied tokens while
  publication history and the durable rollup report the registered 5120.
  Finalization summed only the current hot version table after maintenance had
  archived older versions. The Checker compared each side internally but
  omitted their cross-authority equality.
- Transition: `STAGED_TEST_EXECUTION` -> `TARGETED_REMEDIATION`; valid product
  count 1 and Checker false-PASS count 1. Preserve the complete run, fix the
  terminal authority source and Checker oracle with archive-aware tests, review
  the continuous target, rerun one-node validation, then restart all three P3
  scenarios from one new source commit/fingerprint.

## 2026-08-10T19:27:10+09:00 — P3 terminal applied-total remediation target

- Terminal finalization now reads the all-history `direct_applied` total from
  the singleton token rollup inside the same fenced transaction. It rejects a
  missing ledger or nonzero outstanding work and no longer derives terminal
  accounting from the prunable hot publication table.
- The existing archive-before-terminal authority test now requires the
  terminal total and durable rollup to retain both committed updates after the
  older publication is archived. The aggregate Checker independently requires
  terminal authority to equal the registered exact workload.
- A synchronized negative mutation rewrites terminal authority, both fixed
  controls and the immutable terminal publication to the same incomplete
  value. It leaves publication history and rollup intact, so only the new
  cross-authority oracle can reject the internally consistent false projection.
- The P3 manifest and artifact contract now state the exact five-way agreement
  across publication history, rollup, terminal authority and fixed/immutable
  controls. Login-safe formatting, lint, syntax, JSON and whitespace checks
  pass; project runtime tests remain intentionally unrun on the login node.
- Transition: `TARGETED_REMEDIATION` -> `TEST_REVIEW_TARGET`. Freeze this
  continuous target and complete the mandatory Codex critical-incremental
  review; external review remains skipped under user direction.

## 2026-08-10T19:28:12+09:00 — P3 terminal applied-total review changes required

- Frozen target: `36987b41daa67558ff83024abb282122f40065bf`;
  continuous review base: `4ebee6339fb76f63127874c655d7b109b2ec0b39`.
- Mandatory Codex review found one High regression: the token rollup is lazily
  created by the first receipt, and the current read contract treats its
  absence as an all-zero ledger. Requiring a physical row at finalization
  breaks valid zero-cycle and pre-receipt hard-crash terminal paths.
- Finding `P3-TAT-001` is accepted. Interpret an absent rollup as zero inside
  the terminal transaction and extend the existing zero-cycle test to assert
  the persisted terminal total. The archive authority fix and Checker oracle
  remain valid.
- Verdict: `CHANGES_REQUIRED`; external review remains skipped. Transition:
  `TEST_REVIEW_TARGET` -> `TEST_REMEDIATION`.

## 2026-08-10T19:29:09+09:00 — P3 zero-work terminal finding remediated

- `finalize_terminal` now applies the existing ledger read contract inside its
  fenced transaction: an absent lazily materialized rollup is exactly zero
  applied and zero outstanding work; a present rollup supplies the durable
  all-history values. Nonzero outstanding work still blocks finalization.
- The current zero-cycle test now requires persisted terminal applied work to
  equal zero. Together with the archive-before-terminal assertion, the tests
  cover both sparse-zero and pruned-positive ledger states.
- Login-safe formatting, lint, syntax, JSON and whitespace checks pass. Freeze
  the continuous remediation target and rerun the mandatory Codex incremental
  review before any compute test; `P3-TAT-001` remains open until that verdict.
- Transition: `TEST_REMEDIATION` -> `TEST_REVIEW_TARGET`; external review is
  skipped under user direction.

## 2026-08-10T19:29:48+09:00 — P3 terminal applied-total remediation APPROVE

- Frozen continuous target:
  `8e7f87432895f3a699c93dc252aa225b7d9944a8`; rereview base is the rejected
  target `36987b41daa67558ff83024abb282122f40065bf`.
- Mandatory Codex review rechecked both sparse-zero and archived-positive
  ledger states, terminal transaction fencing, control projections, Checker
  row merging, exact workload identity and the synchronized mutation.
- `P3-TAT-001` is closed. No remaining blocking finding was identified;
  verdict: `APPROVE`. External review remains skipped under user direction.
- Transition: `TEST_REVIEW_TARGET` -> `STAGED_TEST_EXECUTION`. Acquire one
  main-agent-held `interact-g` node and run focused plus complete validation
  before any five-node scenario is resubmitted.

## 2026-08-10T19:32:15+09:00 — P3 terminal applied-total validation PASS

- Main-agent-held allocation `2519904.opbs` ran on exact one-node
  `interact-g` host `mg0004` with `1:ncpus=8:mem=16gb`; requested walltime was
  `01:00:00`, terminal exit status is 0, and actual walltime was 1:31.
- Clean source commit:
  `0e951518c3ef693b20a241e36fc8396d61f609a7`; fingerprint
  `sha256:6b4964840a786276242608746f862ca3ce05ae48f256d8755eadb58cfd4454e5`.
- The structured producer returned `PASS` with no errors. Ruff format and lint
  passed; the focused group passed 120 tests in 12.96 seconds; the complete
  suite passed 506 tests in 25.07 seconds.
- Environment: Python 3.13.13, pytest 9.1.1, pytest-timeout 2.4.0, Ruff 0.15.21,
  Torch 2.13.0+cu132 and compute modules `nvidia/25.9`, `nv-hpcx/25.9`.
- Evidence:
  `artifacts/20260810-193100_p3-terminal-total-validation.json` (SHA-256
  `e53b58570490861b0ccf7936c036a8be1813852a0296b1c2bb2a2431d221cfaa`)
  and matching raw log (SHA-256
  `318899948d345ca5898e3b449be19f1b2e491af7ab28fa5442e6ceb3e03d2f55`).
- The Checker harness counter resets to zero. The allocation closed normally.
  Freeze this evidence, then rerun normal, learner replacement and syncer
  takeover serially from one new source commit/fingerprint.

## 2026-08-10T19:34:53+09:00 — P3 functional-normal rerun PASS

- PBS job `2519926.opbs` ran 15 seconds on exact five-node `debug-g` topology
  `mg0005`, `mg0006`, `mg0010`, `mg0012`, `mg0013`; scheduler exit status is
  0. Source commit is `7528370e6b6635ecc7d6c2b40ac8e337c901826b`,
  fingerprint
  `sha256:6b4964840a786276242608746f862ca3ce05ae48f256d8755eadb58cfd4454e5`.
- Structured status is `PASS` with no errors: versions 0-4, four applied
  proposals per contributor, 16 applied and 12 lawfully dropped proposals,
  5120 applied of 8960 processed tokens, zero local discard/quarantine/
  unpublished/outstanding work and zero ledger balance.
- Independent terminal audit confirms 5120 in merged publication history,
  token rollup, terminal SQLite authority, fixed stop/summary controls and the
  immutable stop publication. The pruned hot publication table contains only
  version 4 and 1280 tokens, directly exercising the repaired authority
  boundary.
- Actor/node attestations cover one syncer plus four learners on the exact PBS
  nodefile; the sole epoch is released; terminal fences, hashes and SQLite
  integrity pass.
- Evidence:
  `artifacts/20260810-193300_p3-functional-normal_result.json` (SHA-256
  `a814c6b991ff4fe0bfefa93cad6e8bb49c19b88ee0772cecd19a15f65e2b7ba4`)
  and PBS log (SHA-256
  `de8b8f09a6efe0f122b2729fab7bdbc9f4d6c7663a93c926d25190823bc3cfa5`).
- Keep report changes uncommitted so all three scenarios retain the exact same
  source commit. Submit learner replacement next.

## 2026-08-10T19:36:25+09:00 — P3 learner-replacement rerun PASS

- PBS job `2519935.opbs` ran 17 seconds on exact five-node `debug-g` topology
  `mg0001`, `mg0005`, `mg0006`, `mg0007`, `mg0010`; scheduler exit status is
  0. It retained source commit
  `7528370e6b6635ecc7d6c2b40ac8e337c901826b` and fingerprint
  `sha256:6b4964840a786276242608746f862ca3ce05ae48f256d8755eadb58cfd4454e5`.
- Structured status is `PASS` with no errors. The original `learner_000`
  attempt exited 143, binding generation advanced 1 -> 2, old history is
  `replaced`, the registered successor became current, contributed accepted
  work and acknowledged the terminal fence.
- Versions are exactly 0-4 with four applied proposals per contributor. The
  ledger records 16 applied and 32 dropped proposals, 5120 applied of 15360
  processed tokens, zero local discard/quarantine/unpublished/outstanding work
  and zero balance.
- Independent audit confirms exactly 5120 tokens in merged publications,
  rollup, terminal authority, fixed controls and immutable stop. The pruned hot
  table again holds only the final 1280-token publication. Topology, epoch,
  hashes, terminal fences and SQLite integrity pass.
- Evidence:
  `artifacts/20260810-193500_p3-learner-replacement_result.json` (SHA-256
  `4e909f836b11f5bb1baa590e65e21419e541235965e2468c03856c3c3e99fade`)
  and PBS log (SHA-256
  `0ce8e4829b94f6005aaee6e2c51660deeec90fda5486c678fdc752caffb3c7de`).
- The product failure counter resets to zero. Keep the report changes
  uncommitted and submit syncer takeover from the identical source identity.

## 2026-08-10T19:38:29+09:00 — P3 syncer-takeover rerun PASS

- PBS job `2519945.opbs` ran 29 seconds on exact five-node `debug-g` topology
  `mg0004`, `mg0005`, `mg0006`, `mg0007`, `mg0010`; scheduler exit status is
  0. It used the same source commit
  `7528370e6b6635ecc7d6c2b40ac8e337c901826b` and fingerprint
  `sha256:6b4964840a786276242608746f862ca3ce05ae48f256d8755eadb58cfd4454e5`
  as the normal and learner-replacement runs.
- The primary syncer was killed with status 137 immediately after version 2;
  the retained marker proves the fault occurred outside a SQLite transaction
  after the lease renewer was quiesced. Epoch 1 durably expired and links to
  epoch 2; the successor committed versions 3 and 4 and released normally.
- Structured status is `PASS` with no errors: exactly four applied proposals
  per contributor, 16 applied and four dropped proposals, 5120 applied of 6400
  processed tokens, zero local discard/quarantine/unpublished/outstanding work
  and zero ledger balance. No stale epoch publication follows takeover.
- Independent terminal audit confirms 5120 in merged publications, rollup,
  terminal authority, fixed controls and the epoch-2 immutable stop. Actor
  attestations, terminal fences, publication hashes and SQLite integrity pass.
- Evidence:
  `artifacts/20260810-193700_p3-syncer-takeover_result.json` (SHA-256
  `219fc5ef00ab0e700838360351627d3437ccdbe8fa8bd5fb8847568bcb7b3769`)
  and PBS log (SHA-256
  `9fa5d202793e172bdc43ed7996adf67ec7538a8c7f7294b3fdb0068b647cf557`).

## 2026-08-10T19:38:29+09:00 — P3 phase review target

- All three registered P3 scenarios are terminal `PASS`, use the identical
  clean source commit/fingerprint and satisfy their durable fault oracles.
  Product and harness counters are zero; obsolete earlier evidence remains
  explicitly invalidated rather than promoted.
- Transition: `STAGED_TEST_EXECUTION` -> `PHASE_REVIEW_TARGET`. Freeze the
  complete evidence set, then perform the mandatory Codex P3 code-and-evidence
  review from P2 phase-final `9398e822ebe6cf9755e55567b18916802b93162f`.
  External review remains skipped under user direction.

## 2026-08-10T19:40:27+09:00 — P3 PHASE_FINAL

- Mandatory Codex phase review approved frozen target
  `a133a98a431566dbd1aef1af6a7f496f2c301d38` after checking the complete P3
  source increment, one-node validation, all three final five-node artifacts,
  raw logs, source identity, scheduler history and durable authority paths.
- The final scenarios share exact source commit/fingerprint, and the reviewed
  evidence proves normal operation, learner fencing/replacement, syncer
  expiry/takeover and archive-safe terminal token accounting. Earlier failed
  evidence remains invalidated; no blocking finding remains.
- `FUNC-4L1S-01` and `FAULT-4L1S-01` are complete. External review was not
  invoked, following the user's Codex-only directive.
- P3 is complete. Begin P4 by freezing the preformal candidate/current-state
  review design before the formal 8+1 experiment and documentation closure.

## 2026-08-10T20:00:00+09:00 — P4 candidate hardening started

- P3 remains phase-final. The P4 candidate adds a single registered fault
  scenario interface, restores the durable `last_update_id` needed by resumed
  learners, makes pytest validation evidence JUnit-backed, and introduces a
  strict same-target formal-ladder completion checker.
- The final ladder is fixed at one U1 validation, three five-node P3 scenarios,
  one nine-node 50-by-10 G1 run, and two Codex internal reviews. External
  reviewers remain skipped under the user's directive and their untracked
  output is not accepted as evidence or as a workflow gate.
- Candidate source and tests are not yet frozen or runtime-validated. Next:
  complete static hardening, commit the candidate, then run focused and full
  validation on one confirmed Miyabi compute node.
