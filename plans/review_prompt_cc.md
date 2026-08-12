Review all code recursively under <TARGET-DIR> as a complete system/subsystem, not just recent changes.

Focus especially on correctness, architectural issues, broken invariants, concurrency/distributed-system behavior, unnecessary complexity, obsolete code, duplicated sources of truth, and meaningful test gaps.

Do not limit the analysis to files under the target path; inspect callers, callees, tests, configuration, and related code elsewhere in the repository when necessary.

Use CodeGraph, Ruff and any other available tools as needed to investigate and validate findings.

Prefer a small number of well-supported findings over speculative observations. Actively verify each finding before reporting it.

Report the results to `reports/reports/DOING/code_review/{current_model_name}_{HEAD commit-id}_{YYMMDD-HHMM}.md`