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
fault_scenario: none | learner_replacement | syncer_takeover for runtime gates
metrics: object
errors: list
evidence_paths: non-empty list of pre-existing raw evidence paths
cleanup:
  owner: named producer
  eligible: boolean
  targets: exact run-owned paths
```

Full Protocol runtime gate metrics additionally project `receipt_count`,
`proposal_count`, `applied_proposal_count`, `dropped_proposal_count` and
`direct_dropped_tokens`. Applied work is the registered workload; processed
work may also contain fully adjudicated supersession or terminal drops.

The one-node validation gate records the exact ordered `ruff-format`,
`ruff-lint`, `focused-pytest`, and `full-pytest` steps. Each pytest step has a
create-only JUnit XML path and parsed `tests`, `failures`, `errors`, and
`skipped` counts. A pytest step passes only when it executed at least one test
and every failure, error, and skipped count is zero; both the raw command log
and JUnit XML files are independent evidence.

Rules:

- Static/review gates may set non-applicable config/protocol/workload fields to
  `null`, but may not omit the top-level identity.
- PASS requires every named evidence path to exist before publication. The
  artifact cannot list its own output or a checker derived only from itself.
- Every formal gate hash-registers at least one tracked supporting file that is
  also named by the gate artifact. The one-node validation gate hash-registers
  its complete evidence set: the raw command log and both JUnit XML files.
- The final aggregate checks exact requirement ownership, identical formal
  source identity, expected config/workload identities, artifact hashes, and
  absence of extra or missing inputs.
- PBS exit status and actor logs are diagnostic fields. Durable SQLite rows,
  immutable object hashes, contributor fences, terminal acknowledgements, and
  scheduler history are the success oracles.
- Runtime PASS requires the exact registered directly applied token total to
  agree across publication history, the durable token rollup, terminal
  authority, fixed terminal controls, and the immutable terminal publication.
- Runtime behavior is selected only by the registered `fault_scenario`.
  Scenario-derived binding generations, learner attempt attestations, syncer
  epochs and durable fault evidence must be exact; normal runs reject any
  unregistered replacement or takeover history.
- Failure artifacts preserve actual identity, partial metrics, error class, raw
  paths, and cleanup eligibility; they are never overwritten by a retry.
