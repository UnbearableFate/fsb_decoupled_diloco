# E4 implementation progress

## 2026-07-17 — plan audit

- Froze the pointer grain to `(learner, fragment)`. A single learner pointer would incorrectly overwrite proposals for fragments waiting on other round-robin turns.
- The corrected protocol uses `updates/latest/learner_XXX_fNNN.json`, a transactional persistent fragment frontier, process-local file-signature parse short-circuiting, no payload-glob fallback, and fail-closed old-layout resume. The discovery bound is exactly `M×K` paths.

## 2026-07-18 — FDX implementation and boundedness verified

- Fragment writers now atomically replace one pointer per `(learner, fragment)`
  after the immutable tensor is complete; payload directories contain no new
  metadata authority.
- Syncer discovery enumerates exactly the configured `M×K` pointer paths and has
  no payload-glob fallback. An inode/size/mtime/ctime signature cache prevents
  repeat JSON parsing within a process; a restarted syncer parses once and the
  persistent frontier rejects replay.
- `fragment_proposal_frontiers` advances transactionally with same-pair pending
  supersession and insertion; selected rows are never overwritten. Maintenance
  resolves per-pair pointer/frontier references before payload GC. Protocol
  identity is bumped to v3 so old fragment discovery layouts fail resume.
- Focused compute-node group: 27 passed across discovery, fragment store,
  retention, and selection. Evidence:
  `artifacts/20260718-0351_fragment-pointer-focused_pass.log`.
- The 1000-cycle bound test parsed exactly 1000 changed pointers, zero unchanged
  pointers, and ended with the fixed eight-pointer/eight-frontier surface for
  M=4, K=2. Evidence: `artifacts/20260718-0400_fragment-1000-cycle_pass.log`.
- Tiny run `e4_fragment_pointer_tiny_20260718_0403` reached four merge events and
  `stop_after_outer_steps` without error; its only metadata files are the four
  expected M=2, K=2 pointers. Evidence:
  `artifacts/20260718-0403_fragment-pointer-tiny_pass.log`.
- E3's three-seed 9-node fragment matrix subsequently exercised this fixed
  pointer surface under the full M=8, K=8 topology for six successful runs,
  providing cross-node runtime evidence in addition to the 1000-cycle bound and
  tiny test. FDX completion predicates remain satisfied.
