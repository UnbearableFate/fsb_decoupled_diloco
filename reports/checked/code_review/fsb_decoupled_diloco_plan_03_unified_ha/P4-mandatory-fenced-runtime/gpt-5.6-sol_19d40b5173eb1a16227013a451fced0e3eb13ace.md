# Codex independent incremental review — P4 mandatory fenced runtime

- Base commit: `d18fae055b5beec1887f38c3f2070f0bf6ec901b`
- Target commit: `19d40b5173eb1a16227013a451fced0e3eb13ace`
- Review relation: the base is an ancestor of the target; this report reviews the complete increment `d18fae055b5beec1887f38c3f2070f0bf6ec901b..19d40b5173eb1a16227013a451fced0e3eb13ace`.
- Reviewer: Codex, actual model `gpt-5.6-sol`
- Scope: all changed production code, protocol/storage paths, Checker logic, tests, PBS scripts, requirement bindings, retained failure/PASS evidence and finding dispositions.
- Verdict: **CHANGES_REQUIRED**

## Critical

None.

## High

### H1 — The exact-current shortcut admits a fresh process that reuses an active attempt ID without proving that this exact request committed

Evidence: `fs_diloco/runtime/syncer_v4.py:437-447` derives the durable command identity from the canonical request SHA, but when the current static binding has the same logical-launch and attempt IDs it assigns `binding = prior` without consulting the corresponding command record and without enforcing `expected_generation`. This shortcut was added to recover the exact hot request left after a post-commit/pre-disposition crash. It also accepts a newly serialized request with a different SHA, PID or timestamp if the process reuses the current attempt ID. In particular, normal startup computes `highest_static_generation()` and publishes that current generation (`learner_entrypoint.py:101-110`), while `LeaderSession.bind_or_replace_static_attempt()` would reject it because the expected generation is checked before the idempotent-current branch (`authority.py:960-968`). The shortcut silently bypasses that fence.

Impact: a restarted or duplicate static process that accidentally reuses an attempt ID can receive the current fence and cross the pre-torch gate concurrently with the already active process. Both processes then possess the same generation fence, so the authority cannot distinguish them. This violates MODE-02's per-process attempt/generation guarantee and its requirement that duplicate processes consume no GPU and commit zero updates.

Required fix: the recovery shortcut must be conditional on a committed `bind_or_replace_static_attempt` command record for this exact content-addressed command ID whose typed result equals the current binding. Expose a narrow typed/read-only command-result query or equivalent replay primitive; do not infer a committed replay solely from the current actor/attempt fields. If no exact record exists, execute the normal fenced command so a current-generation duplicate is rejected.

Missing test: admit generation 1, then publish a fresh request with the same logical-launch/attempt but a new request SHA and `expected_generation=1`; assert it receives a request-specific rejection, never returns an admission context, and leaves the active binding unchanged. Preserve the existing post-commit/pre-disposition cross-epoch recovery test as the positive counterpart.

## Medium

### M1 — Replaying identical malformed bytes in a later leader epoch collides with the first immutable disposition and leaves the hot entry permanently retrying

Evidence: invalid history and disposition paths are both keyed only by `sha256(original)` (`fs_diloco/protocol/admission_v4.py:658-680`), but the disposition payload embeds the observing `leader_epoch` and `leader_owner_id` (`:670-678`). If identical malformed bytes are disposed in epoch 1, then reappear after epoch 2 takes over, history publication is byte-idempotent but disposition publication targets the old path with different epoch/owner bytes. `publish_immutable_bytes()` therefore raises an immutable collision. The per-observation boundary at `syncer_v4.py:370-378` keeps the leader alive, but it only records `admission_request_deferred`; the duplicate remains in hot discovery and incurs the same collision on every subsequent poll.

Impact: a copied invalid file or a launcher retry after takeover creates permanent hot-path and telemetry churn. It defeats the content-addressed disposition's bounded-replay purpose and worsens the already deferred P6 inode/metadata bound.

Required fix: when invalid history/disposition already exist, validate that the retained history exactly binds the raw digest and bytes and that the first-observer disposition is a valid rejection of that history, then remove the identical hot entry without trying to rewrite epoch ownership. Corrupt or mismatching retained state must remain fail closed.

Missing test: dispose malformed non-UTF-8 bytes under epoch 1, recreate the identical bytes, transfer leadership to epoch 2, and assert the second poll removes the hot entry, preserves the original immutable history/disposition byte-for-byte, emits no unbounded deferred loop, and continues processing a neighboring valid request.

## Low

### L1 — A persistently unreadable/non-regular hot entry emits an unbounded telemetry record on every admission poll

Evidence: every discovery pass materializes an unreadable observation (`admission_v4.py:153-174`) and `_admit_requests()` emits `admission_request_deferred` for it on every loop (`syncer_v4.py:361-369`). The main syncer loop does not establish a per-path backoff or first-observation diagnostic budget. The entry is intentionally not deleted because it could be a transient filesystem observation, but persistent directory/symlink pollution now becomes an unbounded telemetry writer.

Impact: admission availability is preserved, but a single permanent poison entry can grow a per-actor JSONL stream and shared-filesystem traffic without bound during a long run.

Disposition option: close this together with P6 G6 by recording a bounded per-path diagnostic fingerprint and suppressing unchanged repeats until the observation changes or a configured retry interval elapses. The first, changed, and recovered observations should remain visible.

## Positive observations

- Read failures, successfully read malformed bytes and valid requests are now distinct; per-observation isolation prevents one hot entry from terminating a leader candidate.
- Valid history uses the exact canonical bytes from which its digest is derived. Response and rejection namespaces now include the complete fence/request identity, and replay validates the same exact controls used by the public reader.
- Cross-epoch recovery of a genuinely committed admission repairs an admitted disposition without fabricating a rejection; response/current control validation is substantially stronger.
- MODE-02 is owned by P4 and the final runtime, requirement and tracked-evidence gates are source-targeted and internally consistent.

## Required disposition before P4 completion

H1 changes the process-identity fence and must be RED-locked, fixed, and target-validated before P4 closes. M1 must be fixed because it is a deterministic immutable-replay failure. L1 may be fixed with M1 or carried explicitly into the already mandatory P6 bounded-resource work. Because H1 changes a public protocol boundary, freeze a new target and repeat the required incremental independent review.
