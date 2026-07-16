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

## Subagent Usage

Use subagents only when the task materially benefits from parallel execution. Avoid delegation for routine sequential work, and do not run more than two subagents concurrently within the same task.
