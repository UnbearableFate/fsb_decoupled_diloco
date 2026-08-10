# Independent Codex current-state review — Plan 02

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Continuity base: `68fdb0ab538b56bb2e99245fb55c1ff3f3c9d364`
- Current-state target: `550296db7eab0dbcc2abcb4d124c81acd162fd8c`
- Ancestry: the base is an ancestor of the target.
- Scope: all 66 tracked files under `fs_diloco/` at the target. The base records review continuity only; this was not a diff-only review.

I saved this report before invoking or reading an external reviewer for this gate.

## Findings

### Medium — the primary analysis command opens the authority database read/write

`fs_diloco/tools/analysis.py:95-99` checks that a database path exists and then calls `sqlite3.connect(path)`. That is a normal read/write/create connection, unlike the enforced `mode=ro` and `PRAGMA query_only=ON` connection supplied by `fs_diloco.storage.schema_bootstrap.open_readonly()`. It violates the plan-wide rule that analysis and Checker processes only open live authority state read-only. It also leaves a time-of-check/time-of-use window in which removal or replacement after `exists()` can cause SQLite to create a new empty authority file or open a different file with write capability.

Impact: an inspection-only command has unnecessary authority write capability and can mutate/create the path through future query changes, SQLite pragmas, or the disappearance race. This is a persistence and operator-safety boundary, even though the current queries happen to be reads.

Fix: import and use the shared `open_readonly()` entry point in `_db_summary()`. Add a RED regression that spies on the analysis connection, proves `PRAGMA query_only == 1`, proves a write is rejected, and proves the normal summary still reads the fixture database.

## Current-state review coverage

- Entry points and configuration: `fs_diloco/__init__.py`, `analysis.py`, `cli.py`, `eval_lm_harness.py`, `learner.py`, `syncer.py`; `core/__init__.py`, `core/config.py`, `core/constants.py`, `core/run_descriptor.py`.
- Modeling: `modeling/__init__.py`, `hf_data.py`, `hf_model.py`, `outer_optim.py`, `param_index.py`.
- Observability: `observability/__init__.py`, `logging_utils.py`, `metrics.py`, `phase1_performance.py`, `resource_monitor.py`, `wandb_logging.py`.
- Protocol: `protocol/__init__.py`, `control_epoch.py`, `dynamic_terminal.py`, `fragment_codec.py`, `fragment_index.py`, `fragment_scheduler.py`, `liveness.py`, `membership.py`, `merge.py`.
- Runtime: `runtime/__init__.py`, `adoption.py`, `failure_sim.py`, `launch_outbox.py`, `learner.py`, `pbs_scheduler.py`, `syncer.py`, `syncer_ha.py`.
- Storage and schema: `storage/__init__.py`, `atomic_io.py`, `fenced_store.py`, `leader_lease.py`, `maintenance.py`, `paths.py`, `schema.sql`, `schema_bootstrap.py`, `sqlite_store.py`, `tensor_codec.py`.
- Operator, acceptance and analysis tools: `tools/__init__.py`, `analysis.py`, `clean_run.py`, `compare_event_traces.py`, `eval_lm_harness.py`, `init_run.py`, `launch_independent_run.py`, `launch_phase1_acceptance.py`, `launch_phase2_acceptance.py`, `launch_phase2_matched.py`, `phase1_matched_performance.py`, `phase2_chaos_evidence.py`, `phase2_matched_evidence.py`, `phase2_test_evidence.py`, `publish_quality_gate.py`, `request_dynamic_close.py`, `run_metrics_csv.py`, `validation_eval.py`.

## Architecture and invariant assessment

The current source cleanly separates legacy writers, fenced HA writers, lease acquisition, read-only views, epoch-scoped publications, dynamic membership, scheduler reconciliation and terminal closure. Fenced business mutations revalidate the token inside `BEGIN IMMEDIATE`; the renewal thread and `LeaseSafetyTracker` cover the local monotonic boundary; canonical control paths are epoch/owner scoped; descriptor and source identity are checked before role startup. Dynamic membership keeps instance, placement and fixed virtual stream identities distinct; admission and merge transactions revalidate incarnation/stream state; launch requests bind physical scheduler identity before admission; merge/starvation capacity observations are atomic with their owning transition. Cleanup has exact-run and evidence gates and revalidates the inventory before unlinking.

The large `runtime/syncer.py`, `runtime/learner.py`, `storage/fenced_store.py` and `storage/sqlite_store.py` modules remain maintenance hotspots, but their responsibilities are separated internally and their state transitions have focused regression coverage; I found no additional actionable correctness defect in them at this target. `LeaderLeaseStore.assert_current()` currently has no production caller, but it is a bounded diagnostic assertion consistent with the active safety tracker rather than a competing mutation path, so I do not classify it as a removal-required finding.

The compatibility wrappers are intentionally thin, dependency direction remains from entry points/runtime toward protocol/storage/modeling, subprocess calls use argument vectors with timeouts, tensor payloads use safetensors and size/path validation, and SQLite analysis in `run_metrics_csv.py` already uses URI `mode=ro`. The retained Phase 0/1 and Phase 2 regression/formal artifacts provide broad recovery, concurrency, compatibility, boundedness and performance evidence; they do not excuse the analysis connection violation above.

## Final decision

**CHANGES_REQUIRED**

Remediate the single Medium read-only-boundary finding with a pre-fix failing test, run the affected analysis group and full repository tests, record the disposition, and perform the required incremental review because the fix tightens an operator/persistence safety boundary.
