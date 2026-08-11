# plan05 critical incremental current-state review

Review the listed files as the current implementation of the sole Full Protocol stream-pool design. This review follows a broader old-target review, but you must reason independently from current source and must not inspect any other reviewer output or reconstruct history.

Concentrate on whether the formal three-scenario evidence consumer can accept malformed state or reject valid registered state after these critical invariants were tightened:

1. every learner admission has exactly one explicit bootstrap slot or authority launch request; replacement binds one lost instance, one stream, one capacity observation, one qsub receipt, one admitted incarnation and a continuous receipt/data cursor;
2. fixed-capacity learner loss closes exactly one frozen stream as a bounded hard crash without inventing processed tokens; healthy and replacement scenarios close all streams with acknowledgements;
3. receipt chains, updates, direct token fates, rollups, per-version four-way/200-step merges and terminal totals form one archive-aware zero-balance ledger;
4. actor attestations bind exact scheduler jobs, run/source/config/model/data identity, authority actor identity and independent initial topology, with duplicate scheduler identities rejected;
5. v0 through v10 each bind canonical epoch/owner/publication weight and outer-state paths to immutable regular files with exact byte size and SHA-256;
6. the formal configs freeze the same GPT-2/WikiText-2 revisions, seed, optimizer, dtype, eight streams, quorum four, 200 local steps and ten global versions, while `scaling.enabled` is the sole scenario capacity difference;
7. validation and formal launchers preserve exact Python/source identity and fail closed; cleanup preserves authority, audit and checkpoint evidence.

Check the coupled production writers/readers, DDL, launchers, Checkers, summary logic and mutation tests listed in scope. The historical plan04 baseline is not comparable unless its source/config/workload/seed/input/terminal identities match exactly; do not propose DDP, periodic-average or diagnostic data as a substitute.

Return findings with severity, exact file/line evidence, consequence, minimal current-design remediation and missing mutation coverage. If no finding remains, enumerate the invariants and files actually inspected. The final non-empty line must be exactly `Verdict: APPROVE` or `Verdict: CHANGES_REQUIRED`.
