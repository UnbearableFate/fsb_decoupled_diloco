# Phase review: P6-acceptance-final-review

- Reviewer: `claude-opus-5`
- **Base commit: `7f797e47d983878e25f9c48c1fddbeb9f0b2ea4f`**
- **Target commit: `e3a89c784005acb6c92603d6fea24170bea4daf5`**
- Review scope: the complete `git diff 7f797e47d983878e25f9c48c1fddbeb9f0b2ea4f e3a89c784005acb6c92603d6fea24170bea4daf5`
  (28 commits, 189 files, 164,006 insertions / 229 deletions; the bulk is JSON evidence)
- Reviewer mode: read-only. No file other than this report was created or modified; no git state was
  changed; no `qsub`/`qdel`, no run-data deletion, no commit/push/PR.
- Working-tree state deliberately excluded from the review target: the uncommitted `plans/AGENTS.md`
  and `reports/.../failures.md` edits, and the untracked `*.partial.json` / review artifacts.

## 1. Scope checked

Source and configuration (full read of every changed hunk):

- Authority / persistence: `fs_diloco/storage/authority.py` (+375), `schema_v4.sql` (schema 8 → 9),
  `schema_v4_dynamic.sql` (unchanged — checked for consistency), `audit_archive.py`, `admission.py`,
  `atomic_io.py`, `paths.py`, `tensor_codec.py`, `core/versions.py`.
- Runtime: `syncer_entrypoint.py`, `syncer_v4.py`, `learner_entrypoint.py`, `learner_v4.py`,
  new `learner_control.py`, new `services/maintenance.py`, `services/merge.py`,
  `services/dynamic_capacity.py`, `services/terminal.py`, `services/__init__.py`.
- Tools: `tools/clean_run.py`, `tools/check_workload_equivalence.py`.
- Configs: `configs/fs_diloco_tiny_ha_static_acceptance.yaml`,
  `configs/fs_diloco_tiny_ha_dynamic_acceptance.yaml`.
- Checker / launcher / PBS: `scripts/miyabi/check_plan03.py`, `scripts/miyabi/run_dynamic_learner.pbs`,
  and the eight new `run_plan03_phase6_*.pbs` scripts.
- New P6 harnesses: `plan03_p6_acceptance.py`, `plan03_p6_boundedness.py`, `plan03_p6_crash_matrix.py`,
  `plan03_p6_dynamic_supervisor.py`, `plan03_p6_performance.py`, `plan03_p6_quality_manifest.py`,
  `plan03_p6_state_machine_gate.py`, `plan03_p6_test_gate.py`, `plan03_p6_tiny_scenarios.py`,
  `plan03_p6_two_node_sqlite.py`, `plan03_p6_validate_run.py`.
- Tests: all new/changed files under `tests/gates/`, `tests/runtime/`, `tests/storage/`,
  `tests/test_clean_run.py`, `tests/test_plan03_checker.py`, plus the `PLAN03_REQUIREMENTS` marker edits.
- Docs: `README.md`, `docs/01`, `03`, `04`, `06`, `08`, `docs/modules/{runtime-learner,runtime-syncer,storage}.md`.
- Plan / evidence: `plans/DOING/plans/...-requirement-matrix.csv` and the final G0–G10, 9-node,
  performance, docs, quality, cleanup and requirement-gate artifacts.

Invariant classes examined: publication prepare→publish→commit crash prefixes; leader-epoch fencing
of GC claims; dependency-closure of the authority history prune; command-journal idempotency across
the prune boundary; contributor receipt hash-chain and terminal acknowledgement; pre-Torch admission
boundary; lease-renewer/SQLite transaction mutual exclusion; cleanup ownership; and the plan's own
acceptance-gate binding rules.

## 2. Findings

### Critical

None.

### High

#### H1 — Online audit archiving can delete a contributor's *current* final cycle receipt, breaking clean terminal acknowledgement

Evidence:

- `fs_diloco/storage/authority.py:5729-5744` — `_audit_history_records` now selects every
  `applied`/`dropped` update bounded only by batch/version:

  ```sql
  SELECT u.* FROM updates AS u
  WHERE u.status IN ('applied', 'dropped')
  ORDER BY u.update_id
  ```

  The base revision guarded this with `LEFT JOIN contributor_progress AS p ON
  p.last_receipt_id = u.cycle_receipt_id WHERE p.stable_contributor_key IS NULL`, and guarded the
  `selection_batches` query with the matching `NOT EXISTS (... JOIN contributor_progress ...)`
  clause. **Both guards were removed in this diff.**
- `fs_diloco/storage/authority.py:5746` — `receipt_ids` is then taken from those update rows and the
  corresponding `cycle_receipts` rows are deleted (`fs_diloco/storage/authority.py:3826-3859`,
  `delete_order` contains `cycle_receipts`).
- `fs_diloco/storage/schema_v4.sql:272` — the FK `last_receipt_id TEXT REFERENCES
  cycle_receipts(receipt_id)` was dropped from `contributor_progress` specifically so that this
  deletion is possible (documented at `docs/06-configuration.md:63`).
- `fs_diloco/storage/authority.py:4536-4545` — `acknowledge_terminal_contributor` still requires the
  receipt row to exist:

  ```python
  receipt = connection.execute(
      "SELECT proposal_expected, planned_update_id FROM cycle_receipts "
      "WHERE stable_contributor_key=? AND cycle_seq=?", ...
  ).fetchone()
  if receipt is None:
      raise MembershipFenceError("final cycle receipt is missing")
  ```

  `fs_diloco/storage/authority.py:4778-4790` (`_require_visibility_receipt`) has the same dependency.
- `fs_diloco/runtime/syncer_v4.py:309` — `maintenance_service.tick()` runs after **every** committed
  version in the main loop, with `cutoff = latest.version - 1`
  (`fs_diloco/runtime/services/maintenance.py:104-107`), and the default trigger is only
  `archive_batch_rows = 256` (`fs_diloco/core/config_v4.py:101`), which a run crosses quickly because
  `command_records` rows are now archived unconditionally.
- `fs_diloco/runtime/services/terminal.py:265-271` — a rejected ack is swallowed into a
  `terminal_ack_rejected` telemetry event; the fence stays `awaiting_ack` until
  `drain_ack_timeout_seconds` expires and `_adjudicate_hard_crashes`
  (`fs_diloco/runtime/services/terminal.py:167, 273-291`) reclassifies the contributor as
  `hard_crash` with a `hard_crash_gap_tokens_upper_bound`.

Failure scenario (concrete): `quorum_min = 7`, `num_learners = 8` (the dynamic acceptance config).
Contributor C's cycle-`k` update lands in the batch targeting version `V`. The other seven
contributors then commit `V+1`. The post-commit maintenance tick runs with `cutoff = V`, so the batch
targeting `V` is archivable, C's update row is archived, and C's receipt `R_k` — still
`contributor_progress.last_receipt_id` for C — is deleted from `cycle_receipts`. C has not yet
produced cycle `k+1` (slow inner loop, pending replacement, or it has entered the new
`awaiting_configured_close` state at `fs_diloco/runtime/learner_v4.py:509-522`). The leader then
closes; C publishes a valid terminal ack with `final_cycle_seq = k`; the leader raises
`MembershipFenceError("final cycle receipt is missing")`, drops the ack, waits out
`drain_ack_timeout_seconds`, and finally records C as `hard_crash` with a fabricated token-gap upper
bound. Result: a cleanly drained contributor is accounted as a hard crash, the terminal token
accounting gains a spurious gap bound, and close is delayed by the full ack timeout. Because
`plan03_p6_validate_run.py:260-263` requires `{state} == {"acked"}`, the same event turns a healthy
9-node acceptance run into a `BLOCKED` gate.

Why the recorded gates did not catch it: in the G8 static run `quorum_min == num_learners == 8`, so
every contributor must publish a new receipt before the next global version can commit and the live
receipt is structurally never archivable. G9 (dynamic, `quorum_min = 7`) is exposed but did not hit
the interleaving in the single recorded run.

Fix suggestion: restore an explicit liveness guard in `_audit_history_records` — exclude any
`cycle_receipts` row that is still referenced by `contributor_progress.last_receipt_id` (and the
`selection_batches`/`updates` rows that would drag it in), i.e. reinstate the removed
`contributor_progress` predicates while keeping the rest of the new closure. If the intent is really
to prune the live receipt, then `acknowledge_terminal_contributor` and `_require_visibility_receipt`
must be reworked to validate against `contributor_progress` (+ archived receipts) rather than the hot
`cycle_receipts` table, and the archived-receipt lookup must be part of the same change.

Missing tests:

- No test archives history and *then* acknowledges terminal for the contributor whose
  `last_receipt_id` was pruned. `tests/storage/test_authority_p3_operational.py:1273` only asserts
  `contributor_progress(...).last_cycle_seq == 2` survives, never that the ack path still works.
- No test covers `record_proposal_visibility` / `_require_visibility_receipt` after a prune.
- `tests/runtime/test_terminal_service.py` has no archived-authority variant.
- `scripts/miyabi/plan03_p6_tiny_scenarios.py` scenarios do not force an archive before drain.

#### H2 — The final P6 requirement gate was bound to a commit that is not the delivered tree, so part of the shipped source has no acceptance evidence

Evidence:

- `reports/.../artifacts/20260810-044000_p6-requirements-completed-pass.json` — `status: PASS`,
  run at working-tree commit `4b152391227cbb743e8813d7764c2805b0f8bc66`, but
  `checks.requirements_source_commit = 320d74d0fee41ddf0c8f6a6634f7b1db34fb00a6`. The
  `--verification-target-ref` was therefore pinned backwards; `scripts/miyabi/check_plan03.py:1075-1078`
  defaults it to `HEAD`.
- All final structured evidence (`20260810-042400_p6-g0-g7-final-pass.json`,
  `20260810-035600_p6-g8-final-pass.json`, `..._p6-g9-final-pass.json`,
  `20260810-040100_p6-g10-*-final-pass.json`, `..._p6-quality-final-pass.json`,
  `..._p6-auth11-g7-disposition-pass.json`) declares `source_commit = 320d74d0…`.
- `git diff --name-only 320d74d0… e3a89c78… -- fs_diloco tests scripts configs pyproject.toml uv.lock`
  is **not** empty; it reports `fs_diloco/tools/clean_run.py`,
  `scripts/miyabi/plan03_p6_acceptance.py`, `scripts/miyabi/plan03_p6_quality_manifest.py`,
  `tests/gates/test_plan03_p6_acceptance_aggregate.py`, `tests/gates/test_plan03_p6_quality_manifest.py`,
  `tests/test_clean_run.py` (all introduced by `5493c2d`).
- `scripts/miyabi/check_plan03.py:710-741` (`_evidence_source_matches_target`) rejects evidence when
  `git diff --quiet <evidence-commit> <target> -- fs_diloco tests scripts configs pyproject.toml
  uv.lock main.py` is non-clean. With the documented default (`HEAD`), every one of those artifacts
  would have failed the binding check and the requirement gate would have been `BLOCKED` with
  `requirements.*.structured-checker-evidence`.

Failure scenario: the plan is marked "P6 completed / all requirements complete" while
`fs_diloco/tools/clean_run.py` (production tool, and the one whose semantics changed from "refuse
cleanup" to "silently retain authority-owned GC paths") is covered by no runtime or aggregate gate
bound to the delivered tree. Only `20260810-042700_p6-g0-g1-doc-cleanup-fix-pass.json` (static checks)
and `20260810-042800_p6-g2-doc-cleanup-fix-pass.json` (747-test suite) were re-run at `5493c2d`; the
G0–G7 aggregate, the 9-node G8/G9 runs, and both G10 performance comparisons were not re-aggregated.
Any future reviewer replaying `check_plan03.py --phase P6-acceptance-final-review --mode completed`
with the default target on `e3a89c7` will get a different (BLOCKED) answer than the retained artifact.

Fix suggestion: re-run the requirement gate with `--verification-target-ref` at the delivered HEAD.
That in turn requires re-emitting a G0–G7 aggregate (and re-attesting G8/G9/G10, or explicitly
recording an approved, scoped exemption for `clean_run.py` + the two gate scripts) at `5493c2d` or
later. At minimum, document in `progress.md` exactly why the target ref was pinned backwards, which
files fall outside the attested source, and why that is acceptable.

Missing tests: `tests/test_plan03_checker.py` has no test asserting that the completed-mode gate
fails when the phase evidence is bound to a commit whose executable source differs from the
verification target — the exact regression this backwards pin hides.

### Medium

#### M1 — `MaintenanceService.tick()` is unguarded in the syncer main loop, so a background-maintenance failure kills the leader

Evidence: `fs_diloco/runtime/syncer_v4.py:287, 309` call `maintenance_service.tick()` with no
exception boundary, unlike `_admit_requests`, which wraps each observation
(`fs_diloco/runtime/syncer_v4.py:424-436`). `tick` performs filesystem publication, hash
verification, and unlink (`fs_diloco/runtime/services/maintenance.py:98-215`), and
`delete_claimed_artifact_object` raises `RuntimeError` on any identity mismatch
(`maintenance.py:44-49`), while `complete_artifact_gc` raises `RuntimeError("artifact GC object still
exists")` (`authority.py:3599-3606`).

Failure scenario: one stale/edited/mode-changed artifact under `weights/epochs`, a transient shared-FS
`EIO` during `publish_audit_batch`, or a `read_json` failure on a hot batch aborts the whole merge
loop. The syncer publishes error control and fails the lease, forcing a candidate takeover, even
though archiving/compaction/GC is a non-essential duty that could simply be retried next tick.

Fix suggestion: wrap `tick()` in a bounded `except (OSError, RuntimeError)` that emits a
`maintenance_deferred` telemetry event and continues, keeping fail-closed behaviour only for
`StaleLeaderTokenError`/`AuthoritySchemaError`. Keep `tick(force=True)` at terminal close strict if
the terminal artifact must be complete.

Missing tests: no test injects a maintenance failure into `run_fenced_syncer` and asserts the merge
loop survives.

#### M2 — Command receipts grow unbounded and have no compaction or GC path

Evidence: `fs_diloco/storage/authority.py:5824-5833` archives **all** `command_records` rows with no
cutoff; `authority.py:3766-3771` publishes one immutable file per archived command via
`publish_command_receipt` (`fs_diloco/storage/audit_archive.py:99-114`) into
`audit/command_receipts/<2-hex>/<sha>.json` (`fs_diloco/storage/paths.py:90-93`). Audit *batches* get
a lifecycle (`archive_partitions` → `audit_gc_candidates` → `complete_audit_gc`); command receipts get
none. `fs_diloco/tools/clean_run.py` has no knowledge of the directory.

Failure scenario: a long run performs one small-file write per authority command (selection, prepare,
commit, ingest, admission, capacity observation, GC claim/complete, …). Over a real training horizon
this is O(10^5–10^6) inodes in 256 directories on a shared Lustre-class filesystem, with no
compaction, no retention bound, and no cleanup story. Every subsequent `_command` also pays extra
`lstat`s in that tree (see M3).

Fix suggestion: bound the receipt set the same way audit batches are bounded — either archive command
receipts into the partition payload and GC the individual files after compaction, or restrict the
archived `command_records` set to a cutoff (e.g. commands whose `owner_epoch` is strictly older than
the current epoch and whose `committed_at` is beyond a retention window) instead of "all rows".

Missing tests: no test measures the receipt-file count growth across repeated archive cycles, and no
test covers reading a receipt after its directory shard has been cleaned.

#### M3 — Every authority command now pays a shared-filesystem lookup before its transaction

Evidence: `fs_diloco/storage/authority.py:5606-5608` — `_command` calls `_command_replay` *before*
`BEGIN IMMEDIATE`; `_command_replay` (`authority.py:5570-5594`) does a DB read, two
`_authority._verify_token` calls, and then `read_command_receipt`, which walks
`audit/` → `command_receipts/` → `<shard>/` with `lstat` plus a final `lstat` on the target
(`audit_archive.py:91-116, 117-140`).

Failure scenario: on Miyabi's shared filesystem, four extra metadata operations per command, on the
critical path of selection/prepare/commit/ingest, are charged to every merge. The G10 performance
gate measured the *unified vs classic* delta at the tiny-model scale where merge time is dominated by
this kind of overhead only weakly; at production model sizes the constant is still paid per command
and grows with the receipt tree.

Fix suggestion: only consult the receipt file when the DB row is absent **and** an archive has
actually occurred (e.g. gate on `archive_batches`/`archive_partitions` being non-empty, cached in
`LeaderSession`), or cache the shard-directory existence per session.

Missing tests: none required for correctness; a micro-benchmark assertion in the perf harness would
document the cost.

#### M4 — `check_plan03.py` dirtiness attestation excludes `tests/`

Evidence: `scripts/miyabi/check_plan03.py:27-35`:

```python
EXECUTABLE_SOURCE_SCOPES = ("fs_diloco", "configs", "scripts", "pyproject.toml", "uv.lock", ".python-version")
```

used at `check_plan03.py:1120-1131` as `git status --short --untracked-files=all -- <scopes>` to set
`source_identity.git_dirty` in every phase artifact.

Failure scenario: the G2 gate's whole purpose is to attest the test suite result. A tree with
uncommitted or untracked changes under `tests/` still emits `git_dirty: false`, and
`plan03_p6_acceptance.validate` (`scripts/miyabi/plan03_p6_acceptance.py:73-75`) accepts it as "clean
source". A locally-patched test can therefore produce a PASS artifact that claims a clean source.
Note the *consumer* side (`_evidence_source_matches_target`, `check_plan03.py:726-741`) does include
`tests` in `relevant_tree`, so the two halves of the same contract disagree.

Fix suggestion: add `"tests"` (and, for G0/G1 doc gates, `"docs"`) to `EXECUTABLE_SOURCE_SCOPES` so
the producer and consumer scopes match.

Missing tests: `tests/test_plan03_checker.py` has no test that a dirty `tests/` tree marks the phase
artifact dirty.

#### M5 — AUTH-11's structured evidence is a hand-authored artifact with no generator in the repository

Evidence: `reports/.../artifacts/20260810-043700_p6-auth11-g7-disposition-pass.json` is a
`status: PASS`, `requirements_covered: ["AUTH-11"]`, `git_dirty: false` JSON with a free-text
`disposition` field. No script in `scripts/miyabi/` emits a `AUTH-11-G7-disposition` gate. It was
created because the real G7 artifact `20260810-035600_p6-g7-final-pass.json` declares
`requirements_covered: ["P6-ACCEPTANCE"]` only, which made the first requirement run fail with
`requirements.AUTH-11.structured-checker-evidence`
(`reports/.../artifacts/20260810-043600_p6-requirements-staged-fail.json`).
`check_plan03.py:820-844` accepts any PASS JSON that lists the requirement and declares
`git_dirty: false`.

I verified the numeric claims in the disposition against the G7 artifact and they match
(`successor_wait_seconds 3.280124678974971`, `old_mpirun_returncode 137`,
`sqlite_transaction_active: true`, `integrity: ["ok"]`), so the artifact is not misleading — but the
evidence chain now admits self-declared PASS files.

Fix suggestion: make `plan03_p6_two_node_sqlite.py` declare
`requirements_covered: ["P6-ACCEPTANCE", "AUTH-11"]` and re-emit G7, or add a small generator that
derives the disposition artifact from the G7 payload so it is reproducible. Optionally have
`check_plan03.py` require a `generated_by`/`inputs` provenance field on structured evidence.

Missing tests: no test asserts that structured evidence must be machine-derivable / carry provenance.

#### M6 — `bootstrap_wait` suppression is a pure delay; the "persistently low" windows recorded during bootstrap still authorise the first post-window scale-out

Evidence: `fs_diloco/runtime/services/dynamic_capacity.py:109-122, 152-160`. Observations taken while
`awaiting_initial_bootstrap` is true are persisted with the same
`productive_instances`/`reserved_launch_capacity` values and only differ in the `action` string.
`low_windows = observations[-consecutive_low_windows:]` at line 155 never inspects `action`, so the
moment `now >= authority_created_at + initial_membership_deadline_seconds` the accumulated
bootstrap-window samples immediately satisfy `persistently_low`. The new test
`tests/runtime/test_dynamic_capacity_service.py:135-166` asserts exactly this behaviour
(`scale_out_planned` on the first tick after the deadline).

Failure scenario: with the acceptance config's `initial_membership_deadline_seconds: 600.0`, a run
whose bootstrap learners are merely slow to be scheduled will fire a replacement `qsub` into
`regular-g` on the very first tick after t+600 s, with zero fresh evidence of genuine starvation,
because `consecutive_low_windows` was satisfied entirely during the intentional wait.

Also note the anchor: `authority_created_at()` (`fs_diloco/storage/authority.py:480-484`) is the
authority-creation wall clock, so a candidate that takes over after the deadline never observes the
bootstrap window at all — correct for takeover, but it means the suppression is unavailable during
recovery.

Fix suggestion: exclude observations whose `action == 'bootstrap_wait'` from the `low_windows`
evaluation (or reset the low-window accumulator when the bootstrap window ends), so the scale-out
decision is based on `consecutive_low_windows` samples of genuinely post-bootstrap capacity.

Missing tests: no test asserts that scale-out requires `consecutive_low_windows` observations
recorded *after* the bootstrap window closed.

#### M7 — Online artifact GC now deletes every committed historical checkpoint, with no configuration switch

Evidence: `fs_diloco/storage/authority.py:3782-3803` inserts a `gc_candidates` row for every archived
`artifact_publications` row — including artifacts of **committed** publications with
`target_version <= safe_cutoff`, not only abandoned/orphan ones. `MaintenanceService` then physically
unlinks them (`fs_diloco/runtime/services/maintenance.py:177-201`). The only protection is
`maintenance.publication_orphan_grace_seconds` (default 120 s), which
`fs_diloco/core/config_v4.py:122-127` validates *only* against the leader lease + clock skew.

Failure scenario: this is the documented intent ("终态只保留 current authority",
`docs/04-data-flow.md`), and the acceptance validator enforces it
(`plan03_p6_validate_run.py:243-244`). But it is a hard behavioural change with no opt-out: after
this diff a run keeps exactly one global checkpoint on disk, so intermediate-checkpoint evaluation,
mid-run restart-from-version-N, and post-hoc convergence analysis are no longer possible. The grace
window is also dimensioned only against the lease, not against learner cycle duration; any consumer
that holds a *non-current* `latest` pointer for more than 120 s and then reads its weight/optim path
will hit `FileNotFoundError`. (I checked the in-tree learner paths — `adopt_global`,
`rebase_local_delta` and `prepare_prediction` all re-read or re-validate `latest` first
(`fs_diloco/runtime/learner_v4.py:230-241, 255-294`), so no current caller is broken — but nothing
enforces that invariant for future callers or external tooling.)

Fix suggestion: gate committed-publication GC behind an explicit `maintenance` setting (default
matching the plan's acceptance requirement), and validate `publication_orphan_grace_seconds` against
a learner-cycle bound as well as the lease. Document the "only the current global version survives"
consequence in `docs/04-data-flow.md` in operator-facing terms.

Missing tests: no test asserts that a reader holding a superseded `latest` pointer is either safe or
fails with a diagnosable error after GC.

### Low

- **L1 — stale FK in the dynamic schema.** `fs_diloco/storage/schema_v4_dynamic.sql:15` still declares
  `streams.last_receipt_id TEXT REFERENCES cycle_receipts(receipt_id)` although the equivalent FK on
  `contributor_progress` was deliberately dropped (`schema_v4.sql:272`) to permit the new prune, and
  `PRAGMA foreign_keys=ON` (`authority.py:285`). The column is never written today, so nothing breaks;
  if it is ever populated, the archive transaction will fail with `FOREIGN KEY constraint failed` and
  take down the leader. Drop the FK for consistency, or reinstate it on `contributor_progress`
  together with the H1 fix. Missing test: no schema test asserts that pruning succeeds with every
  receipt-referencing column populated.

- **L2 — dead publication helpers.** `fs_diloco/storage/tensor_codec.py:105-127`
  (`publish_global_weights_immutable`, `publish_outer_state_immutable`) now have no production or test
  callers after `merge.py` and `_initialize_v0` switched to `encode_*` + `publish_immutable_bytes`.
  Delete them or mark them as the public wrapper contract.

- **L3 — dead branch in `clean_run`.** `fs_diloco/tools/clean_run.py:374-384` still raises
  `CleanupRefusedError` when a candidate is in `authority_owned_gc`, but `build_cleanup_plan`
  (`clean_run.py:497-510`) already filters those paths out of `candidates`, so the branch is
  unreachable via the public entry point. Simplify to keep the refusal semantics in exactly one place.

- **L4 — superseded admission is now silent until timeout.** `fs_diloco/storage/admission.py:463-466`
  returns `None` instead of raising `AdmissionSupersededError` when `expected_fence is None`. This is
  intentional (`tests/runtime/test_p4_mandatory_runtime.py:1377-1470`) and correct for the
  pre-fence poll, but it converts a fast, specific failure into a generic
  `TimeoutError("learner admission timed out before torch import")`
  (`fs_diloco/runtime/learner_entrypoint.py:141`). Consider emitting a distinct telemetry event when a
  `superseded` pointer is observed during the wait, so operators can distinguish "superseded" from
  "leader never answered".

- **L5 — test-only control flow in production entrypoints.** `fs_diloco/runtime/syncer_v4.py:100-135`
  performs `os.kill(os.getpid(), signal.SIGSTOP)` and `fs_diloco/runtime/learner_entrypoint.py:175-190`
  blocks on a release marker, both activated purely by environment variables. The guards are careful
  (marker idempotence, trigger file, explicit `ValueError` on malformed values) and the intent —
  proving the transaction boundary in the real binary — is legitimate, but a stray inherited
  `FS_DILOCO_TEST_PAUSE_*` in a production job will self-suspend the leader. Consider requiring a
  descriptor-level opt-in (e.g. a run-descriptor `test_hooks` field) in addition to the environment.

- **L6 — full checkpoint payloads are materialised in RAM.**
  `fs_diloco/storage/tensor_codec.py:63-102` now returns `bytes` from `safetensors.torch.save`, and
  `merge.py:124-149` holds the weight payload *and* the (≈3×) optimizer payload simultaneously before
  writing. Fine for the tiny acceptance models; a peak-RSS hazard at production model sizes where the
  previous `save_file` streamed to disk. Consider streaming with a two-pass digest if a size threshold
  is exceeded.

- **L7 — boundedness CI and point estimate use different estimators.**
  `scripts/miyabi/plan03_p6_boundedness.py:160-182` reports `slope` as the OLS over all points but
  bootstraps the *mean of block slopes*; the gate at lines 486-489 applies the 0.01 threshold to the
  bootstrap upper bound of the second estimator. Document this, or report the bootstrapped point
  estimate alongside so the CI and the reported slope are comparable.

- **L8 — inconsistent atomic-write durability in the harnesses.**
  `scripts/miyabi/plan03_p6_acceptance.py:58-62` writes the aggregate artifact with `os.replace` but
  no `fsync`, while `scripts/miyabi/plan03_p6_validate_run.py:437-443` fsyncs before replacing. Align
  them.

- **L9 — dtype contract checks are vacuous when the update rows are fully archived.**
  `scripts/miyabi/plan03_p6_validate_run.py:273, 289` are guarded by `if update_dtypes and …`, so if
  every update row has been archived and `_audit_rows` yields nothing the FP32/BF16 contract silently
  passes. Make an empty `update_dtypes` an explicit error.

- **L10 — `syncer.parallel_checkpoint_writes` is pinned but unused.** The new P6 config projections
  (`scripts/miyabi/check_plan03.py:64, 80`) freeze `parallel_checkpoint_writes: True`, but the flag is
  only defined and parsed (`fs_diloco/core/config.py:180, 479, 532`) and read by no runtime code —
  publication is strictly sequential (`merge.py:148-149`). Either implement it or drop it from the
  frozen projection so the acceptance contract does not imply a behaviour that does not exist.

- **L11 — audit GC has no lease-safety grace.** `fs_diloco/storage/authority.py:4110-4142`
  (`claim_audit_gc`) and `schema_v4.sql:514-525` (`audit_gc_candidates`) have no `not_before` column,
  unlike `gc_candidates` (`schema_v4.sql:529-542`), so a successor can unlink an audit batch object
  the instant it is compacted while a not-yet-fenced predecessor may still be reading it
  (`_immutable_audit_object`, `authority.py:3868-3882`). The predecessor is stale and will die anyway,
  but the asymmetry with the artifact-GC grace looks unintentional.

- **L12 — same-epoch GC claims can strand.** `artifact_gc_ready` / `claim_orphan_gc`
  (`authority.py:1245-1255, 3542-3576`) only re-claim rows claimed by a *different* epoch, so a claim
  that fails between `claim_orphan_gc` and `complete_artifact_gc` is never retried by the same leader;
  the object survives until a successor epoch takes over. Bounded, but worth an explicit
  "reclaim my own stale claims" predicate.

## 3. Things checked that are correct

Recorded so the negative results are auditable:

- **Prepare-before-publish reordering** (`merge.py:134-149`, `syncer_v4.py:366-388`) is crash-safe:
  `commit_merge` calls `_verify_prepared_publication_artifacts` (`authority.py:3263, 5547-5568`) before
  the transaction, and `reconcile_publications` (`authority.py:3505-3540`) abandons predecessor-epoch
  prepared intents. The new `_publication_commit_boundary` / `_immutable_publication_boundary` seams
  are genuine no-ops in production and are exercised at 18 boundaries × 10 repetitions.
- **`_initialize_v0` rewrite** correctly substitutes `prepare_publication` + `publish_immutable_bytes`
  + `commit_merge` for `initialize_v0`, writes `param_index.json` before publishing latest, and
  fails closed on `MergeFenceConflict`.
- **Torch-free admission**: `syncer_v4` moved `torch`, `modeling.*`, `services.*` and `tensor_codec`
  imports into function bodies, and `tests/runtime/test_syncer_startup_admission.py:113-133` proves
  the entrypoint/authority/`syncer_v4` import chain leaves `torch` out of `sys.modules`.
- **Startup admission window** (`syncer_entrypoint.py:102-138`) is genuinely bounded (1–5 s), does not
  act as a barrier, and re-scans the same durable request directory in the main loop.
- **Learner target-aware close** (`learner_control.py`, `learner_v4.py:509-522`) re-reads control every
  iteration, so terminal/drain is still observed while awaiting close; the `bool`-vs-`int` guard on
  `version` is correct.
- **Terminal telemetry key rename** (`terminal.py:262`) correctly avoids the duplicate `actor_id`
  keyword against `ActorTelemetryWriter`, and is covered by
  `tests/runtime/test_terminal_service.py:191-241`.
- **Lease-renewer quiescence** (`syncer_entrypoint.py:49-62`) holds the renewal lock across the
  test-only `SIGSTOP` and both `assert_outside_transaction` checks are real
  (`authority.py:815-819` inspects `sqlite3.Connection.in_transaction`).
- **`delete_claimed_artifact_object`** (`maintenance.py:24-49`) correctly restricts to the three
  immutable roots, rejects absolute/`..`/backslash/NUL paths, refuses symlinked ancestors and
  non-regular or writable targets, and re-verifies size + SHA-256 before unlinking.
- **Command-receipt validation** (`audit_archive.py:117-140`) round-trips the exact field set,
  re-checks `content_sha256`, and type-checks every field; `CommandConflictError` on digest mismatch
  is preserved across the archive boundary
  (`tests/storage/test_authority_p3_operational.py:1287-1298`).
- **`claim_orphan_gc` epoch fencing**, `complete_artifact_gc` "object must be gone" precondition, and
  the successor-reclaim path are covered by
  `tests/storage/test_authority_p3_operational.py:1460-1531`.
- **`clean_run` retention semantics** and the manifest fields are covered for both `pending` and
  `claimed` GC states (`tests/test_clean_run.py:338-365`).
- **PBS scripts**: group list `xg24i002`, `#PBS -j oe`, required-variable guards, `set -eEuo pipefail`,
  `ERR` traps, node-count assertions (`run_plan03_phase6_two_node.pbs`, `..._static_9node.pbs`), and
  the performance gate's worktree/venv isolation with matching torch versions all read correctly. The
  `run_dynamic_learner.pbs` duplicate-spawn helper propagates its own non-zero status without masking
  the learner status.
- **Docs** (`docs/01/03/04/06/08`, module docs, README) are consistent with the implemented behaviour,
  including the schema 8 → 9 note, the `audit/command_receipts/` tree, the startup overlap window,
  and the learner await-close state.
- **Requirement matrix**: the eight P6 rows match `EXPECTED_PHASE_REQUIREMENTS`
  (`plan03_p6_acceptance.py:23-34`), every row now carries a `checker requirements.<ID>` contract, and
  the AUTH-11 row moved from `pending/TBD` to `complete` with two evidence paths.

## 4. Verdict

**CHANGES_REQUIRED**

Blocking:

1. **H1** — the removal of the `contributor_progress` liveness guard from `_audit_history_records`
   lets online archiving delete a contributor's current final cycle receipt, which breaks clean
   terminal acknowledgement (`MembershipFenceError: final cycle receipt is missing`), misclassifies a
   drained contributor as a hard crash in the token accounting, and would fail the project's own
   9-node acceptance validator. Restore the guard (or move the terminal/visibility receipt lookups off
   the hot table) and add the archive-then-acknowledge regression test.
2. **H2** — the completed-mode requirement gate was bound to `320d74d0…` rather than the delivered
   `e3a89c78…`, so `fs_diloco/tools/clean_run.py` and two P6 gate scripts ship without acceptance
   evidence bound to them. Re-run the gate at the delivered HEAD (re-aggregating G0–G7 as required) or
   record an explicit, scoped exemption.

Medium findings M1–M7 should be addressed or explicitly dispositioned before the phase is closed; the
Low findings are cleanup and hardening items.
