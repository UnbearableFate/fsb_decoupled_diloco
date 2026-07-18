# perf_fix-E / quality_fix-Q completion audit

Date: 2026-07-18

Scope: `plans/perf_fix-E` and `plans/quality_fix-Q`, including the shared source
identity prerequisite. This audit treats each plan's numbered completion
predicate and test-matrix ID as incomplete until a current source, test, run, or
structured artifact proves it.

## Plan corrections made before acceptance

| Area | Problem found | Correction |
|---|---|---|
| shared R0 | dirty worktree had no executable identity equivalent to a clean commit | source-scoped manifest records commit/dirty/fingerprint; formal groups require identical fingerprints and exclude bytecode |
| E3 | missing/nonpositive materialization meant the most expensive behavior; normal input exhaustion could leave a stale materialized full checkpoint | fragment mode now requires a positive explicit interval and every normal stop forces final materialization |
| E4 | one pointer per learner would overwrite proposals for other fragments | pointer/frontier grain frozen to `(learner, fragment)` with exactly M×K discovery paths |
| E5 | CPU/collocation decision had no frozen quantitative boundary | CPU p95 `<4 s` and three-seed median walltime regression `≤10%` frozen before the deployment comparison |
| E6 | pause share could not use inferred process walltime | completed-cycle elapsed is recorded explicitly; old runs without it are `unavailable` |
| Q2 | terminal validation cannot support a fictitious per-merge validation regression | per-merge linkage is explicitly observational learner loss; validation is run-level and multi-seed |
| Q4 | lm-eval WikiText task did not prove split/tokenization equivalence | dedicated configured-split loss/ppl evaluator and independent `afterok` one-node job |
| Q5 | first workload reached v50 before input closure; plan metadata also called capture “zero code” | formal trigger now requires input closure before any global target; default-off capture/evaluator code is in scope |
| Q5 evaluator | predecessor checkpoint was correct but top-level result version fell back to terminal latest | predecessor results use capture `source_global_version`; regression added and all six evals rerun from fingerprint `sha256:8474988c...` |
| Q6 | “slope CI must contain zero” rejects a safely decreasing error; scope denied code while requiring a reproducible decision | only wholly positive slope CI fails; original failure retained, decreasing regression added, executable three-state gate explicitly Q6-owned |

No threshold was relaxed to force a positive outcome. Q5's ε=.01 and Q6's
loss/ratio limits are unchanged. Q6's slope correction changes the tested risk
direction, and the first invalid formal result remains archived.

## Requirement-by-requirement evidence

| Plan / IDs | Authoritative evidence | Audit result |
|---|---|---|
| R0 source identity | `perf_fix-E/R0/artifacts/20260718-0033_source-identity-focused_pass.log`; quality fingerprint `sha256:122f698...`; Q5 evaluator fingerprint `sha256:8474988c...` stable across two captures | complete |
| E1 PIO-01/03/04/05 | `E1/artifacts/20260718-0224_parallel-publication-focused_pass.log`, `20260718-0238_crash-matrix-full_pass.log`, `20260718-0231_parallel-tiny_pass.log` | complete: worker timing/bytes/error fields, DB-after-both ordering, all failure orders and six crash stages |
| E1 PIO-02/L3 | `E1/artifacts/20260718-1605_pio-formal-matrix.csv`, `20260718-1610_publish-dtype-formal-matrix.csv`, Q6 corrected gate | complete: three seeds/single variable; parallel publish -44.5%; BF16 bytes -50% but publish +62.5%, so FP32 default retained |
| E2 OVL-01–05 | `E2/artifacts/20260718-0505_ovl01-04-focused-green.log`, `20260718-0534_ovl02-pointer-poll-pass.json`, `20260718-0541_ovl04-crash-matrix-pass.log`, `20260718-1300_e2-formal-matrix.csv` | complete: residual <0.3%, poll CPU .0402%, scan time -2.39%; publish-ingest +0.08%, kept opt-in |
| E3 MAT-01–03 | `E3/artifacts/20260718-0016_fragment-materialization-focused_pass.log` | complete: fail-closed config, no implicit every-event branch, telemetry and forced terminal materialization |
| E3 MAT-04 | `E3/artifacts/20260718-1620_mat-formal-matrix.csv` | complete: three paired seeds; bytes -90%, materialization time -81.45%, production interval 10 |
| E4 FDX-01–04 | `E4/artifacts/20260718-0351_fragment-pointer-focused_pass.log`, `20260718-0400_fragment-1000-cycle_pass.log`, `20260718-0403_fragment-pointer-tiny_pass.log`; E3 six 9-node runs | complete: exactly M×K paths/frontiers, signature short-circuit, restart replay rejection, bounded 1000 cycles and runtime equivalence |
| E5 SNC-01 | `E5/artifacts/20260718-0647_snc01-existing-run-ledger-pass.json` | complete: 5.362% duty cycle and 0.3103 estimated idle GPU node-hours |
| E5 SNC-02 | `E5/artifacts/20260718-0702_snc02-tiny-{cpu,gpu}-pass.json`, `20260718-0710_snc02-gpt2-cpu-pass.json`, `20260718-0713_snc02-gpt2-gpu-pass.json` | complete: both scales/devices; CPU merge p95 .2866 s passes 4 s gate |
| E5 SNC-03 | `E5/artifacts/20260718-1315_snc03-colocated-smoke.csv`, `20260718-1500_snc03-formal-pairs.csv` | complete: smoke and all three pairs exit zero; median +1.83% passes, outlier keeps 9-node default |
| E6 APT-01–03 | `E6/artifacts/20260718-0140_adoption-telemetry-focused_pass.log`, `20260718-0128_full-tiny_pass.log`, `20260718-0135_fragment-tiny_pass.log`, `20260718-1625_nine-node-adoption.json` | complete: split pause fields, explicit denominator, legacy unavailable behavior, tiny and current 9-node baselines |
| Q1 | `bug_fix-B/B2/artifacts/20260717-sch-green-attempt2.log`, pipeline assertions/checkers, Q1 progress and run-analysis correction | complete: SCH-02/03 evidence referenced, historical confound declared, all formal Q runs are post-B2/Q3 |
| Q3 DSH-01–04 | `Q3/artifacts/20260718-0010_dsh-focused_pass.log`; all formal quality runs use shuffle=true | complete: permutation integrity, cross-epoch tail, variation, determinism and disabled legacy anchor |
| Q4 PVE-01 | `quality_fix-Q/validation_protocol.md`, `Q4/artifacts/20260718-0850_pve01-focused-green.log` | complete: exact split/tokenizer/EOS/block/shift/token reduction and identity schema |
| Q4 PVE-02/03 | `Q4/artifacts/20260718-1020_pve02-03-legacy-comparison-pass.json` and four full validation logs | complete: four checkpoints and explicit local/validation ranking divergence |
| Q4 PVE-04/05 | `Q4/artifacts/20260718-1610_strategy-formal-matrix.csv`; nine run validation results; submitter/evaluator tests | complete: three seeds per strategy, dependent one-node runtime results nonempty/finite/identified/attached; replace remains default |
| Q6 QGB-01–03 | `Q6/artifacts/20260718-1100_qgb-focused-green.log`, retained invalid `20260718-1515_qgb03-formal-gate.json`, corrected `20260718-1600_qgb03-corrected-formal-gate.json` | complete: ε=.01, paired/worst pass, all trends decreasing/bounded; quality PASS without a BF16 performance default |
| Q2 STL-01/02 | `Q2/artifacts/20260718-1145_stl01-02-focused-green.log`, `20260718-1630_staleness-observational-s1337.json` | complete: effective weighted fields and explicitly observational linkage analysis |
| Q2 STL-03/04 | `Q2/artifacts/20260718-1605_quality-formal-matrix.csv`, `20260718-1625_validation-formal.json`, `plans/00-RESEARCH_PLAN.md` §4.5 | complete: 12 run/evals; λ default unchanged, fresh-only rejected, displacement priority raised |
| Q5 TMN-01/02 | `Q5/artifacts/20260718-1640_terminal-paired-quality.json`, `20260718-1645_terminal-checker-pass.log`; jobs 2406953/54/55 and corrected evals 2407037/38/39 + 2407049/50/51 | complete: three checksum-valid selected=3/quorum=4 captures, correct pre/post versions, all jobs exit zero, mean degradation -0.000330 |
| Q5 TMN-03/04 | TMN-02 did not cross ε=.01 | N/A by the plan's explicit conditional branch; scaling code and extra protocol surface correctly not added |

## Final repository gates

- Compute node `mg0002`, interactive PBS job `2406911`: `pytest -q` → **288
  passed in 21.17 s**; `ruff check fs_diloco scripts/miyabi tests` → all checks
  passed. Artifacts: `quality_fix-Q/Q5/artifacts/20260718-1650_full-pytest-final.log`
  and `20260718-1650_ruff-final.log`.
- `bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh scripts/local/*.sh` passed.
  Every PBS script has exactly one literal `#PBS -W group_list=xg24i002`; no
  placeholder remains.
- `git diff --check` and structured JSON parsing passed.
- Q5's three terminal runs independently passed the plan invariant checker at
  final versions 53/53/52.

## Decision

All unconditional E1–E6 and Q1–Q6 completion predicates are proved by current
evidence. Q5's optional scaling branch is correctly not triggered. No required
implementation, experiment, report, or validation gate remains open.
