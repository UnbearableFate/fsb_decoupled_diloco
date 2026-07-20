# S4 implementation progress

## 2026-07-17 19:20 JST — S4-L1–L3 / STP-01–STP-07

- Baseline implementation commit: `19414a1`; S4 code and synchronized documentation commit: `dab45e8`.
- RED: `.venv/bin/pytest -q tests/test_learner_completion.py` produced exactly the B1 failure for `local_or_global`, `max_local_steps=5000`, `local_step=100`, and an existing stop file; the other eight assertions passed. Evidence: `artifacts/20260717-stp-red.log` (STP-03).
- Implementation: removed `fragment_stop_requested`; both fragment loop checks now call the existing `stop_requested`, preserving the full truth table and making `stop.json` authoritative before the local horizon.
- Targeted tests: `.venv/bin/pytest -q tests/test_learner_completion.py` → 9 passed (`artifacts/20260717-stp-green.log`), covering STP-01–STP-05. Static `rg` found zero `fragment_stop_requested` occurrences under `fs_diloco/` (STP-06).
- Pipeline: `NUM_LEARNERS=2 ... scripts/local/run_tiny_2proc_smoke.sh` with `configs/fs_diloco_tiny_fragment_local.yaml` completed on one compute node. `stop.json` and `summary.json` report `stop_after_outer_steps`, version 4, `all_learners_stopped=true`; both learner logs contain `stop_seen` and exit at `local_step=10`, below `max_local_steps=12` (STP-07). Raw evidence: `artifacts/fragment_stop/run` and `artifacts/fragment_stop.stdout.log`.
- Unchanged full path: current tiny full learner traces match the baseline with profile `learner-adoption-v1` and `--role learner`: `artifacts/20260717-full_learner_trace.txt`.
- Full regression: `.venv/bin/pytest -q` → 135 passed in 39.99s (`artifacts/20260717-full_pytest.log`). `.venv/bin/ruff check fs_diloco tests` and `git diff --check` passed.
- Runtime environment: PBS allocation `2403932.opbs`, one Miyabi-G compute node `mg0007`, default modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Remaining risk/non-goal: `global_only` still intentionally depends on syncer publication of `stop.json`; syncer-death watchdog behavior remains B7 and was not changed.
