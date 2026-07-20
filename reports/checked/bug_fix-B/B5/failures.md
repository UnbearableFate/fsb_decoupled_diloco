# B5 failures

## 2026-07-17 — B5 tiny Checker attempt 1 (consecutive failure 1)

- Command: `python scripts/miyabi/check_plan01_invariants.py --run-root reports/imp_plans/bug_fix-B/B5/artifacts/tiny_run --expected-learners 1 --expected-version 2 --require-complete` on PBS `2404379.opbs`, `mg0041`.
- Expected: PASS for the completed tiny run.
- Actual: `BLOCKED` (`artifacts/checker_tiny.txt`). Direct checker invocation exposed `RuntimeError: expected version not reached`.
- Confirmed cause: the config's original two-version target assumes two learners, but the validation launcher intentionally overrode it to one learner. That learner reached its local horizon and closed input after one applied merge, so the authoritative summary is a clean `input_exhausted`, final version 1, all learners stopped. This is an incorrect checker expectation, not a runtime or gc_pending defect.
- Next falsification: rerun the same checker against authoritative final version 1 and require complete; additionally inspect terminal `gc_pending=0` and the new syncer metric columns.
