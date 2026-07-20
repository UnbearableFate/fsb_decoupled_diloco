# B2 failures

## 2026-07-17 — SCH-GREEN attempt 1 (consecutive failure 1)

- Command: `pytest -q tests/test_inner_scheduler.py tests/test_shared_runtime_primitives.py tests/test_learner_rebase.py tests/test_config.py` in PBS job `2404379.opbs`, node `mg0041`, Python 3.13.13, PyTorch 2.13.0+cu132, modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Expected: scheduler/config/fragment-helper related group passes after the first implementation.
- Actual: 91 passed, 11 failed. Full output: `artifacts/20260717-sch-green-attempt1-fail.log`.
- Confirmed causes: (1) the default dataclass remained `scheduler=cosine` with no horizon, so unrelated partial-config tests failed before reaching their intended validations; fail-closed must apply when cosine is explicitly/default configured without making the repository's default `resolve_config()` invalid, so the default scheduler should be `none`; repository cosine YAMLs remain explicit. (2) the fragment helper's fake optimizer objects cannot supply newly logged optimizer metrics; the test double should use a minimal optimizer-shaped object or the new reset event should limit itself to phase fields. The production event benefits from the metrics, so the test double will be corrected.
- Next falsification: switch the dataclass default to `none`, assert that default explicitly, update the fragment test double to expose `state`, `param_groups`, and `last_epoch`, then rerun the identical group. No scheduler formula change is planned.
