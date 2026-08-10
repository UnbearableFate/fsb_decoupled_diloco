# Plan 03 `P3-operational-robustness` — second incremental remediation review

- Base commit: `9e1b8238c11b15b88883dffc868ef2cd89adb1b9`
- Target commit: `37eeaef70f417820775ad73c01403d3e113bc082`
- Review range: complete `git diff 9e1b8238c11b15b88883dffc868ef2cd89adb1b9..37eeaef70f417820775ad73c01403d3e113bc082`
- Reviewer: `gpt-5.6-sol`
- Method: static source/diff review plus retained Miyabi evidence; no login-node pytest/Torch execution
- Verdict: **CHANGES_REQUIRED**

## Scope and conclusion

The range correctly remediates the protocol, identity, cleanup, scheduler, schema, and evidence-binding blockers reported against `9e1b823...`. In particular:

- a clean contributor with no completed cycle may acknowledge terminal sequence zero without fabricating receipt zero, while every positive sequence still requires the exact final receipt/proposal contract;
- completed-run validation now cross-checks `.identity.mode` against descriptor-derived bootstrap mode;
- unrelated wandb symlinks are skipped without traversal, candidate roots/ancestors remain fail-closed, and the legacy-policy override is explicit, conservative, recorded, and still checks live authority references;
- positive scheduler evidence ends the current uncertainty episode, a later episode receives a new first-write-wins deadline, manual-review reservations have an explicit fenced release primitive, and both v4 launch tables persist `reservation_released_at` under authority schema revision 6;
- operator failed/expired dispositions release the reservation in the same authority transaction;
- structured requirement evidence is source-bound and can use an independent target-bound runtime artifact instead of the checker output proving itself.

Retained job `2509035.opbs` is outside this frozen target's commit contents but tests the exact target tree: operational static checks passed, focused `296 passed`, full `814 passed`, and the completion marker was emitted. Earlier failed attempts and their fixture-only causes are retained and the three-failure comprehensive review is recorded.

One Medium validation-tool defect remains in the frozen target and blocks phase-final generation. It is already reproduced by the post-target static checker invocation and must be fixed before phase-final.

## Findings

### Critical

None.

### High

None.

### Medium

#### M1 — the phase-final checker treats its not-yet-created output as missing evidence

Evidence: `scripts/miyabi/check_plan03.py` in target `37eeaef...`, function `verify_phase_requirements`, excludes `excluded_evidence_path` only while reading structured JSON. The earlier existence check still computes `missing = [item for item in evidence_paths if not (root / item).exists()]` without the same exclusion. The matrix intentionally names the phase-final checker output, and `main` passes that output as `excluded_evidence_path`; on its first generation the file cannot exist yet, so all 40 requirements receive `missing-evidence:<output>` even though the independent runtime artifact is present and target-bound.

Impact: the strengthened anti-self-proof design cannot produce its first PASS artifact. This is a validation bootstrap bug, not a production protocol defect, but it blocks the required phase-final gate.

Required fix: exclude the designated `excluded_evidence_path` from both existence validation and structured-evidence reading. Missing independent runtime evidence must still block.

Required RED/validation: remove the synthetic checker-self file, retain a correct target-bound runtime evidence JSON, call `verify_phase_requirements(..., excluded_evidence_path="self.json")`, and require PASS. Retain the existing cases proving self-only and stale-source evidence are BLOCKED. Then generate the real phase artifact from a nonexistent output path and require 40/40 PASS.

### Low

#### L1 — the legacy reservation disposition validates strings by calling `.strip()` directly

`resolve_manual_review_launch_request` will raise `AttributeError` rather than a stable `ValueError` if a non-string reason/evidence source crosses an untyped caller. Current callers and type annotations are correct, so this is non-blocking. A shared non-empty-string validator would improve boundary diagnostics.

#### L2 — runtime evidence coverage remains a declared ID list

The checker now prevents stale-source and self-only evidence, a material improvement. It does not independently derive which pytest node covers each invariant from the raw log; `requirements_covered` is curated in the retained summary. Keep the raw-log digest, source commit, owner declarations, full-suite pass, and review disposition together; a future JUnit/node-ID mapping could further reduce manual evidence claims.

## Prior blocking finding disposition

| Finding | Disposition in target |
|---|---|
| Codex H1: zero-cycle terminal ack impossible | Fixed with explicit sequence-zero/no-progress branch and RED |
| Codex M1: completed identity mode not checked | Fixed with descriptor-derived bootstrap-mode cross-check and nested-manifest RED |
| Codex M2: structured evidence stale/self-referential | Substantively fixed; M1 above is the remaining output-bootstrap defect |
| Claude H-1: unrelated wandb symlink blocks cleanup | Fixed by non-following skip plus preservation RED |
| Claude H-2: manual-review reservation has no exit | Fixed by explicit fenced terminal disposition and capacity-recovery RED |
| Claude M-1: stale uncertainty deadline cannot re-arm | Fixed in legacy outbox and v4 authority transitions with positive→new-uncertainty RED |
| Claude M-2: pre-P3 cleanup has no opt-in | Fixed by default-off legacy override and manifest/live-reference RED |
| Claude M-3: v4 schemas/operator action lack tombstone | Fixed in both schema families, authority revision 6, and operator/schema REDs |

## Approval condition

Fix M1 without weakening stale-source or self-evidence rejection, run the static real-output generation successfully, retain its target binding, and include this finding in the disposition artifact. No additional protocol/schema review is required for that checker-only fix; any further persistent-state or concurrency change would require another incremental review.
