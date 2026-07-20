# S1 failures

## 2026-07-17 19:33 JST — STR-06 trace attempt 1

- Consecutive failure count: 1.
- Command: `.venv/bin/python -m fs_diloco.tools.compare_event_traces reports/imp_plans/bug_fixing/S1/artifacts/baseline_rebase/run reports/imp_plans/bug_fixing/S1/artifacts/current_rebase/run --profile learner-adoption`.
- Environment: baseline commit `67b6da2`; current dirty S1 implementation; PBS job `2403932.opbs`, node `mg0007`, one learner, `configs/fs_diloco_tiny_rebase_local.yaml`.
- Expected: whole per-actor normalized trace equality.
- Actual: first difference at learner index 22. The baseline observed no latest after the step-8 publication and saved an anchor; the current run observed version 1 at that same poll and directly adopted it. Raw comparator output: `artifacts/str06_rebase_trace.txt`.
- Confirmed cause: this is an allowed asynchronous scheduling branch, not evidence of a strategy-code mismatch. The plan audit had already established this class of nondeterminism for full/fragment syncer traces, but S1's rebase acceptance row had not yet applied that rule. The fixed-seed learner cannot control whether syncer publishes between proposal write and the immediate latest poll.
- Next change: revise STR-06 to use a scripted integration trace that controls the latest-read sequence, retain tiny rebase smoke plus terminal invariants, and do not weaken the general profile to discard rebase-specific events.

## 2026-07-17 19:35 JST — full tiny Checker attempt 1

- Consecutive failure count: 1 for the Checker invocation.
- Command: `check_plan01_invariants.py --expected-learners 2 --expected-version 2 --require-complete` on `artifacts/current_full/run`.
- Expected: PASS at the configured `stop_after_outer_steps=2` target.
- Actual: BLOCKED; the run correctly terminated at version 1 with `stop_reason=input_exhausted` after both `local_or_global` learners reached step 8. `summary.json`, stop, DB, and history all agree on version 1.
- Confirmed cause: the original S1 plan assumed the existing tiny full config deterministically reaches its configured global target. It does not; plan 01's terminal-drain semantics explicitly permit an earlier, internally consistent `input_exhausted` result.
- Next attempt: invoke the invariant checker against the run's actual authoritative final version (1), and record configured-target attainment as outside the behavior-preserving refactor gate.
