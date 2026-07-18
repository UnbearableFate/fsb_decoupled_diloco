# B8 failures

## 2026-07-17 — SHT targeted suite attempt 1 (consecutive failure 1)

- Compute-node command on PBS `2404765.opbs`, `mg0004`: `pytest -q tests/test_syncer_selection.py tests/test_config.py`.
- Expected: formula/config and deterministic timeout-detail tests pass.
- Actual: **1 failed, 81 passed** (`artifacts/20260717-sht-green.log`). The event correctly included learner_000 as active at the seeded `last_seen` and learner_001 as unknown, but learner_000's `status_reason` was null rather than the fixture's initial `"training"`.
- Confirmed cause: each shutdown poll calls the existing `update_liveness_statuses`; active classification intentionally writes its current classification reason (`None`) and therefore replaces the seeded prior reason. The event accurately snapshots the store at timeout. Timeout formula, explicit override, missing-learner detail, validation, and SHT-04 static check all passed.
- Next change: correct the assertion to the authoritative post-classification `status_reason=None`; retain status and last_seen as the required last-heartbeat evidence. Rerun the identical group.

## 2026-07-17 — SHT-03 whole-run trace comparison (new experiment, consecutive failure 1)

- Command compared the B6 normal replace run with the B8 normal replace run using `compare_event_traces --profile core-pipeline --role syncer`.
- Expected: exact normalized trace equality.
- Actual: comparator differed at the first `updates_selected` only because the independently scheduled learner's selected proposal was local step 14 in the older run and local step 2 in the new run (`artifacts/20260717-normal-trace-compare.log`). The surrounding event sequence, selected count/tokens, versions, stop reason, and completion invariants match; B8's normal checker returned PASS and no `learner_shutdown_timeout` occurred.
- Confirmed cause: proposal local-step identity is timing-dependent across two-process runs and unrelated to the shutdown timeout calculation, which is only entered after stop publication.
- Next falsification: compare the ordered syncer lifecycle after removing random/update identities and proposal local-step suffixes, and explicitly assert absence of the timeout event. Keep the shared comparator profile unchanged because other plans rely on its stricter proposal identity guard.
