# Codex incremental review — P3 operational remediation

- Reviewer: `gpt-5.6-sol`
- Base commit: `de3d27879fdef188afa03a233acd4b40d90e5feb`
- Target commit: `9e1b8238c11b15b88883dffc868ef2cd89adb1b9`
- Scope: complete `git diff de3d27879fdef188afa03a233acd4b40d90e5feb 9e1b8238c11b15b88883dffc868ef2cd89adb1b9`, including source, DDL, tests, Checker/PBS, plan/matrix, prior review reports and retained evidence.
- Ancestry: base is an ancestor of target.
- Independence: this report was completed and saved before invoking or reading the fresh Claude review for this target.
- Runtime policy: review used source/diff/static inspection only. It did not run pytest on the login node. The target's accepted compute evidence is PBS job `2508975.opbs` (`294` focused and `808` full tests passed).

## Verdict

**CHANGES_REQUIRED**

The previous target's Critical/High remediation is substantially correct, but one terminal liveness edge was made too strict and two identity/evidence bindings remain incomplete.

## High

### H1 — A contributor that cleanly closes before completing its first cycle cannot acknowledge sequence zero

- Evidence: `acknowledge_terminal_contributor` accepts a frozen close sequence and observed progress of `0`, so `final_cycle_seq=0` is a valid contiguous clean acknowledgement for a contributor with no completed cycle. The new code then unconditionally queries `cycle_receipts` for sequence zero and raises `MembershipFenceError("final cycle receipt is missing")` when none exists (`fs_diloco/storage/authority.py`, final-ack branch). Cycle sequences start at one, so a zero-work contributor cannot have such a receipt.
- Impact: a healthy actor that receives close before its first cycle must be falsely classified as a hard crash. This can add a crash-gap disposition and prevents the intended zero-gap clean drain path. With a zero hard-crash budget it can also leave the operator without a semantically correct acknowledgement route.
- Required fix: treat `final_cycle_seq == 0`, `progress is None`, and `final_update_id is None` as the one receipt-free clean acknowledgement. Continue to require an exact receipt/proposal contract for every positive sequence. Add a RED test that freezes an active contributor at sequence zero, acknowledges zero, and finalizes with zero crash gap.

## Medium

### M1 — The initializer persists `identity.mode` for retry arbitration but completed-run validation does not bind it

- Evidence: the remediation adds `mode` to `.identity` and compares it on staged retry, but `_validate_completed_protocol_identity` checks only `run_id`, `source_fingerprint`, `config_sha256`, and `logical_root` against the descriptor. It derives `bootstrap_mode` later and never compares `identity["mode"]` to it.
- Impact: the newly declared full identity is not consistently validated after publication or during explicit repair. A malformed/tampered identity can claim a mode different from descriptor/DB while its self-hash remains internally valid, weakening the exact identity contract the remediation documents.
- Required fix: derive descriptor/bootstrap mode before constructing `identity_checks`, include `mode`, and add a fail-closed mismatch test.

### M2 — The retained structured requirement artifact is not bound to the target commit it claims to validate

- Evidence: `20260809-062900_p3-review-remediation-requirements_pass.json` records `source_identity.commit=de3d278...`, the pre-remediation HEAD, while it is used as evidence for target `9e1b823...`. `verify_phase_requirements` accepts any cited JSON containing `checks.requirements.<ID>.status=PASS`; it does not require that artifact's source commit equal the reviewed target. The artifact also self-cites as its own structured evidence path.
- Impact: a stale structured PASS artifact can satisfy all 40 matrix contracts after source changes. The behavior suites still provide strong evidence, so this is an evidence-binding defect rather than a demonstrated runtime failure.
- Required fix: regenerate the artifact from `--source-ref 9e1b823...` and require structured evidence `source_identity.commit` to equal an explicit expected source ref/commit at the phase-final gate. Avoid treating an output file as proof of its own validity; use the existing tracked prior artifact as bootstrap input only, then validate the regenerated target-bound artifact separately.

## Reviewed remediation without additional findings

- `clean_run` now fails closed on missing policy/symlinked traversal and anchors each unlink through directory file descriptors with no-follow semantics.
- Scheduler uncertainty deadlines are first-write-wins; no-job reconciliation continues positive-evidence lookup and reaches manual review; reservation capacity derives from unreleased tombstones.
- Audit history retains the latest version closure, and audit GC claims are transferable to a successor epoch.
- Terminal close snapshot re-entry is rejected; positive-cycle final acknowledgements now bind `proposal_expected` and the planned update.
- Schema revision 5 accurately marks the incompatible P3 DDL; token fate preserves applied-version history; runtime primitive-vs-P4-wiring language is now explicit.
- The golden test generates v4 merge/outer-optimizer tensor bytes rather than comparing only static fixtures.
