# S5 failures

## 2026-07-17 20:13 JST — CFG RED attempt 1 (harness interruption)

- Consecutive failure count: 1 for the invocation harness; no configuration assertions ran.
- Command: `module list` followed by `.venv/bin/pytest -q tests/test_config.py` in interactive PBS job `2404248.opbs`, node `mg0039`.
- Expected: CFG-01/02/03/04 tests fail against the pre-migration config implementation.
- Actual: `module list` opened a terminal pager and consumed the leading `.ve` from the next buffered command; shell attempted `nv/bin/pytest` and returned 127. Evidence: `artifacts/20260717-cfg-red.log`.
- Confirmed cause: PTY pager interaction, unrelated to code or test semantics.
- Next command: rerun the exact pytest command by itself, without preceding pager-producing commands, and save to a distinct artifact rather than overwrite attempt 1.

## 2026-07-17 20:14 JST — CFG RED attempt 2

- Consecutive failure count: 1 for the actual CFG contract (attempt 1 did not execute tests).
- Command: `.venv/bin/pytest -q tests/test_config.py` in PBS job `2404248.opbs` on `mg0039`.
- Expected: new CFG-01–04 contracts fail while existing configuration tests stay green.
- Actual: exactly the new contract surface failed: 8 failed, 51 passed. Dead fields still parse; their errors therefore do not say `字段已移除`; the flat timeout is still accepted; nested `learner.prediction` is unknown; strategy-scoped timeout cases cannot parse. Evidence: `artifacts/20260717-cfg-red-attempt2.log`.
- Confirmed cause: the S5 schema, removed-key diagnostics, and resolve-time strategy validation have not been implemented.
- Next change: add path-aware removed-key handling, `PredictionSection`, nested runtime reads, and the strategy validation entry point; then mechanically migrate every repository YAML before rerunning the unchanged CFG suite.

## 2026-07-17 20:23 JST — CFG-06 prediction tiny attempt 1

- Consecutive failure count: 1 for prediction tiny; replace and rebase in the same strategy matrix exited 0.
- Command: one-learner `scripts/local/run_tiny_2proc_smoke.sh` with `configs/fs_diloco_tiny_predict_local.yaml`, run ID `s5_predict_20260717`, PBS job `2404248.opbs`, node `mg0039`.
- Expected: normal completion after the schema-only migration.
- Actual: syncer exited on `FileNotFoundError` while maintenance called `tmp.stat()` for a learner atomic-write temp file that was renamed away between the directory glob and stat. The learner saw `stop reason=error`; launcher exited 1. Evidence: `artifacts/predict.stdout.log` and `artifacts/predict/run/logs/syncer.jsonl`.
- Confirmed cause: a pre-existing TOCTOU race in `collect_runtime_artifacts`, unrelated to the config schema. A concurrent atomic writer may legitimately remove a `.tmp` path after maintenance enumerates it.
- Next change: add a focused regression in maintenance tests, make temp stat/unlink tolerant of concurrent disappearance while retaining age-gated cleanup, run the maintenance group and full suite, then use a new prediction run root for attempt 2.
