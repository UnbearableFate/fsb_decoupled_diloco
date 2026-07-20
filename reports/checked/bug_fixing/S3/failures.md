# S3 failures

## 2026-07-17 19:52 JST — LDU RED attempt 1

- Consecutive failure count: 1 (expected RED).
- Command: `.venv/bin/pytest -q tests/test_shared_runtime_primitives.py tests/test_syncer_selection.py`.
- Expected: the new shared-primitive contract must fail before implementation.
- Actual: test collection failed because `apply_fragment_adoption` does not yet exist; exit code 2. Evidence: `artifacts/20260717-ldu-red.log`.
- Confirmed cause: shared proposal-source and fragment-adoption APIs have not been implemented. The exact-set input-closed cases were added in the same test batch but could not run after collection stopped.
- Next change: implement the parameterized proposal source/drop/collect skeleton and the single fragment-adoption helper, then rerun the same command unchanged.

## 2026-07-17 19:57 JST — LDU-04/05 raw trace attempt 1

- Consecutive failure count: 1 for the trace gate.
- Commands: baseline/current full and fragment comparisons with profile `core-pipeline`.
- Expected: the generic profile might provide a repeatable projection.
- Actual: both returned 1. Full selected learner 000's step-8 versus step-6 proposal at version 0, an allowed timing difference under latest-wins discovery. Fragment reached the same step and fragment but its UUID was not normalized because fragment update IDs include `_fNNN_` before the random suffix. Evidence: `artifacts/ldu04_full_trace.txt` and `artifacts/ldu05_fragment_trace.txt`.
- Confirmed cause: the generic profile includes asynchronous proposal-selection details and therefore was not proven repeatable; additionally, the S2 normalizer recognized full but not fragment update-ID syntax.
- Next change: add a fragment-ID normalization regression, then use narrow publication/adoption profiles whose selected fields are deterministic, without hiding any event or field used by the shared helper contracts. Controlled LDU-01–03 tests remain the semantic hard gate.
