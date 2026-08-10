# Critical-incremental test-design review request

Review plan `plan03-1`, phase `P2-tests-and-harness`, work unit
`P2-W1-reviewed-test-design`.

- Review kind: `critical-incremental`
- Continuous base: `af54925a4d0487a37c20f298f2027003cb079d20`
- Frozen target: `219abe663025adc7ff8f731f65d90fb27c42c0fe`
- Experiment: `test-design-full-protocol-terminal-control-closure-01`
- Domain: `harness`
- Requirements: `HARNESS-01`, `HARNESS-02`, `EVID-01`, `AUTH-01`

## Why this review exists

The prior remediated harness target still admitted an impossible positive
fixture: the test hand-built reduced stop/summary objects rather than invoking
the current publisher, and the Checker did not require their exact current
schemas or immutable mode. The PBS early-exit test also inspected strings but
did not execute the actual EXIT trap. Those coordinator findings are C5 (High)
and C6 (Medium).

## In-scope changes

- `scripts/miyabi/check_full_protocol_run.py`
  - independently derives the exact terminal stop and summary projections from
    the finalized SQLite authority row;
  - binds current format, run, epoch and owner identities;
  - requires the epoch terminal object to be immutable and equal the fixed
    stop cache.
- `tests/harness/test_full_protocol_harness.py`
  - builds terminal controls via current `ControlPublisher`;
  - adds stop-schema, summary-schema and file-mode mutations;
  - executes the real PBS wrapper with bounded stubs, asserting exit status 23
    and the exact structured blocked reason.
- `reports/DOING/**`
  - append-only external availability evidence and C5/C6 dispositions.

Directly affected current sources to inspect include
`fs_diloco/storage/control.py`, `fs_diloco/storage/authority.py`,
`fs_diloco/runtime/syncer.py`, `fs_diloco/tools/clean_run.py`,
`scripts/miyabi/run_full_protocol.pbs`, and the Checker artifact producer.

## Required invariants

1. SQLite remains the terminal mutation authority. Fixed control files are
   repairable caches; the epoch stop is an immutable publication.
2. Checker PASS requires exact current schemas. Missing, changed or extra
   stop/summary fields and a writable epoch stop must produce FAIL.
3. The positive fixture must follow the same current terminal publication path
   as runtime and must not reintroduce a reduced or legacy format.
4. The Checker oracle must remain independent enough to detect publisher or
   fixture drift rather than self-validating a shared constructed object.
5. A pre-Checker allocation failure must leave one structured blocked artifact
   when no evidence already exists and preserve the original allocation exit
   status. Existing evidence must never be overwritten.
6. No compatibility alias, old schema, fallback or second product path may be
   introduced.

## Evidence status

Login-safe checks passed for the frozen target: all Miyabi PBS/shell scripts
parse with `bash -n`, every PBS group is literal `xg24i002`, the base is an
ancestor of the target, and the current worktree has no uncommitted product
change. No project Python/runtime test has run yet because the test-design gate
must close first.

Inspect the complete continuous diff and directly affected callers. Focus on
false PASS/FAIL/BLOCKED classifications, schema drift, authority/control
identity mismatch, immutable-path derivation, shell trap semantics, quoting,
exit preservation, and whether each new test would detect a realistic broken
implementation.

For every finding provide severity, concrete evidence with file/line or a
counterexample, required remediation, and the missing test. If there is no
finding, list the sources and invariants actually inspected. Do not infer
runtime success from this design review.

The final non-empty line must be exactly `Verdict: APPROVE` or
`Verdict: CHANGES_REQUIRED`.
