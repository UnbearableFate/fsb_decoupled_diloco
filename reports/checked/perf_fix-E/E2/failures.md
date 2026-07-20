# E2 failures

## 2026-07-18 — OVL RED: main thread blocks on checkpoint futures

- Command: publish-ingest focused test.
- Result: expected RED, 1 failed.
- Failure signature: `publish_global()` has no bounded main-thread callback while
  its two checkpoint workers are pending, so heartbeat/pointer ingestion cannot
  overlap file I/O.
- Evidence: `artifacts/20260718-0432_publish-ingest-red_fail.log`.

## 2026-07-18 — OVL-INT RED: interval accounting contract absent

- Command: `pytest -q tests/test_interval_telemetry.py`
- Result: expected RED, 2 failed.
- Failure signature: no helper validates non-overlapping discovery/idle/grace/
  read/merge/publish/maintenance components or exposes the residual and quorum
  trigger.
- Evidence: `artifacts/20260718-0455_interval-telemetry-red_fail.log`.

## 2026-07-18 — OVL runtime allocation request failed (attempt 1)

- Command: `qsub -I -q rt_HG -l select=1:ncpus=8:mem=32gb:ngpus=1 -l walltime=02:00:00 -W group_list=xg24i002` from the Miyabi login node.
- Expected: a fresh one-node interactive allocation after job `2405071.opbs` reached its one-hour walltime.
- Actual: PBS rejected the resource specification with `Resource invalid in "select" specification: ngpus`; no job was created and no runtime code executed.
- Confirmed cause: the request used a non-Miyabi queue/resource shape instead of the skill-prescribed `interact-g` / `select=1` form.
- Next attempt: request `interact-g` with literal group `xg24i002`, `select=1`, and a maximum one-hour walltime, then reload modules and the project environment on the compute node.

## 2026-07-18 — experiment-override regression group failed (attempt 1)

- Command: `pytest -q tests/test_config.py tests/test_source_identity.py tests/test_capture_source_identity.py` on Miyabi job `2405305.opbs`, node `mg0048`.
- Expected: config override, source identity, and launcher-facing configuration contracts all pass.
- Actual: 1 failed, 74 passed. `test_fragment_rejects_full_local_delta_rebase` expected the adoption-strategy validation error but its fixture now fails earlier on E3's required positive `fragments.materialize_full_every_events` invariant.
- Confirmed cause: the pre-E3 test fixture enables fragment mode without supplying the now-mandatory materialization interval; the production validation is correct and the failure is unrelated to the new E2 overrides.
- Evidence: `artifacts/20260718-0601_experiment-overrides-pass.log` (the filename predates the result; content is authoritative and records the failure).
- Next modification: make the fixture valid under E3 by setting a positive materialization interval, leaving the adoption strategy as the only violated invariant; rerun the identical group under a new artifact name.

## 2026-07-18 — full lint gate failed (attempt 1)

- Command: `ruff check fs_diloco scripts/miyabi/measure_pointer_polling.py tests` on Miyabi job `2405305.opbs`, node `mg0048`.
- Expected: repository Python lint gate passes before freezing the formal experiment source snapshot.
- Actual: two F401 errors: unused `Path` in `tests/test_adoption_telemetry.py` and unused `torch` in `tests/test_fragment_materialization.py`.
- Confirmed cause: obsolete imports in tests added by the earlier E6/E3 work; no production-code lint error was reported.
- Evidence: `artifacts/20260718-0622_ruff-pass.log` (filename predates the result; content records the failure).
- Next modification: remove exactly the two unused imports and rerun the identical lint command; no automatic broad rewrite.
