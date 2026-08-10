# Plan 03 P0 independent code review — Codex gpt-5.6-sol

- Base commit: `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`
- Target commit: `1563e34d8ae582d46e629f6f8bf419e084318043`
- Reviewed range: complete `git diff a00a3d64a50f10a2478c3f4fe795e658d1b3b52f 1563e34d8ae582d46e629f6f8bf419e084318043`
- Scope checked: Plan 03/matrix amendments, P0 evidence and report records, paired-performance helper/runner, shared-FS capability probe, inventory checker, PBS validation launcher, deterministic oracle, RED characterizations and reusable test support.

## Findings

### High — The frozen initializer fallback omits the crash window between `mkdir(final)` and `.identity`

- Evidence: `scripts/miyabi/plan03_fs_capability.py:124-131` creates `final` and then separately creates `.identity`. A process failure after line 125 and before line 126 leaves an empty final directory. Retry enters the `FileExistsError` path and calls `read_bytes()` on a missing `.identity`, so the claimed same-identity recovery is impossible. The crash-prefix loop at lines 180-197 starts fault injection only after the identity has already been written and therefore cannot detect this prefix.
- Impact: P0 freezes and reports a fallback as crash-recoverable even though one publication prefix is permanently ambiguous. Implementing INIT-01 from this contract would allow a crash to strand the authoritative run path and contradict the plan/matrix promise that every initializer crash point is retryable or explicitly fail-closed with an explainable identity.
- Recommendation: reserve identity atomically in the parent before creating/populating `final`, for example by create-no-replace hard-linking a staged identity object to a sibling reservation path whose name is derived from the final root, fsyncing the parent, and only then creating/recovering `final`. A retry must first validate that reservation object. Include prefixes before/after reservation, before/after `mkdir`, every object link/fsync and the complete marker; also test a pre-existing final without a valid reservation and a conflicting reservation.
- Missing test: inject failure after successful final-directory creation but before any in-directory identity object exists, then demonstrate deterministic same-identity recovery without accepting a different identity.

### Medium — The feasibility runner can emit `FEASIBLE` for non-equivalent workloads

- Evidence: `scripts/miyabi/plan03_p0_performance.py:262-297` hard-codes `"status": "FEASIBLE"` and merely records the equality calculation as `workload_equivalent`. It does not reject or downgrade a false value before returning the artifact.
- Impact: a future rerun can publish a passing-looking P0/performance artifact even if arms commit different versions or token totals. That undermines the frozen performance method and can contaminate later G10 evidence if the helper is reused.
- Recommendation: compute the workload signature before the statistic, require a single exact signature across both measured arms (and expected trial count/one row per pair-arm), and raise or return `FAIL/INCOMPARABLE` before computing performance when it differs.
- Missing test: feed trial summaries with one mismatched version/token total and assert that no `FEASIBLE` result or non-inferiority statistic is produced.

### Medium — Duration validation occurs after division and does not reject non-positive candidates

- Evidence: `fs_diloco/tools/paired_performance.py:37-45` divides by each baseline before checking whether the baseline is positive, so zero yields `ZeroDivisionError` rather than the documented validation error. The validation predicate checks only baseline positivity and signed finiteness, so a negative or zero candidate duration is accepted when its derived overhead is finite.
- Impact: malformed timing evidence can escape the common helper or fail with an unstable exception type. This is a production evidence-boundary helper intended for later formal gates.
- Recommendation: convert and validate both arms as finite positive floats before computing signed overheads, then compute/bootstrap. Add zero, negative, NaN and infinity cases for each arm.
- Missing test: parameterized invalid-duration tests covering both sequences and guaranteeing `ValueError` before any statistic is emitted.

## Verified strengths and coverage

- The base is an ancestor of the target and the target worktree scope was frozen in commit `1563e34d8ae582d46e629f6f8bf419e084318043`.
- The classic/static oracle compares the semantic projections before checking golden serialization, including tensor equality, selection order, weights, version and outer state.
- The five accepted RED cases are strict xfails and have separate `--runxfail` evidence tied to the intended assertion sites.
- The requirement matrix, finding triage and mutator disposition are structurally complete, and P0 evidence records source/job/test identities without credentials.
- PBS group/walltime are literal and the final compute log proves the focused and complete suite on a compute node.

## Verdict

CHANGES_REQUIRED

The shared-FS fallback finding is High and blocks P0 completion. The two Medium evidence-boundary defects should be fixed and covered before the phase-final commit.
