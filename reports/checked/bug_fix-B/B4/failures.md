# B4 failures

## 2026-07-17 — FDR-04 fragment Checker attempt 1 (consecutive failure 1)

- Command on PBS `2404379.opbs`, node `mg0041`: `python scripts/miyabi/check_plan01_invariants.py --run-root reports/imp_plans/bug_fix-B/B4/artifacts/terminal-green --expected-learners 2 --expected-version 2 --require-complete`.
- Expected: PASS for the fragment terminal-drain run, whose summary is `input_exhausted`, final event 2, both learners stopped, zero active proposal rows, zero `gc_pending`, and zero proposal tensor files.
- Actual: `BLOCKED`. The checker assumes a full-mode `global_versions`/`weight_path`/`optim_path` layout and does not branch on `latest_kind=fragment`; this is the known FDR-04 checker coverage gap, not a runtime failure.
- Evidence: `artifacts/terminal-green/`; direct DB/filesystem inspection is recorded in progress after the checker is corrected.
- Next change: make the checker identify fragment latest, validate the current-only `fragment_versions` set and referenced fragment weight/optimizer files, use fragment update counts/history identities, and retain the same terminal/no-temp/metrics/overhead invariants. Rerun the identical command as the falsification test.
