# Multi-agent review request

- Plan ID: `plan03-1`
- Review ID: `test-P2-full-protocol-harness`
- Review kind: `test-design`
- Base commit: `7d4a607b753744d9b57b54fe0400d1267b13cc40`
- Target commit: `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`
- PBS job ID: `2518445.opbs`
- Compute hostname: `mg0004`
- Target tree: `cef375040795107a819ff5022335c0777ab62e0c`
- Target diff SHA-256: `081752c7955018bc031f95b64ab3a13e00e6a3be1a9e711086850ec9882b5b6a`
- Prompt SHA-256: `0c5ed443950e3650ff884781a43aca1661bc75a95412a0ce21765cfe78ddcec1`

## Common prompt

# External test-design review request

Review kind: `test-design`

- Plan ID: `plan03-1`
- Review ID: `test-P2-full-protocol-harness`
- Phase / work unit: `P2-tests-and-harness` / `P2-W1-reviewed-test-design`
- Experiment ID: `test-design-full-protocol-harness-01`
- Base commit: `7d4a607b753744d9b57b54fe0400d1267b13cc40`
- Target commit: `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`
- Scope: the complete implementation and test-design change from the branch
  point through the frozen target. Inspect affected callers and current-state
  contracts, not only files named `test_*`.

Form your conclusion independently. Do not assume that a test name, process
exit or Checker status proves the claimed invariant. Do not search for or read
another review report.

## Design source and requirements

Read:

- `plans/DOING/plans/plan03-1.md`
- `plans/workflow.md`, especially sections 5, 7, 9, 10 and 16
- `reports/DOING/plan03-1/requirement-matrix.csv`
- `reports/DOING/plan03-1/identity-authority.md`
- `reports/DOING/plan03-1/artifact-schema.md`
- `reports/DOING/plan03-1/test-ladder.md`

Primary requirement rows are `UNIT-01`, `HARNESS-01`, `FUNC-4L1S-01`,
`FAULT-4L1S-01` and `FORMAL-8L1S-01`. Also check that tests support
`SURFACE-01`, `CONFIG-01`, `SCHEMA-01`, `CLEAN-01` and `ARCH-01` without
preserving obsolete interfaces or behavior.

This repository intentionally provides no backward compatibility. Do not ask
for aliases, migration paths, fallbacks, deprecated APIs, historical fixtures
or tests for removed behavior. The target must look as though the current
unversioned Full Protocol was the only design from the beginning. Independent
wire/artifact integers such as receipt/proposal/schema format versions remain
valid identities.

## Implementation and test surfaces

Inspect at least:

- `fs_diloco/core/config.py`, `source_identity.py`, `run_descriptor.py`
- `fs_diloco/protocol/`
- `fs_diloco/storage/authority.py`, `run_initializer.py`, `control.py`,
  `audit_archive.py`, `artifact_policy.py`
- learner/syncer entrypoints and `fs_diloco/runtime/services/`
- `fs_diloco/tools/analysis.py`, `clean_run.py`, `init_run.py`,
  `launch_independent_run.py`
- all `tests/`, with emphasis on
  `tests/harness/test_full_protocol_harness.py`
- `configs/full_protocol_functional.yaml` and
  `configs/full_protocol_static.yaml`
- `scripts/miyabi/run_full_protocol.pbs`, allocation/rank scripts,
  `check_full_protocol_run.py`, source capture and reviewer runner
- README and current docs where they state test or operational behavior.

## Registered scenarios and oracles

Normal 4+1 uses 5 distinct nodes, four learners, full quorum, 20 local optimizer
steps per cycle and four committed successors. Formal 8+1 uses 9 distinct
nodes, full quorum, exactly 50 local steps per cycle and exactly 10 committed
successors. Both use synthetic data/model and zero terminal merges.

Learner replacement terminates `learner_000` after at least one durable credit,
publishes authorization matching the exact old logical launch, attempt and
generation plus the exact new identities, and requires the durable binding
generation/history, progress, terminal ack and balanced ledger.

Syncer takeover pauses the exact primary Python PID after committed version 2
while outside the main SQLite transaction and with the lease renewer quiesced.
The harness kills that PID; a successor must obtain a higher epoch, reconcile
and commit a later version without an alternate successor/publication.

PASS must be based on descriptor/config/source identity, query-only authority
rows, validated hot plus immutable archived history, exact contributor credit
and workload, token-fate balance, immutable publication hashes, actor
attestations bound to the full PBS job and nodefile, terminal authority plus
acks, and registered fault evidence. Exit codes and logs are diagnostic only.

Cleanup belongs to the harness only after terminal proof and a matching PASS.
It must retain authority/recovery state, reject live references and symlink
escapes, inventory exact candidates before opt-in deletion, and persist a
report-side manifest.

## Evidence available before runtime

The workflow requires this review before the first project runtime/PBS
experiment. Therefore there is intentionally no pytest or multi-node result in
this packet. Login-safe static preflight passed on the frozen tree:

```text
git diff --check
bash -n scripts/miyabi/*.pbs
bash -n scripts/miyabi/*.sh
all PBS scripts contain literal group_list=xg24i002
base is an ancestor of target
repository scans find no retired product-generation/config/schema/runtime names
```

Do not treat the absence of runtime evidence as a finding by itself. Review
whether the proposed one-node tests and multi-node Checker design are capable
of accepting a valid current run and rejecting realistic mutations before
those costs are incurred.

Formal source scopes are exactly:

```text
fs_diloco/**
configs/**
scripts/miyabi/**
tests/**
pyproject.toml
README.md
docs/**
```

Changing a scope after final common freeze invalidates dependent formal
evidence. Reports and runtime outputs are outside the source fingerprint.

## Questions to answer

1. Does every requirement map to concrete assertions, commands and an
   independent durable oracle?
2. Can any test or Checker false-pass because it repeats production logic,
   trusts mutable filesystem projections, misses archived state, confuses an
   identity, accepts wrong topology/workload, or uses its own output as proof?
3. Can a correct run false-fail because of timing, maintenance archiving,
   takeover/replacement lifecycle, terminal behavior or PBS ID normalization?
4. Are static/dynamic, success/failure, crash/restart/takeover/replay and path
   boundary cases covered in proportion to the plan?
5. Are source/config/schema/PBS/actor/request/fence/version identities canonical
   and owned once?
6. Does the harness distinguish product failure, harness failure, blocked
   preconditions and infrastructure invalidity?
7. Are walltimes, fail-fast ordering, artifact retention and cleanup safe and
   cost-effective?
8. Which mutation/characterization tests must pass before the first 5-node
   allocation?

## Finding format

For every finding provide:

```text
ID and severity: Critical | High | Medium | Low
Evidence: exact file and line/function, control/data path, and counterexample
Impact: requirement and false-pass/false-fail/fault consequence
Remediation: simplest current-design change, with no compatibility layer
Missing test: focused RED/mutation assertion and expected result
```

Distinguish observed facts from hypotheses. If no finding exists, list the
files, flows and invariants actually inspected. The final non-empty line must
be exactly `Verdict: APPROVE` or `Verdict: CHANGES_REQUIRED`.

