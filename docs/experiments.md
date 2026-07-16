# Experiments

## Correctness Matrix

```text
1 learner, quorum=1, stop_after_outer_steps=3
2 learners, quorum=1
2 learners, quorum=2
8 learners, quorum=4
```

## Optimizer Matrix

```text
outer_optimizer: nesterov, momentum, adamw
nesterov lr: 0.1, 0.3, 0.7, 1.0
inner_steps: 10, 50, 100
max_staleness_versions: 0, 1, 2, 4
```

## Resilience Matrix

```text
sleep_jitter_seconds: 0, 10, 30
upload_skip_probability: 0.0, 0.05, 0.2
quorum_min: 2, 4, 6, 8
```

## Metrics To Track

- update write time;
- update read time;
- syncer aggregation time;
- global publish time;
- learner tokens/sec;
- selected learner count per global version;
- effective selected tokens per outer step;
- stale updates dropped.

Use:

```bash
python -m fs_diloco.analysis runs/fs_diloco/<RUN_ID> --json
```

for a machine-readable summary.
