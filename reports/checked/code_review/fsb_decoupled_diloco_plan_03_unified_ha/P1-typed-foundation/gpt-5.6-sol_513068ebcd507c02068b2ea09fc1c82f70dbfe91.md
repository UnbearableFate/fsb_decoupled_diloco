# Codex incremental remediation review — P1-typed-foundation

- Base commit ID: `0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e`
- Target commit ID: `513068ebcd507c02068b2ea09fc1c82f70dbfe91`
- Review scope: complete `git diff 0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e 513068ebcd507c02068b2ea09fc1c82f70dbfe91`, limited to the first-review remediation and its tests/evidence.
- Ancestry: base is the direct ancestor of target.
- Excluded working-tree change: the user-owned `plans/AGENTS.md` edit remains outside both commits.

## Outcome

No Critical, High, or Medium finding remains in this increment. The five High and three Medium findings from the first Codex review are substantively fixed, and the one Low command-ID inconsistency is also fixed.

## Verification of prior findings

### H1 — fixed

`StaticContributorFence`, `DynamicContributorFence`, `CycleReceiptV1`, and `FullUpdateProposalV2` now validate direct construction in `__post_init__`. Direct-construction tests use `dataclasses.replace` to prove untrusted paths, inconsistent tokens, non-finite time, malformed planned IDs, and wrong fence kinds fail before an authority command. Wire decoding still rejects unknown fields and performs the same scalar normalization.

### H2 — fixed

`ingest_proposal` now compares the referenced receipt's run, stable key, cycle identity, all cycle token fields, retained ancestry, cursor range, fence kind/canonical JSON, and `proposal_expected`, in addition to the pre-existing ID/hash/planned-object checks. This occurs after `BEGIN IMMEDIATE` and before existing-update lookup, observation, or insert. The mismatch matrix proves rollback leaves both `updates` and `proposal_observations` empty.

### H3 — fixed

Active static replacement requires the expected generation and a non-empty reason. The coarse command now abandons affected prepared publication intents and selection batches, drops the old fence's pending/selected work and token fate, reconciles other selected contributors, records replaced binding history, and advances the binding generation in one transaction. The new prepared-publication test exercises the whole transition. Terminal-only static shutdown uses the same terminalization primitive.

### H4 — fixed

The impossible immediate pending uniqueness constraint was replaced by a selected-only partial UNIQUE index plus a pending lookup index. `ingest_proposal` inserts the accepted row first, then terminalizes lower pending cycles and their token fates in the same transaction; a late lower cycle is itself terminalized if a newer pending row exists. The consecutive receipt/proposal test proves old pending becomes dropped and the new row is the sole pending proposal. Because only the explicit authority owns writable SQL, no intermediate multi-pending state is externally visible or commit-reachable through the application API.

### H5 — fixed

Plan §6 now distinguishes the implemented P1 foundation subset from concern-specific commands owned by P2–P4. The migration artifact contains exactly 42 legacy keys, each with one target/delete disposition and one blocking owner phase; an architecture test compares that key set with the frozen P0 CSV. The phase no longer claims that future behavior already exists.

### M1/M2/M3 and L1 — fixed

- The removed config path is now exactly `init.resume`; an empty retained init section loads, and direct construction cannot re-enable the removed `syncer_ha` switch.
- Payload open uses `O_NONBLOCK`; schema/finite inspection is followed by a second complete SHA-256 pass on the same descriptor, final metadata comparison, and final pathname-inode comparison. Attempt 1 correctly demonstrated that timestamp metadata alone is insufficient on the target filesystem; attempt 2 verifies the byte-level fix and FIFO race.
- Open validation compares `PRAGMA busy_timeout` with the configured value; a deliberate configuration hook drift is rejected.
- Command IDs use the same 128-character safe identity validator as typed return objects, before any transaction.

## New findings

### Low L2 — Direct and wire validation logic is duplicated

`proposal.py` and `cycle_receipt.py` retain the original detailed validation in `from_dict()` and repeat the invariant set in `__post_init__`. The behavior is correct now and direct-construction tests protect representative cases, but a future field addition could be applied to only one path.

Disposition: `deferred-with-justification` to the P5 protocol-module consolidation owner. Refactor both constructors to parse/coerce into one validated component helper, while keeping strict unknown-field handling at the JSON boundary. This is maintainability risk, not a current acceptance bypass.

### Low L3 — Same-FD payload verification now performs two full file scans

The second digest is required for correctness with the current two-pass “hash then schema inspection” implementation, but doubles shared-filesystem read traffic for every accepted payload.

Disposition: `deferred-with-justification` to P6 G10/performance. Preserve the two-pass implementation until measurement exists; if material, replace it with one streaming verifier that computes digest and validates tensor ranges from the same read snapshot. Correctness and fail-closed behavior take precedence, and the formal performance gate will quantify impact.

### Low L4 — Receipt-link mismatch tests do not include a two-current-contributor reattribution case

The code explicitly compares `stable_contributor_key` and canonical fence JSON, but the new parameterized test varies cycle ID, token partition, retained count, and cursor only. A two-current-contributor case would directly encode the original accounting-attribution threat model.

Disposition: `deferred-with-justification` to P2 safe-ingest adversarial matrix, where multi-contributor conflict/fence cases are already blocking scope. The current P1 code path is direct and covered by the same mismatch branch; no current defect was found.

## Evidence assessment

- First remediation attempt: job `2508666.opbs`, Checker/Ruff passed, focused `1 failed, 246 passed`; complete failure log retained and recorded before the byte-level fix.
- Final remediation attempt: job `2508667.opbs` on `mg0008`, Checker/Ruff passed, focused `247 passed`, full `634 passed, 5 xfailed`; retained log hash matches the structured artifact after documented line-ending normalization.
- `bash -n scripts/miyabi/*.pbs`, literal group checks, Ruff, py_compile, `git diff --check`, and `check_plan03.py --verify-boundaries --require-tracked-evidence` pass.
- The five xfails are unchanged P0-frozen defects assigned to P2/P3, not failures of this remediation.

## Verdict

**APPROVE_WITH_FOLLOWUPS**

The increment is safe to use as the P1 phase-final implementation. L2–L4 are explicitly owned future work and do not weaken the corrected P1 invariants.
