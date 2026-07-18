# B6 failures

## 2026-07-17 — GCR targeted suite attempt 1 (consecutive failure 1)

- Compute-node command on PBS `2404379.opbs`, `mg0041`: `pytest -q tests/test_latest_load_retry.py tests/test_learner_rebase.py tests/test_adoption_strategy.py tests/test_shared_runtime_primitives.py tests/test_config.py`.
- Expected: helper and existing adoption/reconcile tests pass after whole-snapshot retry integration.
- Actual: **1 failed, 104 passed** (`artifacts/20260717-gcr-target-attempt1.log`). Only `test_prediction_preparation_recovers_collected_cached_checkpoint` failed: its mock throws the same v24 `FileNotFoundError` for every candidate and returns v25 from the wait mock even when asked for a version newer than v25. The new helper correctly retries the callback with v25, sees a second missing file, rejects the non-advancing pointer, and exhausts.
- Confirmed cause: the legacy test modeled the former “find newer and return without loading it” behavior, not the frozen B6 contract that retries the entire snapshot against the newer latest. Runtime code and the new generic tests behaved as specified.
- Next change: make the prediction mock fail only for candidate v24 and succeed for candidate v25, assert both attempts, and preserve the existing recovery event fields. Then add explicit full-context and fragment whole-snapshot injection coverage before rerunning the group.

## 2026-07-17 — GCR targeted suite attempt 2 (consecutive failure 2)

- Same compute environment; command added `tests/test_fragment_latest_retry.py` to the attempt-1 group.
- Expected: all entry-point injection tests pass.
- Actual: **2 failed, 106 passed** (`artifacts/20260717-gcr-target-attempt2.log`). Both new fragment tests reached the retry implementation but their fake loader returned five values for each fragment. The real balanced-tensor index for the synthetic parameter boundaries assigns 4 values to fragment 0 and 6 to fragment 1, so codec validation correctly raised `ValueError` before the assertions.
- Confirmed cause: incorrect test fixture sizing; no runtime state was committed and the failures are unrelated to retry control flow. The corrected prediction test from attempt 1 passed and recorded attempts `[24, 25]`.
- Next change: derive fake tensor sizes and expected offsets directly from `fragment_index["fragments"]`, retaining the same old-f0/old-f1/new-f0/new-f1 call-order and no-mixed-version assertions. Rerun the identical targeted group. A third failure will trigger the mandated full code review before further edits.

## 2026-07-17 — GCR-04 predict trace comparison (new experiment, consecutive failure 1)

- Command: `python -m fs_diloco.tools.compare_event_traces artifacts/predict-baseline artifacts/predict-green --profile core-pipeline --role learner` after all four normal pipelines and all invariant checkers passed.
- Expected: normalized learner traces equal.
- Actual: comparator reported a difference at index 41 (`artifacts/20260717-predict-trace-compare.log`). Baseline observed stop after local step 8; green completed one extra local step 9 before seeing the asynchronously published stop and emitted `global_prediction_abandoned_on_stop`. Neither run emitted `latest_load_recovered`; both reached authoritative v3 normally. Full/replace/rebase/fragment checkers all returned PASS (`artifacts/20260717-checkers.log`).
- Confirmed cause: the exact learner step at which a separate syncer process publishes/learner observes stop is an existing scheduling race, so whole-run exact trace equality is too strict for this pipeline profile. The differing events occur after both runs adopted/predicted from v3 and do not indicate a latest-load semantic change.
- Next falsification: compare deterministic strategy/unit event sequences (already in the 108-test GCR group), assert all normal pipeline logs contain zero `latest_load_recovered`, and compare authoritative stop/final version plus the prediction lifecycle through the last common update. Record exact whole-run trace as timing-sensitive rather than weakening the shared comparator profile.
