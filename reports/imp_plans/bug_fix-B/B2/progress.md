# B2 implementation progress

## 2026-07-17 — L0 specification audit and baseline

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; the worktree was already dirty in three 5000-step configs and `tests/test_config.py`, and those user changes are preserved.
- The plan audit found and corrected four specification gaps before runtime code changes: the `LambdaLR` construction off-by-one, the exact warmup/cosine boundary formula, migration values for cosine configs whose old `max_local_steps` was null, and the optional WSD loop that otherwise looked delivered without being a completion requirement.
- The frozen schedule uses completed cumulative optimizer steps. The next step after a rebuild at `local_step=N` must use exactly the same LR as an uninterrupted scheduler after N completed steps. Existing runs and post-fix runs are not controlled quality comparisons because the LR trajectory changes globally.
- SCH-01–05/07 RED tests were added in `tests/test_inner_scheduler.py`; compute-node RED output is archived before implementation.

## 2026-07-17 — L1/L2 scheduler core and configuration, SCH-01–05/07

- Implemented a cumulative-step cosine multiplier with an explicit warmup/cosine boundary, positive `min_lr_ratio`, and exact phase restoration when optimizer/scheduler state is rebuilt. All initial/rebuild call sites now pass completed `local_step`; scheduler math has no dependency on `training.max_local_steps`.
- Added fail-closed config validation, changed the empty-config scheduler default to `none`, and migrated every explicit cosine repository YAML to a literal independent horizon. The null-local-horizon migrations are 2000 for the 8l baseline, 500 for the 50x10 variants, and 200 for the 50x4 variant.
- Added LR/horizon observability to `inner_step_summary`, learner cycle metrics, optimizer reset events, and learner heartbeats for full and fragment paths.
- Related compute-node group passed: `pytest -q tests/test_inner_scheduler.py tests/test_shared_runtime_primitives.py tests/test_learner_rebase.py tests/test_config.py` → 103 passed in 3.10s. Artifact: `artifacts/20260717-sch-green-attempt2.log`; environment PBS `2404379.opbs` on `mg0041`, Python 3.13.13, PyTorch 2.13.0+cu132.
- Covered SCH-01/02/03/04/05/07 and the unit-level phase restoration needed by SCH-08. Remaining B2 gates: real tiny replace/rebase/fragment traces, full pytest, static lint/diff, resolved-config and final-heartbeat inspection.

## 2026-07-17 — L3 pipeline and B2 closeout, SCH-06/08

- Added dedicated one-learner scheduler pipeline configs for replace and fragment and reused the existing rebase tiny config. All three runs exited 0 under PBS `2404379.opbs` on `mg0041`.
- Replace: final v4, 34 local steps, reset phases 0/12/18/26/32. Fragment: final global event 4, 9 local steps, adoption/reset phases 2/4/6/8. Rebase: final v3, 24 local steps with preserved scheduler state on reconcile. In every run the LR sequence after warmup was monotonic non-increasing, every rebuild's optimizer LR equaled the following step's recorded LR, horizon was present in resolved config/step events/final heartbeat, and every observed LR was positive. Assertion output: `artifacts/20260717-sch-pipeline-assertions.txt`; raw runs/logs are under `artifacts/{replace_run,fragment_run,rebase_run}`.
- Full compute-node suite: `pytest -q` → 207 passed in 43.00s (`artifacts/20260717-full-pytest.log`). Static `.venv/bin/ruff check fs_diloco tests`, `git diff --check`, explicit-cosine YAML horizon audit, and scheduler/max-local-step coupling search passed. Full-mode invariant checker returned PASS for replace v4 and rebase v3 (`artifacts/checker_{replace,rebase}.txt`).
- Documentation, review status, run-analysis confound warning, and the superseded TODO pointer were synchronized. No 9-node experiment is required by B2, and no historical quality comparison is claimed. No commit was created because the starting worktree contained unrelated user edits; the implementation is identified by this worktree plus baseline `c359b8322c33e0101328c2fc8522271691f1e52c`.
- B2 completion predicate is satisfied: SCH-01–08 are covered (SCH-07 is the intentional WSD rejection boundary), all runtime paths use cumulative phase, and all repository cosine configs have explicit independent horizons.
