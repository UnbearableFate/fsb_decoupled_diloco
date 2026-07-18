# R0 failures

## 2026-07-18 — SRC RED: formal-run source identity absent

- Command: `pytest -q tests/test_source_identity.py`
- Result: expected RED, 2 failed.
- Failure signatures: `RunSection` has no `git_commit` field, and
  `FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1` does not fail closed when identity is
  unavailable.
- Evidence: `artifacts/20260718-0000_source-identity-red_fail.log`.

## 2026-07-18 — SRC-CAP RED: immutable source manifest absent

- Command: `pytest -q tests/test_capture_source_identity.py`
- Result: expected RED, 1 failed.
- Failure signature: the formal-launch source capture program does not exist, so
  tracked and untracked runtime sources cannot yet be fingerprinted or archived.
- Evidence: `artifacts/20260718-0023_capture-source-red_fail.log`.

## 2026-07-18 — SRC-CAP implementation syntax error

- Command: `python -m py_compile scripts/miyabi/capture_source_identity.py`
- Result: failed before runtime verification.
- Failure signature: an escaped patch newline was emitted literally in the env
  export tuple, producing `SyntaxError: unexpected character after line
  continuation character` at line 155.
- Resolution: remove the stray literal and re-run static plus focused tests.
