# Implementation progress: plan 01

Verified work units are appended here after their complete related test group passes.

## 2026-07-16 21:21:56 JST — L1/L4 login-node static validation

- Scope: persistent-DB configuration removal, fixed proposal paths, current-only maintenance code, updated PBS launch arguments, and new focused tests.
- Changes: removed local SQLite/dump/retention options; fixed SQLite under `control/`; introduced fixed learner pointers, transactional full commits, archival/pruning, reference-driven GC, DB-first resume, and terminal-drain helpers.
- Environment: Miyabi login/control-plane host `miyabi-g1`; no runtime imports or tests executed.
- Commands: `git diff --check`; `uv run ruff check fs_diloco tests`; `bash -n scripts/miyabi/*.pbs`.
- Result: all static checks passed. `ruff` reported `All checks passed!`; every PBS file parsed successfully.
- Artifacts: command output retained in the active Codex execution record; no separate large artifact was produced.
- Remaining: compute-node focused/full pytest, SQLite stress/kill probes, smoke/failpoint matrix, 2-node reopen validation, 9-node 50x10, 5000-step staged observation, documentation synchronization, and independent invariant checker.

## 2026-07-16 21:24 JST — L1–L5 focused compute-node tests

- Scope: removed configuration rejection, DELETE/FULL persistence and reopen, fixed-pointer latest-wins ingestion, transactional full commits and rollback, current-only archive/GC, DB-first resume/cache repair, and strict terminal-drain selection.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, compute host `mg0015`; default modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Command: `uv run pytest -q tests/test_config.py tests/test_sqlite_store.py tests/test_retention.py tests/test_resume.py tests/test_syncer_selection.py`.
- Result: 32 tests passed in 5.88 seconds.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2124_l1-l5-focused_pass.log`.
- Remaining: full pytest, stress/kill/crash matrix, real tiny smoke, multi-node and 9-node validation, documentation, and checker audit.

## 2026-07-16 21:25 JST — Full unit/integration test suite

- Scope: complete repository pytest suite after the persistent protocol and bounded-state changes.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`, default `nvidia/25.9` and `nv-hpcx/25.9` modules.
- Command: `uv run pytest -q`.
- Result: 64 tests passed in 3.89 seconds.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2125_full-pytest_pass.log`.
- Remaining: probe/stress and crash testing, real pipeline smoke, multi-node/9-node ladder, docs, and checker.

## 2026-07-16 21:32 JST — L0 shared-filesystem SQLite single-node probe

- Scope: probe harness unit coverage, 10,000 FULL-synchronous transactions on the shared work filesystem, integrity/reopen, and random before/after-commit SIGKILL recovery.
- Changes: added `scripts/miyabi/sqlite_shared_fs_probe.py` and its focused subprocess test.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`; DB stored below the repository on `/work`.
- Commands: `uv run pytest -q tests/test_sqlite_probe.py`; `uv run python scripts/miyabi/sqlite_shared_fs_probe.py stress ... --count 10000`; `uv run python scripts/miyabi/sqlite_shared_fs_probe.py kill-reopen ... --cycles 100 --seed 1337`.
- Result: probe test passed; 10,000 transactions completed in 55.10 seconds with zero busy errors and `integrity_check=ok`; 100 kill/reopen cycles (48 before commit, 52 after commit) preserved `counter == events == 10052` and `integrity_check=ok`.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2130_sqlite-probe_pass.log`, `20260716-2131_sqlite-10000_pass.json`, `20260716-2131_sqlite-10000_pass.sqlite3`, and `20260716-2132_sqlite-kill-reopen_pass.json`.
- Remaining: two-node concurrent/reopen visibility is not covered by this single-node result.

## 2026-07-16 21:44 JST — L2/L3 publication crash matrix

- Scope: six full-publication failure points (`weight_temp`, weight complete, outer complete, SQLite transaction, DB committed before latest, and latest updated), each killed and recovered ten times.
- Changes: added deterministic publication failpoints and `scripts/miyabi/publication_crash_probe.py`.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`, synthetic full model path, persistent DB and artifacts on shared `/work`.
- Command: one-case rehearsal followed by `uv run python scripts/miyabi/publication_crash_probe.py --root runs/fs_diloco/publication_crash_probe_20260716_2143 --iterations 10`.
- Result: all 60 SIGKILL cases passed. Pre-commit cases recovered from DB v0; post-commit cases recovered from DB v1; every case repaired latest, reached v2, kept exactly one current weight/outer pair, removed terminal tensors, preserved `integrity_check=ok`, and archived the original proposal exactly once.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2141_publication-crash-matrix-1_pass.json`, `20260716-2143_publication-crash-matrix-10_pass.json`; run trees are under `runs/fs_diloco/publication_crash_probe_20260716_2143/`.
- Remaining: real concurrent learner/syncer smoke and cluster-scale validation still need to prove the same invariants on the application loop.

## 2026-07-16 21:51 JST — L4/L5 tiny full pipeline and terminal GC

- Scope: real concurrent synthetic syncer plus two learners, fixed pointer overwrite behavior, terminal input closure, `input_exhausted`, archive/prune, and zero-grace GC after all learners stop.
- Changes: normal-exit maintenance now collapses orphan publication grace only when all expected learners are proven stopped; regular running/startup maintenance retains the specified grace.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`; run ID `20260716_2151_tiny_full_persistent`.
- Commands: `uv run pytest -q tests/test_retention.py tests/test_sqlite_store.py tests/test_resume.py`; `RUN_ID=20260716_2151_tiny_full_persistent bash scripts/local/run_tiny_2proc_smoke.sh`; explicit `find`, SQLite integrity/count queries, and failure-event search.
- Result: 17 focused tests passed; the smoke ended normally at version 1 with `input_exhausted`, DB/latest/summary agreement, two archived applied updates, one active global row, one current weight and outer state, two fixed pointer JSON files, zero active update rows, zero proposal tensors, no dump, no error/no-progress event, and `integrity_check=ok`.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2150_terminal-gc-focused_pass.log`, `20260716-2151_tiny-full-smoke_pass.log`; run root `runs/fs_diloco/20260716_2151_tiny_full_persistent/`.
- Remaining: the small finite workload legitimately exhausted after one merge rather than hitting target 2; target-reaching behavior remains covered by later full-scale tests.

## 2026-07-16 21:57 JST — L4 1000-cycle bounded state machine

- Scope: BND-01/02/03/04/07/10/11/12 fixed discovery, latest-wins active-state bound, current global row, physical SQLite working set, and archive uniqueness across 1,000 commits.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`; four simulated learner frontiers and shared-filesystem SQLite.
- Command: `uv run pytest -q tests/test_bounded_1000_cycles.py`.
- Result: passed in 34.53 seconds. The test proved active updates never exceeded `2M`, finished with four pending rows and no selected rows, retained one global row at v1000, kept exactly four fixed pointers, created no proposal tensor, kept used SQLite pages within 16 pages of the cycle-100 warm-up, archived 4,000 unique update IDs and 1,000 unique prior versions, and passed integrity check.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2156_bounded-1000-cycle_pass.log`.
- Remaining: this is a deterministic state-machine workload, not a replacement for long real-model directory-growth observation.

## 2026-07-16 22:05 JST — Fragment persistent/current-only smoke

- Scope: persistent DELETE/FULL DB in fragment mode, current-only per-fragment checkpoints, current materialized full checkpoint, shutdown proposal terminalization, archive completeness, and terminal tensor cleanup (fragment resume remains intentionally out of scope).
- Changes: normal all-learners-stopped shutdown now terminalizes remaining selected/pending rows with the stop reason before archive and GC.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`; run ID `20260716_2205_tiny_fragment_persistent`.
- Commands: 13 focused store/maintenance tests; fragment pipeline/analysis/store tests; `CONFIG=configs/fs_diloco_tiny_fragment_local.yaml bash scripts/local/run_tiny_2proc_smoke.sh`; explicit artifact, SQLite, and log inspection.
- Result: focused tests passed; the real smoke reached target event 4 with DB/latest/summary agreement and `stop_after_outer_steps`, archived 8 applied and 4 shutdown-dropped proposals, retained only fragment 0 v2 and fragment 1 v2 weight/outer pairs plus materialized `global_v000004`, left zero update rows and zero proposal metadata/tensors, emitted no error/no-progress/dump event, and passed `integrity_check=ok`.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2158_fragment-bounded-focused_pass.log`, `20260716-2204_terminal-finalize-focused_pass.log`, `20260716-2205_tiny-fragment-smoke_pass.log`; run root `runs/fs_diloco/20260716_2205_tiny_fragment_persistent/`.
- Remaining: fragment resume is still intentionally excluded by the plan.

## 2026-07-16 22:08 JST — Post-smoke full regression suite

- Scope: complete repository tests after terminal GC/finalization and probe additions.
- Environment: Miyabi-G 1-node interactive allocation `2398231.opbs`, host `mg0015`.
- Command: `uv run pytest -q`.
- Result: 68 tests passed in 42.38 seconds, including the 1,000-cycle bound.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2207_full-pytest-post-smoke_pass.log`.

## 2026-07-16 22:18 JST — L0/L3 two-node concurrent SQLite and DB-only resume

- Scope: DB-04/06 and the 2-node ladder: simultaneous shared-DB writers, cross-node reopen, latest deletion/rebuild, selected-proposal carryover, exactly-once application, and continued commit.
- Environment: Miyabi-G 2-node interactive allocation `2398303.opbs`, hosts `mg0001` and `mg0002`, one MPI rank per node, default modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Commands: two concurrent 5,000-transaction probe writers via MPI; two-rank verify; node-A `publication_crash_probe.py cross-init`; node-B `cross-resume`; two-rank SQLite final verification.
- Result: both writers reported zero busy errors and together produced exactly 10,000 events with `integrity_check=ok`; both nodes reopened and read counter/events 10,000. Node A committed v1 and left `carry-selected-u1` selected; node B deleted latest, resumed solely from persistent DB, reset the unchanged proposal, committed it exactly once to v2, rebuilt latest, pruned to zero active updates, and both nodes read DB v2 with integrity OK.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2212_sqlite-cross-node_pass.log`, `20260716-2212_sqlite-cross-node_pass.sqlite3`, `20260716-2213_sqlite-cross-node-verify_pass.log`, `20260716-2217_cross-node-resume-a_pass.log`, `20260716-2217_cross-node-resume-b_pass.log`, and `20260716-2218_cross-node-resume-verify_pass.log`; run root `runs/fs_diloco/20260716_2217_cross_node_resume/`.
- Remaining: full 9-node application validation remains required.

## 2026-07-16 — L1/L6 documentation and telemetry synchronization

- Scope: README, architecture, runtime/data flow, configuration, operations, module references, and research-plan alignment with persistent DB authority, fixed full pointers, DB-first recovery, current-only GC, and terminal input closure.
- Changes: removed all operational local-SQLite/WAL/dump/multi-version-retention descriptions; documented fragment resume as out of scope; added per-merge `sqlite_commit_seconds` and `maintenance_seconds` CSV/W&B/log telemetry needed for 9-node acceptance accounting.
- Environment: Miyabi login/control-plane host `miyabi-g1`; static checks only.
- Commands: `git diff --check`; `uv run ruff check fs_diloco scripts/miyabi/publication_crash_probe.py scripts/miyabi/sqlite_shared_fs_probe.py tests`; `bash -n scripts/miyabi/*.pbs`; forbidden-argument/group-placeholder search.
- Result: all checks passed; every PBS script has literal group `xg24i002`; no launcher contains `SYNCER_DB_DIR`, `--sqlite-local-dir`, or a group placeholder.
- Remaining: compute-node regression for the added telemetry and the 9-node 50x10/5000-step ladder.

## 2026-07-16 — Post-telemetry compute regression

- Scope: complete repository regression plus a real two-learner tiny full run exercising the new SQLite commit and maintenance timing fields.
- Changes: added the statically validated `scripts/miyabi/run_plan01_regression.pbs` so compute-only regression is reproducible without command-form qsub quoting.
- Environment: Miyabi-G debug job `2398374.opbs`, compute host `mg0002`, group `xg24i002`; run ID `plan01_regression_2398374`.
- Commands: `bash -n scripts/miyabi/*.pbs` and group/legacy-argument checks before submission; `qsub ... scripts/miyabi/run_plan01_regression.pbs`; inside the job, `.venv/bin/python -m pytest -q`, tiny full smoke, and CSV assertions.
- Result: PBS `Exit_status=0`; 68 tests passed in 46.13 seconds; tiny run produced a committed merge with `sqlite_commit_seconds=0.005303` and `maintenance_seconds=0.028291`, both present and non-negative in `syncer_metrics.csv`.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2208_full-pytest-telemetry_pass.log`; run root `runs/fs_diloco/plan01_regression_2398374/`.
- Remaining: 9-node full BF16 50x10 acceptance and staged 5000-step observation.

## 2026-07-16 — L6 9-node full BF16 50x10 acceptance

- Scope: final full reference application path with eight learners, fresh-only BF16 proposals, ten global commits, persistent shared SQLite, current-only GC, terminal shutdown, and timing thresholds.
- Environment: Miyabi-G batch job `2398380.opbs`, nine nodes (`mg0167`, `mg0191`, `mg0192`, `mg0196`, `mg0202`, `mg0205`, `mg0206`, `mg0208`, `mg0210`), PBS elapsed 4m20s; run ID `codex_plan01_full50x10_20260716_220849`.
- Command/config: `qsub -v RUN_ID=...,WANDB_MODE=offline,HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1 scripts/miyabi/run_9node_no_fragment_gpt2_wikitext2_50x10.pbs`; `configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml` (`inner_steps=50`, `stop_after_outer_steps=10`, `max_staleness_versions=0`, BF16 upload).
- Result: PBS `Exit_status=0`; DB/latest/stop/summary all version 10 with `stop_after_outer_steps`; ten merges each selected all eight learners; integrity OK, DELETE/FULL, one active global row, only v10 weight/outer, eight fixed pointers, zero active update rows, zero proposal tensors/meta, no dump/failure events. Complete training time was 250.496s; SQLite commit p95 was 0.006192s; SQLite commits plus maintenance used 0.3612% of training time.
- Independent checker: `PASS` from `scripts/miyabi/check_plan01_invariants.py --expected-learners 8 --expected-version 10 --require-complete`.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-220849_full50x10_pass.log`, `20260716-2213_full50x10_evidence_pass.json`, and `20260716-2213_full50x10-checker_pass.txt`; run root `runs/fs_diloco/codex_plan01_full50x10_20260716_220849/`; per-rank logs `logs/qsub_codex_plan01_full50x10_20260716_220849/`.
- Remaining: submit and observe the revised 5000-step/50-outer job through at least committed v5 without terminating it.

## 2026-07-16 — L6 staged 9-node full 5000-step observation through v5

- Scope: required staged handoff for the revised 5000-local-step/50-outer full run; observe five consecutive post-v0 commits while leaving the batch job running.
- Environment: Miyabi-G batch job `2398400.opbs`, nine nodes (`mg0933`, `mg0990`, `mg0115`, `mg0123`, `mg0127`, `mg0132`, `mg0135`, `mg0136`, `mg0137`); run ID `codex_plan01_full5000_20260716_221526`; shared root `runs/fs_diloco/codex_plan01_full5000_20260716_221526/`.
- Command/config: `qsub -v RUN_ID=...,WANDB_MODE=offline,HF_DATASETS_OFFLINE=1,HF_HUB_OFFLINE=1 scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs`; `configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml` (`max_local_steps=5000`, `inner_steps=100`, `stop_after_outer_steps=50`, fresh-only).
- Result at the v5 observation boundary: job state `R`; DB/latest both exactly v5 and referenced the same weight/outer payload; integrity OK, DELETE/FULL, one global row, only `global_v000005`/`outer_v000005`, versions v0–v4 archived and checkpoints absent, zero archived terminal payloads remaining, active updates 8 (≤16), five selection/outer/metric rows each selected=8, and 40 adoption events spanning all eight learners. Eleven proposal tensors consisted of the eight active proposals plus publication-grace/in-flight objects; no already-terminalized tensor remained.
- Failure scan: no error, no-progress, dump, integrity, duplicate-version, or missing-checkpoint event. Selection, merge, SQLite transaction, latest publication, and learner adoption all advanced continuously.
- Independent checker: `PASS_WITH_FOLLOWUPS`, where the follow-up is the deliberately unobserved completion/terminal-drain result of the still-running 50-outer job.
- Artifacts: `reports/imp_plans/01/artifacts/20260716-2225_full5000-v5_evidence_pass.json`, `20260716-2225_full5000-v5-checker_pass.txt`, and the v5 log snapshot `20260716-2225_full5000-v5_staged_pass.log`; live PBS output continues in `20260716-221526_full5000_staged_result.log`; syncer/per-rank logs `logs/qsub_codex_plan01_full5000_20260716_221526/`.
- Handoff: do not cancel job `2398400.opbs`. Its complete 5000-step/terminal result remains for the next explicit monitoring request.

## 2026-07-16 — Final static and independent invariant audit

- Scope: final source/config/launcher audit plus independent completed-run and staged-run invariant checks.
- Environment: Miyabi login/control-plane host `miyabi-g1`; only static checks and lightweight read-only run inspection. Long job `2398400.opbs` remained `R`/substate 42 and was not cancelled.
- Commands: `git diff --check`; `uv run ruff check .`; `bash -n scripts/miyabi/*.pbs`; legacy field/argument/group-placeholder searches; WAL/temp scan; independent checker against the completed 50x10 run and live 5000-step run.
- Result: all static checks passed; no runtime local-DB/dump/legacy retention field remains; completed 50x10 checker returned `PASS`; staged long-run checker returned `PASS_WITH_FOLLOWUPS` because full 50-outer completion is intentionally outside this handoff boundary.
- Artifact: `reports/imp_plans/01/artifacts/20260716-2227_final-checker_pass.txt`.

## 2026-07-17 — L6 9-node FP32 fresh-only 5000-step terminal validation

- Scope: complete the staged job `2398400.opbs` audit after all eight learners reached 5000 local steps, including terminal drain, current-only artifacts, performance telemetry and the configured 50-outer target.
- Environment: Miyabi-G nine-node batch run `codex_plan01_full5000_20260716_221526`; PBS `Exit_status=0`, walltime 21m08s.
- Result: all learners reached step 5000; 400 updates were produced, 190 applied and 210 dropped; the run ended normally at v25 with `input_exhausted`. DB/latest/stop/summary agreed, integrity and DELETE/FULL PRAGMAs passed, only v25 weight/outer and eight fixed pointers remained, and no active proposal tensor/meta, temporary file, WAL, dump or failure event remained.
- Performance: complete training time 1257.338s; update write mean/p95 0.21094s/0.45642s; SQLite commit p95 0.01244s; SQLite plus maintenance 0.273% of training time.
- Independent checker: `PASS` for the actual completed v25 state and `BLOCKED` when requiring the configured v50 target.
- Finding: fresh-only admission combined with immediate next-cycle training caused about every second proposal to become superseded/stale; correct terminal behavior was verified, but fixed 5000 local steps did not imply 50 commits.
- Artifact: `reports/imp_plans/01/artifacts/20260716-221526_full5000_staged_result.log`; detailed analysis in `reports/run_analysis.md`.

## 2026-07-17 — 9-node BF16/staleness=2 5000-step comparison and research handoff

- Scope: repeat the full 5000-step workload with BF16 upload payloads and `max_staleness_versions=2`, then compare update utilization, I/O, loss telemetry and terminal invariants with the FP32 fresh-only run.
- Environment: Miyabi-G job `2398817.opbs`, run `codex_plan01_full5000_bf16_s2_20260716_233341`, nine nodes; PBS `Exit_status=0`, no abnormal nodes, walltime 21m00s.
- Result: all learners reached step 5000; 372/400 updates were applied, 28 were superseded, and the run ended normally at v48 with `input_exhausted`. Applied staleness was 27 at zero and 345 at one; no update used staleness two. Terminal DB/cache/artifact invariants all passed and no failure event remained.
- Performance: payload size fell from 497.759MB to 248.880MB; update write mean/p95 were 0.18004s/0.27075s; syncer read mean was 0.34091s; complete training time was 1250.194s. SQLite commit p95 was 0.03078s and commit plus maintenance used 0.450% of training time.
- Independent checker: `PASS` for actual v48 and `BLOCKED` for the configured v50 target.
- Research handoff: BF16 clearly improves proposal I/O and staleness improves update utilization, but neither run proves final model quality or guarantees 50 outer merges from exactly 5000 local steps. A controlled dtype-only multi-seed validation/perplexity comparison and an explicit completion-semantics study remain required.
- Artifact: `reports/imp_plans/01/artifacts/20260716_233341_full5000_bf16_s2_result.log`; detailed comparison in `reports/run_analysis.md`.
