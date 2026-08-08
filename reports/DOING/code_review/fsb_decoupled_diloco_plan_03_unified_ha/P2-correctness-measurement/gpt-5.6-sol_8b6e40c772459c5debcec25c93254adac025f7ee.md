# P2-correctness-measurement incremental remediation review

- Base commit: `e4aba3ee0aee804b8deabb77a9b28bafcbcac7ef`
- Target commit: `8b6e40c772459c5debcec25c93254adac025f7ee`
- Ancestry: verified; base is an ancestor of target.
- Scope: the complete incremental diff, with emphasis on every finding in the first P2 Codex report and the newly added RED/regression tests and evidence.
- Result: **APPROVE_WITH_FOLLOWUPS**

## Prior finding verification

- H1 fixed. `LeaderSession.ingest_proposal` now calls `_require_proposal_receipt` before update-ID/logical-key terminal adjudication; the helper binds receipt hash, run, contributor, cycle, token/cursor ledger, and fence. Only the acceptance path additionally requires planned update/payload equality, preserving legitimate logical-conflict classification. Visibility requires an already ingested matching planned receipt before any tracker/frontier mutation. New tests prove missing receipt collision and visibility gap cannot advance the frontier.
- H2 fixed. Immutable publication defaults to `0444`, rejects requested write bits and writable exact-replay targets, and fsyncs after chmod. Tests prove normal in-place write fails and mutable targets are not accepted as immutable replay.
- M1 fixed for the P2 contract. Repeated identical conflict/collision commands retain fresh observations/conflict records but use one live quarantine disposition rather than raising on the composite unique key. The live quarantine set is pruned to the frozen 64 records per contributor; older observation/conflict audit rows remain available for P3 immutable archive/rollup rather than being silently destroyed.
- M2 fixed. A signature change at the same pointer sequence becomes an immediate, sticky, audited `IDENTITY_MISMATCH`; only a strictly higher generation archives/replaces the live tracker and lower generations are ignored. Old/exact/collision paths are covered.
- M3 fixed. Proposal command identity now hashes the typed immutable request, not device/inode observations. The token-fenced command-record lookup precedes repeatable external I/O, while the normal transactional `_command` still arbitrates races. The same committed command succeeds after its payload path is removed.
- L1 fixed. `git diff --check` passes for the full incremental target; retained diagnostics are unchanged apart from normalized trailing/line-ending whitespace and the new hashes are recorded.

## New findings

No Critical, High, or Medium findings.

### Low L2 — command request hashing is implemented twice

`_command_replay` and `_command` independently canonicalize and hash a request (`fs_diloco/storage/authority.py:3077-3135`). They currently match and the replay regression proves the intended behavior, but later changes could update one path only. Consolidate canonical command identity into one private helper during P5 authority refactoring.

### Low L3 — frozen quarantine bound still enters through an authority default

`LeaderAuthority.__init__` validates and defaults `max_quarantine_records_per_contributor` to the frozen value 64 (`fs_diloco/storage/authority.py:421-482`). This is correct for the P2 standalone authority and tests, but the P4 runtime cutover must pass the validated `maintenance.quarantine_records_per_contributor` value explicitly rather than relying on the default. P3 `AUDIT-02/AUDIT-04` remains responsible for archiving/pruning the separate observation/conflict audit history.

## Validation reviewed

- Static gates: Ruff, format, Plan03 boundary checker, all-PBS `bash -n`, and `git diff --check` pass.
- PBS job `2508780.opbs`, compute `mg0003`: focused `142 passed, 2 xfailed`; full `714 passed, 2 xfailed`.
- The two remaining xfails are the P3-owned fairness and scheduler-uncertainty RED cases, not P2 regressions.
- The only failed remediation attempt was an over-specific diagnostic assertion; the verifier remained fail-closed and the final full suite passed after the assertion was constrained to the stable invariant.

The remediation closes every P2-blocking correctness finding. Low L2 is owned by P5 refactoring; Low L3 is owned by P4 configuration wiring plus P3 audit maintenance. Neither weakens the current P2 authority contract.
