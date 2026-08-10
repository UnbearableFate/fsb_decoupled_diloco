# plan03-1 identity, authority, and fault oracles

## Identity and authority table

| Object | Canonical identity | Authority owner | Durable location / proof |
|---|---|---|---|
| Source target | full Git commit plus ordered formal scopes and SHA-256 fingerprint; dirty must be false | Git tracked tree frozen by the coordinator | run source manifest and formal-ladder manifest |
| Config | canonical resolved YAML bytes and SHA-256 | strict `resolve_config` before run publication | immutable resolved config plus descriptor checksum |
| Run | generated run ID bound to resolved shared root | immutable run descriptor initializer | descriptor, identity reservation, and completion marker |
| Authority schema | independent integer schema version plus DDL SHA/fingerprint and membership mode | fresh authority initializer | SQLite metadata and bootstrap-complete marker |
| Syncer writer | exact `(leader_epoch, owner_id)` lease token; PBS job ID is metadata, not authority | SQLite leader lease transaction | leader lease row and command journal |
| Static learner | descriptor learner ID plus logical launch, attempt, and contributor-generation fence | descriptor scope and fenced admission transaction | admission request/disposition and current contributor fence |
| Dynamic learner | stream ID plus unique instance ID and stream-generation fence; launch request or bootstrap reservation authorizes mutation | dynamic membership authority | launch/admission rows and current contributor fence |
| Receipt/proposal | immutable object ID, payload digest, contributor fence, cursor chain, and cycle identity | accepted authority transaction | immutable object plus receipt/proposal/ledger rows |
| Global version | integer successor, predecessor, publication ID, immutable weight/optimizer hashes | fenced merge/commit transaction | committed version/publication rows and immutable objects |
| Terminal state | terminal request/cutoff, contributor ack fence, final merge accounting | fenced terminal service | controller/terminal rows and command receipts |
| PBS allocation | normalized full job ID and scheduler history | PBS scheduler | `PBS_JOBID`, nodefile, qstat history, job stdout/stderr |
| Gate artifact | experiment ID plus source/config/workload identity and artifact SHA | named checker after independent raw evidence exists | tracked atomic JSON artifact and referenced raw paths |

## Fault scenarios frozen before runtime tests

### F-LEARNER-REPLACE

- Mutation authority: a new admitted attempt obtains a strictly newer
  contributor fence; an old attempt cannot mutate receipt/proposal/adoption
  state after replacement.
- Fault layer: learner process/PBS actor after at least one accepted cycle.
- Injection: terminate one learner, then start a successor for the same logical
  contributor using the current launcher.
- Durable multi-node success oracle: newer contributor fence and replaced
  binding history in SQLite, successor accepted, contributor progresses to
  terminal ack, ledger remains balanced, and no duplicate proposal becomes
  committed. Stale-fence mutation rejection is proven independently by
  authority unit tests; the sequential process injection does not claim to
  execute a live old writer after replacement.
- Replay/recovery: immutable request/proposal IDs and fenced commands may replay
  idempotently; only the successor fence may create new accepted work.
- Cleanup owner: the integration harness after terminal proof and artifact
  projection; live/resumable state is never deleted.

### F-SYNCER-TAKEOVER

- Mutation authority: only the exact current `(epoch, owner_id)` may commit a
  business transaction.
- Fault layer: syncer process/PBS actor outside a SQLite transaction at the one
  launcher-owned takeover boundary version, consumed unchanged by the Checker.
- Injection: pause and terminate the first leader at that safe point, then start
  a successor and wait for a higher epoch.
- Durable multi-node success oracle: higher successor epoch, one
  predecessor-successor publication per version, no stale-epoch publication,
  all learners adopt and acknowledge terminal, and SQLite integrity is `ok`.
  Stale-token rejection and command-replay conflicts are proven independently
  by authority unit tests; the sequential process injection does not claim to
  execute a concurrent stale writer.
- Replay/recovery: publication/merge commands use exact IDs and may replay only
  to the same committed result; successor reconciles an already-published
  immutable object rather than creating an alternate version.
- Cleanup owner: the integration harness after terminal and successor-epoch
  evidence has been projected.

Dynamic membership remains part of the single current Full Protocol and has
focused capacity, scheduler, admission, fencing, replay and terminal unit
coverage. It is not a registered plan03-1 multi-node fault scenario: the
`FAULT-4L1S-01` acceptance topology is the two static scenarios above.
