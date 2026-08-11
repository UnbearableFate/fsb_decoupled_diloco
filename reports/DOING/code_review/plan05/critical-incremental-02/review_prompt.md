# plan05 final critical evidence review

Review the listed files as the current implementation of the sole Full Protocol design. Reason independently from the current source. Do not inspect other reviewer output, reconstruct repository history, or propose compatibility with prior protocols.

Concentrate on the independent formal evidence consumer and its production contracts:

1. each logical global version from v0 through v10 has a contiguous predecessor and canonical epoch/owner/publication weight and outer-state identity;
2. a checkpoint object that is still present must be an immutable regular file whose byte size and SHA-256 match durable authority identity;
3. a missing object is accepted only when its version row came from a validated immutable authority archive, the version is absent from the hot table, and no pending or claimed GC candidate remains;
4. a missing hot object or an archived object missing before GC completion fails closed;
5. immutable audit batches/partitions, the hot authority database, GC claim/completion, and the formal oracle form one coherent proof without a false acceptance or a rejection of valid production retention;
6. actor attestations preserve an absent source-lock identity as JSON null instead of coercing it to text, while still binding the exact run, descriptor, source, workload, actor, scheduler allocation, and topology;
7. a learner killed after admission but before its first durable receipt may have no contributor-progress row: only the one terminal hard-crash stream may use that state, it must have no durable receipt/update, it contributes zero verified optimizer steps, and its configured one-cycle gap remains an upper bound rather than invented processed tokens;
8. acknowledged streams, hard crashes with prior progress, healthy runs, and authorized replacement retain their stricter progress/fence/cursor checks;
9. mutation coverage exercises retained-object identity drift, completed-versus-incomplete archive GC, missing progress on non-crashed streams, and updates without progress.

Also check that this evidence remains consistent with the fixed GPT-2/WikiText-2 formal workload and three registered scenarios. The plan04 baseline remains incomparable unless all pre-registered source, config, workload, seed, input, and terminal identities match exactly.

Return findings with severity, exact file/line evidence, consequence, minimal current-design remediation, and missing mutation coverage. If no finding remains, enumerate the invariants and files actually inspected. The final non-empty line must be exactly `Verdict: APPROVE` or `Verdict: CHANGES_REQUIRED`.
