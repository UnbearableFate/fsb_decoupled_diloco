# Independent Codex review — Plan 02 Phase 1 remediation target

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `b21b29f83067dd36b4ff5ac295fe1f894da64e21`
- Comparison base: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`
- Source identity: clean Git commit `b21b29f83067dd36b4ff5ac295fe1f894da64e21`; cumulative diff `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5..b21b29f83067dd36b4ff5ac295fe1f894da64e21`
- Related runtime evidence: the clean 1+8 acceptance used source fingerprint `sha256:5b5bac2807e9b1caa976e72bc8824721d1703d81e609b5368eacdc65a00e097f`; the post-review source remediation smoke used `sha256:b6663b446b8a76ab763d9d7db42dcdb3864d8846b5c5a5b97a2218f88022352d` before the remediation commit was created.

## Scope and method

I reviewed the complete cumulative Phase 1 diff, including configuration validation and immutable run identity, schema/bootstrap boundaries, lease acquisition and renewal, fenced business mutations, epoch-scoped checkpoint/control publication, the filesystem-only learner reader and watchdog, PBS recovery reconciliation, independent launch receipts, syncer startup and terminal repair, maintenance/GC, Checkers, PBS launchers, focused tests, retained experiments, requirement records, and synchronized documentation. I started from the frozen commits and did not read the Claude report or any Claude substantive conclusion before saving this report.

The prior target's receipt-loss finding is fixed by per-command structured receipts and a partial-submission return that preserves the accepted syncer job ID. The canonical observation finding is fixed by stateful epoch/owner/latest/terminal watermarks, repair-window counters, lower-epoch rejection, and deterministic watchdog tests. The authority-temp race found by the first remediation smoke is also fixed with the fenced-store grace and a boundary regression. The replacement PBS suite reports 39 focused and 418 total tests passing, while the 20-second smoke finishes in 12 seconds with terminal generation 2, a released leader, both terminal publications, and no runtime error event.

No Critical or High finding was identified. One Medium finding remains in the attempted startup-cleanup remediation, and the previously dispositioned Low discovery finding remains a Phase 2 follow-up.

## Findings

### Medium — the post-acquire cleanup guard still ends before all fallible pre-main-loop work

The new guard correctly owns failures through `LeaseRenewalThread.start()` (`fs_diloco/runtime/syncer.py:2599-2655`), but it ends immediately afterward. The started renewer, bound store, exact leader token, and lease connection remain live while `logger.event("process_start", ...)` executes and while `store.terminal_state()` is queried (`fs_diloco/runtime/syncer.py:2665-2688`). Neither call is inside that guard or the later initialization cleanup block, which starts only at line 2735. `JsonlLogger.event()` opens, flushes, and fsyncs its path (`fs_diloco/observability/logging_utils.py:23-38`), so ordinary I/O failures can occur in this window. SQLite observation can also fail. Either exception escapes `run_syncer()` without stopping the renewal thread, closing the store, releasing the exact token, or closing the lease connection.

This is the same ownership invariant as the prior Medium finding, not a theoretical unrelated edge. It is now more dangerous than a passive lease delay because `lease_renewer.start()` has succeeded; although heartbeat publication is not enabled yet, the daemon renews the leader row and can keep the abandoned token active after the main function unwinds. The two new tests inject store-open and renewer-start failures (`tests/test_plan02_phase1_ha.py:200-311`) but do not inject failure after the renewer has successfully started.

Use one outer HA ownership guard or a single idempotent cleanup helper from successful `acquire_candidate()` until ownership is deliberately transferred to the main-loop/finalization guard. It must cover initial logging, `terminal_state()`, terminal-repair dispatch, and initialization entry without double-close/release. Add a RED regression that uses a successfully started fake renewer, makes the initial log write or terminal-state read fail, and asserts renewer stop, bound-store close, exact-token release, and lease close while preserving the original error.

### Low — shared recursive discovery remains only partially adopted

The cumulative Phase 1 contract still says every listed discovery consumer uses shared `RunPaths` recursive iterators, while liveness, analysis, metrics, maintenance, and the Checker retain several consumer-owned globs. This was already identified and dispositioned as `deferred-with-justification` to Phase 2 MEM-02/MEM-20, where dynamic instance admission and path validation are introduced together. Static Phase 1 evidence is nonempty and valid, so this Low issue does not independently block Phase 1 after its recorded follow-up is retained.

## Correctness, risk, and acceptance assessment

The central HA persistence design remains coherent: `BEGIN IMMEDIATE` serializes leadership changes; bound mutators recheck exact epoch/owner and local lease safety inside each business transaction; committed DB rows are authoritative; checkpoint and canonical-control paths are immutable and epoch/publication scoped; takeover resumes from the committed row; and terminal completion requires the matching generation's stop and summary publications. The crash matrix, two-node lock probe, clean 1+8 takeover run, completed Checker, and remediation smoke provide mutually consistent evidence for those paths.

The new learner watermarks prevent lower-epoch/latest/terminal rollback without opening SQLite. Heartbeat progress is distinct from merge progress, the frozen recovery budget supersedes the old 600-second watchdog, and canonical gaps increment bounded-window telemetry without accepting stale control. The launcher now exposes partial qsub state without performing unauthorized cancellation. Authority temp GC now respects the period in which the renewal writer may still own an atomic temp.

Regression coverage is strong for steady state, fencing, takeover, publication repair, GC, claim reconciliation, source pinning, and the three previously reported behavior defects. The uncovered post-renewer/pre-main cleanup window is a concurrency and resource-ownership defect that the successful smoke cannot falsify because the smoke does not inject logger or SQLite observation failure at that boundary.

## Recommendations

1. Extend the ownership guard across every fallible operation after acquisition, using one idempotent cleanup path rather than another narrow nested block.
2. Add the after-successful-renewer RED test and rerun the focused Phase 1 file plus the complete suite with the shortest practical PBS walltime based on the observed 31-second job.
3. A runtime smoke is unnecessary if the fix only restructures exception ownership and the injected regression proves exact cleanup; run it only if normal startup/finalization flow changes.
4. Keep the recursive-discovery item assigned to Phase 2 and narrow any Phase 1 evidence wording that implies the migration is already universal.

## Final decision

**CHANGES_REQUIRED**

The receipt, canonical-observation, and authority-temp findings are satisfactorily addressed, but Phase 1 cannot close while a successfully running renewal thread can escape cleanup on a fallible initial log or terminal-state operation immediately after leadership acquisition.
