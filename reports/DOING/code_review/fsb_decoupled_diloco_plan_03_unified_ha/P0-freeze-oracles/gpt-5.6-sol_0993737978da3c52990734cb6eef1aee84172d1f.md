# Plan 03 P0 remediation incremental review — Codex gpt-5.6-sol

- Base commit: `1563e34d8ae582d46e629f6f8bf419e084318043`
- Target commit: `0993737978da3c52990734cb6eef1aee84172d1f`
- Reviewed range: complete `git diff 1563e34d8ae582d46e629f6f8bf419e084318043 0993737978da3c52990734cb6eef1aee84172d1f`
- Scope checked: all first-review remediation in source, oracle/RED/checker/runner tests, shared-FS protocol probe, performance method/PBS, configuration, plan/matrix, docs, review disposition and retained evidence.

## Findings

### Medium — The batch gate discards the checker detail channel required to diagnose `BLOCKED`

- Evidence: `scripts/miyabi/check_plan03.py` can persist its full payload only when `--inventory-output` is supplied; with `--expect` it intentionally prints only `PASS` or `BLOCKED`. `scripts/miyabi/run_plan03_phase0_tests.pbs:34-37` invokes `--expect` without `--inventory-output`. A future count/hash/tag mismatch therefore leaves the PBS log with only `BLOCKED`, while the computed `differences` list and actual inventory are discarded.
- Impact: the successful gate is correct, but a failing P0/P5 migration-surface check would not satisfy the plan's detailed-JSON and failure-reproduction evidence contract. Diagnosing which frozen boundary drifted would require rerunning the checker outside the original job.
- Recommendation: have the PBS script allocate a timestamped neutral-name checker JSON and pass `--inventory-output`; print its path. Keep stdout restricted to the three allowed status tokens. Bind the successful checker artifact in the P0-SURFACE evidence and retain a BLOCKED artifact on failure.
- Missing test: CLI/PBS static test proving that `--expect --inventory-output` writes a payload with `status` and `differences` while stdout remains exactly one status line.

### Low — The oracle “mutation” test does not exercise the oracle comparison path

- Evidence: `tests/reference/test_plan03_classic_static_oracle.py::test_oracle_fixture_comparison_detects_semantic_mutation` creates a modified fixture and directly asserts `mutated == fixture` inside `pytest.raises`. It does not call `_fixture_projection` or the same comparison helper used by the main oracle.
- Impact: the main oracle itself now correctly performs independent store/filesystem roundtrips and compares both arms to separate fixtures, so correctness is covered. The extra negative test overstates what it proves and could remain green if the main comparison were later removed.
- Recommendation: factor a small `_assert_fixture_matches(projection, fixture)` helper, use it in the main oracle for both arms, and call it from the mutation test with a real minimal projection or a copied actual serialized projection.

## Remediation verification

- The base is an ancestor of the target; the target worktree was clean at review start.
- FS fallback now reserves identity atomically in the parent before final-directory creation, hashes all regular objects, and distinguishes 13 pre-visibility from 2 post-marker durability prefixes. Different identity and pre-existing final paths fail closed.
- Corrected performance timing includes HA initialization. Trial coverage/workload mismatch and invalid duration evidence fail before statistics; per-actor file output removes PIPE backpressure. Final 5-pair evidence is workload equivalent and retains signed negative deltas without clipping.
- The strengthened oracle loads proposal/global/outer tensors via paths round-tripped through each store and reads the actual predecessor before commit; both actual projections are compared to their own golden.
- Frozen inventory reconstruction verifies the immutable source commit, manifests, tags and migration boundaries; unit tests cover count/hash/tag drift and matrix evidence discoverability.
- Exact target RED failure evidence, compute full-suite results, PBS static validation and documentation synchronization are present. No production protocol semantics changed in P0.

## Verdict

CHANGES_REQUIRED

The Medium evidence-retention gap should be fixed before closing P0. The Low oracle test cleanup is small and should be included in the same phase-final fix.
