# Q2 implementation progress

## 2026-07-17 — plan audit

- Corrected the evidence scope: one terminal validation value per current-only run cannot support per-merge validation regression. Per-merge linkage will use the next learner update/loss and be labeled observational; validation is reserved for run-level multi-seed policy comparisons.
- Froze the matrix to λ 0.25/1.0/4.0 with `max_staleness_versions=2`, plus a separately labeled fresh-only control using `max_staleness_versions=0`. Fresh-only is not represented as a fictitious infinite λ.

## 2026-07-18 — STL-01/02 implementation

- Added exact per-merge evidence based on the normalized weights actually passed to aggregation: effective staleness mean, fresh effective-weight mass, and a count histogram. The same mode-parameterized helper covers full global bases and fragment-local bases and is emitted to CSV, W&B, and merge events.
- Added legacy-safe analysis linking each merge to the first later learner update/loss. The output is explicitly labeled observational and reports linkage coverage plus the aggregate staleness histogram; it never treats those asynchronous losses as validation measurements.
- Added explicit λ/max-staleness CLI and 9-/8-node launcher overrides with nonnegative validation. Focused evidence: `artifacts/20260718-1145_stl01-02-focused-green.log` (7 passed) and `artifacts/20260718-1240_q2-q5-overrides-green.log` (85 passed shared gate).
- Remaining STL-03/04: the frozen four-condition, three-seed 9-node matrix and Q4 validation comparison.

## 2026-07-18 — STL-03/04 formal submission

- The same audited quality snapshot supplies valid baseline λ=.25/max=2 jobs `2405771,2405635,2405637` (the original seed-1337 source-hygiene run is excluded), λ=1 jobs `2405657/59/61`, λ=4 jobs `2405663/65/67`, and fresh-only max=0 jobs `2405669/71/73`.
- Every training job has a dedicated `afterok` Q4 validation job. All non-staleness variables, evaluator source, protocol, and seeds are paired; fresh-only remains a separate max-window control rather than an infinite-λ label.

## 2026-07-18 — STL-03/04 formal decision

- All 12 train/eval jobs completed with one source fingerprint and validation
  protocol `sha256:691045...`. Three-seed mean validation loss was 3.056161 for
  λ=.25, 3.054640 for λ=1, 3.058688 for λ=4, and 3.099213 for fresh-only.
  Paired mean deltas versus baseline were -0.001521, +0.002527, and +0.043052;
  λ=1/4 remain inside ε=0.01, while every fresh-only seed exceeds ε.
- Seed-1337 observational telemetry confirms that stronger λ changes the
  intended effective weights: effective staleness mean .4751→.2947→.0795 and
  fresh effective-weight mass .5527→.7200→.9228 for λ=.25/1/4. Fresh-only is
  exactly 0/1 but applied only 61.8–78.7% of produced updates across seeds,
  explaining why it is not an acceptable policy default.
- No λ default changes: λ=.25 remains the baseline and fresh-only is explicitly
  not recommended. Because aggressive downweighting changed the weights but did
  not improve validation, STL-04 raises the priority of base-relative
  displacement in `plans/00-RESEARCH_PLAN.md`.
- Evidence: `artifacts/20260718-1605_quality-formal-matrix.csv`,
  `artifacts/20260718-1625_validation-formal.json`, and
  `artifacts/20260718-1630_staleness-observational-s1337.json`. STL-01 through
  STL-04 are complete.
