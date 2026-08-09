# Codex independent review — P4 mandatory fenced runtime remediation

- Base commit: `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4`
- Target commit: `d18fae055b5beec1887f38c3f2070f0bf6ec901b`
- Review relation: the base is an ancestor of the target; this report reviews the complete incremental diff `0e8b14ed08eacda710a0f1b4ebf3b19f921f31e4..d18fae055b5beec1887f38c3f2070f0bf6ec901b`.
- Reviewer: Codex, actual model `gpt-5.6-sol`
- Scope: changed protocol/runtime/storage/tooling code, phase tests, PBS validation scripts, Checker evidence, requirement matrix, and phase records.
- Verdict: **CHANGES_REQUIRED**

## Critical

None.

## High

None.

## Medium

### M1 — Hot-request read failures are misclassified as malformed payloads, so one filesystem race can discard a valid request or crash every leader candidate

Evidence: `iter_admission_requests()` catches every `OSError` from `_read_hot_request()` and emits `(path, None)` (`fs_diloco/protocol/admission_v4.py:142-155`). `_admit_requests()` interprets `None` as a malformed request and immediately calls `dispose_invalid_admission_request()` (`fs_diloco/runtime/syncer_v4.py:350-371`). That function performs a second read and then archives/removes the request (`fs_diloco/protocol/admission_v4.py:513-552`). Therefore a one-shot open/read/stat failure followed by a successful second read destroys a valid request as malformed. A persistent failure, including a non-regular `.json` entry rejected by `_read_hot_request()` at lines 809-827, escapes from the disposal read and repeatedly terminates the candidate admission loop. In addition, invalid UTF-8 raises `UnicodeDecodeError`, which is not caught by the iterator's `json.JSONDecodeError` clause and can fail the candidate outright.

Impact: a transient shared-filesystem error can turn a valid registration into a durable rejection, and a polluted/non-regular discovery entry can deny admission service to otherwise healthy candidates. This violates retry-safe hot-path handling and the P4 failover availability goal.

Fix: keep filesystem/open/read failures distinct from successfully read but invalid JSON. Skip and retry transient/non-regular entries without disposing them; classify a regular byte payload as malformed only after it was read successfully. Handle invalid UTF-8 explicitly. Preserve the identity-checked second read before removal.

Missing tests: inject a one-shot `_read_hot_request()` `OSError` and prove the request remains hot and is admitted on the next poll; cover a persistent non-regular entry without candidate termination; cover invalid UTF-8 as one durable malformed disposition rather than an uncaught exception.

### M2 — Valid request history uses a canonical semantic hash but archives non-canonical source bytes, creating an immutable collision and permanent hot-path replay

Evidence: `admission_request_sha256()` hashes canonical JSON bytes (`fs_diloco/protocol/admission_v4.py:456-457`), while `archive_disposed_admission_request()` publishes the original byte encoding under the corresponding history path (`fs_diloco/protocol/admission_v4.py:491-509`). Two valid JSON files containing the same object with different whitespace or key order therefore select the same disposition and history name but have different archive bytes. After the first file creates history, handling the second file reaches `publish_immutable_bytes()` with conflicting bytes; archival fails before `_remove_hot_request()`, so the valid duplicate remains in hot discovery and fails every subsequent poll.

Impact: semantically identical, valid retries are not byte-idempotent. An alternate JSON serializer, copied registration, or restart retry can wedge the leader admission loop despite the disposition protocol being intended to make replay bounded and durable.

Fix: make the valid-request archive content canonical and derived from the same canonicalization as the digest, or define the digest over original bytes and propagate that identity consistently. Canonical archival is the smaller compatible change. Continue using raw-byte hashing for invalid requests where semantic canonicalization is impossible.

Missing tests: publish two differently encoded but semantically identical valid hot requests, process them sequentially, and prove both are removed while the single immutable canonical history object remains unchanged.

### M3 — A disposition can authorize removal even when its referenced response/rejection cannot pass the public admission reader

Evidence: `_validate_admission_disposition()` exact-checks the response's top-level field set and fence, but does not validate the nested `resume` value (`fs_diloco/protocol/admission_v4.py:603-634`). It also does not require the rejected control's `message` to be a string (`fs_diloco/protocol/admission_v4.py:635-649`). By contrast, `read_admission_response()` constructs and validates `ContributorResumeState` and `_raise_valid_rejection()` requires both string fields (`fs_diloco/protocol/admission_v4.py:348-453`). A pre-existing or partially corrupted control can consequently pass disposition validation, cause the hot request to be archived and removed, and then remain unusable by the learner. The current-admission pointer and response digest are likewise not checked at this removal boundary.

Impact: recovery can convert repairable hot state into an unrecoverable immutable control/history state. The producer-side replay validator is weaker than the consumer contract it is supposed to certify.

Fix: factor shared exact decoders/validators for admitted responses and rejected controls, reuse them from both the public reader and disposition validator, and require the exact current pointer (including response hash) before removing an admitted hot request. Failed validation must retain the hot request for repair/retry.

Missing tests: corrupt each nested resume invariant, use a non-string rejection message, and corrupt/remove the current pointer; in every case prove disposition replay refuses archival and retains the hot request.

## Low

### L1 — Hot requests have no byte-size bound

Evidence: `_read_hot_request()` reads arbitrary file length into a list and joins it (`fs_diloco/protocol/admission_v4.py:809-827`); invalid disposal then base64-expands and republishes the full content (`fs_diloco/protocol/admission_v4.py:526-535`).

Impact: an oversized discovery file can cause excessive memory and metadata I/O in every leader candidate. This is especially relevant to the P6 bounded-resource gate.

Recommendation: define a protocol maximum, enforce it from `fstat` and during reads, and retain a bounded diagnostic/digest for oversized invalid input instead of embedding an unbounded base64 payload. Add boundary and oversize tests during P6 if it is not fixed with the P4 replay corrections.

## Positive observations

- The target removes static self-authorization and introduces an explicit operator authorization record/tool.
- Admission disposition/history records materially improve retry cost and avoid rescanning unbounded epoch controls on the normal path.
- Heartbeats now publish exact committed lease state, and latest-version discovery is anchored to epoch/owner-scoped immutable pointers.
- Migration uses validation, a cooperative lock, and create-without-replacement publication rather than unconditional replacement.
- The retained P4 evidence is target-bound and the requirement checker passes with tracked-evidence enforcement.

## Required disposition before P4 completion

M1-M3 require RED tests and fixes because they affect the persistent admission/recovery boundary. L1 may be fixed now or explicitly carried into P6 with bounded-resource ownership and a recorded justification. After changes to these protocol invariants, freeze a new target and perform the required incremental independent review.
