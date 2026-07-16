# AGENTS.md

Using the usual skill loading rules, the miyabi-development skill should only be used when the user or the dependency file explicitly indicates that testing or experimentation is required.

This repository implements a filesystem-based Decoupled DiLoCo prototype:

Before submitting PBS scripts, run `bash -n scripts/miyabi/*.pbs` on a safe node. Fill real `#PBS -W group_list=<group_id>` values before submission.

Code changes that have been verified through testing need to be synchronized to the documentation.

Parallel operations using subagents are permitted, but please use them with caution, only when absolutely necessary, and avoid having more than 2 subagents running simultaneously.