# Q5 failures

## 2026-07-18 — TMN formal workload never entered terminal drain

- Training jobs `2405675/78/81` all reached `stop_after_outer_steps` at v50 before learner input closed. Their remaining proposals were finalized as stop-target leftovers, so no `terminal_predecessor_captured` event or `eval_checkpoints/` manifest could exist.
- Terminal post-checkpoint validation succeeded, but dependent predecessor evaluations `2405677/80/83` correctly failed closed on the missing capture. These three runs are excluded from TMN-01/02 rather than interpreted as a zero effect.
- Root cause is the experiment design: `completion_mode=local_or_global` was overridden, but the v50 global target was left active and won the race. The corrected plan requires an input-exhaustion workload with the global target removed or unreachable and validates the terminal event/capture before submitting predecessor evaluation.

## 2026-07-18 — TMN-01 RED

- Command: `pytest -q tests/test_terminal_predecessor_capture.py`
- Artifact: `artifacts/20260718-1200_tmn01-red.log`
- Result: collection failed because the deliberately specified
  `maybe_capture_terminal_predecessor_for_eval` helper did not yet exist.
- Resolution: implement the default-off, terminal-partial-only evidence capture and
  its non-authoritative path/manifest contract.

## 2026-07-18 — predecessor result reported the terminal version

- The first successful predecessor evaluations selected and checksummed the
  correct `terminal_predecessor_v*` files, but their top-level `global_version`
  fell back to `latest.json` because the generic resolver recognizes only
  `global_v*.safetensors` filenames. Thus a v52 predecessor result incorrectly
  displayed v53 even though `terminal_predecessor_capture.source_global_version`
  and the checkpoint identity were correct.
- Fixed the evaluator to use the capture manifest's source version for this
  role, added a regression test, froze evaluator snapshot
  `quality_q5_evalfix_20260718_1135` at fingerprint `sha256:8474988c...`, and
  reran all terminal/predecessor evaluations. Corrected results now report
  v52→v53, v52→v53, and v51→v52 with one evaluator fingerprint.
