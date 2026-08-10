# Mandatory Codex P3 normal Checker remediation review

- Plan: `plan03-1`
- Review kind: `critical-incremental`
- Continuous base: `4e46b0026e044842cf3956ae7f4c95a5a8f2206b`
- Frozen target: `a040cb9c9a073b87a90cdc467dc1c04f72e16ca0`
- Requirements: `FUNC-4L1S-01`, `HARNESS-01`
- External review: skipped under the user's Codex-only review directive

## Scope inspected

I inspected the complete five-node failure artifact and PBS log, durable hot
and archived receipt/update histories, token rollup, terminal fences, config,
learner loop, terminal authority, Checker change, aggregate positive/mutation
fixtures, module selector and P3 manifest/schema synchronization.

## Failure classification

The run satisfies every registered product oracle: exact versions 0-4, four
applied proposals and credit four per contributor, exact 5120 directly applied
tokens, five-node attestation, released epoch, acknowledged terminal fences,
balanced ledger, immutable publication identities and SQLite integrity. Its
four additional proposals are durably classified as superseded or unselected
terminal work. Full Protocol intentionally allows learners to start another
cycle before observing a newer publication or terminal control.

The removed assertion equated total local attempts with committed global
contributions. That is incompatible with asynchronous supersession and the
terminal protocol, and therefore was a Checker false negative rather than a
product failure.

## Remediation correctness

The replacement oracle preserves the exact applied workload and adds stricter
adjudication of all extra work. In a fault-free run every receipt must declare
a proposal, the receipt/proposal identities must be one-to-one, each row still
passes the existing exact inner-step/token/cursor checks, and every proposal
must end `applied` or `dropped`. Dropped-token authority must equal the number
of dropped exact-workload proposals; processed tokens must equal applied plus
dropped; local discard, quarantine/conflict, unpublished and outstanding work
must all be zero. Existing exact per-contributor applied count, publication
token total, cursor/history equality and ledger balance checks remain intact.

This avoids both failure modes: lawful supersession/terminal overshoot is no
longer rejected, while missing proposal identity, pending/selected state,
unbalanced or non-drop fates, wrong per-cycle workload and excess applied work
still fail independently.

## Test sensitivity and repository consistency

The positive aggregate fixture now executes the product authority path with
one extra proposal that terminal acknowledgement durably drops. The negative
mutation converts that direct fate to quarantine while preserving arithmetic
balance, proving PASS depends on the new semantic restriction rather than only
the old balance oracle. Existing workload mutation remains independent. The
coverage selector names the new exact boundary, and the P3 manifest/artifact
description now state the same applied-versus-processed ontology.

No compatibility path, alternate Checker mode or relaxed legacy behavior is
introduced. The failed run remains non-promotable; all P3 scenarios must be
rerun from the post-review source target after one-node verification.

## Findings

No Critical, High, Medium or Low finding remains. Promote the target to
compute-node validation, not directly to five-node evidence.

Verdict: APPROVE
