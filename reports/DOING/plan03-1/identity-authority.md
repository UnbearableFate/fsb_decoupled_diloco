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
- Injection: terminate one learner, start a successor for the same logical
  contributor using the current launcher, and allow the old attempt to retry a
  fenced mutation when practical.
- Durable success oracle: newer contributor fence in SQLite, old-fence mutation
  rejected, successor accepted, contributor progresses to terminal ack, ledger
  remains balanced, and no duplicate proposal becomes committed.
- Replay/recovery: immutable request/proposal IDs and fenced commands may replay
  idempotently; only the successor fence may create new accepted work.
- Cleanup owner: the integration harness after terminal proof and artifact
  projection; live/resumable state is never deleted.

### F-SYNCER-TAKEOVER

- Mutation authority: only the exact current `(epoch, owner_id)` may commit a
  business transaction.
- Fault layer: syncer process/PBS actor outside a SQLite transaction, with a
  second candidate already able to acquire after expiry/failure.
- Injection: terminate or pause the first leader at a pre-registered safe point;
  start/retain a second candidate and wait for a higher epoch.
- Durable success oracle: higher successor epoch, old token rejected, one
  predecessor-successor publication per version, no split-brain command receipt,
  all learners adopt and acknowledge terminal, and SQLite integrity is `ok`.
- Replay/recovery: publication/merge commands use exact IDs and may replay only
  to the same committed result; successor reconciles an already-published
  immutable object rather than creating an alternate version.
- Cleanup owner: the integration harness after terminal and successor-epoch
  evidence has been projected.

### F-DYNAMIC-CAPACITY

- Mutation authority: a stream reservation and exact launch request authorize a
  new instance; scheduler observation alone never admits it.
- Fault layer: scheduler/PBS observation and dynamic learner process lifecycle.
- Injection: a reviewed 4+1 dynamic scenario changes active instance state
  through bootstrap, replacement, and terminal drain without editing authority
  rows out of band.
- Durable success oracle: reservation/launch/admission rows form one identity
  chain, no stream has two current fences, requested replacement becomes
  productive, bounded pending counts drain, and terminal accounting closes.
- Replay/recovery: scheduler reconciliation is keyed by exact request and full
  PBS ID; unknown scheduler state does not produce a second submit.
- Cleanup owner: dynamic harness only after terminal proof and scheduler/run
  identity projection.
