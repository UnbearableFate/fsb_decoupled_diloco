# Independent Codex review supplement — Plan 02 Phase 2

## Review identity

- Decision: **CHANGES_REQUIRED**
- Reviewer source: Codex
- Actual model: `gpt-5.6-sol`
- Comparison base: `fefef86b68aa346afee93680ad9c494657412074`
- Review target: `7feb09992b7f40b255e0858020a50d811a602b9c`
- Reviewed diff: `fefef86b68aa346afee93680ad9c494657412074..7feb09992b7f40b255e0858020a50d811a602b9c`

This append-only supplement records one additional finding discovered while rechecking the scheduler/admission handoff. It does not replace the primary Codex report for the same frozen target; all three findings in that report remain accepted.

## Finding

### High — a physical learner can win admission before its scheduler identity is durably bound

`admit_registration()` compares physical PBS identities only when the durable launch row already has a non-null `pbs_job_id` (`fs_diloco/storage/fenced_store.py:1084-1095`). Both externally launched bootstrap jobs and outbox-launched scale jobs have an unavoidable interval after `qsub` creates the physical job but before the manifest/outbox transaction records that job ID. During this interval the real process already knows `PBS_JOBID`, but the launch row remains null, so the comparison is skipped and admission succeeds.

For bootstrap, the launcher writes the scheduler manifest only after qsub returns; for scale-out, the outbox records `state=submitting` before qsub and binds the returned ID afterward. A duplicate or unintended physical process that knows the deterministic logical request can therefore consume the one-winner admission before the authorized receipt is durable. The later manifest cannot repair this: `record_external_launch_jobs()` sees the request already admitted, while the intended job is rejected because the logical request has a different admitted instance. Logical at-most-once still holds, but the required logical-to-physical scheduler authorization does not.

Treat a request carrying a non-null PBS job ID as transiently pending while its launch row has no bound PBS identity. Registration ingestion must leave the request publication intact and publish no terminal rejection; after manifest/outbox reconciliation binds the receipt, the same immutable request can be retried and either admitted on exact normalized identity or rejected on mismatch. Preserve the existing direct/local path where both IDs are null. Add a RED test for registration arriving before bootstrap manifest reconciliation, followed by exact-ID success, plus a wrong-ID variant that remains rejected after binding.

## Final decision

**CHANGES_REQUIRED**

This scheduler-authorization race changes a Phase 2 admission safety boundary and therefore requires a negative regression, compute-node verification, and incremental dual review together with the primary report remediation.
