# Codex independent review — P1-typed-foundation

- Base commit ID: `889051d15dfd126e1b9c80eaa222a996570d8423`
- Target commit ID: `0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e`
- Review scope: complete `git diff 889051d15dfd126e1b9c80eaa222a996570d8423 0fa1286b7da1782a913fb02f56a1a8d1b27a2c4e`, including source, DDL, tests, config, PBS validation launcher, Checker changes, and phase evidence.
- Excluded working-tree change: the pre-existing user edit to `plans/AGENTS.md`; it is not part of the target commit and was not reviewed as implementation.

## Critical

None.

## High

### H1 — Typed protocol validation is bypassable through normal dataclass construction

Evidence: `FullUpdateProposalV2` and `CycleReceiptV1` perform all validation only in `from_dict()` (`fs_diloco/protocol/proposal.py:73-198`, `fs_diloco/protocol/cycle_receipt.py:47-159`) and have no `__post_init__`. `StaticContributorFence` and `DynamicContributorFence` have the same issue (`fs_diloco/protocol/contributor.py:18-127`). The public authority methods accept these dataclass instances directly and trust their fields (`fs_diloco/storage/authority.py:751-976`). A caller can therefore construct an absolute/staging proposal path, non-UUID IDs, non-finite timestamps, a fence whose `kind` disagrees with its concrete class, or inconsistent token values without crossing the strict decoder. Some values pass the DDL and can create rows which `_decode_proposal()` can no longer decode.

Impact: the claimed application-side typed boundary is not an invariant; invalid in-process objects can corrupt authoritative state or defer failure until selection/readback. This violates P1 gate item 2 and `PROP-01`/`AUTH-08`'s fail-closed boundary.

Required fix: make direct construction validate the same invariant set as wire decoding (prefer shared field validators used by both `__post_init__` and `from_dict`, without recursive reconstruction), and add RED tests that directly construct malformed proposal, receipt, and fence objects before passing them to authority commands.

### H2 — Proposal ingest does not prove that duplicated proposal fields match the referenced receipt

Evidence: `ingest_proposal()` checks only receipt ID/hash, planned update ID, planned payload digest, and cycle sequence (`fs_diloco/storage/authority.py:874-885`). It does not compare `run_id`, `stable_contributor_key`, `cycle_id`, processed/effective/discarded/retained token counts, cursor range, or contributor fence with the receipt row. The DDL foreign key covers only `cycle_receipt_id` (`fs_diloco/storage/schema_v4.sql:165-205`).

Impact: a well-formed proposal can cite a valid receipt created for another current contributor while attributing the accepted update, fairness credit, and selection slot to itself. Token fate remains attached to the original receipt contributor, so selection/accounting diverge even though every individual object passes decoding. This breaks `PROP-01`, contributor progress semantics, and the token ledger foundation.

Required fix: compare every receipt/proposal shared immutable field and the canonical fence JSON in the ingest transaction before any observation or accepted row is written. Add a two-contributor RED matrix that changes each duplicated field independently and proves no update/observation/frontier mutation occurs.

### H3 — Static attempt replacement is split across commands and leaves old active work stranded

Evidence: `bind_or_replace_static_attempt()` rejects any replacement while the current row is active (`fs_diloco/storage/authority.py:662-666`); callers must first invoke `mark_static_attempt_terminal()` (`:735-763`). Neither command drops the old attempt's pending/selected proposals, abandons affected selection/publication intents, or resets still-current peers. This conflicts with the explicitly frozen coarse-command contract in plan §2.4, which requires those changes in the same transaction as advancing the binding generation.

Impact: after the two-command sequence, old proposals retain a stale fence. Selection can repeatedly fail on them, and the partial unique active-update index can prevent the replacement attempt from publishing. A crash between terminalization and rebinding also exposes an unnecessary partially transitioned state. P1's static generation fence is therefore not operationally complete.

Required fix: make `bind_or_replace_static_attempt` the single fenced replacement transition (with an explicit expected old fence/reason), atomically terminalize the old attempt's work and abandon affected batches/intents using the same invalid/still-current classification as dynamic retirement. Keep a terminal-only command only for actual shutdown if needed. Add crash-free and idempotent replacement tests with pending, selected, and prepared old work.

### H4 — The v4 schema/ingest combination cannot accept a newer pending proposal

Evidence: `idx_active_update_per_contributor` permits only one row for each `(stable_contributor_key, status)` while status is pending/selected (`fs_diloco/storage/schema_v4.sql:246-247`). `ingest_proposal()` inserts the new row directly as `pending` and never supersedes an older pending row (`fs_diloco/storage/authority.py:925-973`). Consequently the insertion fails on the unique index before the plan's required “insert accepted row, then supersede lower-cycle pending” transition can happen.

Impact: a contributor cannot publish a second proposal until its previous pending proposal is selected/committed, defeating asynchronous progress and `PROP-05`/`PROP-08`. The fresh schema also gives P2 no way to implement the specified insert-before-supersede ordering without a schema change or a transient accepted state.

Required fix: redesign the constraint/transaction together. A suitable approach is an accepted-proposal staging table (or an explicit transaction-local adjudication row) that proves the new row can be inserted before the old pending row is terminalized, followed by promotion to pending with a final uniqueness check. Add consecutive-cycle tests for old-pending, old-selected, insert failure, conflict, and exact replay.

### H5 — The P1 retained-command completion gate is not met by the frozen target

Evidence: plan §6.1 item 4 and §6.3 require retained public mutations to be mapped to explicit fenced commands. The committed migration artifact lists only 14 public method spellings and explicitly defers 21 concern-specific commands to P2–P4 (`reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260809-020000_p1-mutator-migration_review.json`). The architecture test asserts only that the current subset exists (`tests/architecture/test_authority_surface.py:21-42`), not that the 42-row disposition inventory is fully represented. Several required coarse transitions, including registration/admission, selection reconciliation, control publication, terminal draining, GC, and archive commands, have no v4 method yet.

Impact: the report's `unmapped: 0` describes a future schedule, not the P1 implementation, so the phase gate cannot truthfully pass at this target. Later phases would otherwise begin on an authority surface whose completeness was already declared.

Required fix: either implement the complete retained command surface in P1 with later phases filling behavior behind already-frozen typed commands, or correct the phase contract/matrix so P1 gates only the common command shell plus an exhaustive disposition schedule and each deferred command becomes a blocking gate in its owning phase. The latter is lower-risk and matches the actual dependency order, but must be explicit rather than claiming all retained commands are already migrated.

## Medium

### M1 — The v4 loader removes the entire `init` section instead of only `init.resume`

Evidence: `_REMOVED_V4_PATHS` contains `("init",)` (`fs_diloco/core/config_v4.py:190-195`), and the loader rejects a path as soon as that top-level key is present (`:198-210`). Plan §3.5 removes `init.resume`, not all model/bootstrap initialization settings. The test at `tests/test_config.py:561-582` encodes the broader, incorrect behavior and therefore does not catch it.

Impact: otherwise valid v4 configurations cannot specify initialization backend/model/checkpoint settings, making the new config schema unusable for non-default initialization.

Required fix: change the removed path to `("init", "resume")`, retain the other typed `init` fields, and add both acceptance and rejection cases.

### M2 — Payload verification has an unclosed same-FD mutation window

Evidence: `verify_proposal_payload()` snapshots `fstat` after hashing, then calls `_inspect_safetensors()` on the descriptor, but never performs another `fstat` after that inspection (`fs_diloco/storage/object_store.py:106-144`). A same-inode writer can mutate tensor bytes after the hash snapshot and during/after schema inspection; the old digest may match while the function returns OK for content that no longer has that digest. The final pathname check compares only device/inode. In addition, `os.open()` lacks `O_NONBLOCK`, so a regular-file-to-FIFO replacement between `lstat` and `open` can block before `fstat` rejects it (`:85-100`).

Impact: `FS-03`/`FS-04` identity verification is not fully fail-closed under the races the implementation claims to defend.

Required fix: open with `O_NONBLOCK` where supported; take a final `fstat` after all descriptor reads and compare device/inode/size/mtime/ctime to the initial snapshot before returning any result. Add deterministic mutation-during-inspection and FIFO-replacement tests.

### M3 — Connection open validation omits the configured busy timeout

Evidence: `_configure_connection()` sets `PRAGMA busy_timeout`, but `_validate_open()` verifies only application/user version, foreign keys, journal mode, and synchronous (`fs_diloco/storage/authority.py:332-341`). Plan §3.2 explicitly requires open-time verification of the busy timeout as well.

Impact: a future connection-path regression or SQLite configuration override could silently run business commands with the wrong lock-wait policy while schema validation still reports success.

Required fix: pass the expected timeout into `_validate_open`, query `PRAGMA busy_timeout`, compare exactly, and add reopen tests for both configured values and deliberate drift.

## Low

### L1 — Command ID limits are inconsistent with typed result decoding

Evidence: `_command()` accepts IDs through 192 characters (`fs_diloco/storage/authority.py:1698-1700`), while `SelectionBatch` and `PublicationIntent` validate command IDs with the protocol identity helper capped at 128 characters (`fs_diloco/protocol/authority.py:90-93`, `:118-121`; helper at `fs_diloco/protocol/_validation.py:13`). A 129–192 character select/prepare command can commit and then fail while decoding its return value.

Impact: committed-success/raised-error ambiguity for an avoidable edge case.

Required fix: use one shared command-ID validator and length limit before every transaction; test both boundary lengths.

## Test and evidence assessment

The retained P1 compute evidence is internally consistent: the final job reports focused `233 passed` and full `620 passed, 5 xfailed`, and the Checker/Ruff stages passed. The two failed attempts are recorded before their fixes, with complete failure logs retained. The PBS launcher has a literal group ID and a ten-minute walltime. The test suite nevertheless concentrates on decoder construction, one-cycle proposal flow, and no-work static rebinding, so it does not exercise the findings above. The mutator artifact is transparent about deferral but does not satisfy the phase gate it cites.

## Verdict

**CHANGES_REQUIRED**

The strict boundary, receipt linkage, static replacement, and active-proposal schema issues are foundational correctness defects. The phase also needs an explicit resolution of the retained-command gate before P1 can be marked complete.
