# Codex independent review — P4 mandatory fenced runtime

- Base commit: `f849214be19c66166a3b57962de86f4265e80e68`
- Target commit: `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4`
- Ancestry: verified; base is an ancestor of target.
- Scope: complete `git diff f849214be19c66166a3b57962de86f4265e80e68 0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4`, including runtime/source, authority/control/admission protocol, config migration, launcher/PBS, tests, Checker, requirement matrix and retained evidence. The user-owned unstaged `plans/AGENTS.md` change is outside the frozen target and review scope.

## Verdict

**CHANGES_REQUIRED**

No Critical finding was identified. Two High and four Medium findings remain. The completed tiny/failover jobs demonstrate stale mutation fencing and normal-path cutover, but do not exercise the admission races and publication races below.

## High findings

### H1 — A second same-launch static process self-authorizes replacement of an active attempt

- Evidence: `fs_diloco/runtime/learner_entrypoint.py:98-110` derives `expected_generation` from filesystem history for every later process. `fs_diloco/runtime/syncer_v4.py:345-356` turns any non-null observed generation into the trusted reason `same_logical_launch_rerun`. `fs_diloco/storage/authority.py:963-980` then replaces an `active` binding whenever the generation and caller-supplied reason are present; it does not require terminal scheduler evidence, an authority terminal disposition, or a separate operator authorization.
- Impact: a concurrent duplicate carrying the same logical launch ID can revoke the healthy current learner, drop its pending/selected work, increment the generation, receive admission and load the model/GPU. This contradicts the P4/MODE-02 single-admission contract and the plan requirement that the leader confirm the old attempt is terminal before advancing generation. The generation number is an optimistic concurrency value, not proof that replacement is authorized.
- Missing test: the P4 suite covers an intentionally forced active replacement and a timeout before any admission, but does not start two actual `learner_entrypoint` processes with the same logical launch while the first binding remains active and assert that the second imports no torch/allocates no CUDA and commits nothing.
- Required fix: separate request observation from replacement authorization. Normal learner requests must not manufacture an active-replacement reason. Admit a same-launch successor only after a persisted, fenced terminal/scheduler disposition for the exact old attempt, or through an explicit authority/operator command whose evidence is validated independently of the requesting process. Add the real duplicate-process RED test and preserve the stale-old-resume mutation fence test.

### H2 — Command replay can republish a stale admission under the current leader epoch

- Evidence: `_admit_requests` hashes only immutable request bytes into `command_id` (`fs_diloco/runtime/syncer_v4.py:339-342`). Authority command records are global across leader epochs and `_command` returns their old result without rerunning the operation. `_admit_requests` publishes that returned fence under the current epoch without checking `current_contributor_fences` (`fs_diloco/runtime/syncer_v4.py:395-403`). `read_admission_response` validates only run/descriptor/actor/attempt/current leader identity and accepts the embedded fence without a current-binding pointer or revocation check (`fs_diloco/protocol/admission_v4.py:174-236`).
- Impact: after a static generation is replaced or a dynamic incarnation is revoked, the retained old request is scanned again. Its old command result can be emitted into the active epoch, allowing a delayed/restarted stale actor through the pre-torch gate. Even within one epoch, the loop replays old command results after replacement. Authority later rejects its receipts/proposals, but the actor has already loaded model/GPU state, violating the key P4 admission invariant.
- Missing test: publish an old request, admit it, replace/retire its fence, rescan under both the same leader and a successor, then call the real response reader and assert that no stale `AdmissionContext` is returned and torch/CUDA counters stay zero.
- Required fix: make the current admission/fence an explicit epoch control with a single current pointer or revocation generation, and validate it in the reader immediately before the torch import boundary. On the syncer side, never publish a replay result unless authority confirms that exact fence is still current; scope command execution appropriately for successor re-evaluation.

## Medium findings

### M1 — Filesystem heartbeat expiry is minted after renewal and can outlive the SQLite lease

- Evidence: the renewer commits `renew_leader` and then independently calls `publish_heartbeat()` (`fs_diloco/runtime/syncer_entrypoint.py:50-52`). `publish_heartbeat` samples a new wall time and sets expiry to that value plus the full lease (`fs_diloco/protocol/control_v4.py:55-65`). Initial publication similarly occurs after acquisition without using the committed lease row (`fs_diloco/runtime/syncer_entrypoint.py:129-135`).
- Impact: if the process is suspended after the SQLite renewal/acquisition commits but before filesystem publication, it can resume after the DB lease expired and publish a heartbeat that appears valid for another full lease. Business mutations remain DB-fenced, but readers can treat a leaderless/stale epoch as current and pass admission/latest gates or consume resources until the false heartbeat expires.
- Missing test: inject a pause between renew commit and heartbeat publication, advance beyond lease expiry, and assert that `read_current_control` returns no current epoch unless a valid successor exists.
- Required fix: have acquire/renew return the committed `renewed_at` and `lease_expires_at` values and publish those exact values; reject publication when the committed expiry is already outside the safety boundary. Do not derive authority lifetime from a later filesystem-side clock sample.

### M2 — Config migration write paths do not satisfy the advertised no-clobber/race contract

- Evidence: `migrate` reads and checks the expected hash once (`fs_diloco/tools/migrate_config_v3_to_v4.py:63-66`) and later unconditionally `os.replace`s the path (`:33-48,68-70`). A concurrent edit between those operations is overwritten. The `--output` path opens the final filename and streams bytes into it (`:15-30`), making a partial file visible and leaving that final path behind on write/fsync failure.
- Impact: repository config edits can be silently lost despite a matching `--expected-sha256`, and consumers can observe or become blocked by a partial output. The matrix/test contract explicitly claims overwrite-race and no-clobber coverage, but the existing test only supplies an already-wrong hash and retries an already-existing output.
- Missing test: mutate the source through an injected hook after initial read but before publication; inject write/fsync failures for `--output`; run two writers against one destination. Assert no concurrent edit is overwritten, no partial final is visible/retained, and exactly one output creation succeeds.
- Required fix: serialize in-place migration with a stable sibling lock/CAS protocol and revalidate the exact source identity at the publication boundary; publish output through a fully written/fsynced sibling plus create-no-replace link/rename, cleaning temporary state on failure.

### M3 — Canonical latest-head reads accept an arbitrary pointer and weakly validated payload

- Evidence: `_read_latest` joins the mutable `pointer_path` directly to the run root, reads it, checks only the head-provided SHA, then accepts any JSON mapping containing `publication_id` (`fs_diloco/protocol/control_v4.py:343-370`). It does not require a relative path inside the exact current epoch/latest directory, ensure the resolved path remains below the run root, or validate payload format/kind/run/epoch/owner/version against the head.
- Impact: a corrupt canonical head can redirect learners to an unrelated in-root or outside-root file and can return a latest payload belonging to a stale epoch/version. Fixed-cache corruption is tested, but canonical-head corruption is not. This can load the wrong checkpoint even though the SQLite authority remains correct.
- Missing test: corrupt `head.json` with `../`/absolute paths and with hash-matching payloads whose run, owner, epoch or version differs; readers must fail closed and the current leader/successor must repair from SQLite authority.
- Required fix: derive the only legal pointer path from `(epoch, owner, version)`, enforce anchored relative-path containment/no symlink traversal, and strictly validate the entire latest payload identity before returning it.

### M4 — Admission discovery and telemetry grow on every poll for every historical request

- Evidence: `iter_admission_requests` returns all retained registration files, and `_admit_requests` processes the full set on every sync loop (`fs_diloco/runtime/syncer_v4.py:325-403`). Successful command replay and immutable response replay are followed by a fresh `learner_admitted` telemetry event every time; rejected requests likewise log a fresh error forever. No processed/disposition frontier removes them from hot discovery.
- Impact: work per poll grows with historical attempts, while telemetry grows at roughly `polls × retained_requests`. Long 5,000-step/soak runs can produce unbounded metadata and increasingly slow recovery/control loops. The short P4 jobs do not expose this behavior and P6 boundedness would fail it.
- Missing test: retain admitted and rejected requests across thousands of polls/restart, then assert bounded hot discovery, one durable disposition per request and no repeated telemetry events.
- Required fix: persist a request disposition/frontier under authority, scan only undisposed/current requests, and emit admission/rejection telemetry on state transition rather than every replay. Archive/compact disposed request artifacts outside startup discovery.

## Checked areas without additional findings

- Lease-bound authority mutation commands revalidate the token inside `BEGIN IMMEDIATE`; target-bound two-host takeover and error-successor evidence shows zero stale commits and contiguous version ownership.
- Epoch-scoped checkpoint/control filenames include epoch, owner, version and publication ID; fixed `latest/stop` files are convenience views rather than DB authority.
- Learner accounting/cursor/receipt/proposal wiring, terminal acknowledgement and token-balance checks passed focused/full and real static/dynamic runs for the exercised normal/failure paths.
- Strict v4 config validation, baseline shared-schema-only migration, recursive config/PBS inventory, launcher partial receipt preservation, literal PBS groups and syntax gates are present; M2 is specifically about publication race guarantees rather than semantic conversion.
- Evidence/matrix Checker binding is source-targeted and all eight P4 rows currently resolve to structured PASS artifacts.
