# P2-correctness-measurement Codex review

- Base commit: `7bdf832823b092cc9bdb4195cdf064ea76c68e4d`
- Target commit: `e4aba3ee0aee804b8deabb77a9b28bafcbcac7ef`
- Ancestry: verified; base is an ancestor of target.
- Scope: the complete `git diff` between those commits, including v4 authority/schema/protocol/storage changes, P2 tests, PBS validation, and retained evidence.
- Result: **CHANGES_REQUIRED**

## High

### H1 — conflict and visibility paths can advance the proposal frontier without a contiguous matching receipt

Evidence:

- `fs_diloco/storage/authority.py:961-1032` performs update-ID/logical-key adjudication and calls `_advance_proposal_frontier` before the receipt lookup and immutable receipt-field validation at lines 1033-1060.
- `fs_diloco/storage/authority.py:1256-1450` accepts caller-supplied contributor/cycle/update identities for visibility observations without resolving them to a matching `cycle_receipts` row, then advances the same frontier for terminal visibility outcomes.
- `_advance_frontier_values` at `fs_diloco/storage/authority.py:2531-2567` checks only that some observation row exists. It does not prove that the observation belongs to a consecutively ingested receipt for the same contributor/cycle.
- The frozen `PROP-06` contract explicitly says a missing receipt gap cannot be crossed and requires receipt-gap plus pointer-reorder tests. The P2 tests contain neither a gap test nor a terminal-visibility test bound to a real receipt; `tests/storage/test_visibility_v4.py` uses an empty authority and still advances cycle 1.

Impact: a current contributor—or merely a malformed discovery input passed by the adapter—can submit a conflict/collision or stable visibility failure for an arbitrary future cycle and move `proposal_frontiers` past missing receipts. Discovery can then skip unadjudicated cycles, breaking the hash-chain/cursor ledger and `PROP-06`.

Required fix: split receipt validation into (a) shared receipt identity/hash/run/contributor/cycle/fence/ledger fields required before every replay/conflict/collision terminal adjudication, and (b) planned update/payload equality required only for acceptance. Bind visibility observations to an already ingested `proposal_expected=1` receipt whose planned update ID matches. Refuse gaps before recording a terminal observation/frontier. Add RED tests for missing/mismatched receipts on conflict/collision/visibility, pointer old/new/replay ordering, and a positive logical conflict using the valid receipt for that cycle.

### H2 — objects advertised as immutable remain owner-writable after publication

Evidence:

- `publish_immutable_with_writer` and `publish_immutable_bytes` default to mode `0o644` at `fs_diloco/storage/atomic_io.py:94-100,159-168`.
- `publish_safetensors_immutable`, the proposal/global-weight/outer-state path, does not override that mode.
- The create-no-replace hard link protects only the directory entry. With owner write permission, a stale or accidentally legacy writer can truncate or overwrite the linked inode in place; no pathname collision is involved. A committed DB row then continues to reference a digest/theta identity that no longer describes the artifact.
- P2 tests check replacement collisions and concurrent link publication, but never attempt an in-place write after publish or after commit.

Impact: `PROP-04`, `AUTH-06`, and `PUB-01` depend on artifact content remaining stable after verification/commit. The current helper does not establish that boundary even against the plan's stale-but-non-malicious same-account processes.

Required fix: publish immutable artifacts without write bits (normally `0o444`), fsync after the final metadata change, reject exact replay of a writable target, and add tests proving a normal in-place writer cannot modify a published proposal/checkpoint. Keep mutable pointer/control helpers on their separate atomic-replace path.

## Medium

### M1 — stable conflict replay is neither idempotent nor retention-bounded

Evidence:

- `_record_quarantine` always performs a plain insert at `fs_diloco/storage/authority.py:2488-2516`.
- `proposal_quarantine` simultaneously makes `observation_id` unique and `(stable_contributor_key, cycle_seq, disposition, fingerprint)` unique at `fs_diloco/storage/schema_v4.sql:183-197`.
- Repeating the same already-adjudicated logical conflict under a new deterministic command ID creates a fresh observation and then violates the logical quarantine uniqueness constraint, rolling the whole command back with `sqlite3.IntegrityError` instead of returning `CONFLICT` with auditable disposition.
- Conversely, distinct fingerprints append without any per-contributor pruning. The frozen retention value is 64 and `PROP-10` explicitly requires unique-insert failure to remain auditable and retention to be bounded, but no P2 code consumes `quarantine_records_per_contributor` or `hot_observations_per_contributor`.

Impact: benign rediscovery can turn a terminal conflict into an operational failure, while varied malformed/conflicting inputs make the recovery-hot tables grow without the frozen bound.

Required fix: make terminal quarantine recording idempotent for an existing logical disposition while retaining an observation/audit trail, and enforce the frozen hot quarantine/observation bounds without deleting the frontier-referenced row. Add repeated-same-conflict and >64 distinct-conflict/malformed tests. If older records must remain durable, archive them before pruning as specified by the plan.

### M2 — equal-sequence pointer collisions reset evidence instead of failing closed

Evidence:

- At `fs_diloco/storage/authority.py:1266-1303`, only a strictly lower pointer sequence is rejected as old. Any different signature at the same sequence archives/deletes the existing live tracker and starts a new one.
- Alternating two signatures with the same sequence therefore repeatedly resets missing/malformed grace and count, and an `OK` result on the colliding signature can be accepted as a normal pointer transition.
- The frozen P2 gate explicitly calls for pointer old/new/replay reorder coverage, but `tests/storage/test_visibility_v4.py` only exercises strictly increasing sequences.

Impact: a torn/colliding fixed pointer is not fail-closed and can suppress stable terminal disposition indefinitely.

Required fix: define `(contributor, pointer_sequence)` as a single immutable pointer generation. Treat same-sequence/different-signature as an immediate identity collision (with bounded audit/quarantine), archive only when the sequence strictly increases, and test old replay, exact replay, same-sequence collision, and forward replacement.

### M3 — proposal command replay is coupled to mutable filesystem observations

Evidence:

- `ingest_proposal` verifies the payload before consulting `command_records` at `fs_diloco/storage/authority.py:945-952`.
- It includes the complete `VerifiedPayload`—including device and inode—in the command request hash, although those are verification observations rather than immutable command input.
- The common `_command` replay check therefore cannot return an already committed result if the payload has since been archived/missing, and a same-bytes file restored on another inode turns the same command ID/request into `CommandConflictError`.
- The plan requires the same deterministic command ID plus the same immutable request to return the stored result.

Impact: a crash after DB commit but before the caller receives the result can cease to be replayable due to later filesystem state, undermining command idempotency and recovery.

Required fix: hash only immutable protocol request fields. Add a token-fenced command-record fast path before external I/O; if no record exists, perform payload verification and then enter the normal transactional command path. Add missing-object-after-commit and same-bytes/new-inode replay tests.

## Low

### L1 — retained raw failure logs make the frozen target fail `git diff --check`

The four newly tracked failure logs contain many trailing spaces from module/pytest output. They are valid evidence, but the review-target command `git diff --check 7bdf832... e4aba3e...` reports whitespace errors. Normalize only line-ending whitespace without changing diagnostic content and record the resulting hashes in the remediation artifact.

## Coverage checked

- Proposal insert/supersession/crash rollback, typed payload checks, disposition/frontier schema.
- Dynamic admission/replacement/retire/drain, selection-time and commit-time row classification.
- Visibility grace/count/archive logic and read-result typing.
- Immutable hard-link publication, checkpoint theta verification, prepared/committed/abandoned intent reconciliation, orphan grace/claim.
- Hypothesis state machines, P0 RED conversions, full-suite/PBS evidence, schema/index constraints, architecture surface tests.

The focused and full compute results are useful regression evidence, but the missing adversarial cases above directly contradict P2 completion contracts. Critical findings: none. High findings: 2. Medium findings: 3. Low findings: 1.
