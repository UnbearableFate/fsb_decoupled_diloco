# Plan 02 Phase 1 — Independent Code Review (Claude)

## 0. Review identity and provenance

| Field | Value |
| --- | --- |
| Review target commit | `b21b29f83067dd36b4ff5ac295fe1f894da64e21` |
| Comparison base commit | `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5` |
| Reviewed diff | `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5..b21b29f83067dd36b4ff5ac295fe1f894da64e21` (cumulative) |
| Merge base of the two commits | `1ba9a1a70e4ede6fdd5edf066f11f6921f111da5` (base is a strict ancestor; the diff is linear) |
| Target tree object | `cf1ad53dddf087878dcba5169853273954354a3d` |
| Branch at review time | `codex/fsb_decoupled_diloco_plan_02` |
| Repository | `/work/xg24i002/x10041/fsb_decoupled_diloco` |
| Working tree state during review | `HEAD` at the target commit; tracked files clean. One untracked file was present and was **not** opened: `reports/DOING/code_review/fsb_decoupled_diloco_plan_02/phase-1/gpt-5.6-sol_b21b29f…md` (a parallel reviewer's report). |
| Reviewer model | `claude-opus-5` (Claude Opus 5) |
| Invocation | `claude --print` (non-interactive print mode) |
| Session ID | `61659899-2111-42c0-af22-3ba0edc72c8c` |
| Permission mode | `bypassPermissions` |
| Reviewer authorization | Read-only review. Exactly one file was written: this report. No implementation, test, config, plan, other report, artifact, run data, Git state, or scheduler job was created, modified, submitted, cancelled, or deleted. No experiments were run. No credentials were used. |
| Prior sessions | None. This review was performed from a cold context; no prior Claude session was resumed and the work was not delegated to a subagent. |
| Other reviewers' conclusions | Not consulted for this target. The pre-existing phase-1 report for the *earlier* target `6042886…` was listed by directory scan but its findings were not read before forming the conclusions below; where this report references "Codex findings", the source is the implementer's own `progress.md`/`artifacts` disposition record, not the reviewer report. |

### Diff scope actually reviewed

`git diff --stat` for the range: **75 files changed, 14 422 insertions(+), 370 deletions(−)**.

Reviewed in full:

- **Storage**: `fs_diloco/storage/{schema_bootstrap,leader_lease,fenced_store,paths,maintenance,sqlite_store}.py`
- **Protocol**: `fs_diloco/protocol/control_epoch.py`
- **Runtime**: `fs_diloco/runtime/{syncer,syncer_ha,learner,launch_outbox,pbs_scheduler}.py`, plus the surviving `fs_diloco/runtime/adoption.py` stop-signal call sites
- **Core**: `fs_diloco/core/{config,constants,run_descriptor}.py`
- **Tools**: `fs_diloco/tools/{init_run,launch_independent_run}.py`
- **Checkers / probes**: `scripts/miyabi/check_plan02_phase1.py`, `plan02_phase1_fault_probe.py`, `plan02_phase1_lock_probe.py`
- **PBS**: `scripts/miyabi/run_plan02_phase1_{acceptance_launcher,checker,faults,lock,smoke,tests}.pbs`, `run_static_learner.pbs`, `run_syncer_candidate.pbs`
- **Config**: `configs/fs_diloco_tiny_ha_static{,_acceptance}.yaml`
- **Tests**: `tests/test_plan02_phase1_ha.py` (1 535 lines, 39 tests)
- **Plan / reports / docs**: requirement matrix, `plans/artifacts/plan02_phase1_mutator_inventory.json`, `reports/DOING/fsb_decoupled_diloco_plan_02/{progress.md,failures.md,artifacts/*}`, `README.md`, `docs/*`

### Verification commands executed by this review (read-only)

| Command | Result |
| --- | --- |
| `git diff --check 1ba9a1a..b21b29f` | clean (exit 0) |
| `bash -n scripts/miyabi/*.pbs` | clean (exit 0) |
| `.venv/bin/ruff check fs_diloco tests scripts/miyabi --output-format concise` | `All checks passed!` |
| `.venv/bin/python -m pytest tests/test_plan02_phase1_ha.py -q` | `39 passed in 74.90s` |
| `.venv/bin/python -m pytest tests -q` | `418 passed in 175.35s` |

**Fact.** The locally reproduced test counts (39 focused / 418 full) match the counts recorded in `artifacts/20260806-134242_phase1-review-remediation_pass.json`. The static gates (G1) reproduce clean.

---

## 1. Executive summary

**Fact.** The Phase 1 design is implemented substantially as specified: `init-run` is the sole DDL path; `open_existing`/`open_readonly` issue no DDL; `LeaderLeaseStore` produces monotonic, non-reused epochs; every HA business mutator is routed through `FencedSQLiteStore`/`LeaderBoundSQLiteStore` with an in-transaction `syncer_leader` re-check plus a local monotonic lease-safety boundary; checkpoints and canonical control artifacts are epoch- and publication-scoped; learners read the highest valid filesystem epoch and never open SQLite; recovery submission is off by default with claim/backoff/budget/reconciliation.

**Fact.** The 39 focused tests and the 418-test full suite pass at this commit, and the previously recorded 1+8 independent-job acceptance (`artifacts/20260806-130015_phase1-completed-checker_pass.json`) shows a genuine cross-job takeover (epoch 1 `SIGKILL` at `after_db_commit`, epoch 2 resuming from SQLite and committing v1–v10), contiguous epochs, contiguous versions 0–10, canonical/DB agreement, and zero failure events over 3 741 log events.

**Inference.** Two issues block approval:

1. A production error-handling gap makes the plan's own documented writer-lock recovery path (HA-11) unusable: a candidate that attempts to acquire while the old leader holds the SQLite writer lock dies with an unhandled `sqlite3.OperationalError` instead of waiting out `candidate_wait_seconds` (Finding H1). The same class of gap exists in the lease-renewal loop and in the fenced business connection (M1, M2).
2. The completed-Checker `PASS` evidence predates the reviewed code. `b21b29f` changed `control_epoch.py`, `learner.py`, `syncer.py`, `maintenance.py`, `syncer_ha.py` and `launch_independent_run.py` after that artifact was produced, and the only post-remediation runtime evidence is a 2-merge smoke that returned `PASS_WITH_FOLLOWUPS` (Finding H2). Plan §1.3/§15 do not accept `PASS_WITH_FOLLOWUPS` for the phase gate.

**Recommendation.** `CHANGES_REQUIRED`. Fix H1 (and, in the same change, M1/M2), then re-run the 1+8 acceptance ladder and completed Checker against the new frozen commit before marking HA-01…HA-20 `complete`.

---

## 2. Findings

Severity key — **Critical**: data loss / safety-invariant violation reachable in normal operation. **High**: blocks a stated Phase 1 acceptance criterion, or defeats a documented recovery path. **Medium**: real defect, plan-contract deviation, or material regression risk. **Low**: correctness-preserving quality, observability, or latent issues.

### Critical

None found. **Fact.** I specifically probed the safety-critical claims and could not falsify them by inspection:

- No path allows a superseded token to commit: every mutating statement goes through `_FencedConnection.execute`/`executemany` (`fs_diloco/storage/fenced_store.py:84-114`), which forces `BEGIN IMMEDIATE` (`:127-131`) and re-reads `syncer_leader WHERE epoch = ? AND owner_id = ? AND state = 'active'` plus the wall-clock lease safety margin (`:138-154`) before the write and again at `commit()` (`:116-119`).
- `FencedSQLiteStore.conn` raises `AttributeError` and `execute` is removed from the public API (`:233-238`); `__getattr__` allow-lists reads only (`:163-186`, `:246-249`).
- The `stamp_global_version_leader` / `stamp_update_selection_leader` / `stamp_update_terminal_leader` triggers (`fs_diloco/storage/schema_bootstrap.py:129-162`) call `current_leader_epoch()`/`current_leader_owner()`, which are only registered on the fenced connection (`fenced_store.py:56-57`) and raise without an active token (`:156-160`), so a raw insert on an unfenced connection fails closed.
- I audited all 34 mutating methods of `SQLiteStore` by AST for `commit`/`rollback`/`BEGIN`; every full-mode mutator reachable through `_mutate` terminates with `self.conn.commit()`, so no fenced call can leave the writer lock held after `deactivate()`.

### High

#### H1 — Candidate acquisition has no `SQLITE_BUSY` handling; the documented writer-lock recovery path kills the successor instead of waiting

**Fact.** `fs_diloco/runtime/syncer_ha.py:224-241`:

```python
if eligible:
    try:
        token = lease.acquire(...)
        ...
    except LeaseUnavailableError:
        pass
```

Only `LeaseUnavailableError` is caught. **Fact.** `LeaderLeaseStore.acquire` issues `BEGIN IMMEDIATE` as its very first statement (`fs_diloco/storage/leader_lease.py:156`) on a connection whose busy timeout is `lease_busy_timeout_ms` (`syncer_ha.py:193` → `leader_lease.py:110-115` → `schema_bootstrap.open_existing:339` `timeout=busy_timeout_ms/1000` and `:210` `PRAGMA busy_timeout`). Default 5 000 ms; the shipped HA configs use 3 000 ms.

**Fact.** When another process holds a write transaction, that `BEGIN IMMEDIATE` raises `sqlite3.OperationalError: database is locked`. This is exactly the behaviour the project's own probe asserts: `scripts/miyabi/plan02_phase1_lock_probe.py:190-206` wraps `contender.acquire(...)` in `except sqlite3.OperationalError` and requires the message to contain `locked`/`busy`. **Fact.** A repository-wide grep for `OperationalError` outside tests returns only that probe and the read-only guard at `fenced_store.py:870` — production HA code has no busy handler.

**Failure scenario.** The plan's §2.3 availability boundary and `docs/07-operations.md` §2.1 instruct the operator to: (a) leave the successor candidate running, (b) confirm and kill the old job holding the lock, (c) let the successor take over. With this code:

1. Old leader is `SIGSTOP`ped inside a business transaction and holds the SQLite `RESERVED` lock.
2. Its lease expires (it cannot renew).
3. The candidate's `lease.observe()` (a plain `SELECT`, not blocked by `RESERVED`) sees `now_wall > lease_expires_at + max_clock_skew` → `eligible = True` (`syncer_ha.py:218-222`).
4. `lease.acquire()` blocks for `lease_busy_timeout_ms` and raises `sqlite3.OperationalError`.
5. The exception escapes `acquire_candidate`, propagates through `run_syncer`'s `except BaseException as startup_error: … raise` (`fs_diloco/runtime/syncer.py:2578-2604`), and terminates the candidate process. `scripts/miyabi/run_syncer_candidate.pbs` has no `-r y`, so PBS does not requeue it.
6. When the operator later kills the stuck writer, no candidate is left. `candidate_wait_seconds` (default 180 s) and `learner_recovery_wait_seconds` (default 1800 s) never come into play.

**Inference (related, lower confidence).** The same short timeout also governs connection setup: `schema_bootstrap._configure_connection` (`:204-210`) executes `PRAGMA journal_mode=DELETE` and `PRAGMA synchronous=FULL` before installing the explicit `busy_timeout`, relying on the `connect(timeout=…)` handler, which is the same short value. `reports/DOING/.../failures.md` records that this precise class of startup-lock failure was already hit and remediated in the Phase 0 probe by installing a five-second *startup* busy timeout; the production path did not receive the analogous treatment. I could not falsify this by inspection alone.

**Coverage.** No test exercises `SQLITE_BUSY` against `acquire_candidate`, `LeaderLeaseStore.acquire`, or `LeaderLeaseStore.renew`. The two `pytest.raises(sqlite3.OperationalError)` occurrences in `tests/test_plan02_phase1_ha.py` (`:369`, `:1116`) are read-only-store assertions.

**Recommended fix.** In `acquire_candidate`, treat a busy/locked `sqlite3.OperationalError` from `acquire()` exactly like `LeaseUnavailableError` — log a `writer_lock_blocked` candidate event with the observed blocker (`epoch`, `owner_id`, `pbs_job_id`, `hostname`, `pid` from `lease.observe()`) and continue polling until `candidate_wait_seconds`. Distinguish it from non-busy `OperationalError` (which should stay fatal). Do the same for the connection-setup path, e.g. open `LeaderLeaseStore` with a bounded startup timeout independent of `lease_busy_timeout_ms`.

**Missing test.** RED test: a holder connection opens `BEGIN IMMEDIATE` and writes without committing; a candidate with a 250 ms busy timeout and an already-expired lease row must log `writer_lock_blocked` and keep polling until the holder rolls back/closes, then acquire epoch `N+1` — and must time out with `TimeoutError` (not `OperationalError`) if the holder never releases within `candidate_wait_seconds`.

#### H2 — The Phase 1 completed-Checker `PASS` evidence predates the reviewed code

**Fact.** `git show --stat b21b29f` shows the remediation commit changed runtime code: `fs_diloco/protocol/control_epoch.py` (+144), `fs_diloco/runtime/learner.py` (+74), `fs_diloco/runtime/syncer.py` (+86), `fs_diloco/runtime/syncer_ha.py` (+14), `fs_diloco/storage/maintenance.py` (+15/−…), `fs_diloco/tools/launch_independent_run.py` (+53), plus `tests/test_plan02_phase1_ha.py` (+301).

**Fact.** The only `phase1-completed` Checker artifact with `status: PASS` is `reports/DOING/fsb_decoupled_diloco_plan_02/artifacts/20260806-130015_phase1-completed-checker_pass.json`, for run `plan02_phase1_acceptance_2498481`. `progress.md` (entry `2026-08-06 13:00 JST`) records that run's frozen implementation as commit `24e181bc8031e68ae32310c628d56422b3d7654b`. The commit timeline is `24e181b 12:56` → `6042886 13:03` → `b21b29f 13:44`.

**Fact.** The post-remediation runtime evidence is `artifacts/20260806-134242_phase1-review-remediation_pass.json`: `smoke.checker = "PASS_WITH_FOLLOWUPS"`, `smoke.final_version = 2`, `tests.full.passed = 418`.

**Fact.** Plan §1.3 states `PASS_WITH_FOLLOWUPS` "只允许继续观察同一长作业，不允许开始 Phase 2"; plan §15 Phase 1 checklist requires "Phase 1 completed Checker `PASS`"; plan §7.3 G6 requires 1 syncer + 8 learner independent jobs, ≥1 real cross-job takeover, ≥10 committed merges. `check_plan02_phase1.py:254-264` enforces `latest_version >= 10` and `leader.state == 'released'` for `completed_ready`.

**Fact (mitigating).** The implementers did not overstate this. `plans/DOING/fsb_decoupled_diloco_plan_02-requirement-matrix.csv` marks HA-01…HA-20 `completion-candidate`, not `complete`, and `progress.md` explicitly records the decision and its rationale ("受影响runtime smoke与完整回归均已通过，因此无需重跑已经冻结通过的正式1+8 acceptance").

**Assessment (inference).** The rationale is partly sound — `launch_independent_run.py` is not on the recorded acceptance path (`run_plan02_phase1_acceptance_launcher.pbs` calls `init_run` and raw `qsub` directly), so that change is not covered by the 1+8 run either way. But `control_epoch.py` (learner watermarks, `canonical_repair_wait_seconds` consumption, `_observe_canonical_gap`), `learner.py` (watchdog/observation flow), `maintenance.py` (authority-temp GC grace) and `syncer.py` (startup ownership) all execute in the 1+8 topology, and the `maintenance.py` change was itself provoked by a **real race discovered only in a runtime smoke**, not by unit tests (`failures.md`, `2026-08-06 13:28 JST`: final maintenance unlinking a live heartbeat temp). Substituting a 2-merge single-node smoke for a 10-merge, 9-job, cross-node takeover is not equivalent coverage for exactly the code that changed.

**Recommended action.** Re-run the frozen ladder at the final Phase 1 target: G2 (full suite), G3 fault matrix, G4 two-node lock boundary, G5/G6 1+8 independent-job acceptance, and `check_plan02_phase1.py --mode phase1-completed` returning `PASS`; record the artifacts before flipping HA-01…HA-20 to `complete`. Note that H1's fix will itself change candidate startup behaviour and must be inside that frozen target.

### Medium

#### M1 — A single transient `SQLITE_BUSY` in the renewal loop aborts the leader

**Fact.** `fs_diloco/runtime/syncer_ha.py:152-155` calls `lease.renew(self.token)` with no retry; `:164-167` converts *any* `BaseException` into `self._failure`, sets `_stop`, and exits the loop. `fs_diloco/runtime/syncer.py:2793-2794` calls `lease_renewer.raise_if_failed()` at the top of every main-loop iteration, converting that into a fatal `RuntimeError`.

**Fact.** The renewal thread uses a **separate connection** (`syncer_ha.py:131-138`) to the same database, with `busy_timeout = lease_busy_timeout_ms`. The main thread holds the writer lock for the duration of each fenced business transaction.

**Failure scenario.** A `commit_full_merge` or `delete_archived_rows` transaction that exceeds `lease_busy_timeout_ms` (3 s in `configs/fs_diloco_tiny_ha_static*.yaml`, 5 s by default) while the renewal thread's timer fires ⇒ `sqlite3.OperationalError: database is locked` ⇒ `_failure` set ⇒ leader aborts on the next loop iteration **and** stops renewing, so a candidate can take over a healthy run. On a shared Lustre filesystem with `journal_mode=DELETE` + `synchronous=FULL`, multi-fsync commits under load are the realistic trigger. Note the design margin is thin by construction: plan §11.1's own target is "business transaction p99 < renew_interval/2" = 2 500 ms in the tiny config, against a 3 000 ms busy timeout.

**Assessment.** This fails *closed* (no double writer), so it is availability, not safety. But it converts a recoverable transient into a leadership change.

**Recommended fix.** In `LeaseRenewalThread._run`, catch busy/locked `OperationalError` from `renew()` and retry with jitter while `monotonic elapsed since last successful renew < lease_duration − max_clock_skew − renew_interval`; only escalate to `_failure` after that budget is exhausted or on `StaleLeaderTokenError`. Emit a `lease_renew_busy_retry` counter so plan §11.1's `lease_renew_failures=0` metric distinguishes retries from failures.

**Missing test.** RED test: a foreground holder keeps `BEGIN IMMEDIATE` open for `2 × lease_busy_timeout_ms` while the renewal thread runs; assert the thread retries, that `mark_renewed` still fires after the holder commits, and that `raise_if_failed()` does not raise.

#### M2 — Business/fenced transactions inherit the *lease* busy timeout

**Fact.** `fs_diloco/runtime/syncer_ha.py:266-275` constructs `FencedSQLiteStore(..., busy_timeout_ms=ha.lease_busy_timeout_ms)`, overriding the class default of `60_000` (`fenced_store.py:199`).

**Fact.** Plan §6.1/§6.3 scope `lease_busy_timeout_ms` to the renew path: "Renew：独立短超时 connection … `busy_timeout <= renew_interval`". The constraint `0 < lease_busy_timeout_ms <= renew_interval * 1000` is enforced in `fs_diloco/core/config.py` for that reason.

**Failure scenario.** During the takeover overlap window (old leader still finishing a transaction, new leader already holding the lease), any business transaction the new leader attempts fails after 3–5 s with an unhandled `OperationalError`, reaching `run_syncer`'s `except Exception:` (`syncer.py:3201-3204`), setting `stop_reason="error"` and terminating the run.

**Recommended fix.** Use a separate, longer `business_busy_timeout_ms` (default `60_000`, or derive it from `lease_duration_seconds`) for `FencedSQLiteStore`, and keep `lease_busy_timeout_ms` for `LeaderLeaseStore` only. Add the corresponding validator.

#### M3 — The HA learner performs 2–3 full epoch-directory scans per inner training step and lost the cheap deadline short-circuit

**Fact.** In `run_learner`, `stop_requested(paths, local_step, config)` is evaluated once per outer cycle (`fs_diloco/runtime/learner.py:2429`) **and once per inner step** (`:2442`), and `confirm_syncer_unresponsive(...)` is called once per inner step (`:2568`).

**Fact.** `stop_requested` now calls `read_authoritative_terminal` (`:640`), and the HA branch of `confirm_syncer_unresponsive` calls `reader.read_current_heartbeat()` and `read_authoritative_terminal(paths)` (`:759-768`) — three `EpochControlReader` entry points per inner step, each of which independently re-runs `_scan_current_epoch()`.

**Fact.** `_scan_current_epoch` (`fs_diloco/protocol/control_epoch.py:357-398`) iterates *every* directory under `control/syncer_epochs` (bounded by `max_retained_epoch_dirs`, default 32) and for each: reads and re-serializes+SHA-256s `heartbeat.json` (`:286-311`), reads `latest/head.json`, then reads and SHA-256s `latest/vNNNNNN.json` (`:313-355`). `read_current_terminal` additionally globs `terminal/stop_g*.json` and hashes each match (`:519-538`). There is no cache or TTL.

**Fact (regression).** The legacy branch short-circuits cheaply: `if not watchdog.deadline_reached(...): return False` (`learner.py:780`). The HA branch at `:747-778` returns *before* reaching that line and therefore performs the scans unconditionally, every inner step, regardless of whether any deadline was near. Previously the per-step cost was a single `paths.stop_json.exists()` `stat()`.

**Fact.** No control-plane critical-path measurement exists in the Phase 1 artifacts, although plan §11.1 requires "lease/heartbeat控制面 CPU time和阻塞 critical-path wall time分别报告" and that publish/commit regression thresholds be frozen against a matched Plan 01 baseline. The recorded acceptance used `inner_steps: 2`, `poll_latest_during_inner_steps: false` (`configs/fs_diloco_tiny_ha_static_acceptance.yaml:65,100`), which minimises exposure; the repository's documented 50-local-step × 10-global-step baseline would multiply it by ~25.

**Recommended fix.** Give `EpochControlReader` a single per-call scan cache keyed on a monotonic deadline (e.g. `min(heartbeat_interval, stop_file_poll_seconds)`), so one poll interval costs one scan instead of three; restore a cheap short-circuit in the HA branch of `confirm_syncer_unresponsive` (only scan when `now − last_heartbeat_monotonic >= heartbeat_stale_after_seconds`, or when the caller's watchdog deadline is within one poll interval).

**Missing test/measurement.** A benchmark artifact reporting learner control-plane wall time per inner step, HA vs. matched non-HA baseline, against a frozen threshold — this is a plan §11.1 deliverable that is currently absent.

#### M4 — `EpochControlReader` fails hard on any stale epoch directory, so a partially garbage-collected old epoch aborts a healthy learner

**Fact.** `_scan_current_epoch` validates *all* epoch directories, not just the highest, and raises on any inconsistency:

- `control_epoch.py:293-298`: `path.is_file()` → then `safe_read_json(path)`; `safe_read_json` returns `None` on `OSError` (`fs_diloco/storage/atomic_io.py:77-81`), and the caller then raises `RuntimeError(f"invalid syncer heartbeat JSON: {path}")`.
- `control_epoch.py:345` → `_verified_json` → `path.read_bytes()` (`:62`) with no guard, so a missing pointer file raises a raw `FileNotFoundError`.

**Fact.** `maintenance.archive_ha_history` tears down old epoch directories non-atomically: it unlinks each archived publication artifact (`fs_diloco/storage/maintenance.py:64-69`), then walks `directory.rglob("*")` unlinking files and `rmdir`-ing (`:79-91`).

**Failure scenario.** After `max_retained_epoch_dirs` takeovers (8 in the shipped HA configs, 32 by default), the leader's maintenance begins deleting old epoch directories. A learner scanning concurrently observes an epoch directory in which `heartbeat.json` has been unlinked but `latest/head.json` survives (or vice versa) and raises out of `stop_requested()` — which is evaluated in the `while` condition at `learner.py:2429`, so the exception is not caught and the learner terminates, even though the current epoch is healthy and authoritative.

**Assessment.** Only the highest epoch is authoritative (`control_epoch.py:394-398`), so strict validation of stale directories buys nothing and costs availability.

**Recommended fix.** Sort candidate directories by epoch descending and validate lazily, accepting the first that validates; treat validation failures in strictly-lower epochs as skip-with-counter (`stale_epoch_scan_rejected_count`) rather than raise. Keep fail-closed behaviour for the *highest* epoch. Also make `_verified_json` tolerate `FileNotFoundError` by returning `None` for non-current epochs.

**Missing test.** RED test: create epochs 1 and 2, delete `e000001_*/latest/v000000.json` while leaving its `head.json`, and assert `read_current_latest()` still returns epoch 2's payload without raising; repeat with `heartbeat.json` removed from the stale directory.

#### M5 — Old-epoch orphan deletion bypasses the `gc_candidates` ledger and the per-file fenced re-check the plan freezes

**Fact.** Plan §6.4: "maintenance禁止'目录扫描结果减DB引用后立即unlink'。明确orphan写入 `gc_candidates` … deletion worker每个文件删除前用短fenced transaction重新校验token、candidate状态、grace和全部DB引用，失败即停止本轮."

**Fact.** `fs_diloco/storage/maintenance.py:197-216` does precisely the forbidden pattern for old-epoch artifacts: it iterates `paths.iter_epoch_weights()` / `iter_epoch_optim()`, subtracts an in-memory `keep_checkpoints` (computed once at `:151-168`) and `ledger_paths` (`:192-195`), and calls `_unlink(artifact)` directly, guarded only by `artifact_epoch >= current_epoch` and an mtime age check.

**Fact.** For ledger-driven deletions, the fenced re-check is per *batch*, not per file: `claim_ready_gc_candidates` re-validates references for all claimed rows in one transaction (`fs_diloco/storage/fenced_store.py:554-586`), then `collect_runtime_artifacts` unlinks and only afterwards calls `complete_gc_candidate` (`maintenance.py:181-188`).

**Assessment (inference).** I traced the pause-mid-GC scenario and could not construct a case where a current checkpoint is deleted: the `artifact_epoch >= current_epoch` guard protects the current and all newer epochs, and any older-epoch file still referenced by the DB is in `keep_checkpoints` because versions only advance. So this is **not** currently a data-loss defect. It is a deviation from a frozen plan contract, and the safety argument now rests on epoch monotonicity rather than the specified re-validation, which is fragile against future changes (e.g. any code that lets a leader reference a strictly-older-epoch path it did not observe at scan start).

**Recommended fix.** Either (a) route old-epoch orphans through `gc_candidates` with a reconciler-registered `not_before = lease_duration + max_clock_skew` as the plan specifies, or (b) if the epoch-guard shortcut is intentionally retained, amend the plan/design record explicitly and add a fenced `assert_safe`/token re-check immediately before each `_unlink` so the "fails ⇒ stop this round" contract holds.

**Missing test.** The existing `test_stale_gc_after_takeover_deletes_only_frozen_orphans` covers the ledger path. Add a case for the *direct* old-epoch orphan loop: pause after `keep_checkpoints` is computed, let a successor commit a new version whose predecessor lives in an older epoch directory, resume the stale leader, and assert both the successor's current weight/outer files and their sizes are unchanged **and** that `ha_old_epoch_orphans > 0`.

#### M6 — The shared recursive-discovery contract was not adopted outside maintenance/Checker; HA runs are invisible to existing tooling

**Fact.** Plan §6.4 and HA-05 freeze that "maintenance、Plan 01 Checker、publication crash probe、liveness、analysis和metrics不得保留自有glob" and that no scan may silently return empty.

**Fact.** Only `fs_diloco/storage/maintenance.py` and `scripts/miyabi/check_plan02_phase1.py` were migrated. Still using their own non-epoch-aware globs / legacy paths:

| Location | Behaviour on an HA run |
| --- | --- |
| `scripts/miyabi/check_plan01_invariants.py:226,235,236` — `(root/"weights").glob("*.safetensors")` | Empty (HA checkpoints live under `weights/epochs/eNNNNNN/<owner>/`) |
| `scripts/miyabi/check_plan01_invariants.py:279` — `read_jsonl(root/"logs"/"syncer.jsonl")` | File absent → `read_jsonl` returns `[]` at `:24-25` → the `{"error","no_progress_timeout","db_dumped"}` invariant passes **vacuously** |
| `scripts/miyabi/publication_crash_probe.py:356,358` | Asserts exactly one file in `paths.weights` / `paths.optim` → cannot describe HA runs |
| `fs_diloco/tools/analysis.py:546`, `fs_diloco/tools/run_metrics_csv.py:247` — `logs/syncer.jsonl` | Absent; HA writes `logs/syncers/eNNNNNN_<owner-short>.jsonl` (`fs_diloco/runtime/syncer.py:2563-2568`) → zero syncer events/metrics silently |
| `fs_diloco/tools/analysis.py:246,498`, `run_metrics_csv.py:300`, `fs_diloco/protocol/liveness.py:68,95` — `learner_*` globs | Work for Phase 1 static IDs; break for Phase 2 `learner_li_<uuid4>` |

**Fact.** `artifacts/20260806-134242_phase1-review-remediation_pass.json` records this as `"recursive_discovery_contract": "deferred-with-justification to Phase 2 MEM-02/MEM-20"`, and `progress.md` argues the Phase 1 static paths are non-empty and correct.

**Assessment (inference).** The deferral is defensible for the *learner-identity* surfaces (`learner_*` globs), which genuinely need the Phase 2 admission validator to migrate coherently. It is **not** defensible for the two Phase-1-only regressions: the syncer log path moved in this diff, and the epoch checkpoint layout was introduced in this diff. Those two produce silent empty results today, which is the specific failure mode the plan forbids.

**Recommended fix (scoped, does not pre-empt Phase 2).** Add `RunPaths.iter_syncer_logs()` returning `logs/syncer.jsonl` ∪ `logs/syncers/*.jsonl` and use it in `analysis.py:546`, `run_metrics_csv.py:247`, `check_plan01_invariants.py:279`. Make `check_plan01_invariants.py` and `publication_crash_probe.py` use `paths.iter_epoch_weights()`/`iter_epoch_optim()` when `control/bootstrap_complete.json` exists, and fail closed (rather than pass vacuously) when an expected-non-empty scan returns zero. Keep the learner-identity globs deferred, and record that narrowed deferral in the plan.

**Missing test.** Assert non-emptiness for each migrated surface against a synthetic HA run root, per plan §6.4 ("每个测试必须断言期望非空的扫描面确实非空").

#### M7 — Fixed convenience caches still drive control flow in the HA learner

**Fact.** Plan §6.5: "`control/stop.json` 的'文件存在'不是停止依据；必须通过最高epoch、owner、generation和canonical hash校验."

**Fact.** `fs_diloco/runtime/adoption.py:441`, `:540`, `:579` still test `ctx.paths.stop_json.exists()`. These strategies are mode-independent and are used by HA learners.

**Fact.** `EpochControlPublisher.publish_terminal` mirrors every terminal generation — including `stop_reason="error"` — into `control/stop.json` (`fs_diloco/protocol/control_epoch.py:211`), while `read_authoritative_terminal` deliberately filters `error` as non-final (`fs_diloco/runtime/learner.py:352-358`).

**Failure scenario.** An HA leader exits with `stop_reason="error"` (the resumable diagnostic case documented in `docs/07-operations.md`). `stop_requested()` correctly returns `False` and the learner keeps training. But `GlobalPredictionStrategy.on_cycle_end` (`adoption.py:539-542`) now sees `stop_json.exists()` and silently calls `on_stop(ctx)`, discarding the in-flight prediction; `RebaseAdoptionStrategy.on_after_publish` (`adoption.py:441-447`) silently skips its anchor. A stale old-epoch writer can also recreate this file at any time (this is the explicitly-allowed pollution mode).

**Recommended fix.** Replace the three `ctx.paths.stop_json.exists()` checks with a `ctx.terminal_published()` hook backed by `read_authoritative_terminal(paths)` in HA mode and by the fixed-cache check in legacy mode.

**Missing test.** Publish an `error` terminal generation for the current epoch, then assert that neither adoption strategy takes its stop branch; separately, write an arbitrary `control/stop.json` from a simulated stale epoch and assert no adoption-path behaviour change.

#### M8 — The formal acceptance launcher hard-codes `--allow-dirty-snapshot` without creating an immutable snapshot

**Fact.** `scripts/miyabi/run_plan02_phase1_acceptance_launcher.pbs:40` passes `--allow-dirty-snapshot` unconditionally.

**Fact.** `fs_diloco/tools/init_run.py:43-44` gates only on the flag (`if config.run.git_dirty is not False and not allow_dirty_snapshot: raise`); nothing creates a snapshot, and the PBS role scripts fingerprint the live `$PROJECT_ROOT` at start-up (`run_syncer_candidate.pbs:38-53`, `run_static_learner.pbs:40-55`) with no re-verification during the run.

**Fact.** Plan §4.3: "正式 HA run默认要求 clean commit。若确需 dirty source，必须先创建不可变快照并以快照 hash作为 source identity，不能引用继续变化的主工作树."

**Assessment.** The recorded acceptance ran on clean commit `24e181b`, so the flag was a no-op there and HA-16 evidence is not invalidated. The defect is that the committed acceptance script will silently accept a mutable dirty worktree on any future run, which is exactly the identity guarantee HA-16 exists to provide.

**Recommended fix.** Remove `--allow-dirty-snapshot` from the acceptance launcher and let it fail closed on a dirty tree; if dirty acceptance is ever needed, add an explicit snapshot step (`git worktree add` at the target commit, or a read-only copy) and fingerprint the snapshot.

#### M9 — Test-coverage gap: the Phase 1 HA-16 gate (`load_run_descriptor`) has no direct test

**Fact.** `fs_diloco/core/run_descriptor.py` is the production source/config/descriptor gate for HA (called at `fs_diloco/runtime/syncer.py:2535-2547` and `fs_diloco/runtime/learner.py:2229-2416`). It has thirteen distinct rejection branches: `:45-46` (self-checksum), `:47-51` (submitted-identity mismatch), `:64-70` (run_id / git_commit / source_fingerprint / shared_root / protocol / schema / mode), `:73-76` (path escape), `:77-80` (config & manifest checksums), `:82-83` (manifest self-checksum), `:84-86` (manifest↔descriptor), `:88-95` (resolved config ↔ descriptor).

**Fact.** A repository-wide grep shows `load_run_descriptor` appears in `tests/` only as a `monkeypatch.setattr` target (`tests/test_plan02_phase1_ha.py:225`, `:294`). None of its branches is exercised. The Phase 0 tests (`tests/test_plan02_feasibility.py:579-640`) cover a **different** implementation, `scripts/miyabi/plan02_source_gate.py`.

**Recommended fix / missing tests.** A parametrised RED suite over a real `init_run`-produced run root that mutates, one at a time: `descriptor_sha256`, `run_id`, `git_commit`, `source_fingerprint`, `git_dirty`, `protocol_version`, `schema_version`, `mode`, `resolved_config_path` (escape), the resolved config bytes, and the manifest bytes — asserting `RuntimeError` in each case **and** asserting that no `syncer_leader` row and no `syncer_epochs` row was created.

### Low

| ID | Finding | Evidence | Recommendation |
| --- | --- | --- | --- |
| L1 | Terminal GC candidates are never collected, so completed HA runs retain superseded checkpoint binaries. `claim_ready_gc_candidates` requires `not_before <= now` with `not_before = recorded_at + gc_grace_seconds` (`fenced_store.py:542`); `input_closed=True` only zeroes the *orphan* grace (`maintenance.py:316-318`). The recorded acceptance ends with `gc_candidate_rows: 20` and `epoch_weights observed: 11` for one active version. | `artifacts/20260806-130015_phase1-completed-checker_pass.json` (`boundedness`, `discovery`) | At terminal, after the final `finalize_terminal_state`, run one bounded collection pass that waits out `gc_grace_seconds` once (or forces `not_before = now` when the lease is confirmed released), so completed runs converge to current-only retention as in legacy. Add a Checker field asserting `epoch_weights == active_versions` for `phase1-completed`. |
| L2 | `find_by_request_fingerprint` matches `FS_DILOCO_RECOVERY_REQUEST=<key>:<n>` as a substring of `Variable_List` (`pbs_scheduler.py:204-206`); attempt `…:1`'s marker is a prefix of attempt `…:10`'s. Safe at the default `max_attempts_per_observation = 3`; wrong-job reconciliation becomes possible at ≥10. | `pbs_scheduler.py:204-206`; `launch_outbox.py:125` | Anchor the match on a delimiter (`f"{marker},"` / end-of-value) or compare parsed `Variable_List` key/value pairs. |
| L3 | The `syncer_recovery_exhausted` event logs `timeout_seconds=syncer_watchdog.timeout_seconds`, which in HA is `no_progress_timeout_seconds` — not the `learner_recovery_wait_seconds` actually used by the HA decision. Misleading evidence in the artifact the Checker scans. | `learner.py:2575-2586` vs. `:773-777` | Log the effective HA budget and the elapsed heartbeat-stall time. |
| L4 | `LeaderLeaseStore.assert_current` is dead code in production; its `_last_successful_renew` map on the main-thread lease store is never refreshed (renewals happen on the renewal thread's own store), so a future caller gets a spurious `StaleLeaderTokenError` after `lease_duration − max_clock_skew`. | `leader_lease.py:280-293`; `syncer_ha.py:131-138` | Delete it, or make it consult the shared `LeaseSafetyTracker` instead of a per-store map. |
| L5 | `collect_runtime_artifacts` reads the non-authoritative `control/latest.json` to derive `materialized_weight_path` inside a GC decision path, contrary to HA-06. Harmless today (HA `latest` payloads have no such key, and the only effect would be extra retention). | `maintenance.py:169-172`; `control_epoch.py:121-167` | Guard the read behind `not isinstance(store, LeaderBoundSQLiteStore)`, or source the materialised path from the DB. |
| L6 | Legacy behaviour change not required by the plan: `init_wandb_run` moved from before `initialize_run`/`resume_run` to after it, for the **non-HA** full path as well, so legacy runs no longer record init/resume-phase failures in W&B. (Plan §6.6 mandates the new ordering for HA only; HA-19 concerns math/CLI/results, so this is not a numerical regression.) | `syncer.py` diff at `run_syncer`, `wandb_run = None` + `:2732-2739` | Either restore the legacy ordering under `if not ha_mode`, or record the delta explicitly against HA-19. |
| L7 | `check_plan02_phase1.py` defaults `--output` into the **live run root** (`args.run_root / "reports" / "phase1" / …`), while plan §2.4 says the Checker only reads the live run. The PBS wrapper always passes `--output`, so the default is the only exposure. | `scripts/miyabi/check_plan02_phase1.py:411-415` | Make `--output` required, or default outside the run root. |
| L8 | Every normal HA completion writes **two** terminal generations (`stop_g000001.json`, then `stop_g000002.json` + `summary_g000002.json`), and each `repair_completed_ha_terminal` adds another. Bounded and internally consistent, but generation numbers no longer map 1:1 to logical stop decisions, which complicates reading `control_publications`. | `syncer.py:3207-3221`, `:3278-3287`; `:3193-3214` (repair) | Document the two-phase generation contract in `docs/modules/protocol.md`, or publish the early stop under the same generation and record only the summary as a second publication row. |
| L9 | `_keyword()` classifies a statement by its first token and only strips `--` comments, so a `WITH … INSERT/UPDATE/DELETE` CTE or a `/* … */`-prefixed statement would skip `_ensure_write_transaction` and execute unfenced. No such statement exists in the codebase today. | `fenced_store.py:24-29`, `:84-114` | Add an explicit allow-list check (raise on any unrecognised leading keyword) so new statement forms fail closed rather than silently bypassing the fence. |

---

## 3. Correctness and regression risk

**Fact — what is preserved.** `LegacySQLiteStore` is a literal alias of `SQLiteStore` (`fenced_store.py:17`). The three touched legacy mutators (`upsert_global_version`, `initialize_full_run`, `commit_full_merge`) keep their original SQL byte-for-byte on the `publication_id is None` branch; the HA columns are only written when `publication_id` is supplied, which only `FencedSQLiteStore` does (`fenced_store.py:335-399`). Legacy `prepare_run_dirs` and the legacy `config.init.resume` + missing-DB `FileNotFoundError` guard are unchanged. The full 418-test suite passes locally.

**Fact — behaviour changes on the legacy path.** (a) W&B initialisation moved after `initialize_run`/`resume_run` for non-HA full runs (L6). (b) A new `except Exception:` handler around init/resume now closes the store before re-raising — a strict improvement.

**Fact — behaviour changes on the HA path that carry regression risk.**

- `resume_requested = store.committed_global_count() > 0` in HA mode (`syncer.py:2705`) makes `config.init.resume` inert; any HA process finding a committed version resumes. This is correct for takeover but means a mis-targeted `--shared-root` silently resumes rather than failing.
- The learner's per-step stop/liveness check changed from one `stat()` to up to three hashed directory scans (M3).
- `paths.latest_json` in HA carries the epoch-control payload schema (`control_epoch.py:121-167`), not the legacy `latest_payload` schema. Any consumer that still parses it as legacy (`eval_lm_harness.py:55,149`) sees a different shape.

**Inference — highest residual risk.** The lease/writer-lock error paths (H1, M1, M2). All three fail closed with respect to safety, but together they mean that any SQLite contention lasting longer than 3–5 s either kills the leader or kills the successor. That is the opposite of the availability property Phase 1 exists to deliver, and it is unexercised by the current test suite.

---

## 4. Error handling

**Fact — strengths.**

- Startup ownership: `run_syncer`'s `except BaseException as startup_error` block (`syncer.py:2578-2604`) releases the lease renewer, leader store, and lease in order, attaching each cleanup failure as a note rather than masking the original error. `LeaseRenewalThread.start` sets `_stop` before raising if the thread did not start within 10 s (`syncer_ha.py:91-105`), and `stop()` tolerates a thread that never started (`:114-125`).
- `launch_independent_run` preserves the accepted syncer receipt when the learner array `qsub` fails, returns `submission_status: "partial"`, exits non-zero, and explicitly does not auto-`qdel` (`launch_independent_run.py:128-145`, `main:177-178`) — matching plan §1.5 and §14.
- `pbs_scheduler` converts `OSError`/`TimeoutExpired` into auditable observations rather than exceptions (`pbs_scheduler.py:113-120`, `:169-176`, `:200-201`).
- `_publish_immutable_json` uses `os.link` and treats `FileExistsError` with identical bytes as success, different bytes as a hard collision (`control_epoch.py:45-50`).
- Publication collision is fail-closed before any write (`syncer.py:106-107`).

**Fact — gaps.**

- No `SQLITE_BUSY` handling anywhere in production HA code (H1, M1, M2).
- `EpochControlReader` raises on stale-epoch inconsistencies, and those exceptions surface in the learner's `while` condition, which has no handler (M4).
- The syncer's outer `finally` block (`syncer.py:3205-3300`) contains a bare `try:` with only an inner `finally:` and no `except`. If `finalize_terminal_state` raises `StaleLeaderTokenError` (leader lost its lease mid-run), that exception replaces the original failure and skips `wait_for_learner_shutdown`, `write_training_summary`, and terminal publication. This is the correct *safety* outcome — a stale leader must not write — but the operator-visible error becomes the fencing error rather than the root cause. Recommend catching and logging `StaleLeaderTokenError` there explicitly (`logger.event("terminal_publication_fenced", …)`) so the root cause survives.

---

## 5. Concurrency and persistence invariants

I checked each Phase 1 invariant against the code. `✔` = satisfied by inspection; `~` = satisfied with a caveat; `✘` = not satisfied.

| ID | Verdict | Evidence / caveat |
| --- | --- | --- |
| HA-01 monotonic, non-reused epochs | ✔ | `leader_lease.py:158-234`: `epoch = current.epoch + 1` inside `BEGIN IMMEDIATE`; `syncer_epochs.epoch` is `PRIMARY KEY` so reuse fails. |
| HA-02 stale token cannot commit | ✔ | `_FencedConnection._verify_token` before the first write, at every write, and at `commit()` (`fenced_store.py:97-119`, `:138-154`), plus `LeaseSafetyTracker.assert_safe` on the local monotonic clock (`leader_lease.py:57-63`). |
| HA-03 all HA mutators fenced | ✔ | 25 explicit fenced wrappers (`fenced_store.py:332-785`), `_BOUND_MUTATORS` (`:788-814`), raw `conn`/`execute` closed (`:233-238`), read allow-list (`:163-186`). Inventory frozen at 31 and enforced by the Checker (`check_plan02_phase1.py:58-67`). Caveat: L9. |
| HA-04 unique, contiguous versions with writer identity | ✔ | `idx_global_versions_publication_id` unique partial index (`schema_bootstrap.py:123-125`); `commit_full_merge` predecessor check; Checker contiguity + per-version epoch/owner/publication assertions (`check_plan02_phase1.py:131-191`). |
| HA-05 epoch isolation; no silent-empty scans | ~ | Isolation ✔ (`paths.py:191-211`, `:167-189`). Scan-contract only partially adopted; two Phase-1-only surfaces silently return empty — see M6. |
| HA-06 fixed caches non-authoritative | ~ | Readers ✔ (`control_epoch.py:237-547`; `learner.py:337-358`). But `adoption.py:441,540,579` and `maintenance.py:169` still consult the caches — M7, L5. |
| HA-07 DB-first takeover, N→N+1 | ✔ | `resume_run` reads `latest_global_version()` and resolves relative paths against `shared_root` (`syncer.py:928-931`); `resume_requested` from `committed_global_count()` (`:2705`). Confirmed empirically by the recorded acceptance (epoch 1 → v0, epoch 2 → v1…v10). |
| HA-08 post-commit/pre-publish crash repairable | ✔ | `EpochControlPublisher.repair_latest_from_db` (`control_epoch.py:169-173`); `publish_latest` uses the stable DB `created_at` so same-epoch re-publication is byte-identical (`:137-139`). Fault matrix covers all six failpoints × 10 iterations. |
| HA-09 heartbeat-based watchdog | ~ | Design ✔: liveness comes only from `heartbeat_seq` advancing on the learner's monotonic clock (`learner.py:709-733`), with `syncer_recovery_exhausted` as a distinct reason (`:2576-2580`). Caveats: M3 (cost, lost short-circuit), L3 (misleading logged timeout). |
| HA-10 expired owner cannot renew or start work | ✔ | `renew` rejects `now > lease_expires_at` (`leader_lease.py:251-252`) and requires an exact epoch+owner+`state='active'` row (`:258-263`). |
| HA-11 in-transaction pause blocks takeover without dual writers | ~ | Serialization ✔ (single `BEGIN IMMEDIATE` per DB) and demonstrated by the two-node probe. But the **production** candidate cannot survive the blocked window — H1. |
| HA-12 old epoch cannot clobber/GC current | ~ | Path isolation ✔; the epoch guard prevents deletion of current/newer artifacts. But the deletion protocol deviates from the frozen contract — M5. |
| HA-13 claim/qsub is not leadership | ✔ | `RecoveryClaimManager` never touches the lease; candidates always go through `acquire_candidate` (`launch_outbox.py` has no lease import). |
| HA-14 one `mkdir` winner per attempt | ✔ | `attempt_dir.mkdir(parents=True, exist_ok=False)` as the arbitration point (`launch_outbox.py:111-117`); covered by `test_eight_recovery_claimants_create_only_one_attempt_and_submission`. |
| HA-15 reconciliation / backoff / budget; queued jobs stay reserved | ✔ | `_outstanding_attempts` keeps queued/prologue/running/suspended/`submission_unknown` outstanding regardless of TTL and reconciles the receipt-missing window by fingerprint (`launch_outbox.py:152-194`); `_archive_expired_claims` refuses to archive an outstanding attempt (`:275-276`) and preserves the current observation's budget (`:265-266`). Caveat: L2. |
| HA-16 source/config/descriptor gate | ~ | Implemented at `run_descriptor.py:33-102` and enforced pre-import in both PBS role scripts. But it has no direct test (M9) and the acceptance launcher weakens it (M8). |
| HA-17 single-writer logs | ✔ | Candidate log keyed by `owner_short` (`syncer_ha.py:183-186`); syncer log keyed by `eNNNNNN_<owner-short>` (`syncer.py:2563-2568`). |
| HA-18 controlled DDL; fail-closed compatibility | ✔ | `initialize_new_run` refuses an existing DB/marker (`schema_bootstrap.py:237-240`); `open_existing` validates marker ⋅ `schema_meta` ⋅ `run_state` ⋅ `PRAGMA user_version` and issues no DDL (`:328-367`); `open_readonly` uses `mode=ro` + `query_only` (`:370-382`); fragment+HA rejected in config (`config.py:523-525`); `init_run` refuses an existing run root (`init_run.py:48-51`). Note: plan §4.2 item 2 contemplated an explicit incomplete-bootstrap recovery path; the implementation always fails closed instead, which is a stricter subset and is acceptable. |
| HA-19 legacy regression | ~ | Preserved by construction (alias + `publication_id is None` branches) and by the full suite. No dedicated matched-run byte-for-byte artifact exists; the matrix cites only the test-suite artifact. L6 is an unflagged legacy delta. |
| HA-20 bounded active state | ~ | `archive_ha_history` bounds epoch rows/dirs (`maintenance.py:50-92`); `_archive_expired_claims` bounds claims; `test_1000_takeover_and_claim_cycles_keep_active_surfaces_bounded` passes. But completed runs retain up-to-grace-window orphan binaries (L1), and the Checker only asserts a *lower* bound on discovery counts (`check_plan02_phase1.py:206-210`). |

**Persistence.** `journal_mode=DELETE` + `synchronous=FULL` are asserted at every open (`schema_bootstrap.py:206-210`) and re-verified by the Checker. Archive-before-delete ordering with `fsync` is correct in `archive_and_prune` (`maintenance.py:36-38`) and `archive_ha_history` (`:61-62`). `initialize_new_run` builds into a temp file, `integrity_check`s, `os.replace`s, `fsync`s the directory, and only then publishes the marker (`schema_bootstrap.py:243-295`) — correct ordering. `archive_ha_history` deletes DB rows before unlinking files, so a crash between the two leaves orphan files with no rows; this self-heals because the directory sweep is recomputed from `before_epoch` each pass, which only grows.

---

## 6. Test coverage

**Fact.** `tests/test_plan02_phase1_ha.py` contains 39 tests, all passing. Mapping to plan §7.2 groups:

| Group | Coverage | Assessment |
| --- | --- | --- |
| LEASE | `test_lease_epoch_is_monotonic_and_stale_owner_cannot_renew`, `test_concurrent_first_acquire_has_exactly_one_winner`, `test_1000_takeover_and_claim_cycles_keep_active_surfaces_bounded` | Good. Missing: busy-lock behaviour (H1/M1). |
| SCHEMA | `test_schema_bootstrap_has_double_version_identity_and_readonly_open`, `test_incomplete_or_pre_ha_database_fails_closed`, `test_ha_initializer_writes_identical_root_and_control_config`, `test_ha_config_defaults_and_artifact_versions` | Good. |
| FENCE | `test_all_fenced_public_mutators_require_token` (signature-level, via `inspect`), `test_fenced_store_rejects_raw_and_superseded_writes`, `test_fenced_store_enforces_local_monotonic_lease_boundary`, `test_fenced_named_parameter_mutation_preserves_metadata_values`, `test_readonly_store_rejects_mutating_surface` | Good. Missing: a CTE/comment-prefixed statement negative test (L9). |
| DIRS | `test_learner_directory_creation_does_not_create_authority` | Adequate. |
| PUB | `test_epoch_control_ignores_fixed_cache_pollution_and_repairs_takeover`, `test_ha_checkpoint_digest_modes_preserve_publication_contract` | Good. |
| GLOB | Indirect only, via the Checker's `discovery` block, which asserts a lower bound of 1 | Weak; see M6. |
| GC | `test_ha_gc_registers_then_rechecks_and_deletes_only_archived_publication`, `test_stale_gc_after_takeover_deletes_only_frozen_orphans`, `test_ha_gc_removes_only_unreferenced_superseded_epoch_orphans`, `test_ha_maintenance_does_not_delete_current_authority_writer_temp` | Good for the ledger path; the direct old-epoch loop is untested for the pause-mid-scan case (M5). |
| WATCHDOG | `test_ha_watchdog_uses_heartbeat_progress_and_recovery_budget_not_model_merges` | Good. |
| LOCK | **Not in pytest** — covered by `scripts/miyabi/plan02_phase1_lock_probe.py` + `artifacts/20260806-125404_phase1-lock-boundary_pass.json` | Acceptable as probe-based G4 evidence, but it means the production candidate's busy-lock path is untested (H1). |
| CLAIM | `test_recovery_claim_has_one_mkdir_winner_and_queued_job_stays_outstanding`, `test_eight_recovery_claimants_create_only_one_attempt_and_submission`, `test_recovery_reconciles_historical_scheduler_state_before_retry`, `test_recovery_archival_does_not_reset_current_observation_budget`, `test_pbs_scheduler_failures_are_nonfatal_observations` | Strong. |
| SOURCE | **Absent for the Phase 1 implementation** — see M9 | Gap. |
| TERMINAL | `test_incomplete_completed_terminal_is_repaired_before_future_rejection`, `test_independent_launcher_preserves_syncer_receipt_when_learner_qsub_fails`, `test_syncer_releases_acquired_lease_when_leader_store_open_fails`, `test_syncer_cleans_all_acquired_resources_when_renewer_start_fails` | Good. |
| BOUNDED | `test_1000_takeover_and_claim_cycles_keep_active_surfaces_bounded`, `test_epoch_history_compaction_keeps_active_rows_bounded` | Good. |

**Consolidated list of missing tests** (all detailed in §2): writer-lock busy retry for candidate acquire (H1) and lease renew (M1); torn stale-epoch directory tolerance (M4); pause-mid-scan for the direct old-epoch orphan loop (M5); non-empty assertions for migrated discovery surfaces (M6); `error`-generation and polluted `stop.json` behaviour in adoption strategies (M7); the thirteen `load_run_descriptor` rejection branches with a no-business-row assertion (M9); a `WITH …`/comment-prefixed statement rejection in `_FencedConnection` (L9).

---

## 7. Alignment with plan acceptance criteria

### Plan §15 Phase 1 pre-release checklist

| Item | Status | Note |
| --- | --- | --- |
| init / open-existing / open-readonly with no implicit DDL; dual schema versions consistent | ✔ | `schema_bootstrap.py`, verified by the Checker |
| 31 mutators mapped to Legacy/Fenced store, transaction, and RED test | ✔ | `plans/artifacts/plan02_phase1_mutator_inventory.json`, count enforced at `check_plan02_phase1.py:58-67` |
| All HA business mutators fenced in-transaction; fragment does not use an optional/no-op token | ✔ | `FencedSQLiteStore.finalize_unconsumed_updates`/`delete_archived_rows` explicitly reject fragment rows (`fenced_store.py:470-502`) |
| Raw writable escape hatch removed/closed | ✔ | `fenced_store.py:233-238`; caveat L9 |
| Learner creates only its own instance directory | ✔ | `prepare_learner_instance_dir` (`paths.py:290-296`) |
| Old epoch cannot modify the current epoch's checkpoint/control | ✔ | Path isolation + epoch guard |
| `RunPaths` recursive scans non-empty and consistent across maintenance / Checker / probe / liveness / analysis / metrics | ✘ | **M6** — only maintenance and the Phase 1 Checker migrated |
| Old epoch resuming mid-GC cannot delete the current checkpoint; expected orphans actually deleted | ~ | Property holds; the mechanism deviates from the frozen protocol — **M5** |
| Fixed-cache pollution does not affect learner/Checker | ~ | Holds for stop/latest adoption; **M7** for adoption strategies |
| Learner watchdog uses epoch heartbeat / claim / recovery; lower-epoch stop and long PBS queues do not false-kill | ✔ | `learner.py:709-733`, `:747-778` |
| Candidate/epoch logs single-writer | ✔ | `syncer_ha.py:183-186`, `syncer.py:2563-2568` |
| Claim reconciliation/backoff/budget complete and off by default | ✔ | `launch_outbox.py`; `config.py` default `enabled: false` |
| Transaction-external `SIGSTOP` takeover and transaction-internal availability boundary both pass | ~ | Probe evidence ✔; **production candidate cannot survive the blocked window — H1** |
| Active epoch/claim state bounded | ~ | ✔ during the run; **L1** at terminal |
| Phase 1 completed Checker `PASS` | ✘ | **H2** — the `PASS` artifact predates the reviewed commit; post-remediation evidence is `PASS_WITH_FOLLOWUPS` |

### Plan §11.1 performance/reliability reporting

**Fact.** The following required §11.1 numbers are absent from the Phase 1 artifacts: renew p99 with ≥100 samples, business-transaction p99/max, takeover protocol latency against the `≤ 2 × renew_interval + 10 s` threshold, separated control-plane CPU vs. blocking critical-path wall time, the candidate-observer `sqlite_commit_seconds` p99 regression threshold (which §11.1 says must be *frozen in the P1-L1 RED test*), and the publish p99 regression threshold vs. a matched Plan 01 baseline (to be frozen in the P1-L3 RED test). The Checker records none of these. `stale epoch business commits = 0` and `canonical adoption errors = 0` are supported by the recorded artifacts.

**Assessment.** Plan §11.1 states "Checker遇到核心指标缺失返回 `BLOCKED`". The Phase 1 Checker does not currently evaluate any §11.1 metric, so a `PASS` from it does not certify that clause. Recommend either implementing the §11.1 metric collection + thresholds in `check_plan02_phase1.py`, or recording an explicit, justified narrowing of §11.1 in the plan.

### Requirement matrix

**Fact.** HA-01…HA-20 are marked `completion-candidate` — a status value not defined in plan §13 (which specifies `complete` with a verifiable `evidence_path`, and forbids `TBD`). **Assessment.** This is an honest intermediate marker consistent with H2, and preferable to a premature `complete`. Recommend defining `completion-candidate` in the plan's §13 vocabulary, or reverting these rows to `planned` until the gate closes.

---

## 8. Consolidated recommendations, ordered

**Must fix before the phase gate**

1. **H1** — Handle busy/locked `sqlite3.OperationalError` in `acquire_candidate` (retry until `candidate_wait_seconds`, log `writer_lock_blocked` with the blocker identity), and use a bounded startup busy timeout for `LeaderLeaseStore` connection setup. Add the RED test described in §2/H1.
2. **M1 / M2** — Retry transient busy in `LeaseRenewalThread._run` within the monotonic lease budget; give `FencedSQLiteStore` its own longer busy timeout instead of `lease_busy_timeout_ms`. Add both RED tests.
3. **H2** — After (1) and (2) land, freeze a new target and re-run G2 → G6 including `check_plan02_phase1.py --mode phase1-completed` returning `PASS` on a ≥10-merge 1+8 run with a real cross-job takeover. Only then set HA-01…HA-20 to `complete`.

**Should fix in the same change**

4. **M4** — Make `EpochControlReader` skip-with-counter on stale-epoch inconsistencies; keep fail-closed for the highest epoch.
5. **M7** — Replace the three `stop_json.exists()` checks in `adoption.py` with an authoritative-terminal hook.
6. **M9** — Add the `load_run_descriptor` rejection-branch suite.
7. **M8** — Drop `--allow-dirty-snapshot` from the acceptance launcher.
8. **M6 (narrowed)** — Migrate the two Phase-1-only surfaces (syncer log path, epoch checkpoint layout) in `analysis.py`, `run_metrics_csv.py`, `check_plan01_invariants.py`, `publication_crash_probe.py`, with fail-closed non-empty assertions. Keep the learner-identity globs deferred and record that narrowed deferral in the plan.

**Should address before Phase 2**

9. **M3** — Add a scan cache/short-circuit to the learner control observation path and produce the §11.1 control-plane critical-path measurement.
10. **M5** — Either route old-epoch orphans through `gc_candidates`, or add a per-file fenced pre-check and amend the frozen plan text.
11. **L1** — Converge completed HA runs to current-only retention and assert it in the Checker.
12. **§11.1** — Implement (or explicitly narrow, in the plan) the missing performance/reliability metrics and their frozen thresholds.
13. **L2, L3, L4, L5, L6, L7, L8, L9** — as tabulated.

---

## 9. Verdict

**CHANGES_REQUIRED**

**Basis.** The implementation is high quality and the core safety invariants (HA-01, HA-02, HA-03, HA-04, HA-07, HA-10, HA-13, HA-14, HA-15, HA-17, HA-18) hold under inspection, with strong test and cluster evidence behind them. Approval is withheld for two reasons:

1. **H1** — a production error-handling gap that makes the plan's own documented writer-lock recovery path (HA-11, `docs/07-operations.md` §2.1) unusable: the successor candidate dies on `SQLITE_BUSY` instead of waiting out `candidate_wait_seconds`. M1 and M2 are the same gap on the renew and business-transaction connections.
2. **H2** — the Phase 1 completed-Checker `PASS` evidence was produced at commit `24e181b`, before `b21b29f` changed `control_epoch.py`, `learner.py`, `syncer.py`, `maintenance.py` and `syncer_ha.py`. The only post-remediation runtime evidence is a 2-merge smoke returning `PASS_WITH_FOLLOWUPS`, which plan §1.3 and §15 do not accept for this gate.

Six Medium plan-contract deviations (M5, M6, M7, M8, M9, and the §11.1 reporting gap) and nine Low items are also recorded above with concrete fixes.
