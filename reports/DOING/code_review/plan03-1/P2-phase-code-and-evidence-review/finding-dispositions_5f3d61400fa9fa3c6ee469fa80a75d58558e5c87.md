# P2 phase finding dispositions

- Reviewed target: `5f3d61400fa9fa3c6ee469fa80a75d58558e5c87`
- Remediation target: `296b4cd595719b1b0f61ceb5fcbd97dd0585e76a`
- Mandatory remediation review: `APPROVE`
- External review: skipped by user direction

## P2-F1 — fixed, runtime verification pending

The group-level cross-product manifest was replaced by exact per-surface
ownership with resolvable test selectors. Focused behavioral tests now own the
previously uncovered CLI, inspection, manual-close, learner-admission and actor
identity boundaries.

## P2-F2 — fixed, runtime evidence pending

One create-only atomic validation producer now runs the fixed focused and full
ladder, binds clean source/environment/PBS identity and publishes a schema-
complete raw-log-backed artifact. Its PASS/FAIL/BLOCKED/no-clobber behavior has
unit coverage. Final disposition requires its real one-node artifact to pass
and be tracked in the phase target.

## 2026-08-10T18:35:12+09:00 — runtime closure

- P2-F1: `fixed and verified`; the exact 81-surface manifest and its new
  boundary tests passed in the focused and complete suites.
- P2-F2: `fixed and verified`; the producer published a schema-complete PASS
  from clean source `8d92bcbe2c16cc813fc5cdca6273e869617401ea` in exact
  one-node allocation `2519464.opbs` on `mg0012`.
- The fixed ladder passed Ruff across 127 Python files, 118 focused tests and
  all 504 tests. The compact raw log and structured artifact are retained and
  tracked with this disposition.
