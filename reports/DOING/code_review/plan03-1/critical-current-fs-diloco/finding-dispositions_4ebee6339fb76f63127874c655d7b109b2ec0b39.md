# Current `fs_diloco` review synthesis

- Reviewed target: `4ebee6339fb76f63127874c655d7b109b2ec0b39`
- Continuity base: `7d4a607b753744d9b57b54fe0400d1267b13cc40`
- PBS job: `2519854.opbs`
- Compute node: `mg0021`
- Valid external report: `claude-opus-5` (actual model verified from raw
  `modelUsage`)
- Other invocations: GLM-5.2, DeepSeek V4 Flash, and MiniMax M3 timed out and
  produced no valid report. The runner's fourth legacy slot is named
  `kimi-k3`, but the submitted requested model was
  `opencode-go/minimax-m3`; Kimi K3 was not requested.

## Coordinator validation

The Claude report is structurally complete, covers the requested frozen target,
names the correct base and target, ends in `CHANGES_REQUIRED`, and is bound to
an unchanged read-only snapshot digest. Raw invocation metadata verifies actual
model `claude-opus-5`, session
`3ae1cd2a-b730-4231-88ba-f1099ece1bcd`, successful terminal reason, and no
permission denial.

While the reviewer job was running, the shared branch advanced to
`e1f76a85f77c33765ebaaddb4828e40cc45d4d24`. The only committed product/harness
changes after the reviewed target concern terminal applied-token authority;
they do not close the findings below. At synthesis time, uncommitted concurrent
work has begun addressing FSD-H2 by making the fault scenario authoritative and
checking exact binding generations/history. That work is not yet frozen,
tested, or reviewed, so FSD-H2 remains open.

## Blocking findings

| ID | Severity | Decision | Required direction |
|---|---|---|---|
| FSD-H1 | Critical | accepted-open | Make validation results structured and false-PASS resistant: publish and validate collected/pass/fail/error/skip counts instead of treating return code zero plus a free-text log as the complete UNIT/HARNESS oracle. |
| FSD-H2 | High | accepted-open; remediation in progress | Bind the fault scenario to Checker expectations and unconditionally reject binding generations/history that do not match the registered normal or replacement scenario. |
| FSD-H3 | High | accepted-open | Carry the durable final planned update identity through contributor resume/admission so a replacement or restart can produce a valid graceful terminal acknowledgement. |
| FSD-H4 | High | accepted-open | Add valid fixtures and mutation coverage for every learner-replacement and syncer-takeover branch in the aggregate Checker before another fault experiment or promotion. |

These findings invalidate promotion based only on the prior Codex phase review.

## Non-blocking but required remediation

| ID | Severity | Decision | Summary |
|---|---|---|---|
| FSD-M1 | Medium | accepted-open | `runtime/syncer.py` lacks behavioural composition coverage for static replacement admission and proposal ingestion. |
| FSD-M2 | Medium | accepted-open | The registered stale-writer rejection claims cannot be produced by the current strictly sequential kill-then-successor fault injections. Align injection and durable evidence without weakening the authority invariant. |
| FSD-M3 | Medium | accepted-open | Repository config discovery is CWD-relative and can become an empty skipped parameter set; resolve from the test file and assert non-empty discovery. |
| FSD-M4 | Medium | partially accepted-open | The vacuous/path-only scan gap is valid. The suggestion that `CycleReceiptV1` and `FullUpdateProposalV2` are obsolete is rejected: the startup inventory explicitly retains independently versioned wire artifacts. Add a content scan with a precise allowlist for current non-product versions. |
| FSD-M5 | Medium | accepted-open | Narrow proposal/receipt ingestion exception handling so schema, command-conflict, and stale-leader integrity failures become durable terminal errors instead of unbounded telemetry-only retries. |
| FSD-M6 | Medium | accepted-open | Give the takeover boundary one authoritative configured value consumed by launcher and Checker. |
| FSD-M7 | Medium | accepted-open | Remove unused duplicate console/dispatcher/re-export surfaces or select and document exactly one current invocation interface. |

## Low findings

- `FSD-L1` through `FSD-L6` are accepted for cleanup: duplicated owner-path
  derivation, an unwritten schema enum value, conflicting Python-version
  declarations, stale config defaults, swallowed leader-release failure, and
  repeated/inert static configuration.
- `FSD-L7` is informational, not a source defect. Dynamic membership has unit
  coverage but is intentionally outside the current P3 multi-node requirement;
  its evidence boundary should be explicit before final aggregation.

## Overall decision

The independently valid external review supplies concrete counterexamples in
current runtime and Checker paths. Evidence takes precedence over the earlier
Codex-only approval.

Verdict: CHANGES_REQUIRED
