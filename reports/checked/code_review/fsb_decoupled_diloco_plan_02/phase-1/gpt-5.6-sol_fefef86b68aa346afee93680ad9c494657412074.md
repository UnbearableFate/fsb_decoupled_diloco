# Independent Codex review — Plan 02 Phase 1 final evidence target

## Review identity

- Decision: **APPROVE**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `fefef86b68aa346afee93680ad9c494657412074`
- Comparison base: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`
- Reviewed diff: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5..fefef86b68aa346afee93680ad9c494657412074`
- Executable source identity: clean commit `36762854bfcbbc23b71ab838913023d64cf37b5e`, source fingerprint `sha256:c2cd29b369c4825af1cf491ad414de68e11c77e78df0173915f6ef834827dbe2`; the later target commit adds only retained reports and synchronized documentation and does not change `fs_diloco`, configurations, PBS scripts, or tests.
- Formal run identity: `plan02_phase1_final_3676285`, descriptor SHA-256 `151a3f77ffa8c54bb9e2a038e36ed04382d1ccfa2ecfd75c401627cc3ba6bc17`, config SHA-256 `1b0afea92e9ffe98afb692360dc7a2edd9d57a706d7afde1a1826c556490c3f9`.

## Scope and method

I reviewed the complete cumulative Phase 1 diff and target-tree context, including configuration validation and source pinning; controlled schema bootstrap and read-only open; leader acquisition, renewal, local monotonic safety and exact-token release; the fenced business-store SQL/mutator surface; DB-first recovery; epoch-scoped checkpoint and control publication; learner filesystem observation, watchdog and canonical adoption; recovery claim arbitration and PBS reconciliation; maintenance and per-file fenced GC; independent launch receipts; completed Checker derivations; the matched candidate/checkpoint performance harness; PBS launchers; focused and full regression tests; fault/lock/1+8 artifacts; requirement records; and synchronized documentation.

The preceding Codex report for `831b1751c5572c39121113ac73099238f3fa9ed4` was used only as a remediation checklist. I rechecked each accepted finding against the current implementation and negative tests, then checked the newly added matched-sampling remediation and final clean artifacts. The external reviewer for this review cycle had already returned a verified session-limit result and was recorded as `skipped-session-limit`; no Claude report exists or was treated as an approval.

## Findings

### Critical

None.

### High

None.

### Medium

None.

### Low

None within the Phase 1 contract. The previously recorded universal dynamic-instance discovery migration remains explicitly owned by Phase 2 MEM-02/MEM-20; Phase 1 static discovery paths used by the formal evidence are recursive, nonempty, and validated, so this is not represented as completed Phase 2 work.

## Correctness, concurrency, persistence, and error handling

- Leadership changes remain serialized by SQLite `BEGIN IMMEDIATE`; every bound business mutation checks local monotonic lease safety and exact epoch/owner after obtaining the writer lock. Version, update, controller, terminal and control-publication writer identities are checked against immutable epoch ownership by the completed Checker.
- Recovery submission now takes one filesystem-global atomic reservation before reconciliation, global-outstanding accounting, per-observation attempt allocation and durable pending-claim publication (`fs_diloco/runtime/launch_outbox.py`). The synchronized different-observation regression proves that `max_outstanding_candidates=1` yields one attempt and one qsub.
- The acceptance launcher writes a pending artifact before the first qsub and atomically persists every success or failure receipt. Partial submission preserves accepted job IDs and requested walltimes, exits nonzero and does not cancel scheduler jobs (`fs_diloco/tools/launch_phase1_acceptance.py` and its PBS wrapper).
- The syncer stops the renewal thread before the final metrics snapshot and `process_exit`; stop failure is logged with a blocking event and propagated. The finalizer does not silently report zero renew failures from a still-running thread (`fs_diloco/runtime/syncer.py`).
- The SQL fence rejects assignment and parenthesized setter PRAGMAs while retaining only explicitly modeled read-only argument forms (`fs_diloco/storage/fenced_store.py`). Tests cover `journal_mode(WAL)`, `synchronous(OFF)`, `query_only(OFF)` and `busy_timeout(1)`.
- The completed Checker no longer fabricates stale-commit or canonical-adoption zeros. It derives writer ownership from persisted rows, requires exactly one successful exit for every expected learner at the terminal version, scans the blocking runtime-event vocabulary, and treats missing evidence as blocking (`scripts/miyabi/check_plan02_phase1.py`).
- The matched business gate uses 32 fine-grained 25-sample AB/BA blocks with one persistent read-only candidate. Every baseline block proves zero observations, every observer block proves at least one complete `terminal_state + observe` cycle, and SQLite tracing counts actual `BEGIN IMMEDIATE/EXCLUSIVE` attempts. The checkpoint gate constructs the target model/seed/tensor and alternates 100 legacy and 100 HA publications on the same shared filesystem (`fs_diloco/tools/phase1_matched_performance.py`).

## Test and acceptance evidence

- Static checks passed: Ruff, targeted format check, `py_compile`, `git diff --check`, `bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh`, and the literal PBS group-ID scan.
- PBS `2499320.opbs` passed `67` focused Phase 1 tests and `448` full-tree tests in 33 seconds with a 60-second request.
- Replacement fault matrix `2499210.opbs` passed all 60 crash cases; two-node writer-lock probe `2499211.opbs` passed both outside-transaction takeover and inside-transaction exclusion boundaries; smoke `2499212.opbs` completed without runtime failure.
- Final clean independent acceptance used launcher `2499329.opbs`, injected-crash syncer `2499331.opbs`, successor `2499332.opbs`, and learner array `2499333[0-7].opbs`. The predecessor died as injected after the v0 DB commit, epoch 2 recovered and committed v1–v10, all eight learners exited normally, terminal generation reached 2, total seen tokens reached 5,120, and the leader was released.
- Formal matched performance `2499345.opbs` passed: business baseline/observer had 400 samples each, observer p99 `0.020052672s` was below `0.030637979s`, all 16 observer blocks recorded two observations, all 16 baseline blocks recorded zero, and writer transaction attempts were zero. Legacy/HA checkpoint publication had 100 samples each and HA p99 `0.015461578s` was below `0.020234488s`. Artifact SHA-256: `63299493aa9eaeb8d372c8296cbd1d73135b6cce358e838d761f0238a5a49d78`.
- Completed Checker `2499349.opbs` returned `PASS` with no errors: 120 renew samples and 457 business transactions had zero failures; business p99 was `0.018572575s < 0.05s`; takeover was `1.030941486s < 10.2s`; runtime failure events, stale-epoch commits and canonical-adoption errors were all zero. Artifact SHA-256: `590129ac221c51679aafca517b92b86a92727e0d70908769021336609c58f74e`.
- Requested walltimes retained practical margins over the measured runtime: launcher 15/2 seconds, crash 15/6, successor 40/22, learners 35/18, matched 60/35 and Checker 15/4. This follows the repository rule that scheduling benefit must not compromise reliable completion.

## Regression risk and plan alignment

The remaining regression risk is concentrated in filesystem and scheduler behavior outside the measured Miyabi conditions, especially long-tail shared-filesystem latency and optional learner-assisted recovery when explicitly enabled. The implementation fails closed on source/config identity mismatch, incomplete bootstrap, unsupported fragment+HA configuration and unmodeled SQL. The crash matrix, real two-node writer-lock probe, exact-source 1+8 takeover, two consecutive corrected matched-performance passes and evidence-derived completed Checker jointly cover the Phase 1 acceptance boundaries. No Phase 2 dynamic-membership behavior is enabled or claimed.

## Final decision

**APPROVE**

No Critical, High, Medium or in-scope Low defect remains in the reviewed Phase 1 diff. The accepted findings from the preceding review are fixed with negative regression coverage, and the final clean 9-node runtime, matched performance gate and completed Checker are mutually bound to the same descriptor/source identity.
