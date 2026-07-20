# Q5 implementation progress

## 2026-07-17 — L0 availability audit

- Current-only GC leaves no predecessor for the historical partial-terminal checkpoints, so TMN-01 cannot be reconstructed from old runs.
- Corrected the plan with a default-off research capture: immediately before a terminal merge below quorum_min, hardlink/copy the authoritative current weight into `eval_checkpoints/` and record checksum/version/selected/quorum. It is evidence only and never participates in DB/latest/resume.
- The pre/post decision threshold is frozen before data: degradation greater than Q6 baseline ε in the paired three-seed mean or any seed triggers the optional scaling study; otherwise the plan closes with a negative result.

## 2026-07-18 — TMN-01 capture implementation

- Added the default-off `sync.capture_terminal_predecessor_for_eval` flag and a full-mode helper that runs only for input-closed terminal merges below `quorum_min`.
- Captures use a same-filesystem hardlink when possible and an atomic copy fallback. Each unique source version receives a manifest with checkpoint/source paths, SHA-256, selected update/learner IDs, and quorum values. Capture happens before DB selection and never mutates SQLite/latest; `eval_checkpoints/` is deliberately absent from runtime authority and maintenance references.
- Unit coverage proves default-off zero files, nonterminal no-op, hardlink, copy fallback, multiple partial versions, checksum, and unchanged DB/latest. Focused result: 81 passed (`artifacts/20260718-1215_tmn01-green.log`). Full suite: 282 passed; ruff clean (`artifacts/20260718-1250_full-pytest.log`, `artifacts/20260718-1250_ruff.log`).
- Remaining TMN-01/02: at least three captured pre/post pairs evaluated by the frozen Q4 protocol and compared with the Q6 FP32 ε. Outer-LR scaling remains intentionally unimplemented until that evidence crosses the frozen threshold.

## 2026-07-18 — TMN-01/02 formal submission

- Submitted local-horizon/capture jobs `2405675`, `2405678`, and `2405681` for the three frozen seeds from the same quality fingerprint. Their terminal validation jobs are `2405676/79/82`.
- Each post evaluation is followed by a second dependent predecessor evaluation (`2405677/80/83`). The evaluator resolves the highest capture version, verifies its manifest checksum, and writes a separate result; a missing partial-terminal capture fails closed.
- No outer-LR scaling code or job was submitted. That conditional stage remains gated on the completed paired loss deltas versus Q6 ε.

## 2026-07-18 — TMN-01/02 trigger correction

- The first three training runs were invalid for TMN-01 because the still-active v50 global target stopped them before input closure; the predecessor eval jobs failed closed as designed. Details are recorded in `failures.md`.
- Added a dedicated formal profile that keeps the same 5000-local-step workload and quality baseline settings but uses `completion_mode=local_or_global`, `stop_after_outer_steps=null`, and enables predecessor capture. This forces the syncer to drain only after all learners close input instead of accepting a global-target stop as evidence.
- The corrected three-seed run set must expose `terminal_input_closed`, a below-minimum terminal selection, and a checksum-valid capture per seed before pre/post evaluation. Outer-LR scaling remains unimplemented pending that result.

## 2026-07-18 — TMN-01/02 corrected formal decision

- Corrected train jobs `2406953/54/55` all exited zero with no abnormal nodes,
  eight learners at local step 5000, `terminal_input_closed`, and
  `input_exhausted`. Each selected exactly 3 updates below `quorum_min=4`,
  captured a checksum-valid hardlink predecessor, and advanced one terminal
  version: 52→53, 52→53, and 51→52.
- After fixing the predecessor result-version metadata issue recorded in
  `failures.md`, terminal eval jobs `2407037/38/39` and predecessor jobs
  `2407049/50/51` all exited zero. Every result used evaluator fingerprint
  `sha256:8474988c...`, protocol `sha256:691045...`, 243 blocks, and 248,589
  predicted tokens.
- Terminal-minus-predecessor validation-loss deltas were -0.0001643,
  -0.0001249, and -0.0007005 nats; paired mean was -0.0003299 and the worst
  seed was -0.0001249. All are improvements and far below frozen ε=.01.
- Decision: the terminal partial merge has no visible degradation at this
  horizon. Per the predeclared conditional plan, TMN-03/04 outer-LR scaling is
  not implemented and no scaling experiment is launched. TMN-01/02 are
  complete and the plan closes with a negative result.
- Evidence: `artifacts/20260718-1640_terminal-paired-quality.json` plus each
  run's capture manifest and terminal/predecessor validation result.
