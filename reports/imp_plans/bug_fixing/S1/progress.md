# S1 implementation progress

## 2026-07-17 19:44 JST — STR-01–STR-09

- Baseline commit: `67b6da2`. Strategy state machines and isolated unit tests: `4ce7262`. Learner integration: `f2c6961`.
- STR-01/02: `GlobalAdoptionStrategy`, replace/rebase/predict implementations, a single factory, typed `StrategyAction`, and one runner finalization path now own adoption/reset/preserve behavior.
- STR-03/06: the rebase strategy owns its anchor, carried-token count, and update ID. The controlled `test_rebase_strategy_owns_anchor_tokens_and_clears_after_adoption` trace asserts `latest_polled → local_rebase_anchor_saved → global_rebased`, state clearing, carried tokens, and preserve semantics. The real tiny rebase run completed and its plan-01 checker passed at authoritative version 3.
- STR-04/09: prediction creation, reconciliation, timeout, and abandon-on-stop live in the prediction strategy. S2's explicit reconcile helper is reused; tests assert state clearing, timeout, stop return value, and unchanged event payloads.
- STR-05/07: normalized per-actor tiny projections match baseline for replace (`artifacts/str05_full_trace.txt`) and predict (`artifacts/str07_predict_trace.txt`).
- The first raw rebase whole-run comparison differed only in the legal timing of publish-followed-by-immediate-poll; it is retained in `artifacts/str06_rebase_trace.txt` and documented in `failures.md`. The acceptance plan now correctly uses a controlled latest-read sequence plus real-run terminal invariants for this path rather than weakening the comparator profile.
- STR-08: the seven legacy strategy-state names and two `*_enabled` booleans have zero occurrences in `run_learner`; strategy state is private to its object.
- Targeted regression: 35 passed in 2.85s (`artifacts/20260717-targeted_final.log`). Full regression: 143 passed in 40.71s (`artifacts/20260717-full_pytest.log`).
- All three current tiny runs exited normally. `check_plan01_invariants.py` returned PASS for replace at its authoritative input-exhausted version 1, rebase at version 3, and predict at version 3 (`artifacts/checker_*`). Requiring the replace run to reach its configured global target was corrected because `local_or_global` permits an earlier, internally consistent `input_exhausted` terminal state.
- Static checks: `.venv/bin/ruff check fs_diloco tests`, `git diff --check`, and the STR-08 search passed. Runtime validation used PBS allocation `2403932.opbs` on one Miyabi-G compute node `mg0007`.

## Review boundary

The strategy state machines and their isolated tests are separate from runner integration so either layer can be reviewed and reverted without manufacturing invalid per-strategy intermediate runner states. On-disk protocol, update metadata, and checkpoint layout are unchanged.
