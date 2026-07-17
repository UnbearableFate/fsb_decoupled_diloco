# B1 completion record

## 2026-07-17 19:20 JST

- Fix commit: `dab45e8` (`fragment_stop_requested` removed; fragment learner uses unified `stop_requested`).
- STP-03 RED/GREEN and STP-07 pipeline evidence are maintained under `reports/imp_plans/bug_fixing/S4/`.
- The one-node STP-07 run ended both learners at local step 10 while the configured local horizon was 12, after syncer published `stop_after_outer_steps`.
