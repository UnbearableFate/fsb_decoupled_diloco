# Independent Codex review — Plan 02 Phase 1 completion candidate

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Review target: `6042886f1a1ec55759cc01c1af230ab82c0f9ebe`
- Comparison base: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5`
- Relevant cumulative diff: `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5..6042886f1a1ec55759cc01c1af230ab82c0f9ebe`
- Branch: `codex/fsb_decoupled_diloco_plan_02`
- Formal acceptance source identity: clean commit `24e181bc8031e68ae32310c628d56422b3d7654b`, source fingerprint `sha256:5b5bac2807e9b1caa976e72bc8824721d1703d81e609b5368eacdc65a00e097f`. The later review-target commit changes retained evidence and documentation, not the fingerprinted runtime source.

## Scope and method

I reviewed the complete Phase 1 cumulative diff, including configuration and bootstrap identity gates, lease acquisition/renewal/release, fenced storage, schema migration boundaries, epoch-scoped checkpoint and control publication, the filesystem-only learner reader and watchdog, recovery claim/PBS reconciliation, maintenance and GC, syncer startup/resume/finalization, independent launch scripts, the Phase 1 Checker, focused tests, requirement records, and retained PBS evidence. I did not read another review report before writing this report.

The retained final evidence is internally consistent: the focused suite reports 34 passing tests, the full suite reports 413 passing tests, the six-failpoint matrix passes 60/60 cases, the two-node lock boundary passes, and the independent 1+8-job run completes 10 merges through a real successor. The completed Checker artifact has SHA-256 `a49fd10e97f0d2b22efdd9111738d5e7eb3ddeb7332d9b9ea564d0172424dcba`, status `PASS`, zero scanned failure events, one retained epoch directory, and a released generation-2 terminal. Static `git diff --check` and `bash -n scripts/miyabi/*.pbs` also pass at the review target.

No `Critical` or `High` finding was identified. Three `Medium` findings and one `Low` finding follow.

## Findings

### Medium — partial independent-job submission loses the already-created syncer receipt

`launch_independent_run.launch()` submits the syncer and learner array sequentially, assigning both IDs only after each `_qsub()` returns (`fs_diloco/tools/launch_independent_run.py:101-109`). `_qsub()` uses `check=True`, so a learner-array rejection raises immediately (`:33-38`). In that path the syncer has already been accepted by PBS, but the CLI emits no structured result containing its job ID and writes no durable launch receipt. The operator is left with an active or queued syncer whose identity is absent from the launcher output, while the immutable run root already exists.

This is a real operational failure window in the required independent-job launcher. It does not create two writers, but it can orphan compute, delay diagnosis, and make a retry submit an unnecessary second syncer candidate. The tests cover missing/invalid walltime before initialization but do not inject failure on the second qsub.

Fix by recording each submission result as it happens and catching a later qsub failure so the command emits or durably stores the successful syncer job ID together with the failed command/error. Do not auto-`qdel`; preserve the plan's operator-only cancellation rule. Add a regression in which the first qsub succeeds and the second fails.

### Medium — failures between lease acquisition and the main cleanup guard leave an active lease and open resources

The HA startup sequence acquires leadership at `fs_diloco/runtime/syncer.py:2598-2602`, then opens the leader store, constructs the epoch logger and renewal thread, and starts the thread at `:2603-2624`. The cleanup guard that stops the renewer, closes the store, releases the token, and closes the lease store does not begin until the initialization/resume block at `:2703-2750`.

Therefore an exception from `open_leader_store`, logger construction, `LeaseRenewalThread` construction, or `LeaseRenewalThread.start()` escapes without releasing the just-acquired lease or closing every resource. The most harmful variant is a `start()` timeout: the daemon may subsequently initialize and keep renewing while the main process unwinds. Other variants leave the lease active until expiry, delaying takeover by at least the configured lease window. There is no startup failpoint test at these boundaries.

Put acquisition and all post-acquire setup under one ownership/cleanup guard. Cleanup must tolerate partially initialized objects, stop any started renewer, close the bound store, release the current token when still valid, and close the lease connection without masking the original exception. Make renewal-thread startup cancellation explicit so a timed-out thread cannot begin renewing later. Add failure-injection tests after acquire, after store open, and during renewal-thread startup.

### Medium — the required canonical-repair watchdog state and its RED tests are absent

Plan §6.5 requires learners to retain monotonic epoch/control watermarks and, when a higher epoch lacks canonical head past `canonical_repair_wait_seconds`, increment `cache_rejected_lower_epoch_count` / `canonical_repair_wait_count`, rescan, and continue recovery without accepting lower-epoch control. The requirement matrix marks HA-09 as a completion candidate and cites the Phase 1 test/checker artifacts.

The implementation has no occurrence of either required counter. `EpochControlReader` is stateless (`fs_diloco/protocol/control_epoch.py:236-242`) and recomputes the current filesystem epoch on every call (`:323-364`). `confirm_syncer_unresponsive()` observes only heartbeat progress and applies `learner_recovery_wait_seconds` (`fs_diloco/runtime/learner.py:742-774`); `canonical_repair_wait_seconds` is never consumed by learner runtime. If a higher epoch has a valid heartbeat but no canonical head, `read_current_latest()` simply returns `None` (`control_epoch.py:380-383`), with no repair-window telemetry or recovery transition. A fresh learner can consequently wait until its broad startup timeout while the specific canonical-repair window is invisible.

The focused test file has no HA watchdog test covering the four scenarios required by §7.2: heartbeat renewal without merges, a queued recovery candidate beyond the old watchdog limit, lower-epoch stop rejection, and canonical repair timeout without false exit. The legacy watchdog tests exercise only fixed `latest.json` behavior.

Implement explicit learner-side control observation state with monotonic epoch, heartbeat, latest-version and terminal-generation watermarks; consume `canonical_repair_wait_seconds`; emit the two required counters/events; and add deterministic tests for all four WATCHDOG cases. The reader must never adopt a lower epoch after a higher valid epoch has been observed.

### Low — the shared recursive discovery contract is only partially adopted

Phase 1 §6.4 and HA-05 require liveness, analysis, metrics, maintenance, and Checker discovery to use shared `RunPaths` iterators and to prove expected surfaces nonempty. `RunPaths` adds the requested iterators, and the Checker uses several of them, but production and analysis code still owns fixed globs: `protocol/liveness.py:68,95`, `tools/analysis.py:246,498`, and `tools/run_metrics_csv.py:300`. Maintenance and the Checker also retain their own epoch/claim directory globs (`storage/maintenance.py:72`; `scripts/miyabi/check_plan02_phase1.py:212,218`).

Static Phase 1 paths happen to match these globs, so the retained acceptance run is valid, but the matrix's broader statement that maintenance, Checker, probe, liveness, analysis, and metrics all exercise shared recursive discovery is not yet true. Route the intended discovery surfaces through `RunPaths` now or narrow the Phase 1 claim and leave explicitly dynamic-only discovery work in Phase 2; add nonempty assertions at each consumer boundary.

## Correctness and evidence assessment

The central fencing design is sound in the reviewed paths. Lease acquisition is serialized by `BEGIN IMMEDIATE`; every HA business mutation is bound to an exact epoch/owner token and rechecks local monotonic safety plus the current DB lease inside the write transaction. Checkpoint targets are immutable and epoch/publication scoped. DB commit is authoritative, and a successor reconstructs canonical latest control from the committed row. GC registers candidates and rechecks fencing and references before deleting paths that cannot be reused by a later epoch.

The terminal repair protocol now closes the important completed-state gap: the early stop generation lets learners drain, while a higher generation records both stop and summary after maintenance. Candidate rejection validates both publication rows, exact paths, owner/epoch, and file digests; incomplete completion can be acquired and repaired. Unit, crash-matrix, smoke, and formal independent-job evidence all agree on this behavior.

Source/config/run mismatch actors fail before business writes, HA learners do not open SQLite, and incomplete/pre-HA/fragment HA databases fail closed. Legacy full and fragment regressions pass in the complete suite. Scheduler reconciliation preserves outstanding queued/running/uncertain jobs and bounds attempts without granting authority through qsub.

The findings above concern startup error ownership, auditable launch failure, and an explicitly required watchdog/control-observation slice that the current green suite does not exercise. They are not contradicted by the successful steady-state and takeover evidence.

## Recommendations

1. Fix the two concrete error-handling windows and add failure-injection regressions before rerunning the full suite.
2. Complete HA-09's learner observation state and deterministic WATCHDOG matrix, then update the Checker/evidence so the claimed requirement is independently falsifiable.
3. Reconcile HA-05's iterator claim with the actual consumers.
4. Rerun only the shortest evidence affected by these changes first; use observed runtimes plus a small safety margin for every explicit PBS walltime. Repeat the 1+8 completion run only if runtime/control behavior changes.

## Final decision

**CHANGES_REQUIRED**

The core HA fencing, takeover, checkpoint, GC, and completed-terminal protocols have strong evidence, but Phase 1 cannot close while an acquired leadership token can escape startup cleanup and the plan's canonical-repair/watchdog contract remains unimplemented and untested.
