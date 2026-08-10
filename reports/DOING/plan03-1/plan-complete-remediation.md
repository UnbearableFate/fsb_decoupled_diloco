# plan03-1 plan-complete remediation

Preformal target:
`1f02e7b7a4d96cbacca7451b8b902ceebb34de2e`.
Controlling Codex verdict: `CHANGES_REQUIRED`.
External review is skipped by explicit user direction.

## Ordered remediation

1. **P4-R2-F1 — durable resume authority (High, architecture correctness)**
   - Add `contributor_progress.last_update_id` to the sole fresh schema.
   - Store it atomically with receipt progress and read progress directly.
   - Bump the authority schema revision consistently; retain no migration,
     fallback or compatibility reader.
   - Prove the value exists in the progress row itself and recheck static and
     dynamic replacement/terminal lineage.
2. **P4-R2-F2 — evidence binding (High, acceptance correctness)**
   - Require registered support to be a subset of the gate artifact's own raw
     evidence paths.
   - Require U1 to hash-register its full raw/JUnit evidence set.
   - Add an unrelated-support mutation that the aggregate must reject.
3. **P4-R2-F3 — retention ordering (Medium, workflow correctness)**
   - Preserve complete runtime gate inputs through completed aggregation and
     plan archive.
   - Authorize exact PASS-bound cleanup only after that point.
4. Run login-safe static checks, freeze the continuous remediation, and verify
   it on one confirmed Miyabi compute node: affected focused tests first, then
   the complete U1 producer.
5. Perform a Codex critical-incremental rereview of the frozen remediation.
   Only an `APPROVE` verdict with passing candidate evidence may promote the
   repository to `FINAL_COMMON_TARGET_FREEZE`.

The prior P4 candidate artifact remains retained but is invalidated for final
promotion because the formal source and acceptance boundary change.

## Closure

- Remediation implementation: `59abff8978f795e05cb35fc1bf8abb80a8a8bc1a`.
- Clean validation source: `2b0c9a004e04af0907ce7766d4d9df47b29cf545`.
- Validation evidence target: `272fa81331a110f815a52d871c2fd61f7d1c3abb`.
- Codex incremental report:
  `reports/DOING/code_review/plan03-1/plan-complete-preformal-remediation/codex-gpt_272fa81331a110f815a52d871c2fd61f7d1c3abb.md`.
- Verdict: `APPROVE`; P4-R2-F1, P4-R2-F2 and P4-R2-F3 are closed.
- External review remains `skipped-by-user` and contributes no conclusion.
