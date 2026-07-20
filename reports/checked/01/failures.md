# Implementation failures: plan 01

Failures are recorded here before the next targeted modification.

## 2026-07-16 21:40 JST — publication-crash-probe lint, failure 1

- Experiment: `publication-crash-probe-static`; consecutive failure count: 1.
- Environment: Miyabi login/control-plane host `miyabi-g1`; static-only validation.
- Command: `uv run ruff check fs_diloco scripts/miyabi/publication_crash_probe.py tests`.
- Expected: the new failpoint and crash-probe implementation passes static lint.
- Actual: Ruff F401 reported unused `sqlite3` at `scripts/miyabi/publication_crash_probe.py:9`; no runtime code executed.
- Minimal symptom: a single unused standard-library import.
- Artifact: terminal output in the active Codex record; no separate artifact was needed for this deterministic one-line lint diagnostic.
- Confirmed cause: `sqlite3` was imported during probe drafting but is not used by the final implementation.
- Next change: remove only the unused import, then rerun the identical Ruff command to falsify the diagnosis before running the crash probe.

## 2026-07-16 21:47 JST — tiny full bounded-state smoke, failure 1

- Experiment: `tiny-full-bounded-smoke`; consecutive failure count: 1.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`, default modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Command/config: `RUN_ID=20260716_2146_tiny_full_persistent bash scripts/local/run_tiny_2proc_smoke.sh`; `configs/fs_diloco_tiny_local.yaml`; run root `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/20260716_2146_tiny_full_persistent`; no PBS batch job.
- Expected: normal `input_exhausted` terminal drain is allowed below the configured target, but the ending artifact set must contain only fixed pointers and zero terminal/unreferenced proposal tensors.
- Actual: the run ended consistently at DB/latest/summary version 1 with `input_exhausted`, `integrity_check=ok`, one current weight/outer pair, and no active DB update rows; however six earlier pointer-overwritten payload tensors (steps 2, 4, and 6 for both learners) remained below `updates/payloads/`.
- Minimal symptom: `find .../updates -type f` listed the two fixed pointers plus six unreferenced `.params.safetensors` files after all processes exited.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2146_tiny-full-smoke_fail.log`; full run tree at the path above.
- Confirmed cause: regular maintenance correctly grants unobserved payloads the publication grace, but normal-exit maintenance reused that grace even after all expected learners had stopped, when no pointer publication could still be in flight.
- Next change: allow maintenance to collapse proposal grace to zero only when input closure/all-learner shutdown is proven; pass that terminal condition from both full and fragment normal-exit paths, then rerun the same smoke and explicitly inspect artifacts.

## 2026-07-16 22:00 JST — tiny fragment terminal bounded-state smoke, failure 1

- Experiment: `tiny-fragment-bounded-smoke`; consecutive failure count: 1.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`, default modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Command/config: `RUN_ID=20260716_2159_tiny_fragment_persistent CONFIG=.../configs/fs_diloco_tiny_fragment_local.yaml bash scripts/local/run_tiny_2proc_smoke.sh`; run root `/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco/20260716_2159_tiny_fragment_persistent`.
- Expected: version 4 target completion with one current weight/outer pair per fragment, one current materialized full checkpoint, no old versions, no terminal proposal tensors, and no remaining active proposals after all learners stop.
- Actual: DB/latest/summary reached version 4 with `stop_after_outer_steps`, eight archived applied updates, current-only checkpoints, `integrity_check=ok`, and no errors; but four proposals published while learners observed the stop remained `pending`, with four metadata/tensor pairs under `updates/payloads/`.
- Minimal symptom: SQLite returned `pending|4` for `fragment_updates`, and artifact inspection listed those four proposal pairs.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2159_tiny-fragment-smoke_fail.log`; run tree at the path above. Focused fragment tests passed separately in `20260716-2158_fragment-bounded-focused_pass.log`.
- Confirmed cause: after the target is reached, final ingestion captures proposals emitted during shutdown, but no further merge is intended; these rows were left pending, so reference-driven GC correctly preserved them even with terminal grace disabled.
- Next change: when normal shutdown and all expected learners are stopped are both proven, atomically terminalize all remaining pending/selected rows with the stop reason before final archival and GC; add focused full/fragment coverage and rerun the smoke.

## 2026-07-16 — telemetry regression batch command, failure 1

- Experiment: `full-pytest-telemetry`; consecutive failure count: 1.
- Environment: Miyabi-G debug queue job `2398363.opbs`, allocated host `mg0002`, group `xg24i002`.
- Command/config: `qsub ... -- /bin/bash -lc $'set -e\n...\nuv run pytest -q'`; requested one node for 20 minutes; output path `reports/imp_plans/01/artifacts/20260716-full-pytest-telemetry.log`.
- Expected: the complete pytest suite runs on the compute node and writes its result to the artifact.
- Actual: PBS reported `Exit_status=0` with zero wall time and an empty output file; no pytest process or result was produced, so this is not a passing test.
- Minimal symptom: the retained artifact is zero bytes and `qstat -H` reports `resources_used.walltime=00:00:00`.
- Confirmed cause: the command-form `qsub -- /bin/bash -lc` invocation did not preserve the multiline shell program as the required single `-c` argument.
- Next change: add a small statically validated PBS regression script through `apply_patch`, submit that literal script after repeating the required `bash -n`/group checks, and require non-empty pytest output before recording a pass.
