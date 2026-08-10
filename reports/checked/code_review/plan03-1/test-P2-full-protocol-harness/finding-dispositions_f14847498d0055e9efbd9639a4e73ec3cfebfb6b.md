# Test-design finding dispositions

- Plan: `plan03-1`
- Review target: `f14847498d0055e9efbd9639a4e73ec3cfebfb6b`
- External reviewer job: `2518445.opbs`
- Disposition scope: mandatory Codex findings plus independently corroborated
  observations from structurally invalid external raw output.

## External reviewer availability

No external invocation produced a valid reviewer report. Claude failed
authentication, GLM returned an incomplete transcript, DeepSeek returned a
substantive transcript whose final verdict was not in the required exact
format and whose actual model identity was not verifiable, and Kimi timed out.
These statuses are not approvals and do not add blocking external findings.

DeepSeek's raw transcript was read only after the mandatory Codex report had
been saved. Its observations below are treated as coordinator-corroborated
advice, not as a valid external report.

## Accepted findings

| ID | Severity | Source | Decision | Required closure evidence |
|---|---|---|---|---|
| C1 | High | Codex mandatory review | `fixed` pending implementation and runtime verification | Build a deterministic current-schema terminal run, obtain Checker PASS, and mutation-test source/config, epoch/topology, terminal, archive, workload, and publication identities. |
| C2 | High | Codex mandatory review | `fixed` pending implementation and runtime verification | Missing run prerequisite remains BLOCKED; malformed current run/config/schema/audit evidence produces FAIL. |
| C3 | Medium | Codex mandatory review | `fixed` pending implementation and runtime verification | Require exact epoch lifecycle `released` for normal and `expired,released` for takeover, including successor linkage and terminal timestamps. |
| C4 | Medium | Codex mandatory review | `fixed` pending implementation and runtime verification | Track an exact current source-surface-to-test manifest and enforce completeness statically. |
| A1 | Medium | Corroborated DeepSeek raw observation | `fixed` pending implementation and runtime verification | The PBS wrapper must atomically publish a structured BLOCKED artifact if allocation execution exits before the Checker can run. |
| A2 | Medium | Corroborated DeepSeek raw observation | `fixed` pending implementation and runtime verification | Unit-test the actual syncer pause boundary, quiesce ordering, transaction assertion, marker fields, and final SIGSTOP side effect. |
| A4 | Low | Corroborated DeepSeek raw observation | `fixed` pending documentation verification | Remove the obsolete `F-DYNAMIC-CAPACITY` multi-node scenario registration; dynamic behavior remains covered by current focused tests but is not a plan03-1 multi-node acceptance scenario. |

## Rejected observation

### A3 — hostname short-name/FQDN equivalence

Disposition: `rejected-with-evidence`.

The acceptance identity intentionally requires the exact scheduler node name
recorded in `PBS_NODEFILE` to equal actor attestations. Collapsing a short name
and arbitrary FQDN to their first label is not canonical and can alias distinct
hosts. Miyabi's registered topology uses `mg<number>` in both sources, as also
shown by reviewer job `2518445.opbs` (`mg0004`). Lowercasing or stripping a
trailing dot would not establish the proposed short/FQDN equivalence. The
aggregate fixture will instead prove exact match acceptance and mismatch
rejection.

## Promotion decision

Runtime tests remain blocked. The accepted changes touch the Checker,
allocation failure boundary and fault oracle, so the remediated commit requires
a continuous critical-incremental Codex/external test-design review before the
first `interact-g` allocation.
