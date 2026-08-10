# plan03-1 gate artifact schema

Every gate producer atomically publishes a JSON object with these required
fields. A producer writes to a same-directory temporary file, fsyncs it, and
renames it only after validation.

```text
artifact_version: 1
status: PASS | FAIL | BLOCKED | REVIEW
gate: stable gate ID
experiment_id: stable experiment ID
requirements_covered: non-empty exact requirement ID list
source_identity:
  commit: full Git commit
  dirty: false for formal evidence
  scopes: ordered explicit scope list
  fingerprint: sha256
config_schema_identity:
  version: integer
  sha256: resolved config sha256
protocol_schema_identity:
  version: integer
  ddl_sha256: sha256
  mode: static | dynamic
environment:
  interpreter: executable and version
  packages: required package provenance
  pbs_job_id: normalized full ID or null for static gates
  nodes: exact hostname list
  topology: actor-to-node map
workload_identity:
  configured_local_steps: integer
  committed_global_steps: integer
  processed_tokens: integer
  direct_weight_tokens_applied: integer
  cursor_terminal: per-contributor terminal cursor
metrics: object
errors: list
evidence_paths: non-empty list of pre-existing raw evidence paths
cleanup:
  owner: named producer
  eligible: boolean
  targets: exact run-owned paths
```

Rules:

- Static/review gates may set non-applicable config/protocol/workload fields to
  `null`, but may not omit the top-level identity.
- PASS requires every named evidence path to exist before publication. The
  artifact cannot list its own output or a checker derived only from itself.
- The final aggregate checks exact requirement ownership, identical formal
  source identity, expected config/workload identities, artifact hashes, and
  absence of extra or missing inputs.
- PBS exit status and actor logs are diagnostic fields. Durable SQLite rows,
  immutable object hashes, contributor fences, terminal acknowledgements, and
  scheduler history are the success oracles.
- Failure artifacts preserve actual identity, partial metrics, error class, raw
  paths, and cleanup eligibility; they are never overwritten by a retry.
