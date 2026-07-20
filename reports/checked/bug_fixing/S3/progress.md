# S3 implementation progress

## 2026-07-17 20:03 JST — L0/L1, LDU-01/02/06

- Baseline commit: `1fedd4a`; implementation commit: `777e913`.
- Difference inventory — full candidates use `(current global version, max staleness)` and `updates/drop_updates`; fragment candidates add `(fragment id, current fragment version)` and use `fragment_updates/drop_fragment_updates`. Missing-file event names and grace event context fields differ accordingly. Selection policy, adaptive deadline, liveness refresh, quorum termination, and sleep behavior were identical. No unintended semantic divergence was found.
- `UpdateProposalSource` now contains the enumerator, degradation callback, missing-file event, and context fields. `collect_with_grace_window` and `drop_missing_update_files` are the only skeletons for both modes; the two fragment-specific identifiers have zero source occurrences (LDU-06).
- Controlled full/fragment source tests cover zero-second grace expiry, selection, table-specific missing-file degradation, event names, and context fields. The unchanged RED command now passes as part of 20 targeted shared-runtime tests (`artifacts/20260717-ldu-green.log`).

## 2026-07-17 20:04 JST — L2/L3, LDU-03/07

- `all_expected_learners_stopped` remained the already-shared implementation. Six exact-set cases now cover no rows, missing learner, active, dead, exactly stopped, and unexpected extra stopped learner; only the exact expected set returns true. Fragment main-loop input-closed wiring remains intentionally absent and is specified separately in `plans/followups/B4-fragment-terminal-drain.md`.
- Adoption inventory — inner poll and post-upload use `fragments_adopted`, reset changed-fragment tokens, conditionally reset optimizer/scheduler, and include all fragment versions. Final wait resets tokens but neither optimizer nor version field; final latest does none of those three. All contexts increment the adoption count and update last-adopted fragments.
- `apply_fragment_adoption` plus one runner wrapper now implement the shared transition; four parameterized contexts assert exact events, token state, count, last-adopted list, optimizer identity, and version-field presence (LDU-03).
- Targeted final regression: 26 passed in 1.49s (`artifacts/20260717-ldu-targeted-final.log`). Full regression: 156 passed in 41.34s (`artifacts/20260717-full_pytest.log`).

## 2026-07-17 20:06 JST — L4, LDU-04/05

- Baseline/current/repeat tiny full and fragment runs all exited 0 on one Miyabi-G compute node. Generic whole-run comparisons were correctly rejected as a hard gate after exposing latest-wins timing variance; the failure and fragment-ID normalizer gap are retained in `failures.md`.
- The deterministic syncer publication projections were proven repeatable current-to-current and equivalent baseline-to-current for both modes: `artifacts/ldu04_{publication_trace,repeatability}.txt` and `artifacts/ldu05_{publication_trace,repeatability}.txt`. Controlled LDU-01–03 tests remain authoritative for selection and learner adoption details.
- Full current run: authoritative version 1, `input_exhausted`, all learners stopped, full plan-01 checker PASS (`artifacts/checker_current_full.txt`). Fragment current/repeat: version 4, `stop_after_outer_steps`, all learners stopped; fragment DB versions equal latest, no pending/selected fragment updates, and no error/no-progress event.
- PBS job `2404224.opbs` ran `run_2node_fragment_debug.pbs` on `mg0879` + `mg1035`: exit 0, walltime 45s, four committed fragment merges, selected count 1 each, DB integrity `ok`, DB/latest/summary final event 4, zero active fragment proposals, all learners stopped, and no error/no-progress event. Evidence: `artifacts/2node_fragment/run` and `artifacts/2node_fragment/pbs.stdout.log`.
- Before submission, `bash -n scripts/miyabi/*.pbs` passed and every PBS file contained literal `group_list=xg24i002`.
- Static closeout: `.venv/bin/ruff check fs_diloco tests`, `git diff --check`, and LDU-06 passed. On-disk protocol, config, and fragment drain behavior did not change.
