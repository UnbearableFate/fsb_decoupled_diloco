# Codex independent incremental review — P5-delete-classic-refactor

- Reviewer: `gpt-5.6-sol`
- Base commit: `d2dbfed19eb5e9e0835167c13da40a80bc15273a`
- Target commit: `eb56219e13817b1f659921ea093c2dfdfa473abd`
- Review range: `git diff d2dbfed19eb5e9e0835167c13da40a80bc15273a eb56219e13817b1f659921ea093c2dfdfa473abd`
- Decision: **CHANGES_REQUIRED**

## Scope and independent evidence

Reviewed the complete incremental diff, with particular attention to the new dynamic-capacity, PBS reconciliation, merge, terminal, manual-close, legacy output-guard, authority/schema, Checker, config, test, and documentation paths. This report was saved before running or reading a fresh Claude review of this target.

The target itself was also tested from a clean detached worktree on Miyabi compute node `mg0006`, PBS job `2511495.opbs`: Ruff lint and the 45-file format scope passed, P3/current-boundary/P5 Checker passed, focused tests were `434 passed in 39.64s`, and the full suite was `605 passed in 39.56s`. The findings below are gaps in the exercised contracts rather than failures in that run.

## High findings

### H-01 — Receipt-loss recovery invokes a `qstat` form that Miyabi rejects

`PBSScheduler.find_by_launch_request()` builds `qstat [-H] -f` without a job ID (`fs_diloco/runtime/pbs_scheduler.py:218-237`). On the actual Miyabi PBS client this returns nonzero with `SIM4550: Job ID is required with -f option`; it never returns the full job records parsed by this method. The new unit test hard-codes the invalid command as its expected success path (`tests/runtime/test_pbs_scheduler.py:14-40`), so the mock masks the production incompatibility.

When `qsub` creates a learner but its reply is lost, the service therefore cannot rediscover the job by `FS_DILOCO_LAUNCH_REQUEST_ID`. The launch remains without `pbs_job_id`; the real learner cannot pass exact scheduler-bound admission and the reservation eventually needs manual intervention. This breaks the advertised no-resubmit, automatic receipt-loss reconciliation path required by `SCHED-01..04`.

Required fix: enumerate the caller's visible live/history job IDs using a PBS-supported listing command, then issue `qstat [-H] -f <job IDs>` (in bounded chunks if needed) and match the exact variable. Add a subprocess-contract test that models the listing and per-ID/full-detail calls, plus a Miyabi read-only command-shape probe; never treat a nonzero listing/detail call as “no record.”

### H-02 — Terminal bounds are process-local and can be reset by leader failover

`TerminalService.finalize()` creates fresh monotonic ack and proposal deadlines on every invocation (`terminal.py:140-159`) and runs `range(max_terminal_merges)` from zero every time (`:160-168`). The authority persists neither the absolute drain/visibility deadlines nor how many terminal merges have already been consumed. Consequently:

- every successor receives a fresh full `drain_ack_timeout_seconds` and `proposal_visibility_grace_seconds`;
- if a predecessor commits the one permitted terminal merge and crashes before finalization, a successor may commit a second terminal merge;
- repeated failover can make a nominally bounded drain unbounded.

This violates `TERM-01..03` and the explicit `max_terminal_merges <= 1` contract. The current tests cover 0/1 calls only within one service instance and have no successor-after-terminal-merge case.

Required fix: persist close-generation timing and terminal-merge budget consumption in fenced authority state. A successor must compute remaining waits and remaining merge allowance from that durable state. Add crash-prefix tests before/after acknowledgement, proposal visibility, terminal commit, and finalization, including a predecessor that already consumed the only terminal merge.

### H-03 — The pre-close admission cutoff is not durable before the visibility wait

For `allow_preclose_admission_during_drain`, `finalize()` records `close_intent_at` only in memory, waits through the registration visibility grace, and calls `begin_terminal_close()` afterward (`terminal.py:116-138`). If the leader dies during that grace, authority still says `open`; a successor chooses a later cutoff and may admit registrations created after the original close intent. There is no durable state that distinguishes “ordinary open” from “pre-close visibility scan in progress.”

This defeats the frozen-cutoff rule under exactly the failover scenario the terminal service is meant to make safe. The existing test proves one in-process cutoff is reused, but not that the cutoff survives successor takeover.

Required fix: persist a fenced pre-close intent containing reason, immutable wall-clock cutoff, and visibility deadline before scanning. Capacity planning must stop in that state, while admission is limited to requests whose immutable `created_at` is at or before the stored cutoff. Successors must resume the same intent/deadline. Add takeover tests during the visibility grace and reject a request created just after the original cutoff.

## Medium findings

### M-01 — Ack/proposal loops can overshoot their configured deadline by a full scan interval

The registration-grace loop sleeps `min(remaining, scan_interval)`, but the ack and proposal loops sleep the full scan interval (`terminal.py:149,159`). If `scan_interval_seconds` exceeds the remaining timeout, wall duration exceeds the configured bound. Use the same remaining-time sleep rule for all bounded loops and test `scan_interval > timeout` with a virtual clock.

### M-02 — CSV legacy immutability trusts a caller-controlled manifest label

`results_to_csv()` enforces the outside-root rule only when the manifest already contains `source_protocol == "legacy-v1-v3"` (`eval_lm_harness.py:300-313`). An older manifest, an independently produced manifest, or a modified manifest with that field absent can still name a legacy `source_run_root` and write the CSV inside it. Other updated paths classify the authority directly; this one should do the same and reject a mismatching supplied label. Add missing-label and forged-current-label regressions.

## Verified remediations from the preceding review

The target correctly fixes the previous frozen-target test failure, P3/P4/current-boundary Checker breakage, broad legacy config downgrade, P5 requirement owners, duplicate adoption validation, dead candidate outbox/phase1 performance surface, URI escaping/timeout, literal DDL test weakness, documentation drift, and the absence of `runtime/services/`. Legacy model/evaluation outputs are now guarded at their principal mutation boundaries. Dynamic launch/admission identity is exact and reservations remain durable through uncertainty. Those improvements do not close the failover and real-PBS gaps above.
