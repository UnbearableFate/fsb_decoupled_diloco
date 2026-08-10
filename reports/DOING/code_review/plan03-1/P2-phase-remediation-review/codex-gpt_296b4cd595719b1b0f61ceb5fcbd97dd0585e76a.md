# Mandatory Codex P2 phase-remediation review

- Plan: `plan03-1`
- Review kind: `critical-incremental`
- Continuous base: `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87`
- Frozen target: `296b4cd595719b1b0f61ceb5fcbd97dd0585e76a`
- Findings under review: `P2-F1`, `P2-F2`
- External review: skipped under the user's Codex-only review directive

## Scope inspected

I inspected the complete continuous diff, not only the final formatting commit:
the replacement module-coverage schema and validator, all 81 retained surface
owners, the new CLI/inspection/learner-admission/actor-identity tests, the
one-node validation producer and all of its unit tests, the prior phase report,
and the artifact/source/environment requirements it must satisfy.

Static syntax, JSON, exact retained-surface equality, selector resolution,
literal PBS group and shell checks pass on the login control plane. Project
runtime tests remain intentionally unexecuted until this review is saved.

## P2-F1 closure

The manifest no longer represents a cross-product between a group of surfaces
and unrelated test files. Schema version 2 contains one sorted record for each
retained Python/SQL/config/PBS/shell surface, with a named behavior boundary and
one or more exact `file::test_function` owners. The validator enumerates both
tracked and non-ignored untracked current surfaces through Git, rejects missing
or duplicate surfaces, parses each test module and requires the named top-level
test to exist.

The previously false-covered public boundaries now have direct assertions:

- the four CLI dispatcher branches and remaining-argument forwarding;
- read-only run summarization, JSON projection and CLI assertions;
- manual-close descriptor/reason binding and policy rejection;
- pre-Torch static learner admission plus exact fence revalidation;
- actor-identity shell export versus a newly captured source identity;
- one current learner/syncer public and runtime composition surface.

The remaining shared selectors are specific static or behavioral contracts for
the named surface rather than file-existence claims. P2-F1 is fixed pending
execution of those tests.

## P2-F2 closure

`run_validation_suite.py` is the single current producer for the one-node P2
ladder. It owns a fixed sequence: repository-wide Ruff format, Ruff lint,
explicit focused tests, then the complete suite. It refuses existing outputs,
requires an exact one-node Miyabi PBS identity and all required package
versions, rejects dirty input, captures and compares the complete source
identity before/after execution, records every exact argv/return code/duration,
and publishes the raw log before a create-only, fsynced structured artifact.

The artifact binds `UNIT-01` and `HARNESS-01`, clean commit/scopes/fingerprint,
interpreter/packages/PBS node topology, exact command metrics, errors, one
pre-existing raw evidence path and a non-destructive cleanup projection. Unit
tests independently exercise PASS, command FAIL, dirty-source BLOCKED and
create-only replay refusal. P2-F2 is fixed pending a real compute-node run and
tracking of its compact evidence files in the next frozen target.

## Findings

No new Critical, High, Medium or Low finding remains in this remediation
increment. One clean single-node execution of the producer is authorized. A
non-PASS result must be recorded and remediated before phase promotion.

Verdict: APPROVE
