# S5 implementation progress

## 2026-07-17 20:18 JST — L0/L1/L2, CFG-01/02/03/05

- Baseline commit: `77edc08`; configuration implementation commit: `c53f14d`.
- Consumer inventory: `sync.upload_mode`, `liveness.quorum_policy`, and `inner_optimizer.reset_on_global_update` had no read sites; only their dataclasses, YAML, and documentation existed. Prediction reconcile timeout is consumed only by prediction snapshot recovery and strategy reconcile wait. Post-publish wait/poll are consumed by the shared strategy-base path and remain common `learner.*` fields. `global_adoption_strategy`, inner polling, and post-upload adoption remain strategy-selection/common controls.
- Unknown keys were already rejected before S5. `_from_dict` now tracks the dotted path so the four removed keys receive actionable diagnostics: the three dead fields say `字段已移除` with no replacement; the old flat timeout points to `learner.prediction.reconcile_timeout_seconds` (CFG-01/02).
- Added `PredictionSection`; default is 60 seconds and YAML override parsing is covered. All prediction YAMLs use the nested key; rebase YAMLs do not carry an irrelevant explicit prediction timeout; no empty rebase section was created (CFG-03).
- Removed all three dead fields from dataclasses and all 23 repository YAMLs. `test_every_repository_config_resolves` permanently parameterizes every `configs/*.yaml` file (CFG-05).

## 2026-07-17 20:19 JST — L3, CFG-04/06

- Validation ownership inventory: syncer device/dtypes, grace, completion, fragment structure, and common post-publish wait/poll stay in `resolve_config`. The unique strategy type table rejects names; replace has an explicit no-op `validate`; rebase owns its adopt/poll requirements; predict owns adopt/poll, Nesterov, zero outer weight decay, and nested timeout >0.
- `validate_global_adoption_strategy` uses the same type table as the runtime factory. The duplicated inline rebase/predict validation was removed from config resolution. Tests prove timeout 0 is accepted for replace/rebase and rejected only for predict (CFG-04/06).
- CFG/adoption/reconcile group: 88 passed in 7.66s (`artifacts/20260717-cfg-green.log`).

## 2026-07-17 20:27 JST — L4 and pipeline closeout

- Final full suite: 189 passed in 42.05s (`artifacts/20260717-full-pytest-final.log`). Static `.venv/bin/ruff check fs_diloco tests`, `git diff --check`, and YAML/dataclass removed-field searches passed.
- One-learner replace, rebase, and prediction tiny pipelines all completed. Authoritative final versions were 1 (`input_exhausted`), 3, and 3; all learners stopped and `check_plan01_invariants.py` returned PASS for each. Resolved snapshots contain no removed/flat legacy keys and show nested timeout defaults/override. Evidence: `artifacts/{replace,rebase,predict_attempt2}/run` and `artifacts/checker_*.txt`.
- Prediction attempt 1 exposed an unrelated atomic-temp maintenance TOCTOU and is retained in `failures.md`. Commit `a0eebcc` adds a deterministic disappearing-temp regression and treats a rename between glob/stat as normal; retention tests passed 3/3 before prediction attempt 2 exited 0.
- Runtime environment: PBS job `2404248.opbs`, one Miyabi-G node `mg0039`, default `nvidia/25.9` and `nv-hpcx/25.9` modules.
