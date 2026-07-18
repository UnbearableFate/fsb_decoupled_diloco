# E3 implementation progress

## 2026-07-17 — plan audit

- Confirmed fragment resume uses authoritative per-fragment checkpoints; periodic materialized full weights serve learner startup/latest, export, analysis, and evaluation.
- Added a missing terminal requirement to the plan: `input_exhausted` and every other normal stop must force a final materialization, even when the event is not divisible by the configured interval. Telemetry uses the existing `materialize_full_seconds` name plus bytes/boolean.

## 2026-07-18 — MAT focused implementation verified

- Fragment mode now rejects missing, zero, and negative
  `materialize_full_every_events` values instead of silently materializing on
  every event.
- Fragment latest publication returns structured timing/bytes/event telemetry,
  and every non-error terminal path explicitly forces a final full checkpoint.
  Periodic events expose the same telemetry in CSV and W&B metrics.
- Compute-node verification: `pytest -q tests/test_fragment_materialization.py`
  — 5 passed.
- Evidence:
  `artifacts/20260718-0016_fragment-materialization-focused_pass.log`.

## 2026-07-18 — MAT-04 formal experiment control

- Added an explicit positive `materialize_full_every_events` runtime override and propagated it with seed through the 9-node fragment 50×10 launcher, allowing `1` versus `10` without generated-config drift.
- The shared final test/lint gate after adding the E1/E3 controls is 285 passed and ruff clean. MAT-04 paired 9-node submission remains pending the dedicated immutable snapshot.

## 2026-07-18 — MAT-04 paired submission

- From the same `sha256:dd993230...` snapshot, submitted 9-node fragment 50×10 jobs `2405766` (`materialize=1`) and `2405767` (`materialize=10`) at seed 1337.
- The launcher passes the value to every role and the resolved-config diff is designed to contain only the materialization interval/run identity. Final forced materialization remains active in both arms. Quantitative I/O/coordination separation awaits both jobs.

## 2026-07-18 — MAT-04 three-seed formal decision

- The initial pair covered only seed 1337, which was insufficient for the
  repository's unified multi-seed experiment discipline. Added paired jobs
  `2406925/26` (seed 2027) and `2406927/28` (seed 4049) from the same immutable
  fingerprint after the mandatory PBS syntax/group gate; all four exited zero.
- Across three seeds, interval 10 reduced materializations 10→1 per run,
  materialized bytes by 90%, and mean materialization walltime 1.4713→0.2730 s
  (-81.45%). Mean complete-training time changed 214.42→209.68 s (-2.21%);
  per-seed walltime remained noisy, so the causal conclusion is restricted to
  materialization I/O rather than a guaranteed end-to-end speedup.
- Evidence: `artifacts/20260718-1620_mat-formal-matrix.csv`. Production
  fragment profiles now explicitly use interval 10; debug/tiny profiles retain
  interval 1 for observability, and every normal terminal path still forces a
  final full materialization. MAT-01 through MAT-04 are complete.
