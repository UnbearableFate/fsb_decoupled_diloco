# Codex R2 final-target preformal current-state review

- Plan: `plan03-1`
- Review kind: `preformal-current-state`
- Final source target: `5b474d5c1735beb8cca922fd6cc7b6304926df2c`
- Scope: complete tracked current repository and formal-ladder design
- Requirements: `SURFACE-01`, `CONFIG-01`, `SCHEMA-01`, `CLEAN-01`, `ARCH-01`
- External review: skipped by explicit user direction

## Complete current-state scope

I reviewed every tracked implementation, public entry point, config, schema,
Miyabi launcher/PBS script, Checker, maintenance/cleanup tool, test and current
documentation file. I also reviewed the exact retained-surface inventory,
requirement matrix, artifact schema, formal-ladder manifest, all P1--P3 phase
closure evidence, the initial P4 candidate evidence, the preformal
`CHANGES_REQUIRED` report, all three finding remediations and their one-node
evidence, and the over-baseline nine-node documentation-verification result.

The final target is documentation-complete and restores the single canonical
formal config to exactly 8 learners, 50 local steps and 10 global steps. The
51-by-11 run is retained only as source-bound evidence for the required
documentation synchronization and is explicitly not promoted as the final G1
gate.

## Requirement assessment

### SURFACE-01

The repository exposes one unversioned Full Protocol. Tracked file/path/name
scans find no generation-suffixed product module, config, schema, run layout or
public symbol. The public CLI and console entry points reach the same current
config, learner, syncer, initializer, launcher and analysis implementations.
There is no second baseline, fragment, legacy reader or compatibility layer.

### CONFIG-01

`fs_diloco.core.config.Config` is the only config type and load/resolve/write
path. Exactly three current configs exist: formal static, functional static and
dynamic membership. Unknown or removed keys fail closed, and formal static
workload identity is once again exactly 8 learners, full quorum, 50 local steps
and 10 global steps. PBS/Checker expected arguments match those values.

### SCHEMA-01

`schema.sql` plus the dynamic feature extension are the sole fresh authority
schema family. Revision 10 is consistent across the code constant, schema
CHECK, bootstrap metadata, descriptor, open validation, Checker and tests.
Contributor progress directly owns cursor, receipt-chain and last-update resume
identity in the receipt-ingestion transaction. There is no migration, old
schema reader or archive-dependent fallback.

### CLEAN-01

All 82 executable/config/schema surfaces have exactly one current boundary-test
mapping. Repository-wide spelling and tracked-path scans find no obsolete
Full-Protocol generation, baseline, fragment, legacy or compatibility surface.
Only run-owned artifact cleanup remains; it validates a completed identity,
exact PASS evidence and authority liveness before acting. Formal run evidence
is retained through completed aggregation and archive.

### ARCH-01

Protocol values do not import storage/runtime; storage does not import runtime;
runtime composes explicit services and one leader-fenced `LeaderSession` write
surface. Initialization uses crash-recoverable create-only publication,
immutable source/config identity and fail-closed open validation. Admission,
replacement, resume, proposal ingestion, publication, terminalization,
accounting and audit cleanup each have a single authority owner. Error and
fault outcomes remain distinct from PASS in every gate producer.

## Harness and evidence adequacy

The final ladder is exact: one U1, three 4+1 F1 scenarios, one 8+1 G1 and two
Codex-internal reviews. Each runtime gate binds one clean source identity,
exact config/workload/topology, durable SQLite state, immutable object hashes,
terminal controls and balanced token accounting. The completion aggregate
rejects wrong source, self-input, untracked evidence, unrelated support,
non-internal review and incomplete requirement binding. U1 hash-binds its raw
log and both JUnit files; runtime support must be named by its own gate artifact.

The preformal remediation validation passed Ruff, 169 focused tests and 527
full tests on one confirmed compute node. The documentation prerequisite then
passed a 51-local-step by 11-global-step workload on nine independent nodes:
versions `0..11`, 88 applied proposals, 71,808 direct applied tokens, zero
balance/outstanding, exact attestations, released epoch, terminal agreement,
object hashes and SQLite integrity. Documentation statements match that
tracked artifact.

## Findings and promotion decision

P4-R2-F1, P4-R2-F2 and P4-R2-F3 remain closed. No Critical, High, Medium or Low
finding remains in the final target or formal test design. The target may enter
the final same-source ladder. Any edit under the formal source scopes after
this point invalidates every subsequently collected gate and review artifact.

Verdict: APPROVE
