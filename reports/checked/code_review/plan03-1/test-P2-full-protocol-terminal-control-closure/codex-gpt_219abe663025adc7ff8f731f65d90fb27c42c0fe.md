# Mandatory Codex critical-incremental review

- Plan: `plan03-1`
- Review kind: `critical-incremental`
- Review base: `af54925a4d0487a37c20f298f2027003cb079d20`
- Frozen target: `219abe663025adc7ff8f731f65d90fb27c42c0fe`
- Scope: C5 exact terminal-control evidence and C6 PBS early-exit execution coverage
- Runtime evidence: intentionally absent; the test-design gate still blocks all
  project runtime tests

## Scope inspected

I inspected the continuous target diff, the complete Checker validation path,
the terminal authority row and `ControlPublisher.publish_terminal` projection,
the aggregate fixture and every acceptance mutation, the real PBS wrapper and
its EXIT trap, the new shell-level wrapper test, the prior review disposition,
and the workflow requirements governing test design and external review.

The external availability records added between the two implementation targets
are append-only evidence from job `2518777.opbs`; their snapshot hashes and
runner summary are internally consistent and do not assert an external
approval.

## Acceptance-boundary analysis

### Terminal control identity

The fixture now finalizes terminal state through the fenced authority, reads the
committed authority row, and invokes the same current `ControlPublisher` used by
the syncer before releasing the leader. It no longer manufactures a reduced
stop or summary schema.

The Checker independently derives the exact fixed stop object from the
finalized authority row, current control format, run descriptor and terminal
epoch/owner identity. It also derives the exact summary projection field by
field. Equality is exact in both cases, so missing, changed and extra fields all
fail. The epoch-scoped stop path is derived from the finalized authority owner
and generation, must be a non-writable regular file, and must equal the fixed
cache. The existing authority state, final-version, epoch lifecycle and object
identity checks remain in force.

The mutation table now independently changes the fixed stop schema, summary
schema and immutable object's mode. Each mutation reaches the same aggregate
Checker subprocess as the positive fixture and requires `FAIL` plus cleanup
ineligibility. This directly detects the frozen C5 counterexamples.

### PBS early-exit producer

The new test executes the tracked `run_full_protocol.pbs` under Bash. Only its
dependencies are replaced: the allocation script exits 23, the module command
is inert, and a Checker stub records the received `--blocked-reason` and output
path. The assertions require the original exit status 23 and the exact blocked
reason written to the evidence path. This exercises the real array expansion,
EXIT trap ordering, missing-artifact guard and exit-status preservation instead
of restating source strings. The separate real-Checker blocked-artifact test
continues to own the full artifact schema contract.

## Findings

No Critical, High, Medium or Low finding remains in this continuous scope.

## Required next evidence

This approval is limited to proceeding with the best-effort external
critical-incremental review. Before promotion to the runtime test stage, the
target still requires that external orchestration reach terminal state and that
every valid external finding, if any, be disposed. The focused harness tests,
format/lint checks and full pytest suite must then execute inside the main
agent's one-node `interact-g` allocation.

Verdict: APPROVE
