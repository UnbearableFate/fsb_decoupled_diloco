# Repository Agent Instructions

## Skill Usage

Follow the standard skill-loading rules. Invoke the `miyabi-development` skill only when the user request or an applicable scoped instruction explicitly requires testing or experimentation. Do not invoke it for work limited to static source inspection, documentation analysis, or file-only editing.

## Project Context

This repository implements a filesystem-based Decoupled DiLoCo prototype.

## PBS Script Validation

Before submitting any PBS script:

1. Run `bash -n scripts/miyabi/*.pbs` in a safe static-validation environment.
2. Replace every `#PBS -W group_list=<group_id>` placeholder with a valid, literal group ID.
3. Do not submit the script until both checks are complete.

## Documentation Synchronization

When a code change has been verified by a 9-node experiment whose workload exceeds the 50-local-step × 10-global-step baseline, update the relevant documentation to reflect the verified behavior and experimental result.

## Test Artifact Retention and Cleanup

After each test or experiment reaches a terminal state, reduce the run output before starting the next work unit:

1. Persist the core evidence first in the applicable `reports/DOING/<plan-id>/` record. Keep the exact command and resolved configuration, source identity, run ID, PBS job ID, structured Checker result, final or summary metrics, and paths needed to audit the result.
2. For a successful test, retain only the smallest representative logs and artifacts needed to prove the tested invariant. For a failed test, retain the complete error log, the minimal reproduction evidence, and any artifact still needed for root-cause analysis.
3. Delete redundant files produced solely by that completed test, including duplicate or intermediate checkpoints, temporary/staging files, caches, superseded raw telemetry, repeated successful per-rank logs, orphan payloads, and other run-generated files whose information is already captured by the retained summary or manifest.
4. Resolve and inventory the exact cleanup targets before deletion. Limit cleanup to the completed test's known run directory; never delete files from a live, queued, or resumable run, the current database/checkpoint needed for recovery, source/configuration files, reports, unresolved failure evidence, pre-existing user data, or any path whose ownership is uncertain.
5. Write, use, and extend fs_diloco/tools/clean_run.py to better clean up generated runs.

## Subagent Usage

Use subagents only when the task materially benefits from parallel execution. Avoid delegation for routine sequential work, and do not run more than two subagents concurrently within the same task.
