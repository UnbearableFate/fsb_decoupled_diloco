# 模块参考：scripts

## Miyabi runtime

- `run_syncer_candidate.pbs`：独立 v4 candidate。
- `run_static_learner.pbs` / `run_dynamic_learner.pbs`：admission-gated learner。
- `run_v4_allocation.sh`：在一个 allocation 内组合 v4 actors。
- `run_1node_debug.pbs`、`run_2node_debug.pbs`、`run_9node_*.pbs`：full v4 workload。
- `run_8node_torch_*.pbs`：torch DDP/periodic-average baseline。
- validation/LM eval PBS：离线 checkpoint evaluation。

## Verification

- `check_plan03.py`：单一 Plan03 boundary/requirement checker；stdout 只输出 gate status。
- `build_plan03_p5_test_accounting.py`：从 P5 base AST 重建逐 test-function 删除 disposition 与 count artifact。
- phase4/phase5 PBS：compute-node regression gates。
- source capture、SQLite/shared-FS probe、pointer polling 和 device benchmark：证据工具。

所有 PBS 提交前必须 `bash -n`、确认 literal group ID，并根据证据选择至少 10 分钟且有安全裕量的 walltime。脚本只做编排，不改变 protocol/authority 语义。classic/fragment writer 的 PBS 已从主线删除。
