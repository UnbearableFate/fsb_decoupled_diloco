# Codex incremental review — P5-delete-classic-refactor

- Base commit: `a540febd489abfac245790967a0b2a5667f90345`
- Target commit: `57fd2bef341df75c373f433ba3a38252240c6e26`
- Scope: complete `git diff a540febd489abfac245790967a0b2a5667f90345 57fd2bef341df75c373f433ba3a38252240c6e26`, including the operator-file implementation, regressions and retained evidence/report changes.
- Review order: this report was completed and saved before reading or invoking any Claude report for this target.
- Ancestry: `git merge-base --is-ancestor` passed; the range is the next continuous increment after the preceding P5 review target.
- Verdict: **APPROVE**

## Findings

No Critical, High, Medium or Low finding was identified in this increment.

## Checked remediation and invariants

- The previous archive path is removed completely: the leader no longer creates or writes `scheduler_operator_requests/processed`, so an existing child symlink or a caller-derived long archive basename cannot redirect/crash publication (`fs_diloco/runtime/services/dynamic_capacity.py:416-470`).
- Input is opened with `O_NOFOLLOW` and `O_NONBLOCK`; symlinks and non-regular entries receive bounded synthetic rejection observations. Regular input is SHA-256 streamed in 64 KiB chunks while retaining at most 1 MiB+1 for parsing, with opened/named identity checked after the read (`dynamic_capacity.py:473-542`). This resolves the prior unbounded-memory finding without weakening the durable content disposition.
- Hot-entry deletion occurs only after the durable applied/rejected disposition. A successor first looks up that disposition, then reopens no-follow, checks full digest and file identity twice, unlinks only the still-matching name, and fsyncs its parent (`dynamic_capacity.py:430-470,544-566`). Missing files are an idempotent success; changed or transient entries are retained for a later scan.
- Regressions cover no write through a pre-existing `processed` symlink, bounded retained bytes for a 2 MiB request, one-time durable rejection, disposition-before-unlink successor cleanup and preservation of a replacement observed under the same name (`tests/runtime/test_dynamic_capacity_service.py:280-389`). Existing valid operator-resolution replay coverage now also asserts that no secondary archive is created (`test_dynamic_capacity_service.py:245-275`).
- Clean target job `2512047.opbs` used commit `57fd2bef341df75c373f433ba3a38252240c6e26` with `git_dirty=false`; it passed Ruff/format/Checker, all 454 focused tests and all 625 repository tests. The tracked evidence records the clean fingerprint, exact command, history state, exit status and normalized/raw log digests.

The plan explicitly excludes guarantees against an actively malicious symlink race, so the unavoidable final name-check-to-unlink race is outside scope; the implementation neither follows the entry nor writes through it, and ordinary replacement/recovery behavior is covered.
