# B7 implementation progress

## 2026-07-17 — L0 specification audit and RED

- Baseline commit: `c359b8322c33e0101328c2fc8522271691f1e52c`; current worktree includes completed B1–B5 changes plus preserved pre-existing user edits.
- The plan was corrected to make the independent nullable timeout field mandatory, replace the nonexistent “one poll period” bound with the real step/wait-point bound, and require one fixed-pointer confirmation read only when the deadline is reached. This avoids false positives when latest advanced before a strategy's next normal poll.
- Frozen behavior: start after initial latest load; refresh only on strictly newer full version/global merge event; stop.json always suppresses watchdog; trigger produces event + stopped heartbeat reason + exit code 0. Full and fragment use the same pure watchdog state.
- SWD-01/05 RED tests cover deadline boundaries, same/new version refresh, full/fragment latest confirmation, stop suppression, and config validation. Runtime kill tests remain for SWD-02/03/06.

## 2026-07-17 — L1 watchdog unit GREEN

- Implemented the shared monotonic `SyncerProgressWatchdog`, nullable timeout resolution/validation, and deadline confirmation read for both full and fragment pointer schemas.
- Compute-node command: `pytest -q tests/test_learner_completion.py tests/test_config.py` — **76 passed**. Evidence: `artifacts/20260717-swd-unit-green.log`.
- Covered SWD-01 and SWD-05. The runtime loops now start the watchdog after their initial latest load, refresh it on adopted progress, check after every optimizer step, and preserve a distinct terminal heartbeat reason.

## 2026-07-17 — L2 full/fragment kill tests GREEN

- SWD-02/03 full evidence: `artifacts/swd-full-Uol18h/`. The syncer was SIGKILLed at `1784289353.095599619`; learner exited at `1784289353.608016827` (0.512 s later), exit code 0. Its event reports timeout 1.0 s, last observed version 0, local step 1, and 1.368 s since the last signal. Final heartbeat is `stopped` with `status_reason=syncer_unresponsive`; no stop.json or temporary partial files exist.
- SWD-06 fragment evidence: `artifacts/swd-fragment-ikOQWS/`. The same SIGKILL test exited in 0.527 s with code 0; event reports global merge event 0/local step 1/1.181 s since signal, and the final heartbeat carries the required reason and fragment state. No stop.json or temporary partial files exist.
- The fragment watchdog path skips the normal final-target wait/adoption so an unavailable syncer cannot turn a controlled watchdog exit into another `no_progress_timeout_seconds` delay.

## 2026-07-17 — L3 no-regression and completion

- SWD-04 normal full and fragment pipelines completed with `stop_after_outer_steps`, exactly one `syncer_watchdog_started` event per learner, no `syncer_unresponsive` event/reason, and final adopted versions/events of 4. Evidence: `artifacts/normal-full/`, `artifacts/normal-fragment/`, and their `20260717-normal-*.log` summaries.
- Compute-node full suite: `pytest -q` — **215 passed** in 11.56 s (`artifacts/20260717-full-pytest.log`). Login-node `ruff check` and `git diff --check` both passed.
- Documentation now records the timeout configuration, symmetric liveness contract, confirmation read, and controlled terminal heartbeat behavior. SWD-01 through SWD-06 are complete.
- Launcher/PBS orchestration that reacts to a learner's `syncer_unresponsive` exit remains an explicit out-of-scope follow-up; the learner exit code is intentionally 0 and the durable event/heartbeat identify the failure mode.
