# plan05 preformal current-state review

Review every file in `.review-scope/paths.txt` as the current source for the sole Full Protocol stream-pool membership design. Do not reconstruct history or review a diff.

The implementation must have exactly one strict `MembershipScope` and `ContributorFence`, one merged authority schema, and one admission path. Every learner must use either an explicit bootstrap slot or an authority-backed launch request; replacement must advance the exact stream incarnation and stale ownership must have no later durable effect. `scaling.enabled` changes only capacity automation.

Trace these boundaries end to end:

1. strict config, descriptor, source identity, run initialization, protocol versions and old-shape rejection;
2. admission request/response/current pointer, instance/placement/stream transactions, fence validation, receipt/proposal ingestion, merge selection, token fate, terminal drain and recovery;
3. scheduler uncertainty, capacity observation, launch authorization, qsub evidence, replacement admission, cursor continuity, stale effect rejection and cleanup ownership;
4. co-allocated functional Checker and independent experiment04 formal supervisor, including source/workload/topology binding, fault timing, archive-aware reads, token ledger, hard-crash accounting, create-only artifacts and failure behavior;
5. the 8-stream GPT-2/WikiText-2 200×10 configs, PBS scripts, existing test owners, summary/comparison tool, generated API reference, README and website consistency;
6. repository-wide dead/legacy surface and any compatibility alias, fallback, nullable old field, duplicate config/API/schema or self-proofing oracle.

The formal matrix is no-failure, learner failure without replacement, and learner failure with scheduler-authorized replacement. All three must be runnable on one clean target. A valid formal PASS must prove ten exact four-contributor merges of 200 inner steps, terminal authority, token ledger balance, independent scheduler topology and scenario-specific durable facts. Comparison with a historical baseline is allowed only when source/config/workload/seed/input identities strictly match; otherwise the result must be explicitly incomparable. Fault-scenario wall time is report-only.

Pay special attention to interactions among `do_experiments/experiment04/scenario_supervisor.py`, `tools/summarize_runs.py`, authority tables and the corresponding test fixtures. Identify paths that would let malformed or incomplete state pass, as well as valid registered state that the consumer would incorrectly reject.

Return findings with severity, exact file/line evidence, consequence, minimal current-design remediation and missing mutation coverage. The final non-empty line must be exactly `Verdict: APPROVE` or `Verdict: CHANGES_REQUIRED`.
