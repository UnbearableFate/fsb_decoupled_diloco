# Independent Codex review — Plan 02 Phase 1 final target

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `831b1751c5572c39121113ac73099238f3fa9ed4`
- Comparison base: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`
- Source identity: clean Git commit `831b1751c5572c39121113ac73099238f3fa9ed4`; cumulative diff `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5..831b1751c5572c39121113ac73099238f3fa9ed4`
- Runtime identity checked independently from the retained run descriptor: clean commit `831b1751c5572c39121113ac73099238f3fa9ed4`, source fingerprint `sha256:559daa3a650c9647781eb54bdfd19cbad32c836b10cd28a9418982ba506bb253`, descriptor SHA-256 `6c5086c8e23282693a4e67ed40be945344883e9dcbac54dc0aa22cb7bc2dde52`, run `plan02_phase1_final_831b175`.

## Scope and method

I reviewed the complete cumulative Phase 1 diff and target-tree context: configuration and immutable source identity; controlled schema bootstrap; leader acquisition, renewal and local safety; the fenced business-store surface; epoch-scoped checkpoint/control publication and filesystem-only reader; learner watchdog/adoption; recovery claim and PBS reconciliation; maintenance/GC; independent launchers and role scripts; Checkers and analysis/metrics discovery; focused regression tests; the fault, lock and clean 1+8 artifacts; requirement matrix; and synchronized documentation. I started the fresh external reviewer first and did not read its report, result, or substantive conclusions before this report was saved.

The latest 1+8 execution is genuinely tied to the target commit. Its crash predecessor exits as intended, the successor takes epoch 2 and reaches v10/generation 2/5,120 tokens, all eight learners and the completed Checker exit zero, and the measured renew/business/takeover samples that are actually collected pass their configured thresholds. The post-renewer startup-cleanup defect from the preceding Codex target is fixed for initial logging and terminal-state observation. The report nevertheless cannot approve Phase 1 because the completed Checker currently asserts several strict-zero acceptance results without measuring them, a required global recovery budget is racy, and the acceptance launcher still loses partial scheduler receipts.

## Findings

### High — completed Checker fabricates strict-zero safety results and can miss shutdown renewal failure

The structured result reports `canonical_adoption_error_count=0` and `stale_epoch_business_commit_count=0` as literals, not values derived from DB state or runtime events (`scripts/miyabi/check_plan02_phase1.py:583-584`). Neither field participates in `performance_missing` or any `_check`, so any run receives both zeros. The failure scan also recognizes only event types exactly `error` and `uncaught_exception` (`scripts/miyabi/check_plan02_phase1.py:326-330`), even though the runtime emits failure-bearing events such as `lease_renewer_stop_failed`, `leader_release_failed`, `no_progress_timeout`, `syncer_recovery_exhausted`, `canonical_latest_wait_failed`, and adoption failures. This makes the artifact claim of zero failure events narrower than the runtime failure vocabulary.

There is an additional end-of-run observation gap: the syncer snapshots `lease_renew_failure_count` into `process_exit` before `wandb.finish()` and before `lease_renewer.stop()` (`fs_diloco/runtime/syncer.py:3331-3365`). A renewer failure during that interval is logged only as `lease_renewer_stop_failed`, which the Checker ignores, while the earlier process-exit snapshot still says zero. Thus the completed gate can return `PASS` while its reported normal-run renew-failure count and failure-event count are false.

This directly violates the plan §11.1 strict-zero requirements and the Checker contract that missing core evidence returns `BLOCKED`. The retained clean run happens not to contain suspicious event names, but that manual fact does not make the Checker falsifiable for future runs or substantiate the two hard-coded fields.

Derive stale-epoch commits from the version/update/controller writer epoch-owner mappings and epoch history, add explicit runtime counters/events for rejected stale attempts versus any impossible accepted stale write, and derive canonical adoption failures from learner exit/adoption telemetry. Define and scan the complete blocking event vocabulary. Stop the renewer before taking its final metric snapshot, while retaining leadership until terminal publication is finished. Add negative Checker tests that inject each nonzero condition and require `BLOCKED`; no current unit test imports or exercises `check_run`.

### Medium — the cross-observation global outstanding-candidate budget is not atomic

`RecoveryClaimManager.maybe_submit()` reads all attempts and checks `max_outstanding_candidates` at lines 73-89, but the only atomic arbitration is the later mkdir under the specific `observation_key` at lines 108-127 (`fs_diloco/runtime/launch_outbox.py`). Two learners holding adjacent heartbeat observations can both read an empty global set, create different per-observation attempt directories, and both call qsub. The same-key eight-claimant test is safe because all contenders race on one directory, and the different-key assertion is only sequential (`tests/test_plan02_phase1_ha.py:1310-1361`); it does not cover this cross-key race.

I reproduced the defect without scheduler side effects by synchronizing two managers immediately after `_all_attempts()`: with `max_outstanding_candidates=1`, both returned `submitted`, the fake scheduler received two submissions, and two attempt directories remained. This violates HA-15 and the frozen global outstanding/reserved budget. Add a global filesystem reservation lock or equivalent atomic slot claim covering reconciliation, the budget decision, per-observation claim creation, and durable pending state; release it before the external qsub wait only after the pending attempt itself counts globally. Add a two-key concurrent RED test and receipt-missing variants.

### Medium — the PBS acceptance launcher loses accepted job IDs on partial qsub failure

The production Python launcher now retains a structured partial receipt, but the actual Phase 1 acceptance launcher still performs three qsubs in command substitutions under `set -e` and writes its artifact only after all three succeed (`scripts/miyabi/run_plan02_phase1_acceptance_launcher.pbs:51-85`). If successor submission fails, the accepted crash job ID is never persisted or printed; if learner submission fails, both accepted syncer IDs are lost from the structured record. The trap reports only a line number. This can leave exact live jobs requiring operator action without an auditable receipt, the same operational invariant that was already fixed in `fs_diloco/tools/launch_independent_run.py`.

Persist a pending launch artifact before the first qsub and atomically update it after each receipt. On partial failure, exit nonzero without automatic cancellation but preserve every accepted ID, failed command result, requested short walltime, run identity, and a `partial` status. Add shell-facing or factored Python tests for first/second/third submission failure. The successful final run is valid, but one success does not cover this launcher failure boundary.

### Medium — two frozen §11.1 matched-performance gates are absent from the evidence

The completed Checker measures renew, business, takeover and checkpoint-publish distributions, but it never evaluates the plan-required healthy-leader candidate observer against a no-candidate matched run, nor normal checkpoint publish p99 against a matched Plan 01 baseline (`plans/DOING/fsb_decoupled_diloco_plan_02.md:1120-1121`; `scripts/miyabi/check_plan02_phase1.py:272-325,569-581`). Its candidate `writer_transaction_attempt_count` merely counts `writer_lock_blocked` log events. That is not an instrumentation count of every attempted writer transaction, and the final takeover workload necessarily includes two successful candidate acquisitions. The report therefore labels 50 candidate log events and zero `writer_lock_blocked` events as zero writer-transaction attempts without isolating a healthy observer or a matched control.

Freeze explicit numerical regression ratios in tests, retain matched configuration/source identities and both sample sets, and make those results required Checker inputs for completed mode. Instrument candidate acquire transaction attempts directly so a healthy-observer run can prove zero rather than infer it from absence of one busy event. Until these required comparisons exist, the progress statement that every §11.1 gate passed is too broad.

### Low — parenthesized PRAGMA arguments bypass the write-PRAGMA filter

`_read_only_pragma()` rejects `=`, but deliberately discards everything after `(` before checking the allowlist (`fs_diloco/storage/fenced_store.py:48-54`). SQLite accepts setter syntax such as `PRAGMA journal_mode(WAL)`, `PRAGMA synchronous(OFF)`, `PRAGMA query_only(OFF)`, and `PRAGMA busy_timeout(1)`. I verified those forms change the connection/database setting in Python sqlite3. `_FencedConnection.execute()` consequently classifies them as read-only (`fs_diloco/storage/fenced_store.py:109-129`), and `ReadOnlySQLiteStore.execute()` applies the same parser. The latter remains file-read-only due to `mode=ro`, and the fenced proxy is not exposed as a public production method, so this is defense-in-depth rather than a demonstrated current mutator escape; it still contradicts the claimed disguised-write-PRAGMA regression.

Parse a PRAGMA as read-only only when it has no assignment and no parenthesized argument, except for specifically modeled introspection pragmas whose parentheses are part of a read-only query such as `table_info(name)`. Add both assignment and parenthesized setter cases to the SQL fence tests.

### Low — universal recursive discovery remains a deferred Phase 2 item

The shared `RunPaths` iterators now cover substantially more analysis, metrics, Checker and probe surfaces. Some plan wording still says every listed consumer has migrated, while remaining dynamic-instance discovery and validation is assigned to Phase 2 MEM-02/MEM-20. Static Phase 1 evidence is nonempty and the prior `deferred-with-justification` disposition remains reasonable; retain that explicit owner and do not represent the Phase 2 identity-aware migration as already complete.

## Correctness, concurrency, persistence, and regression assessment

Facts: leadership changes are serialized by `BEGIN IMMEDIATE`; business mutations bind an immutable token and recheck both local monotonic safety and exact DB owner after obtaining the writer lock; DB rows are authoritative; epoch/publication paths prevent overwrite; takeover resumes from the committed current row; terminal repair requires canonical stop and summary publications; lower stale torn directories cannot hide a current-or-higher malformed epoch; and old-epoch checkpoint deletion is ledgered and fenced per file. Source/config/dirty identity is checked before runtime import, and the final run descriptor confirms a clean target tree. The replacement focused/full tests, six-failpoint matrix, two-node writer-lock test and final 1+8 run provide strong evidence for those paths.

Inference: the implemented fencing and takeover protocol is likely sound under the tested crash and lock boundaries. The blocking problems are not contradictions in the observed successful run; they are falsifiability and concurrency holes that allow the completed gate or recovery controller to violate frozen contracts under untested counterexamples.

Regression risk: fixing the Checker and launcher can be isolated with negative tests and need not alter the persisted HA schema. Fixing the global reservation race changes the recovery submission concurrency protocol and therefore requires the complete relevant Phase 1 test group plus a fresh target review. Any new runtime instrumentation used for strict-zero counts must remain owner-scoped and bounded.

## Recommendations

1. Make every completed safety/performance field evidence-derived and blocking when absent; stop the renewer before its final snapshot and expand failure-event classification.
2. Implement an atomic global recovery reservation and add a synchronized two-observation race test.
3. Make the acceptance PBS launcher append-safe for partial receipts without canceling jobs automatically.
4. Run the two missing matched §11.1 experiments with evidence-based shortest practical qsub walltimes, and pass immutable result paths into the completed Checker.
5. Close the parenthesized-PRAGMA parser gap and retain the recursive-discovery Phase 2 follow-up.

## Final decision

**CHANGES_REQUIRED**

The target demonstrates a successful clean takeover workload, but Phase 1 cannot be marked complete while its Checker self-asserts strict-zero invariants, its recovery controller can exceed the global candidate budget, and its acceptance launcher can lose accepted scheduler receipts. The missing matched performance gates are also explicit Phase 1 acceptance criteria rather than optional follow-ups.
