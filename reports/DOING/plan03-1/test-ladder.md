# plan03-1 test ladder and resource budget

All runtime tests use the `miyabi-development` routing rules. The current shell
is a Miyabi login/control-plane host, so project imports and tests wait for a
confirmed compute allocation.

| Gate | Topology | Planned workload | Walltime budget | Promotion evidence |
|---|---:|---|---:|---|
| S0 static preflight | login only | shell syntax, tracked-name/path/import scans, diff check, PBS literal group checks | no PBS | static artifact with explicit file set |
| U1 focused + unit | 1 compute node | focused tests, retained-module suite, harness unit suite, then full suite | `01:00:00` interactive as required by workflow | command log, environment/modules, source identity, pytest summary |
| R1 test-design review | login only | frozen integration harness, requirements, fault layers, oracles, cleanup | no PBS | Codex internal report and dispositions |
| F1 final 4 learners + 1 syncer | 5 `debug-g` nodes per scenario | normal, learner replacement and syncer takeover; 20 local steps and 4 committed global steps | `00:10:00` | three same-target structured authority/object/terminal/fault checker PASS artifacts |
| R2 preformal current-state review | login only | complete tracked current state and final ladder design | no PBS | Codex internal report with all findings closed |
| G1 formal 8 learners + 1 syncer | 9 `regular-g` nodes | exactly 50 local optimizer steps per learner cycle and 10 committed global steps | `00:10:00` | formal checker PASS from the final common target |
| R3 final evidence review | login only | same-target formal evidence, matrix, docs, cleanup | no PBS | Codex internal final-evidence report with all findings closed |

The formal estimate uses the retained larger nine-node FP32 run
`2514623.opbs` (60 local steps and 20 committed versions within a 20-minute
request) together with the final P3 five-node runtimes of 15--29 seconds. The
50 by 10 G1 workload is materially smaller than the retained nine-node run;
10 minutes is therefore the shortest practical request and still leaves ample
startup, runtime-variance, and orderly-teardown margin. Queue/runtime evidence
is rechecked before submission, and the request is increased if conditions
invalidate that margin, but it is never reduced below 10 minutes.

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
