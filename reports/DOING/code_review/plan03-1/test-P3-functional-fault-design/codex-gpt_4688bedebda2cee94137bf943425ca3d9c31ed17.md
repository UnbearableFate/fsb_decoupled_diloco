# Mandatory Codex P3 functional/fault test-design review

- Plan: `plan03-1`
- Review kind: `test-design`
- Phase base: `9398e822ebe6cf9755e55567b18916802b93162f`
- Frozen target: `4688bedebda2cee94137bf943425ca3d9c31ed17`
- Requirements: `FUNC-4L1S-01`, `FAULT-4L1S-01`
- External review: skipped under the user's Codex-only review directive

## Scope inspected

I inspected the frozen P3 manifest against the strict functional config, PBS
wrapper/allocation/rank launchers, learner and syncer entrypoints, fault seams,
aggregate Checker, identity/authority table, artifact schema, P2 unit evidence,
resource policy and the plan's exact functional/fault requirements. The target
changes no runtime source relative to the approved P2 phase-final commit.

## Workload and topology

All scenarios use one five-node `regular-g` allocation with one rank per node:
rank zero owns the syncer and ranks one through four own exact descriptor
learners `learner_000` through `learner_003`. The qsub resource override and
runtime environment must both state five nodes and four learners, so the
allocation runner's `nodes - 1` assertion detects a topology mismatch before
initialization.

The current functional config fixes synthetic-tiny FP32 identity, synthetic
data, block size 16, seed 1337, full quorum four, 20 local optimizer steps and
four committed global successors. The independent arithmetic is correct:
`20 * 1 accumulation * 1 microbatch * 16 = 320` tokens per applied update and
`4 versions * 4 contributors * 320 = 5120` exact direct applied tokens.

Ten minutes is a defensible shortest request. It is the repository minimum,
while the retained larger 9-node workload completed within 20 minutes and the
fault scenarios add bounded waits of at most 120 seconds plus a 15-second lease
expiry. The manifest keeps adequate startup and teardown margin without using
the longer provisional 20-minute default.

## Oracles and false-pass resistance

The normal scenario's PASS requires durable SQLite and immutable evidence, not
process exit: exact versions 0..4, exact four-way contribution/selection,
receipt/proposal workload, cursor equality, balanced ledger, terminal fences,
one released syncer epoch, fixed/immutable terminal projections, object hashes,
attested PBS topology and SQLite integrity.

Learner replacement is injected only after `learner_000` has durable committed
credit. The old process must exit nonzero; one exact replacement authorization
advances the binding generation by one; binding history must name the old
attempt as replaced and the terminal current row must name the successor.
Common workload/terminal/integrity formulas still apply, so successor startup
without accepted progress cannot pass.

Syncer takeover is injected at committed version 2 by the real runtime fault
seam. The marker must bind the killed PID and prove both no active SQLite
transaction and a quiesced lease renewer. The Checker requires exactly two
epochs with `expired -> released` linkage, successor publication, and no lower
epoch commit at or beyond the successor's first version. The same exact
publication/token/terminal formulas rule out split-brain or merely surviving
processes.

PASS, FAIL and BLOCKED are distinct, evidence publication is create-only, and
scenario submission is serial. A scoped source change after any scenario
invalidates all scenario evidence, preventing mixed-target phase promotion.

## Retention and safety

Each scenario has a unique run, log, PBS stdout and artifact path. Failures
retain full diagnostic state. Successful runs retain the authority, descriptor,
resolved config, terminal controls, attestations and publication identities
through P3 review. Any later pruning is limited to the evidence-bound
`clean_run` planner; the manifest does not authorize whole-run deletion.

## Findings

No Critical, High, Medium or Low test-design finding remains. Submit only the
normal scenario first. Fault jobs remain blocked until its structured artifact
is terminal and classified PASS.

Verdict: APPROVE
