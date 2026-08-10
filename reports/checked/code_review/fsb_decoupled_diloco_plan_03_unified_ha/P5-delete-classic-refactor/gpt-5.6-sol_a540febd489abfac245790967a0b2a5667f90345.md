# Codex incremental review — P5-delete-classic-refactor

- Base commit: `eb56219e13817b1f659921ea093c2dfdfa473abd`
- Target commit: `a540febd489abfac245790967a0b2a5667f90345`
- Scope: complete `git diff eb56219e13817b1f659921ea093c2dfdfa473abd a540febd489abfac245790967a0b2a5667f90345`, including source, tests, schema, Checker, documentation, reports and deletion of the obsolete P4 replacement PBS.
- Review order: this report was completed and saved before reading or invoking the Claude report for this target.
- Verdict: **CHANGES_REQUIRED**

## High

### H-01 — Disposed operator-file archival can follow a `processed` symlink and publish outside the run root

`DynamicCapacityService._archive_operator_file()` constructs `path.parent / "processed"`, calls `mkdir(..., exist_ok=True)`, and then sends a digest-prefixed target to `publish_immutable_bytes()` (`fs_diloco/runtime/services/dynamic_capacity.py:480-494`). Neither `Path.mkdir(exist_ok=True)` nor `atomic_io.ensure_dir()` rejects an existing directory symlink (`fs_diloco/storage/atomic_io.py:41-44,115-118`). A shared-root writer can therefore pre-create `control/scheduler_operator_requests/processed` as a symlink and cause the leader to create immutable files outside the run root. Long invalid source names can also make the digest-prefixed archive basename exceed `NAME_MAX` after the disposition has committed.

Impact: the new replay-bounding path violates the repository's no-symlink/path-containment boundary and can crash the leader or write to an operator-selected location. The source request is already represented by a fenced durable disposition (and valid requests by the full scheduler audit row), so a second filesystem archive is not needed for hot-scan correctness.

Fix: after the durable disposition commits, remove only the exact still-observed hot entry with no-follow identity/content revalidation and parent-directory fsync; do not create a caller-name-derived `processed` path. If invalid raw bytes must be retained, use a separately initialized, symlink-checked audit boundary with digest-only names. Add regressions for a `processed` symlink, source replacement between read and disposal, and successor cleanup after disposition-before-unlink crash.

## Medium

### M-01 — The 1 MiB operator-file limit is checked only after unbounded `read_bytes()`

The scan reads the complete regular file at `dynamic_capacity.py:413-420` and only then rejects `len(raw) > 1_048_576` at lines 435-440. A malformed or accidental very large file can allocate arbitrary memory in the leader before the advertised bound applies. The subsequent archive path duplicates the same bytes and may allocate/write them again.

Fix: open regular files with `O_NOFOLLOW`, stream a SHA-256 digest, and retain at most the configured parsing limit plus one byte. Persist an oversize rejection without materializing the whole object; revalidate the opened/named identity before unlink. Add a sparse/large-file regression proving bounded retained bytes and one durable rejection.

## Reviewed without additional findings

- Schema 8 correctly persists the preclose cutoff, registration/ack/proposal deadlines and terminal merge count; terminal commits validate generation and increment the budget in the same transaction, while normal commits are closed after terminal intent.
- Static and dynamic admissions now carry the request timestamp into the fenced command; preclose input remains open only until the durable snapshot, and successor tests cover cutoff/deadline recovery.
- `MergeAttemptStatus` distinguishes no-batch from fence conflict; selection command IDs are deterministic within leader epoch and terminal merge conflict does not consume the persisted commit budget.
- PBS recovery now uses Miyabi-supported summary-list to per-job detail queries with a bounded newest-first scan; launch transition IDs include the persisted row version and scheduler evidence; active reservations exclude streams from subsequent scale-out.
- Legacy config projection remains strict-entry-only and strips the complete historical v4 envelope after eligibility; CSV export independently classifies the named authority and rejects forged protocol labels.
- The baseline protocol migration exemption is now byte-bound to an explicit SHA-256, and the obsolete P4 script that forged a launch request is deleted with historical evidence retained.
- The clean target evidence (`2511948.opbs`) reports Ruff/format/Checker PASS, focused `451 passed`, full `622 passed`, and `git_dirty=false`; these tests do not exercise the symlink/large-file counterexamples above.
