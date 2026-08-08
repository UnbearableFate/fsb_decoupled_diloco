# Plan 03 `P3-operational-robustness` — incremental remediation review (claude-opus-5)

- **Base commit ID**: `de3d27879fdef188afa03a233acd4b40d90e5feb` (`plan03: implement P3 operational robustness`)
- **Target commit ID**: `9e1b8238c11b15b88883dffc868ef2cd89adb1b9` (`plan03: remediate P3 phase review`)
- **Review range**: `git diff de3d27879fdef188afa03a233acd4b40d90e5feb 9e1b8238c11b15b88883dffc868ef2cd89adb1b9` — 35 files, +2909 / −268
- **Ancestry**: verified — `git merge-base --is-ancestor <base> <target>` returns 0; the range is exactly one commit
- **Reviewer model**: `claude-opus-5` (Claude Opus 5). Date: 2026-08-09
- **Reviewer role**: read-only. No file was created or modified except this report. No Git state change, no `qsub`/`qdel`, no run-data deletion, no commit/push/PR.
- **Runtime policy compliance**: **no runtime evidence was produced or used.** No `pytest`, no repository Python, no reproducers, no benchmarks, no PBS submission, no package installation, no network access. Every claim below is derived from static source inspection plus read-only filesystem listing (`ls`/`find`) of already-existing run directories. This explicitly corrects the process deviation recorded against the previous Claude review in `artifacts/20260809-063000_p3-review-finding-dispositions_review.json`.
- **Independence**: the Codex report `gpt-5.6-sol_9e1b8238c11b15b88883dffc868ef2cd89adb1b9.md` is present in the working tree but was **not opened or read** before this report was written.
- **No secrets**: this report contains no token, credential, key, or full environment dump.

---

## 1. Scope

### 1.1 Files reviewed (whole committed range)

| Category | Files | Coverage |
|---|---|---|
| Core | `fs_diloco/core/config.py`, `fs_diloco/core/versions.py` | line-by-line |
| Observability | `fs_diloco/observability/logging_utils.py` | line-by-line |
| Runtime | `fs_diloco/runtime/launch_outbox.py` | line-by-line + surrounding `reconcile` control flow |
| Storage | `fs_diloco/storage/{authority.py,fenced_store.py,run_initializer.py,schema_v4.sql}` | line-by-line |
| Tools | `fs_diloco/tools/{clean_run.py,init_run.py}` | line-by-line |
| Checker / PBS | `scripts/miyabi/check_plan03.py`, `scripts/miyabi/run_plan03_phase3_tests.pbs` | line-by-line |
| Tests | `tests/protocol/test_p3_unified_v4_golden.py`, `tests/storage/{test_authority_p3_operational.py,test_run_initializer_p3.py,test_schema_v4.py}`, `tests/observability/test_p3_operational_evidence.py`, `tests/test_clean_run.py`, `tests/test_plan02_phase2_dynamic.py`, `tests/test_plan03_checker.py` | line-by-line |
| Plan / matrix | `plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha.md`, `…-requirement-matrix.csv` | full diff parsed |
| Evidence | `reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/{progress.md,failures.md}` + 8 new artifacts, and the two base-target review reports being remediated | read |

Surrounding (unchanged) code consulted for invariant checking: `fs_diloco/core/run_descriptor.py`, `fs_diloco/storage/{atomic_io.py,paths.py,artifact_policy.py,audit_archive.py,schema_bootstrap.py,schema_v4_dynamic.sql}`, `fs_diloco/runtime/syncer.py`, `fs_diloco/observability/wandb_logging.py`.

### 1.2 Read-only verification actions

- `git rev-parse` / `git log` / `git show` / `git diff` / `git merge-base --is-ancestor` / `git status --porcelain`.
- `rg` and targeted `sed`/`head` reads of source, schema, tests, plan and evidence files.
- `find`/`ls` over the pre-existing `runs/` tree to establish whether real run roots contain symlinks (relevant to H-1). **No file under `runs/` was read, modified, or deleted.**
- `git diff --stat HEAD -- fs_diloco tests scripts plans/DOING` is empty, i.e. the worktree source/test/checker/plan trees are identical to the target.

### 1.3 Explicitly out of scope

- The uncommitted worktree change `M plans/AGENTS.md` (3 insertions / 1 deletion) — not part of the frozen target.
- The untracked Codex report for this target.
- Everything at or before `de3d2787…` (only evaluated where the increment interacts with it).
- P4/P5/P6 runtime cutover, config migration, and formal 9-node measurement (explicitly deferred by the plan).

### 1.4 Severity definitions used

**Critical** = breaks the normal running path or durable correctness. **High** = violates an explicit phase gate clause, or causes an unrecoverable / amplifying operational failure. **Medium** = invariant drift, cross-boundary inconsistency, or reduced regression-detection that must be fixed or explicitly deferred. **Low** = optional improvement, contract clarity, or process observation.

Per `plans/AGENTS.md` §"处置问题并验证", Critical/High/Medium are **blocking**; Low may be recorded as follow-up.

---

## 2. Verdict

## CHANGES_REQUIRED

The remediation genuinely closes the substantive blocking findings from both base-target reviews. In particular F-01/H4 (unbounded, entry-exhaustive startup validation), F-02 (uncertainty deadline refreshed every reconcile), F-04/H3 (drain ack and terminal-close snapshot), H1 (archive pruning the current frontier), H2 (initializer identity binding), F-11 (authority schema revision), F-12 (GC claim recovery), F-13/F-14/F-17/F-19 and L1 are all correctly and testably resolved — see §5.

However the range introduces **2 High + 3 Medium** new blocking defects, two of which make `fs_diloco/tools/clean_run.py` — the tool `AGENTS.md` §"Test Artifact Retention and Cleanup" item 5 mandates for post-test cleanup — unusable on every run currently on disk, and one of which permanently disables dynamic scale-out after two scheduler-uncertainty escalations.

---

## 3. Findings

### Critical

**None.** No defect in this range corrupts durable authority state, publishes a half-initialized run, admits a duplicate instance, or deletes data outside the target run root. The `dir_fd + O_NOFOLLOW` re-anchoring in `_unlink_owned_candidate` is correct and closes the base-target Codex C1 destructive-boundary finding.

---

### High

#### H-1 (High, regression / operational) `clean_run` now refuses any run root that contains a symlink anywhere, so `build_cleanup_plan` fails on every real run

**Evidence**

`_files_below` was rewritten to walk the tree and hard-fail on any entry that is not a non-symlink directory or a regular file (`fs_diloco/tools/clean_run.py:112-141`):

```python
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        for name in tuple(directory_names):
            child_metadata = child.lstat()
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise CleanupRefusedError(
                    f"cleanup scan refuses a symlink or non-directory parent: {child}")
        for name in file_names:
            child_metadata = child.lstat()
            if not stat.S_ISREG(child_metadata.st_mode):
                raise CleanupRefusedError(
                    f"cleanup scan refuses a symlink or non-regular entry: {child}")
```

`os.walk(..., followlinks=False)` still *lists* a symlink-to-directory in `dirnames` (it only declines to recurse), so the first check fires on it. The first caller is the wandb cache scan (`clean_run.py:427-430`), and the last caller walks the entire run root (`clean_run.py:457`).

wandb is enabled by default (`fs_diloco/core/config.py:365-367`: `enabled: bool = True`, `mode: str | None = "offline"`) and the syncer points it at the run's own log dir (`fs_diloco/runtime/syncer.py:1899`: `"dir": str(paths.logs)`). wandb offline runs therefore create `logs/wandb/latest-run`, `logs/wandb/debug.log` and `logs/wandb/debug-internal.log` as symlinks. This is not hypothetical — a read-only `find` over the existing repository runs shows exactly those three symlinks in **every** `runs/fs_diloco/*` run directory, e.g.:

```
runs/fs_diloco/adopt200x25_predict_s1337_20260721_0150/logs/wandb/debug.log
runs/fs_diloco/adopt200x25_predict_s1337_20260721_0150/logs/wandb/latest-run
runs/fs_diloco/adopt200x25_predict_s1337_20260721_0150/logs/wandb/debug-internal.log
```

**Failure scenario**

A P3-initialized run finishes with the default config → the operator runs `clean_run` on it → `_files_below(run / "logs" / "wandb", run_root=run)` hits `latest-run` → `CleanupRefusedError: cleanup scan refuses a symlink or non-directory parent`. No plan is produced, nothing is cleaned, and there is no flag to proceed. Before this commit, `_files_below` filtered symlinks out (`… if path.is_file() and not path.is_symlink()`), so unrelated symlinks were simply never selected and cleanup succeeded.

The base-target Codex finding C1 asked only that a **symlinked candidate root or candidate ancestor** be refused. Refusing *any* symlink anywhere in the tree — including one that is neither a candidate nor an ancestor of a candidate — exceeds the specification recorded in the plan (`§8.5`: "遇到 … symlinked candidate ancestor … 时一律 fail closed") and breaks the tool. `reports/DOING/…/failures.md` (attempt 2, 2026-08-09 06:24) shows the symptom was seen in the test suite and resolved by relaxing the *test* ("accept the stronger fail-closed diagnostic"), not by re-checking real run layouts.

**Impact**: the cleanup tool mandated by repository `AGENTS.md` §"Test Artifact Retention and Cleanup" item 5 cannot produce a plan for any run created with the default configuration. Because the refusal happens during planning, this is fail-closed (no data loss), but it removes the retention/cleanup capability the phase is supposed to strengthen (`AUDIT-03`).

**Recommended fix**

Scope the refusal to ownership-relevant paths: keep `_validate_owned_parents` and the candidate-leaf `lstat` checks (which are what C1 required), and in `_files_below` **skip** entries that are symlinks or non-regular instead of raising — but only for entries that are not themselves a scan root or a parent of a selected candidate. If a stricter posture is wanted, record skipped foreign entries in the plan/manifest and gate the hard refusal behind an explicit `--refuse-foreign-symlinks` flag (default off).

**Required RED test**

Build a completed run whose `logs/wandb/` contains `latest-run -> offline-run-…/` plus regular files, and assert (a) `build_cleanup_plan` succeeds, (b) neither the symlink nor anything reachable only through it appears in `plan.candidates`, and (c) the symlink and its target still exist after `execute_cleanup`. Keep the existing `updates/payloads -> outside/` refusal test unchanged so the C1 boundary stays proven.

---

#### H-2 (High, resource invariant) `manual_review` permanently consumes launch reservation capacity; there is no release path in the wired store

**Evidence**

The F-03 remediation replaced the three hard-coded `reserved_states` tuples with a tombstone predicate (`fs_diloco/storage/fenced_store.py:1225-1234`, `:2158-2165`, `:2268-2276`):

```python
                    SELECT COUNT(*) FROM launch_requests
                    WHERE reservation_released_at IS NULL AND admitted_instance_id IS NULL
```

But `reservation_released_at` is set in exactly one place, for exactly five states (`fenced_store.py:2490-2494`):

```sql
                    reservation_released_at=CASE
                        WHEN ? IN ('failed','expired','cancelled','capacity_fulfilled','completed')
                        THEN COALESCE(reservation_released_at, ?)
                        ELSE reservation_released_at
                    END
```

`manual_review` is **not** in that list, and `manual_review` is **not** in the `active_only` state set (`fenced_store.py:340-350`), so `LearnerLaunchOutbox.reconcile` never revisits such a row — the `expires_at` TTL fallback at `launch_outbox.py:520-532` is unreachable for it. No other `FencedSQLiteStore` mutator writes `manual_review` or releases its tombstone (`rg "manual_review" fs_diloco/` returns hits only in `protocol/`, `storage/authority.py`, the SQL schemas, and `runtime/launch_outbox.py`).

The remediation also newly made `manual_review` *reachable*: `launch_outbox.py:453-466` (known job) and the new `launch_outbox.py:500-519` (no job) both transition to it once the deadline elapses. The new test itself pins the leak (`tests/test_plan02_phase2_dynamic.py:1298-1300`):

```python
        assert row["state"] == "manual_review"
        assert row["reservation_released_at"] is None
```

**Failure scenario**

`scaling.max_pending_launch_requests` defaults to `2` (`fs_diloco/core/config.py:252`). Two `scale_out` requests that hit the uncertainty deadline (e.g. two transient `qstat` outages, or two qsub receipts lost) land in `manual_review`. From then on `pending_scale == 2` forever, so `pending_scale < max_pending_launch_requests` at `fenced_store.py:2296` is permanently false and **`can_scale` can never be true again for the rest of the run**. The same rows also inflate `reserved`, so `productive + reserved < desired_contributors` (`:2295`) and `active_count + reserved < stream_pool_size` (`:2299`) are additionally under-satisfied, and `register_learner_instance`'s capacity guard (`:1225-1246`) sees phantom reservations too.

Before this commit `manual_review` was outside `reserved_states`, so it did not block scaling (that permissiveness was exactly base-target finding F-03). The fix moved from "never blocks" to "blocks forever"; the correct semantics — held until the leader applies an operator disposition, as `SCHED-05` states ("anti-duplicate tombstone 保留到 leader 应用 operator disposition") — is not implemented anywhere: `apply_scheduler_operator_request` is the disposition command, but it targets the v4 authority tables, and even there it does not release a tombstone (see M-3).

**Impact**: directly contradicts requirement `SCHED-04` ("deadline 后进入 failed/expired/manual_review，**不无限占用隐式状态**", matrix marked `complete`) and the §8.8 gate on scheduler capacity accounting. Recovery requires hand-editing the authority SQLite file.

**Recommended fix**

Add one fenced mutator on `FencedSQLiteStore` — e.g. `resolve_manual_review_launch_request(token, *, request_id, expected_states={"manual_review"}, state, reason)` — that writes a terminal state **and** `reservation_released_at=COALESCE(reservation_released_at, ?)`, and route the operator disposition through it. Alternatively, add `manual_review` to the release CASE only once a paired `manual_reason`/`evidence_source` disposition row exists, so the tombstone still blocks automatic duplicates but cannot outlive the operator decision.

**Required RED test**

`record_capacity_observation` drives two `scale_out` requests to `manual_review`; assert a third low-capacity window produces `launch_request is None` and `reserved_launch_capacity == 2`; then apply the operator disposition and assert the next low-capacity window issues a new `scale_out` and `reserved_launch_capacity` drops. Add a structural test asserting that every `launch_requests.state` outside `launch_requests(active_only=True)` either has `admitted_instance_id` set or has a mutator that sets `reservation_released_at` — this is the low-cost guard that would have caught both F-03 and H-2.

---

### Medium

#### M-1 (Medium, correctness) `uncertainty_deadline` can never be re-armed, so a stale deadline forces an immediate `manual_review` on a later, unrelated uncertainty

**Evidence**

`fenced_store.py:2486` now makes the first write win and provides no way to clear the column:

```sql
                    uncertainty_deadline=COALESCE(uncertainty_deadline, ?),
```

`launch_outbox.py:442-479` reads that persisted value on every uncertain classification and escalates as soon as `now >= deadline`. Positive evidence only advances `last_positive_evidence_at` (`:431`, `:494`); nothing resets `uncertainty_deadline` or `first_uncertain_at`.

**Failure scenario**

`scheduler_uncertainty_timeout_seconds = 300`:

1. `t=110` — transient `qstat` failure while the job is still queued → `terminal_uncertain`, `uncertainty_deadline = 410`.
2. `t=150` — `qstat` reports `queued`/`running` → `submitted`/`started`, `last_positive_evidence_at = 150`. `uncertainty_deadline` stays `410`.
3. `t=5000` — the job disappears before registering (classification `finished`/`no_record`) → `launch_outbox.py:453` sees `deadline=410 <= 5000` and jumps straight to `manual_review`.

The request never gets the bounded uncertainty window `SCHED-03`/`SCHED-04` promise; combined with H-2 it permanently burns a reservation slot. The new test `test_no_job_uncertainty_deadline_is_anchored_and_reserves_scale_capacity` only exercises a single uncertainty episode, so this asymmetry is uncovered.

**Recommended fix**

Clear the uncertainty anchor when positive evidence is recorded — e.g. add an explicit `clear_uncertainty: bool = False` parameter and write `uncertainty_deadline = CASE WHEN ? THEN NULL ELSE COALESCE(uncertainty_deadline, ?) END` (same for `first_uncertain_at`), and pass it from the two positive-evidence branches (`launch_outbox.py:424-435` and `:483-498`). The first-write-wins property that fixes F-02 is preserved *within* one uncertainty episode.

**Required RED test**

Uncertain → positive evidence → advance the clock past the original deadline → uncertain again; assert the row is `terminal_uncertain` with a **new** deadline anchored at the second `first_uncertain_at`, and only reaches `manual_review` after the full timeout from that second anchor.

---

#### M-2 (Medium, operational) `clean_run` fail-closed on a missing artifact policy has no escape hatch, so no pre-P3 run can be cleaned

**Evidence**

`fs_diloco/tools/clean_run.py:283-294`:

```python
    except FileNotFoundError:
        raise CleanupRefusedError("artifact policy is required to prove generic cleanup safety")
```

`control/artifact_policy.json` is only written by the new P3 initializer (`fs_diloco/tools/init_run.py:217-218`). Every run currently under `runs/fs_diloco/` predates it, so `build_cleanup_plan` now refuses all of them.

The direction (fail closed) is correct and matches the base-target F-10 recommendation and the amended plan §8.5. What was **not** implemented is the second half of that recommendation: an explicit, default-off opt-in (`--allow-legacy-run-without-policy`) recorded in the cleanup manifest.

**Impact**: `AGENTS.md` §"Test Artifact Retention and Cleanup" item 5 requires `clean_run.py` to be used and extended for generated-run cleanup; with H-1 this leaves no supported path to clean any existing run.

**Recommended fix**: add the explicit opt-in flag, default off; when set, record `artifact_policy_sha256: null` plus `legacy_policy_override: true` in the manifest, and keep the authority live-reference check mandatory (it does not depend on the policy file).

**Required RED test**: (a) missing policy without the flag → `CleanupRefusedError` (already covered by `test_clean_run_fails_closed_without_artifact_policy`); (b) missing policy **with** the flag → plan succeeds, live authority-referenced paths are still excluded, and the manifest records the override.

---

#### M-3 (Medium, cross-store invariant) the v4 authority cannot express the reservation tombstone the fenced store now depends on, and `apply_scheduler_operator_request` bypasses it entirely

**Evidence**

- `fenced_store` capacity/admission accounting now depends entirely on `launch_requests.reservation_released_at` (`:1229`, `:2162`, `:2272`).
- Neither v4 schema declares that column: `grep -n reservation_released_at fs_diloco/storage/schema_v4.sql fs_diloco/storage/schema_v4_dynamic.sql` → no match. `schema_v4_dynamic.sql:65-94` (`launch_requests`) and `schema_v4.sql:533-550` (`candidate_launch_outbox`) both omit it, while the legacy bootstrap schema has it (`fs_diloco/storage/schema_bootstrap.py:249`).
- `LeaderSession.apply_scheduler_operator_request` writes launch terminal states through raw per-action SQL (`fs_diloco/storage/authority.py:1961-1971` `MARK_FAILED`, `:1972-1983` `MARK_EXPIRED`, `:1984-1996` external-cancel → `manual_review`) and never touches a tombstone column — it cannot, because the column does not exist in v4.

**Impact**: the two persistence layers now disagree on how a launch reservation is released. Any P4 cutover that moves the outbox onto the v4 authority will either lose the anti-duplicate tombstone (`SCHED-05`, `DMB-05`) or silently reintroduce F-03. It also means the operator disposition path — the designated way out of H-2 — cannot release the reservation on either side today.

**Recommended fix**: add `reservation_released_at REAL` to `launch_requests` in `schema_v4_dynamic.sql` (and the analogous column/semantics to `candidate_launch_outbox`), bump `AUTHORITY_SCHEMA_VERSION` accordingly, and have `apply_scheduler_operator_request` set it inside the same fenced transaction for `mark_failed`/`mark_expired`. If the intent is instead that v4 derives "reserved" from states, then say so explicitly and add a single shared constant/table consumed by both stores rather than two divergent rules.

**Required RED test**: a structural test asserting that for both schemas the set of launch states treated as "reserved" is derived from one shared definition, plus an authority test asserting `apply_scheduler_operator_request(mark_failed)` leaves the request out of the reserved set.

---

### Low

| ID | Location | Issue | Suggested fix |
|---|---|---|---|
| L-1 | `fs_diloco/storage/authority.py:4566-4570` vs `:2903-2930` | `_audit_history_records` clamps to `safe_cutoff = min(cutoff, MAX(version)-1)`, but `archive_batches.cutoff_version` (and the batch payload identity check at `:2898-2901`) stores the **requested** cutoff. An offline consumer that trusts "rows ≤ cutoff are archived" will believe the latest version was archived when it was deliberately retained. | Persist both `requested_cutoff_version` and `effective_cutoff_version`, or reject a request whose cutoff exceeds `MAX(version)-1` instead of silently clamping. |
| L-2 | `fs_diloco/storage/run_initializer.py:575-579` (unchanged code, reached from the new bounded path) | Startup validation no longer walks the tree, but `_validate_completed_protocol_identity` still runs `PRAGMA integrity_check` over the entire authority DB on **every** `load_run_descriptor`. Startup cost is therefore O(authority DB pages), not constant, so the `AUDIT-04` "audit growth does not participate in startup scan" gate is only partly met. | Make the full integrity check opt-in (explicit `verify-run` / repair path) and keep a cheap `PRAGMA quick_check` or header/identity check on the actor startup path. |
| L-3 | `fs_diloco/storage/run_initializer.py:28-52` vs `fs_diloco/storage/artifact_policy.py:107-157` | Two independent hard-coded classifications of the same paths now exist (`_MUTABLE_SUBTREES`/`_MUTABLE_FILES` vs the policy `classes`/`temporary` patterns), plus a third temp-file rule in `_is_atomic_temporary` (`:439-446`) that is narrower than the policy's `**/*.tmp`. They can drift silently; only `repair_identity_reservation` consumes the first. | Derive the initializer manifest's mutable sets from `build_artifact_policy()` so there is one source of truth, and add a test asserting the two agree. |
| L-4 | `scripts/miyabi/check_plan03.py:388-410` | The new structured-evidence gate requires each requirement's evidence JSON to contain `checks.requirements.<ID>.status == "PASS"`, and the matrix points every P3 row at the checker's own output. The committed artifact confirms the circularity: each entry's `structured_evidence_paths` is the very file containing it (`…-requirements_pass.json`). The gate can only be satisfied by bootstrapping a prior BLOCKED run into a PASS artifact, and adds no independent assurance. This is consistent with the recorded `F-09: partially accepted` disposition, so it is listed as Low rather than re-litigated. | Point `evidence_paths` at the **behaviour** artifact (`…-tests_pass.json`, which records the pytest node results) for the structured check, and keep the checker's own output as provenance only. |
| L-5 | `fs_diloco/storage/authority.py:3212-3237` + `fs_diloco/storage/audit_archive.py:243-272` | `claim_audit_gc` can now reclaim another epoch's `claimed` row (correct F-12 fix), but if the previous epoch crashed **after** `target.unlink()` and before `complete_audit_gc`, the successor's `delete_claimed_audit_batch_object` raises an uncaught `FileNotFoundError` from `target.lstat()`. `complete_audit_gc` already tolerates a missing object (`:3250-3254`), so only the deleter is inconsistent. | Have `delete_claimed_audit_batch_object` treat a missing target as success (idempotent) after proving the parent chain, and add a crash-after-unlink test. |
| L-6 | `fs_diloco/runtime/launch_outbox.py:500-505` | A `terminal_uncertain` row with a NULL deadline raises `RuntimeError` from inside `reconcile`, aborting the whole pass for every request. Currently unreachable (both writers always set a deadline and `COALESCE` never clears it), but if ever reached it wedges the outbox permanently rather than degrading. | Escalate that single request to `manual_review` with an explicit `manual_reason` and continue the loop. |
| L-7 | `tests/protocol/test_p3_unified_v4_golden.py:43-100` | The new generating test genuinely fixes the self-referential F-07 anchor for the merge/outer-optimizer math, but it drives `normalized_update_weights` / `weighted_average_tensors` / `outer_optimizer_step` directly. The base-target recommendation was to run the v4 `LeaderAuthority` pipeline (`initialize_v0 → ingest_receipt → submit_proposal → try_select_batch → commit_merge`) and project that. A divergence introduced inside `commit_merge`'s own weight assembly would still pass. | Add one authority-level case that commits v1 through `LeaderAuthority` and compares the resulting projection to `classic_full_v1_trace.json`. |
| L-8 | `scripts/miyabi/run_plan03_phase3_tests.pbs:86-87` | The retained structured checker output is named `${PBS_JOBID//./_}_p3-requirements.json`, which neither matches `plans/AGENTS.md`'s `YYYYMMDD-HHMMSS_<experiment-id>_<result>.<ext>` convention nor the artifact name the matrix references, so the referenced evidence still has to be produced by a manual rename. | Emit `$(date +%Y%m%d-%H%M%S)_<experiment-id>_requirements_pass.json` and make the matrix reference that pattern. |
| L-9 | `fs_diloco/tools/clean_run.py:588-598` | If `os.fsync(descriptor)` fails after `os.unlink` succeeds, the exception is converted to `CleanupRefusedError` before `deleted_count`/`deleted_bytes` are incremented, so the failure manifest under-reports what was actually deleted. | Increment the counters immediately after a successful `unlink`, before the directory fsync. |

---

## 4. Plan §8.8 gate re-check against this target

| Gate clause | Verdict | Basis |
|---|---|---|
| Receipt-ledger terminal balance = 0 for continuously adjudicated cycles | **Pass** | `acknowledge_terminal_contributor` now rejects an ack that ignores a promised proposal (`authority.py:3491-3513`) and the hard-crash path drains orphan `outstanding` fates through `_terminalize_fenced_updates` (`:3931-3953`) for both static and dynamic (`_retire_dynamic_in_transaction:4214-4219`). Covered by `test_terminal_ack_rejects_a_missing_proposal_promised_by_final_receipt`. |
| ≤ one replayed cycle per lost incarnation; run bound = per-incarnation sum | **Pass** | `begin_terminal_close` accepts only `open` (`authority.py:3305`), so the frozen `hard_crash_cycle_token_budget` is immutable; `_command` replay is request-hash checked (`:4491-4501`). Covered by `test_terminal_close_snapshot_cannot_be_rewritten_by_a_second_command`. |
| scheduler duplicate admission = 0; uncertainty has an explicit state within the deadline | **Not met** | Deadline anchoring and the no-job path are fixed (F-02 / M1), but H-2 makes `manual_review` occupy reservation capacity indefinitely — the explicit "不无限占用隐式状态" half of `SCHED-04` — and M-1 can skip the bounded window entirely. |
| wall-clock jumps do not change process timeouts | **Pass** | Unchanged in this range; still covered by the existing monotonic-clock test. |
| init: no crash point exposes a half-published run; same-identity retry completes; logical path valid after `.complete` | **Pass** | Identity binding now covers resolved-config SHA + mode + git + source on both the staged-retry (`init_run.py:110-126`) and completed-run (`:70-89`) paths; `recovered` is honest (`:94`, `:135`). `test_descriptor_validation_is_bounded_and_accepts_runtime_control_publications` proves a published run with runtime `control/*` writes still loads. |
| recovery hot set bounded; audit growth does not enter the startup scan | **Mostly pass** | The recursive `rglob` is gone from `_validate_completed_run` (`run_initializer.py:280-287`) and the checker enforces its absence (`check_plan03.py:441-456`); the monkeypatched-`rglob` test pins it. Residual: L-2 (`PRAGMA integrity_check` still O(DB)). Archive no longer prunes the frontier (`authority.py:4566-4570`) and the continuation test commits v2 afterwards. |
| unified v4 trace and attribution complete | **Pass with a gap** | Real merge/selection/outer-optimizer math is now executed and byte-compared to the P0 projection; L-7 notes the authority pipeline itself is still not the generator. |
| torch baseline data/optimizer/protocol tests still pass | **Not independently verified** | The retained PBS artifacts record `focused 294 passed / full 808 passed` on job `2508975.opbs`. Per the execution policy for this review, no test was run here; this row is recorded from evidence, not reproduced. |

---

## 5. Base-target findings: remediation assessment

| Finding | Claimed | Assessment |
|---|---|---|
| Claude F-01 / Codex H4 — startup validation enumerated the whole run root | fixed | **Confirmed.** Non-strict validation now checks only `.complete`, `.identity`, the reservation inode, each manifest object's mode/size/SHA and each manifest directory's type (`run_initializer.py:263-288`). Exhaustive entry scanning is confined to `strict_initial=True` (first publication) and `repair_identity_reservation` (`:304-322`). Manifest `format_version` bumped to 2 with a `mutable_files` list. |
| Claude F-02 — deadline refreshed every reconcile | fixed | **Confirmed** (`fenced_store.py:2486`). See M-1 for the newly exposed opposite failure mode. |
| Claude F-03 — `terminal_uncertain` missing from reserved accounting | fixed | **Direction correct, new defect.** Tombstone-based accounting removes the three-way state-list drift, but see H-2 and M-3. |
| Claude F-04 — ack ignored `proposal_expected` | fixed | **Confirmed** (`authority.py:3491-3513`), with the hard-crash escape preserved and tested. |
| Claude F-05 — O(audit objects) startup | fixed | **Confirmed**, with residual L-2. |
| Claude F-06 / Codex M2 — matrix overstated runtime wiring | plan-contract correction | **Confirmed and honest.** `AUDIT-05`, `TOK-01..03`, `DATA-01`, `DMB-05`, `ENV-01` now state that P3 closes the primitives and `P4-MIGRATE` owns production call sites; the plan body §8.5/§8.8 was amended in the same commit. Note for the record: amending the frozen plan to match the implementation is legitimate remediation here because it is explicitly dispositioned, but it does reduce the independence of the gate. |
| Claude F-07 — self-referential golden anchor | fixed | **Mostly** — see L-7. |
| Claude F-08 — `fenced_store.py` removed from the boundary manifest | fixed with a semantic guard | **Partial.** Whole-file hashing is still relaxed; `verify_p3_operational_contracts` (`check_plan03.py:423-456`) substitutes four brittle substring checks plus one AST check. It would not have caught H-2 or M-1 (both satisfy `fenced_store.count("reservation_released_at IS NULL") >= 3`). Acceptable as recorded, but weak. |
| Claude F-09 — declaration-only requirement gate | partially accepted | **Confirmed as accepted**; see L-4 for the circularity the new structured check introduces. |
| Claude F-10 / Codex C1 — cleanup fail-open + symlink traversal | fixed | **C1 confirmed fixed** (`_validate_owned_parents`, `dir_fd`+`O_NOFOLLOW` unlink with dev/ino recheck, parent-swap test). **F-10 over-corrected** — see H-1 and M-2. |
| Claude F-11 — schema changed without a version bump | fixed | **Confirmed.** `AUTHORITY_SCHEMA_VERSION = 5`, `schema_meta` CHECK = 5, `PRAGMA user_version` enforced at open (`authority.py:438-439`), with a RED test. |
| Claude F-12 / GC claim leak | fixed | **Confirmed** (epoch-stamped claims, successor reclaim, matching test). Residual L-5. |
| Claude F-13 / F-14 / F-17 / F-19 | fixed | **Confirmed.** `recovered=False` on fresh init; duplicate `streams.resume_cursor`/`last_receipt_id` writes removed with `contributor_progress` as the sole source (`_dynamic_admission_result:4299-4300`); `applied_version=COALESCE(?, applied_version)`; dropped updates bounded by `base_global_version <= safe_cutoff`. |
| Claude F-15 / F-18 | deferred-low to P4 | Reasonable; both are operator-UX CLIs over already-implemented, tested APIs. |
| Claude F-16 | accepted intentional override | Reasonable; the CAS fence and durable audit row are real, and the code now says so (`authority.py:1935-1937`). |
| Codex H1 — archive could prune the current frontier | fixed | **Confirmed** (`safe_cutoff`, empty-table handling, dependency closure preserved because publications/batches are also clamped), with the continuation test. Missing: the v0-only case the Codex report asked for (behaviour is correct — `safe_cutoff = -1` — but untested). |
| Codex H2 — retry did not bind full run identity | fixed | **Confirmed** for staged retry and completed replay. Note: identity now includes the *literal* `run.shared_root` spelling via the config SHA, so a differently-spelled but equivalent path fails closed — safe, but worth documenting for operators. |
| Codex H3 — terminal close could be rewritten | fixed | **Confirmed**; stricter than the Codex recommendation (a second command ID always fails rather than returning an unchanged close), which matches the amended plan §8.5. |
| Codex M1 — no-job uncertainty ignored its deadline | fixed | **Confirmed** (`launch_outbox.py:500-519`), with a test that keeps the clock below the TTL while crossing the uncertainty deadline. |
| Codex L1 — telemetry identity override / claim durability | fixed | **Confirmed** (`logging_utils.py:76-80` reserved-key rejection, `:75` and `:95-96` directory fsync), with a test. |

---

## 6. What is good in this range

- The initializer split — bounded identity/manifest validation for actors, exact-entry strict scan only at first publication and in the explicit repair — is exactly the right decomposition, and the `monkeypatch(Path.rglob)` assertion pins it structurally rather than by timing.
- `_unlink_owned_candidate` is a textbook TOCTOU-resistant delete: component-by-component `O_NOFOLLOW` `dir_fd` walk, `dev`/`ino`/`size`/`mtime` recheck against the inventory, `unlink` by name relative to the proven fd, then directory `fsync`. Adding `device`/`inode` to `CleanupCandidate` also strengthens the `refreshed != plan` equality recheck for free.
- `archive_audit_batch` recomputing `_audit_history_records` inside the fenced transaction and requiring exact payload equality means the new `safe_cutoff` clamp cannot desynchronise the batch object from the rows actually deleted.
- The terminal-close/ack tightening is coherent end to end: `open`-only snapshot creation, `_command` request-hash replay, `proposal_expected`/`planned_update_id` cross-check, and a hard-crash path that still drains `outstanding` token fates in both membership modes.
- `failures.md` attempts 1 and 2 are recorded with cause, fix and falsification per `plans/AGENTS.md`, and the finding-disposition artifact assigns an explicit `fixed`/`accepted`/`deferred-low` to all 27 base-target findings.

---

## 7. Required actions before this phase can be marked final

**Blocking (must fix):** H-1, H-2, M-1, M-2, M-3.

Suggested order, each preceded by its RED test:

1. **H-2 + M-3** together — they are one invariant ("who releases a launch reservation, and where is that written"). Fix the release path and align the two schemas in one change.
2. **M-1** — small, localized, and it is what makes H-2 reachable in practice.
3. **H-1 + M-2** together — both are `clean_run` usability under the new fail-closed posture; fix them in one pass and validate against a synthetic run root that mirrors the real `logs/wandb` layout.

**Non-blocking follow-ups:** L-1 … L-9.

Because H-2/M-3 touch persistent accounting semantics and a v4 DDL column, per `plans/AGENTS.md` §"处置问题并验证" item 4 the fix will again change a persistence format and concurrency protocol, so a new review-target commit and a further incremental dual-model review over `9e1b8238c11b15b88883dffc868ef2cd89adb1b9..<new-target>` are required before P3 phase-final.
