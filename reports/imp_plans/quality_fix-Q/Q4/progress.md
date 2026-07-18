# Q4 implementation progress

## 2026-07-17 — plan audit and protocol direction

- The existing lm-eval harness does not establish that its `wikitext` task uses the configured validation split/tokenization. The corrected PVE-01 deliverable is a dedicated validation loss/ppl evaluator using the run config, tokenizer, EOS/block pipeline, causal shift, and predicted-token weighting.
- Automatic evaluation will be a separate one-node `afterok` job submitted with the training job, not an eval stage that leaves eight nodes idle. Success requires non-empty blocks/tokens, finite loss/ppl, checkpoint checksum/source identity, a structured result, and atomic summary attachment.
- Available first-wave checkpoints were confirmed for prediction F/H, rebase-preserve, and replace v48/v49/v50 roots; no evaluation has yet been claimed.

## 2026-07-18 — PVE-01 evaluator core

- Added a dedicated validation evaluator with the repository's exact configured validation split and text→tokens+EOS→non-overlap block pipeline. It computes causal shift explicitly and reduces summed cross-entropy by the number of predicted tokens before `ppl=exp(loss)`.
- Added fail-closed checks for zero blocks/tokens, non-finite loss/ppl, checkpoint/latest mismatch, source identity mismatch, and conflicting summary attachments. A deliberate `--allow-non-latest` mode exists only for Q5 evidence checkpoints; legacy runs require an explicit missing-identity waiver.
- Results include checkpoint SHA-256/size, dataset fingerprint/version/cache files, tokenizer/EOS/block/batch/dtype/device protocol, protocol hash, and separate training/evaluator source identities. `metrics/validation_eval.json` and the compact attachment to `control/summary.json` are atomic and idempotent for the same checkpoint.
- Focused command: `pytest -q tests/test_validation_eval.py tests/test_data_shuffle.py`. Result: 10 passed in 3.96 s on Miyabi job `2405414.opbs`, node `mg0006`.
- Artifact: `artifacts/20260718-0850_pve01-focused-green.log`.
- Remaining: PBS evaluator/afterok submitter, real checkpoint full-split validation, and ≥4 legacy checkpoint observations.

## 2026-07-18 — PVE-05 separate eval job and dependency chain

- Added `run_1node_validation_eval.pbs`: it requires a completed training summary, captures evaluator source identity, runs the dedicated evaluator on one node, and independently asserts success, positive blocks/tokens, finite metrics, and checkpoint SHA before exiting zero.
- Added `submit_train_with_validation.sh`: it freezes run ID/root for the train job and submits validation with `depend=afterok:<train_job_id>`. A failed training job therefore cannot create a validation success attachment. Seed/scan/ingestion experiment overrides propagate only to training; evaluation reads the resolved run config.
- Static gate: `bash -n scripts/miyabi/*.pbs scripts/miyabi/*.sh` passed; all 14 PBS scripts use literal `group_list=xg24i002` and no placeholder.
- Focused command: `pytest -q tests/test_validation_eval.py tests/test_validation_submitter.py tests/test_data_shuffle.py`. Result: 11 passed in 1.36 s on job `2405414.opbs`, node `mg0006`.
- Artifact: `artifacts/20260718-0914_pve01-05-focused-green.log`.
- Remaining PVE-05 runtime evidence: a real one-node full-split eval and a dependent-job execution; unit evidence covers dependency construction and atomic attachment failure semantics.

## 2026-07-18 — PVE-01/02/03 full-split legacy evaluation

- Added a narrowly scoped historical resolved-config loader: normal training config loading remains strict, while evaluation snapshots migrate/remove only keys enumerated in `REMOVED_CONFIG_KEYS`. Regression: 8 evaluator tests passed before rerunning H.
- All four requested terminal checkpoints completed the same full protocol (`protocol_sha256=sha256:691045...`, 243 blocks, 248,589 predicted tokens, CUDA BF16, batch 4):
  - prediction F v50: loss 3.09426795, ppl 22.0711;
  - prediction H v50: loss 3.06641002, ppl 21.4647;
  - rebase-preserve v49: loss 3.07335062, ppl 21.6142;
  - replace BF16/staleness-2 v48: loss 3.09122897, ppl 22.0041.
- Each result records its distinct checkpoint SHA-256 and was atomically attached to that run's summary. These are legacy observations: training source identity is unavailable and run versions/semantics differ, so they are not a causal strategy comparison.
- PVE-03 divergence is direct: local last-10 ranks rebase-preserve first, then H, F, replace; validation ranks H first, then rebase-preserve, replace, F. In particular, rebase-preserve's local loss is 0.06898 below H while validation loss is 0.00694 above H. Local training loss cannot substitute for validation.
- Artifacts: `artifacts/20260718-0938_pve01-h-full-validation-pass.log`, `20260718-0945_pve02-f-full-validation-pass.log`, `20260718-1010_pve02-rebase-preserve-validation-pass.log`, `20260718-1010_pve02-replace-v48-validation-pass.log`, and `20260718-1020_pve02-03-legacy-comparison-pass.json`.
- PVE-01/02/03 are complete. PVE-04 still requires the B2+Q3 same-fingerprint, single-variable, three-seed strategy matrix; no default prediction claim is made from legacy observations.

## 2026-07-18 — formal evaluator hardening gate

- The train+validation submitter now forwards every frozen quality-matrix override to training while evaluation continues to read the resolved snapshot.
- Added `--terminal-predecessor`: Q5 evaluation selects the highest captured source version, verifies its manifest checksum/path, implies non-latest mode, and writes a versioned result without replacing the terminal checkpoint attachment.
- Final compute-node gate after Q2/Q5 integration: 284 passed in 14.56 s and ruff clean (`artifacts/20260718-1350_full-pytest-final.log`, `artifacts/20260718-1350_ruff-final.log`). The formal same-fingerprint matrix is ready for train→afterok validation submission.

## 2026-07-18 — PVE-04/05 formal submission

- Froze snapshot `/work/xg24i002/x10041/fs_diloco_experiment_snapshots/quality_20260718_1400` with stable source-only fingerprint `sha256:122f6982264d63cd06f39e06a35dcde81cde789d0713bf84a2ed2189d553b173` after the final test, lint, PBS syntax, literal-group, and bytecode-exclusion gates.
- Submitted 24 nine-node train jobs across 8 conditions × seeds 1337/2027/4049; every train has a one-node `afterok` full validation job. After excluding the source-hygiene race `2405633/34`, the valid strategy subset is baseline rebase `2405771,2405635,2405637`, prediction `2405639/41/43`, and replace `2405645/47/49`; each has a dependent validation.
- The same submission also covers FP32/BF16 publish, λ/fresh-only, and terminal evidence without changing source fingerprint. PVE-05 is no longer merely a constructor test: its runtime evidence will be the dependent jobs when they reach terminal state.

## 2026-07-18 — PVE-04/05 formal strategy decision

- The valid rebase, prediction, and replace matrix completed for seeds
  1337/2027/4049 with one source fingerprint and protocol. Mean validation loss
  was 3.056161 (rebase), 3.053650 (prediction), and 3.051954 (replace).
  Prediction-versus-rebase paired mean was -0.002511; replace-versus-rebase was
  -0.004207. Every individual delta remained inside ε=0.01, and prediction was
  0.001696 worse than replace on the three-seed mean.
- Prediction is quality-compatible at this horizon but has not demonstrated an
  advantage over replace, so replace remains the default and prediction remains
  opt-in. This conclusion does not reuse the confounded legacy rankings.
- All nine `afterok` evaluation jobs ran on one node and produced 243 nonempty
  blocks, 248,589 predicted tokens, finite loss/ppl, matching checkpoint SHA,
  source identity, and atomic summary attachment. This closes PVE-05 runtime
  evidence as well as PVE-04.
- Evidence: `artifacts/20260718-1610_strategy-formal-matrix.csv` and each run's
  `metrics/validation_eval.json`. PVE-01 through PVE-05 are complete.
