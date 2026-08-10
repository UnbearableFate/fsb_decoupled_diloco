# Codex/GPT mandatory test-design review

- Plan: `plan03-1`
- Review kind: `test-design`
- Review ID: `test-P2-full-protocol-harness`
- Phase / work unit: `P2-tests-and-harness` / `P2-W1-reviewed-test-design`
- Base commit: `7d4a607b753744d9b57b54fe0400d1267b13cc40`
- Frozen implementation target: `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`
- Reviewer: Codex/GPT coordinator in the implementation session; this is a
  mandatory self-review, not an independent session.
- Runtime evidence read before this report: none. External reviewer conclusions
  have not been requested or read. Project imports and tests are intentionally
  deferred because the current host is the Miyabi login/control plane and the
  workflow requires this review before the first runtime experiment.

## Scope inspected

The review covered the current-state implementation, not only test filenames:

- config, source identity, descriptor and strict authority open paths in
  `fs_diloco/core/` and `fs_diloco/storage/`;
- learner/syncer entrypoints, merge/terminal/maintenance services, actor
  attestations and process-fault boundary;
- all tests, especially `tests/harness/test_full_protocol_harness.py`, schema,
  fencing, publication, terminal, cleanup and PBS scheduler tests;
- `configs/full_protocol_functional.yaml` and
  `configs/full_protocol_static.yaml`;
- `run_full_protocol.pbs`, allocation/rank launchers, source capture, the
  structured Checker and reviewer runner;
- the frozen identity/authority table, artifact schema, test ladder and
  requirements `UNIT-01`, `HARNESS-01`, `FUNC-4L1S-01`, `FAULT-4L1S-01` and
  `FORMAL-8L1S-01`.

Static inspection confirmed that the target contains one unversioned Full
Protocol product surface, exact functional/formal configs, literal PBS group
IDs, a 15-minute formal request justified by prior larger-run evidence, and a
Checker that reads SQLite in query-only mode, validates audit objects and
immutable publications, reconstructs hot plus archived history, rejects its
own output as evidence, and binds source/config/PBS/actor/workload identities.

## Requirement-to-assertion review

| Requirement | Current assertion/oracle | Review conclusion |
|---|---|---|
| `UNIT-01` | Focused tests exist for protocol, storage, runtime, modeling, tools and architecture boundaries. | The suite is broad, but the required retained-module coverage manifest does not yet exist, so completeness is not demonstrated. |
| `HARNESS-01` | Parser, missing-run artifact, publication hashes/path types, self-proof, token balance, source capture and PBS script contracts have unit tests. | The helpers are tested, but the complete positive/negative `validate_run` projection and audit/attestation schema paths are not exercised as one harness contract. |
| `FUNC-4L1S-01` | Five-node Checker requires four exact contributor IDs, four credits each, versions `0..4`, exact applied proposals/direct tokens, terminal acks, immutable objects, five hosts and SQLite integrity. | Durable oracles are appropriate; the untested aggregate projection leaves false-pass/false-fail risk. |
| `FAULT-4L1S-01` | Replacement uses an exact old/new fence and durable binding history. Takeover kills the exact stopped Python PID outside a transaction and requires two epochs plus successor publications. | Injection layers are registered and durable history is used. The Checker does not yet validate epoch terminal states, and malformed-run exceptions are not correctly separated from infrastructure blocking. |
| `FORMAL-8L1S-01` | Formal config fixes 8 learners, full quorum, 50 local steps, 10 global steps and zero terminal merges. Checker requires exact fault-free receipt/proposal/direct-token counts and 9 distinct attested nodes. | Acceptance formula matches the plan. A pre-launch resolved-config assertion would fail faster, but the post-run Checker is authoritative. |

## Identity, authority, fault, recovery and flow-control review

Normal flow is bound by descriptor SHA, source fingerprint, config SHA, static
contributor fence, receipt/proposal IDs, selection credit, publication ID and
global successor version. SQLite transactions own mutation; immutable files are
content-addressed evidence. Learners wait for an exact receipt ack and then an
adoption/terminal control. Terminal proof is the finalized authority row plus
acked contributor fences and immutable epoch stop object, not process exit.

Replacement occurs after one durable credit. The harness terminates the exact
learner process, publishes an operator authorization matching the old logical
launch/attempt/generation and the new logical launch/attempt, and checks the
current binding plus history. Replay is keyed by immutable requests and fenced
commands. Extra processed work caused by the injected process failure is
allowed only in the fault scenario; applied contribution and token fate remain
exact and balanced.

Takeover pauses the primary Python PID after committed version 2 while the main
SQLite connection is outside a transaction and the renewer is locked out. The
harness kills that PID, starts a candidate on the same syncer node, and requires
a higher epoch that commits a later version. Publication prepare/commit and
reconcile commands are idempotent. Exact version succession and the leader
token transaction are the flow-control fence.

Cleanup is harness-owned only after terminal and PASS projection. The cleaner
requires the current terminal authority, immutable stop, exact evidence target,
artifact policy and no live authority reference; it inventories before any
opt-in deletion and revalidates inode/size/mtime and evidence.

## False-pass and false-fail analysis

- PASS cannot come from actor exit codes, logs, a mutable fixed control alone,
  or the Checker output itself.
- Source dirty state, wrong commit/fingerprint, wrong config checksum, wrong PBS
  job/node topology, missing attestations, non-contiguous versions, unequal
  credits, unbalanced tokens, pending work, missing terminal acks and object
  hash mismatches are explicit failures.
- Timing remains in launch-boundary waits (120 seconds) and lease acquisition,
  but success is decided from durable state after completion. Timeout failures
  therefore need correct product/harness/infra classification.
- Archived history is a necessary input once maintenance moves hot rows. The
  Checker now validates batch/partition/manifest hashes, but no harness unit
  test proves that reconstruction or detects corrupt archive evidence.

## Findings

### C1 — High — aggregate Checker acceptance is not independently tested

`tests/harness/test_full_protocol_harness.py` tests several helpers and a
missing-run result, but never constructs a valid current run/evidence fixture
that reaches `validate_run` PASS. It therefore does not demonstrate that the
combined descriptor, strict config, authority, audit, attestation, PBS,
workload, terminal and publication projection accepts a valid run. It also
lacks mutation probes showing that each major oracle changes that PASS to FAIL.
The 5-node and 9-node allocations would otherwise be the first execution of
most Checker branches, contrary to `HARNESS-01` and workflow 7.2.

Required remediation: add a deterministic current-schema harness fixture or
factor the projection into typed pure inputs, obtain one positive PASS, and add
focused mutations for source/config identity, epoch/attestation topology,
terminal authority, archive integrity, exact workload and object identity.

### C2 — High — malformed product evidence is classified as infrastructure blocking

`check_full_protocol_run.py::main` catches every exception from `validate_run`
and emits `BLOCKED`. Missing preconditions can be blocked, but malformed JSON,
invalid current config/schema, corrupt immutable audit data and authority
identity mismatches are valid acceptance failures. The current behavior cannot
reliably distinguish product/harness failure from infrastructure failure, which
breaks workflow failure accounting and can hide a durable corruption behind a
non-product status.

Required remediation: introduce explicit validation/precondition error classes
or a partial-result boundary so observed run corruption becomes `FAIL`, while
only absent/unavailable prerequisites remain `BLOCKED`; add unit tests for both.

### C3 — Medium — takeover evidence does not assert durable epoch lifecycle states

The Checker requires exactly two epochs, a higher successor and successor
publication, but does not assert the predecessor's durable terminal state or
the successor's terminal/released state. The version-order check cannot by
itself prove the failed/expired predecessor was durably fenced. Unit authority
tests prove stale-token rejection, but the real fault artifact should also bind
the observed epoch lifecycle.

Required remediation: register the exact accepted epoch states for the
takeover and normal scenarios, assert them in the Checker, and cover the oracle
with a mutation test.

### C4 — Medium — retained-module and harness-surface completeness is not inventoried

The plan requires every retained module to have a focused boundary test or a
documented data-only justification. No generated/tracked module-to-test
inventory exists. Static inspection therefore cannot exclude an untested
retained tool, entrypoint or artifact producer, and the current harness list
does not explicitly cover package provenance projection or audit validation.

Required remediation: create the module coverage manifest after the compute
suite is collected, bind each module to tests or a narrow justification, and
add the missing harness-specific assertions before marking `UNIT-01` or
`HARNESS-01` complete.

## Cost and execution decision

No runtime/PBS experiment is authorized yet. The next action is the required
external test-design review of this same frozen target. After all valid external
findings are dispositioned together with C1-C4, remediation must be frozen and
reviewed as required. Only then may the main agent acquire one 1-node
`interact-g` allocation for focused/harness/full pytest. Five-node functional
and fault runs follow that unit gate; the 9-node exact 50-by-10 run remains
post-preformal-review work.

Verdict: CHANGES_REQUIRED
