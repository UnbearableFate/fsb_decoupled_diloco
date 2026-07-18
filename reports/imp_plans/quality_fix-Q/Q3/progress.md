# Q3 implementation progress

## 2026-07-17 — L0 specification freeze

- DSH-01/02/03/04 semantics are frozen as an infinite stream of independently permuted epochs, cut into fixed-size microbatches. Non-divisible epoch tails cross into the next permutation; flattening and slicing the stream by epoch length must still yield exactly one complete permutation per epoch.
- `data.shuffle_blocks=true` is the new default; `false` reproduces the old modulo batches. Permutations use `training.seed`, learner index, epoch, and a fixed 64-bit mixing function so reproducibility does not depend on process-global RNG state.

## 2026-07-18 — DSH focused implementation verified

- Added deterministic learner/epoch-specific block permutations with a stable
  SplitMix64 seed derivation. Fixed-size batches consume the continuous shuffled
  stream, including batches that cross epoch boundaries.
- Added the default-on `data.shuffle_blocks` switch and threaded
  `training.seed`/learner identity through the WikiText iterator; the disabled
  branch preserves the legacy modulo order exactly.
- Compute-node verification: `pytest -q tests/test_data_shuffle.py` — 3 passed.
- Evidence: `artifacts/20260718-0010_dsh-focused_pass.log`.

## 2026-07-18 — DSH multi-node baseline verification

- All formal Q2/Q4/Q6 9-node jobs used `data.shuffle_blocks=true` from the same
  audited source snapshot and completed workloads beyond the 50×10 baseline;
  their dependent full validation jobs were finite and reproducible across
  three seeds. Together with the deterministic iterator tests, this verifies
  the shuffled path in the production workload rather than only a tiny fixture.
- No compatibility fallback was needed. DSH-01 through DSH-04 are complete and
  shuffled block order remains the default.
