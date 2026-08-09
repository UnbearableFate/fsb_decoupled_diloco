# Codex independent incremental review — P4 mandatory fenced runtime

- Base commit: `e565ad8f9a71af128c6df7c1dfb4e42a9e520388`
- Target commit: `cb9e4645b4126f46bfa1b73ce302d67e47b0a044`
- Review relation: the base is an ancestor of the target; this report reviews the complete increment `e565ad8f9a71af128c6df7c1dfb4e42a9e520388..cb9e4645b4126f46bfa1b73ce302d67e47b0a044`.
- Reviewer: Codex, actual model `gpt-5.6-sol`
- Independence: completed and saved before starting or reading any Claude review for this target.
- Scope: admission reader/recovery, static authority-command replay, Checker evidence cleanliness, focused/full tests, clean six-job runtime matrix, requirement bindings and cleanup evidence.
- Verdict: **CHANGES_REQUIRED**

## Critical

None.

## High

None.

## Medium

### M1 — Malformed committed replay JSON is downgraded from authority corruption to a learner rejection

Evidence: `LeaderSession.replay_committed_static_binding()` calls `_command_replay()` outside its schema-error translation block (`fs_diloco/storage/authority.py:948-958`). `_command_replay()` decodes `command_records.result_json` directly at `authority.py:4715`, so malformed JSON raises `json.JSONDecodeError`. That exception subclasses `ValueError`, which `_admit_observations_unprotected()` catches as an expected request error at `fs_diloco/runtime/syncer_v4.py:589-594`; it then publishes a permanent learner rejection and archives the hot request.

Impact: corruption of authoritative committed state is misclassified as an invalid admission request. The candidate continues after manufacturing a durable `JSONDecodeError` rejection, while the malformed authority record remains. This regresses the fail-loud schema boundary that the preceding review required for other malformed command-record results.

Required fix: include `_command_replay()` in the replay API's decode/translation block and translate malformed JSON or a non-object result into `AuthoritySchemaError`. Do not add `AuthoritySchemaError` to the request-rejection tuple. Preserve `CommandConflictError` for a valid record with a different canonical request.

Missing RED test: commit a valid static admission, corrupt only that command record's `result_json`, restore the exact hot request, and assert `_admit_requests()` raises `AuthoritySchemaError`, retains the hot request, and publishes neither rejection nor disposition.

## Low

None.

## Positive observations

- The global rejected-disposition fallback validates the complete immutable identity and exact old control path before exposing a permanent rejection, closing repeated-takeover visibility without trusting a current-epoch repair.
- Static replay now reconstructs replacement context from immutable history and retained authorization, then compares the exact canonical command request through `_command_replay`; the same-kind/equal-result collision is RED/GREEN covered.
- Runtime evidence requires an explicit exact-false cleanliness marker, with only one finite named pre-marker artifact retained.
- Clean target `ba2922d` passed all six runtime components, 85 focused tests, 908 full tests, tracked P4 requirement evidence and evidence-bound cleanup.

## Required disposition before P4 completion

Fix M1 with the stated RED test and rerun the focused plus complete P4 gate. This is a narrow error-classification repair and does not alter the public command request, persistence format, or concurrency protocol; after the repair is target-validated, it does not require another recursive phase review unless the remediation expands beyond this boundary.
