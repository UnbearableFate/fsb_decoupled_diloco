# B4 implementation progress

## 2026-07-17 — L0 specification audit and RED

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; S3's shared input-closed predicate and proposal-source abstraction are already present.
- FDR semantics were frozen: one configured grace plus reingest; then partial-quorum merges for the scheduled target fragment, one proposal per learner and bounded by `quorum_max`; repeat across scheduled targets until the current target has no eligible proposal, without skipping round-robin order. Future/stale proposals remain ineligible and are terminalized.
- FDR-02/03 RED adds a fragment selector test requiring a one-of-two partial quorum and an explicit `future_base` rejection. FDR-01 runtime RED remains to be captured with a finite local-only fragment config.
- FDR-01 RED captured on compute node `mg0041` with `fs_diloco_tiny_fragment_terminal_local.yaml`, run `b4_terminal_red_1784289753`: both learners stopped at local step 4 and two fragment merges completed, but syncer continued polling for 3.0 s and stopped as `no_progress_timeout` rather than `input_exhausted`. Summary duration was 6.963 s. Evidence: `artifacts/terminal-red/` and `artifacts/20260717-fdr-runtime-red.log`.
- Selector RED is the expected import failure for the not-yet-implemented fragment drain helper: `artifacts/20260717-fdr-selector-red.log`.

## 2026-07-17 — L1/L2 selector and loop unit GREEN

- Added a shared proposal-source terminal selector used by full and fragment wrappers, plus fragment-specific strict future/staleness terminal dropping. The fragment loop now detects the existing shared stopped-set predicate, performs one grace/reingest, permits partial quorum only after input closure, and exits when the scheduled target fragment is exhausted.
- Compute-node command: `pytest -q tests/test_syncer_selection.py tests/test_fragment_store.py` — **17 passed** (`artifacts/20260717-fdr-unit-green.log`). This covers FDR-02/03, including a 1-of-2 terminal selection and explicit future-base rejection; full selector tests remain green (FDR-05 component guard).

## 2026-07-17 — L3 terminal integration and completion

- FDR-01 GREEN: compute run `b4_terminal_green_1784289898` (`artifacts/terminal-green/`) detected both stopped learners at global event 2, waited the 0.2 s grace/reingest, emitted `fragment_terminal_drain_no_pending_updates`, and stopped as `input_exhausted`. Duration was 4.256 s versus RED's 6.963 s with the artificial 3 s timeout; the terminal gap is now grace-bound rather than no-progress-bound.
- FDR-04: fragment-aware invariant checking now verifies current-only fragment DB rows, per-fragment latest references/files, one materialized full weight, fragment archive identity/completeness, terminal active rows/files, metrics, GC, and temp files. The previously blocked identical checker command now **PASS** (`artifacts/20260717-fragment-checker-pass.log`); DB inspection found zero active fragment proposal rows and zero `gc_pending`, with no proposal tensor remaining.
- FDR-05: a normal full pipeline reached version 4 and the same checker passed (`artifacts/full-guard/`, `artifacts/20260717-full-checker.log`). Full-suite compute result: **217 passed** in 10.69 s (`artifacts/20260717-full-pytest.log`). `ruff check` and `git diff --check` passed.
- Documentation now states that terminal drain covers both modes and records fragment's schedule-preserving exhaustion semantics. FDR-01 through FDR-05 are complete. The optional two-node debug is not a gate; shared-FS visibility is already exercised by the existing checker/runtime contract and no PBS submission was requested for this one-node validation.
