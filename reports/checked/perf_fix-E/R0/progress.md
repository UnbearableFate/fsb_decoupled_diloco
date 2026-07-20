# R0 source-provenance progress

## 2026-07-17 — prerequisite audit

- Baseline HEAD is `c359b8322c33e0101328c2fc8522271691f1e52c` on `buf-fixing`, with preserved user edits plus completed bug_fix-B work in a dirty tree.
- The E/Q indexes require controlled comparisons, but current `run_identity()` records neither commit nor dirty state. Formal experiments are blocked until identity/resolved config carry `git_commit`, `git_dirty`, and an immutable source fingerprint. Because no commit authorization was given, the corrected protocol accepts an identical archived fingerprint/manifest for every run in a comparison and never labels HEAD alone as the source version.
- No formal E/Q comparison job was submitted before this gate.

## 2026-07-18 — SRC runtime identity gate verified

- Resolved configs and SQLite run identity now carry `git_commit`, `git_dirty`,
  and `source_fingerprint` from the frozen launcher environment.
- Formal launchers can set `FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1` to fail closed
  when commit or fingerprint is missing; environment boolean values are parsed
  explicitly instead of relying on non-empty string truthiness.
- Compute-node verification: `pytest -q tests/test_source_identity.py` — 2 passed.
- Evidence: `artifacts/20260718-0013_source-identity-focused_pass.log`.

## 2026-07-18 — SRC manifest capture and formal launcher gate verified

- Added an atomic source-manifest capture program. Its fingerprint covers
  tracked, modified, deleted, and untracked files under the runtime/config/script
  scopes, including paths, modes, sizes, symlink targets, and content hashes.
- Every 9-node launcher now archives `control/source_identity.json`, sources the
  generated fail-closed environment, and propagates the frozen identity to every
  MPI rank.
- Compute-node verification:
  `pytest -q tests/test_capture_source_identity.py tests/test_source_identity.py`
  — 3 passed.
- Login-node static verification: source helper compiled; `bash -n
  scripts/miyabi/*.pbs` passed; no group placeholder remains.
- Evidence: `artifacts/20260718-0033_source-identity-focused_pass.log`.
