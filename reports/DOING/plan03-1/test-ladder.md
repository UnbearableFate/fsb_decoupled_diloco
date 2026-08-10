# plan03-1 test ladder and resource budget

All runtime tests use the `miyabi-development` routing rules. The current shell
is a Miyabi login/control-plane host, so project imports and tests wait for a
confirmed compute allocation.

| Gate | Topology | Planned workload | Walltime budget | Promotion evidence |
|---|---:|---|---:|---|
| S0 static preflight | login only | shell syntax, tracked-name/path/import scans, diff check, PBS literal group checks | no PBS | static artifact with explicit file set |
| U1 focused + unit | 1 compute node | focused tests, retained-module suite, harness unit suite, then full suite | `01:00:00` interactive as required by workflow | command log, environment/modules, source identity, pytest summary |
| R1 test-design review | 1 compute node, 4 reviewer processes | frozen integration harness, requirements, fault layers, oracles, cleanup | `00:30:00` | Codex report saved first, reviewer-job JSON, dispositions |
| F1 4 learners + 1 syncer | 5 `regular-g` nodes | bounded synthetic Full Protocol normal/fault scenarios; exact workload finalized by reviewed harness | provisional `00:20:00` | structured authority/object/terminal/fault checker PASS |
| R2 preformal current-state review | 1 compute node, 4 reviewer processes | complete tracked current state and final ladder design | `00:30:00` | Codex report saved first, external reports, remediation closure |
| G1 formal 8 learners + 1 syncer | 9 `regular-g` nodes | exactly 50 local optimizer steps per learner cycle and 10 committed global steps | provisional `00:15:00` | formal checker PASS from frozen common target |
| R3 final evidence review | 1 compute node, 4 reviewer processes | same-target formal evidence, matrix, docs, cleanup | `00:30:00` | final evidence reports and dispositions |

The formal estimate is based on the repository's retained prior evidence: PBS
job `2514623.opbs` completed a larger nine-node FP32 workload (60 local steps
per cycle and 20 committed versions) within a `00:20:00` request. The new 50 by
10 workload is roughly half the committed-cycle work, so 15 minutes is the
shortest provisional request with startup and teardown margin. Before qsub,
actual recent queue/run evidence must be rechecked; requests may be increased
when the reviewed harness adds a fault or startup cost that invalidates this
estimate, but never below 10 minutes.

The 4+1 exact workload is intentionally not guessed before its test-design
review. Its final manifest must fix local/global steps, model/data identity,
fault injection points, timeouts, PASS formulas, and cleanup policy before the
first allocation.

Formal source scopes:

```text
fs_diloco/**
configs/**
scripts/miyabi/**
tests/**
pyproject.toml
README.md
docs/**
```

Changing any formal scope after the final common target freeze invalidates all
dependent formal evidence. Reports, archived plans, and checked historical
evidence are outside the runtime-source fingerprint.
