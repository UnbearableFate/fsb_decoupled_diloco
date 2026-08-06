# Independent Codex review — Plan 02 Phase 0 final candidate

## Review identity

- Decision: **APPROVE**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `f404fbd4831adcd9ffb8e6229a0004b1affe9f4e`
- Comparison base: `c1c61153548ff7b2543d3ce1bc764c19432b138e`
- Relevant cumulative diff: `c1c61153548ff7b2543d3ce1bc764c19432b138e..f404fbd4831adcd9ffb8e6229a0004b1affe9f4e`
- Source identity: branch `codex/fsb_decoupled_diloco_plan_02`; the final experiment records commit `c43a519997a581357561981cd448b07a24df5fdb`, `git_dirty=true`, and source fingerprint `sha256:ccdc35c09745ebbdff4be5ae9b50646a04b785a27c84ecc685aa4fd0e345682a`. Recomputing the committed scopes at the review target produces the same fingerprint with `git_dirty=false`. The manifest includes ignored explicit `uv.lock` with SHA-256 `240e2fd8dc4294b2e3aef8c5c2061e209549451a66632d8419c90bc374d64a8b`.

## Scope and methods

I reviewed the complete cumulative diff from the Plan 02 branch point: scoped review-governance changes, plans and requirement matrix, research/operations/module documentation, all Phase 0 probes and aggregators, the Checker, three PBS scripts, source-identity capture, focused tests, append-only records, and retained failure/success evidence. No production `fs_diloco/` implementation is changed by Phase 0.

I traced every retained requirement result back to raw fields and the code that produces and checks them. I inspected the two-node writer-lock control flow, clock interval derivation and discontinuity bound, SQLite transaction aggregation, scheduler terminal/physical-incarnation queries and sanitization, canonical cache adoption, guarded source/runtime writes, JSON publication, job-level failure handling, and cleanup guards. Static PBS syntax, Python compilation, Ruff, and diff-whitespace checks pass. The recorded compute-node focused group is `23 passed in 12.45s`; the final two-node parent job exits zero and the successful work directory is absent.

## Findings

No `Critical`, `High`, `Medium`, or `Low` finding was identified for Phase 0 completion.

The following previously risky surfaces were specifically inspected and are now adequately covered:

- FEAS-01 includes both a local control and a real two-node holder/contender/successor sequence. The holder's stopped state is observed before publication, the remote contender reports SQLite lock contention and cannot see the tentative row, the holder exits by `SIGKILL`, and the remote successor commits only after the kill acknowledgment. The Checker independently requires two hosts and consistent holder/contender/successor placement.
- FEAS-02 selects epoch 2 independently for `latest`, `stop`, and `summary` after an epoch-1 writer clobbers every convenience cache; exact discovery/pollution/repair maps prevent empty-set pass-through.
- FEAS-03 uses a mathematically conservative two-way offset interval. It requires interval intersection, a configured absolute bound, a separate wall/monotonic discontinuity bound, two hosts, read-only reopen, exact per-writer/DB transaction equality, both acquire and renew, observed busy contention, zero starvation, DELETE/FULL PRAGMAs, and integrity `ok`.
- FEAS-04 requires real queued/prologue/running/finished observations and terminal exit zero; workload markers cannot hide later failure. Array capability requires automatic submission, selected array orchestration, rerunable evidence, and two successful physical child records. The fallback must select the independent manifest. qstat persistence is allowlisted.
- FEAS-05 compares independent expected run/protocol/schema values before import. Its control case reaches a post-gate subprocess that imports `fs_diloco` and writes exactly one row, while every mismatch case proves gate import false, runtime not started, runtime import false, and zero writes. Typed before/after counts and per-owner DB queries make the business-write evidence falsifiable.
- Present ignored explicit source files are fingerprinted, so the environment-specific dependency lock can no longer silently fall outside the declared source scope.
- A real aggregation failure produced a structured `_blocked.json`; later test and experiment failures are recorded before modification. Success cleanup is restricted to a resolved job-specific child path and runs only after the final Checker artifact is fsynced.

## Facts

1. Final artifact `20260806-095900_phase0-feasibility_pass.json` has SHA-256 `56056fdafed7b0f1bd7f472ca78771f876edb3204cb0989ff97cf1159d9cdc56` and returns `PASS` for FEAS-01 through FEAS-05.
2. Source bytes match the review target even though the artifact necessarily records its pre-commit parent and dirty state.
3. FEAS-01 runs across `mg0004` and `mg0008`; its single-node control also passes.
4. The clock absolute offset upper bound is `0.003113702` seconds and maximum wall/monotonic delta is `0.000000544` seconds, below their 2.0/0.1-second bounds.
5. Eight writers commit 400/400 rows across two hosts, with 4,139 handled busy events, zero starvation, 48 acquire and 352 renew actions, safe PRAGMAs, and integrity `ok`.
6. Scalar job `2497335.opbs` and array children `2497336[0].opbs` and `2497336[1].opbs` have terminal exit zero; array children are rerunable and record `run_count=1`.
7. Matching source identity produces exactly one guarded control write. Seven mismatch classes produce zero runtime/business writes.
8. The requirement matrix correctly labels the five requirements `completion-candidate`; historical fallback evidence is explicitly identified as cross-version rather than current-source evidence.
9. The plan completion gate is correctly scoped in `plans/AGENTS.md` and uses a fresh verified `claude --print`/`claude -p` process rather than Herdr.

## Correctness, regression, and invariant assessment

The Phase 0 tree changes only evidence tooling and one pre-existing diagnostic script, not the training protocol. Existing SQLite `stress`, `verify`, and `kill-reopen` behavior remains covered. New open-existing/read-only entry points have explicit schema and DDL boundaries.

Concurrency safety is demonstrated rather than inferred: SQLite itself serializes the stopped writer and remote successor, contention rows are uniquely keyed, non-busy operational errors propagate, and DB-side totals cross-check writer reports. File persistence uses destination-directory temporary files, file fsync, and replace. Cross-process handoffs use atomic JSON paths, unique job-scoped directories, and bounded waits.

Error handling is fail closed. Probe failure, MPI failure, scheduler uncertainty, Checker exception, missing evidence, and terminal child failure cannot produce `PASS`. The deliberate decision to classify a submitted-but-unobserved scheduler child as `BLOCKED` is conservative; deferring richer uncertainty reconciliation to Phase 1 does not weaken Phase 0.

The measured contention maximum wait of 7.37 seconds is above the Phase 1 draft's 5-second busy-timeout suggestion. Progress correctly records it as a P1-L1 tuning input rather than claiming the draft default is validated. This is a forward implementation constraint, not a Phase 0 defect.

## Test coverage and acceptance criteria

The focused tests cover all accepted behavioral remediation defects, including the previously missing hostname publication/aggregation regression and guarded source write. The real two-node run covers the topology that local tests cannot. PBS syntax and literal group IDs are separately checked.

| Requirement | Decision |
| --- | --- |
| FEAS-01 writer-lock pause/release boundary | Satisfied for one and two nodes. |
| FEAS-02 stale fixed-cache writer | Satisfied for all three canonical artifact kinds. |
| FEAS-03 clock/shared SQLite | Satisfied with measured bounds, readonly visibility, contention, PRAGMAs, and integrity. |
| FEAS-04 PBS capability | Satisfied with real lifecycle, terminal, fingerprint, array, physical-incarnation, and fallback evidence. |
| FEAS-05 source pinning | Satisfied with independent expectations and falsifiable guarded runtime/write evidence. |
| Two-value Checker and structured blocked artifact | Satisfied. |
| No Phase 0 production-protocol change | Satisfied. |
| Documentation/records and evidence retention | Satisfied; historical contradictions are corrected append-only. |
| Non-interactive dual-review workflow | Satisfied by the scoped `claude -p` rules. |

## Inferences

- The Phase 0 evidence supports beginning Phase 1 after the second independent report and phase-final bookkeeping commit; it does not claim that HA or dynamic membership already exists.
- Rerunable PBS semantics conservatively require Phase 2 to distinguish physical incarnations even though the measured children each ran only once.
- Cross-node lock correctness and the observed 7.37-second worst wait make Phase 1 lease/busy-timeout tuning an empirical design task, not a reason to weaken fencing.

## Recommendations

1. Carry the 7.37-second contention maximum and the explicit stopped-writer operator-kill boundary into P1-L1 RED tests and timeout selection.
2. Preserve scheduler uncertainty as fail-closed until Phase 1 implements persistent reconciliation/backoff.
3. After the Claude report is verified, disposition any new finding, set FEAS rows to `complete`, append the phase-final record, and create the phase-final commit before starting Phase 1.

## Final decision

**APPROVE**

All Phase 0 acceptance criteria have current-source, fail-closed, auditable evidence. No unresolved finding blocks the phase completion gate.
