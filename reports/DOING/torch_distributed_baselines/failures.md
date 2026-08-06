# Failures

## 2026-08-06 10:55:29 JST — static-lint-01 (consecutive failure 1)

- Environment: `miyabi-g1` login/control plane; static validation only; branch
  `codex/torch_ddp_baselines` based at `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Command:
  `/work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/ruff check fs_diloco/baselines fs_diloco/core/config.py tests scripts/miyabi/check_torch_baseline_health.py`
- Expected: all newly added Python sources pass the repository lint rules.
- Actual: Ruff reported E731 for the default all-reduce lambda in
  `fs_diloco/baselines/protocol.py` and F401 for an unused `math` import in
  `fs_diloco/baselines/train.py`. Python compile checks and `bash -n
  scripts/miyabi/*.pbs` passed in the same static group.
- Confirmed cause: two local style defects; no runtime behavior was exercised.
- Evidence: `artifacts/20260806-105529_static-lint-01_fail.txt`.
- Next change: replace the lambda with a named nested function and remove the unused
  import, then rerun the complete static group to disprove any remaining syntax or lint
  defect.

## 2026-08-06 10:56:38 JST — static-placeholder-scan-01 (consecutive failure 1)

- Environment: `miyabi-g1` login/control plane; static validation only.
- Command: `rg -n '^#PBS -W group_list=' scripts/miyabi/*.pbs && if rg -n
  '^#PBS -W group_list=<|<group_id|<num_nodes>|<config' scripts/miyabi/*.pbs; then
  exit 1; fi`.
- Expected: confirm all PBS group directives are literal and no launcher template
  placeholders remain.
- Actual: the second expression also matched pre-existing informational strings such as
  `${TRAINING_SEED:-<config>}`. It exited 1 even though every listed `group_list` was the
  literal `xg24i002`. Compile, Ruff, and `bash -n` all passed.
- Confirmed cause: the inspection command was over-broad; it did not identify a PBS
  directive or launcher argument placeholder.
- Evidence: `artifacts/20260806-105638_static-placeholder-scan-01_fail.txt`.
- Next change: restrict the placeholder scan to PBS directive lines and the two newly
  added scripts, then rerun the complete static group.

## 2026-08-06 11:06:38 JST — focused-pytest-01 (consecutive failure 1)

- Environment: confirmed PBS compute node `mg0004`, allocation `2497687.opbs`,
  `select=1`, modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Command: `/work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/pytest -q
  tests/test_torch_baseline_protocol.py
  tests/test_torch_baseline_artifacts_and_data.py
  tests/test_torch_baseline_health.py`.
- Expected: collect and run the new focused unit/distributed test group from the plan
  worktree.
- Actual: collection failed for all three files with `ModuleNotFoundError:
  fs_diloco.baselines`; no test body ran.
- Confirmed cause: the reused virtualenv has an editable-package finder bound to the
  primary worktree, whose separate dirty branch does not contain this plan's new package.
  Invoking the environment's `pytest` console script did not put the plan worktree ahead
  of that finder.
- Evidence: `artifacts/20260806-110638_focused-pytest-01_fail.txt`.
- Next change: do not mutate or reinstall the shared environment; invoke the same Python
  with `PYTHONPATH` explicitly set to the plan worktree and rerun the identical focused
  group.

## 2026-08-06 11:08:50 JST — focused-pytest-02 (consecutive failure 2)

- Environment: compute node `mg0004`, allocation `2497687.opbs`; 00:03:15 of
  00:30:00 used; plan worktree explicitly first on `PYTHONPATH`.
- Command: `PYTHONPATH=/work/xg24i002/x10041/fsb_decoupled_diloco-master
  /work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/python -m pytest -q
  tests/test_torch_baseline_protocol.py
  tests/test_torch_baseline_artifacts_and_data.py
  tests/test_torch_baseline_health.py`.
- Expected: all focused protocol, distributed Gloo, artifact/data, and checker tests pass.
- Actual: 14 passed and one DDP equivalence assertion failed. Both ranks produced weight
  `1.1000000238`; the reference expected `2.5`.
- Confirmed cause: the distributed path correctly applied configured gradient clipping
  at norm 1.0, while the hand-written combined-batch reference omitted the same clip.
  Both ranks agreeing and the exact 0.1 update are consistent with the configured
  clipped SGD step; this is a test-oracle defect, not evidence of a reducer defect.
- Evidence: `artifacts/20260806-110850_focused-pytest-02_fail.txt`.
- Next change: apply the identical `clip_grad_norm_=1.0` to the combined-batch reference,
  then rerun the complete focused group. A third same-group failure will trigger the
  required comprehensive review before another attempt.

## 2026-08-06 11:12:06 JST — full-pytest-01 (consecutive failure 1)

- Environment: compute node `mg0004`, allocation `2497687.opbs`; 00:06:10 of
  00:30:00 used.
- Command: `PYTHONPATH=/work/xg24i002/x10041/fsb_decoupled_diloco-master
  /work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/python -m pytest -q`.
- Expected: complete repository regression suite passes with the new baseline surface.
- Actual: `2 failed, 373 passed in 19.55s`. The existing config-root invariant assumed
  every repository YAML writes under `runs/fs_diloco`; the formal baseline correctly
  configured `runs/torch_baselines`, while the tiny config used `null` because its CLI
  smoke always overrides the root.
- Confirmed cause: the existing parameterized invariant lacked the newly intentional
  baseline run-root namespace, and the tiny baseline did not provide a repository-safe
  default. This is a configuration/test-contract integration defect.
- Evidence: `artifacts/20260806-111206_full-pytest-01_fail.txt`.
- Next change: give both baseline configs the primary-worktree
  `runs/torch_baselines/{run_id}` default and extend the existing invariant to select
  that namespace only when `torch_baseline.enabled`; rerun the full suite.

## 2026-08-06 11:13:46 JST — full-pytest-02 (consecutive failure 2)

- Environment: compute node `mg0004`, allocation `2497687.opbs`; 00:07:20 of
  00:30:00 used.
- Command: identical complete repository pytest command from `full-pytest-01`.
- Expected: both loaded-template and resolved-root assertions recognize the dedicated
  baseline namespace.
- Actual: `2 failed, 373 passed in 18.16s`. The loaded-template assertion passed, but a
  second assertion in the same test still compared the resolved path against the legacy
  filesystem-only root.
- Confirmed cause: the previous targeted test update covered only the first of two
  assertions in the existing invariant.
- Evidence: `artifacts/20260806-111346_full-pytest-02_fail.txt`.
- Next change: reuse the already selected `expected_root` for the resolved path assertion
  and rerun the complete suite. A third same-suite failure will trigger the required
  comprehensive review before a fourth attempt.

## 2026-08-06 — claude-review-invocation-01 (consecutive failure 1)

- Environment: `miyabi-g1` login/control plane; clean review target
  `e30d49f102ca91c44af5e5700457c98a6e26de6e` against
  `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Invocation: fresh `claude --print`, requested model `claude-opus-5`, session
  `4c7c2fdc-c5af-4af3-aafc-49436742051a`, `--output-format json`, permission mode
  `bypassPermissions`, and `--dangerously-skip-permissions`.
- Expected: independent reviewer-only report for the frozen target.
- Actual: Codex interrupted the still-running invocation during an unnecessary
  re-audit of whether the final hardening changes preceded the target commit. Git
  inspection then confirmed that they were already included in the target. No Claude
  report was created and no substantive result or model metadata was read.
- Confirmed cause: operator sequencing error, not a reviewer/session/model failure.
- Evidence: process exit 1, empty captured stdout at interruption, and no file under the
  target code-review directory.
- Next change: start a new independent session with a new UUID against the same frozen
  commit and allow it to finish while Codex completes its independent report.

## 2026-08-06 — formal-8node-launch-01 (consecutive failure 1)

- Jobs and runs: DDP `2497995.opbs` /
  `20260806_115410_torch_ddp_gpt2_wikitext2_8n_5k`; periodic average
  `2497996.opbs` /
  `20260806_115410_torch_periodic_average_gpt2_wikitext2_8n_5k`.
- Environment: two independent `select=8:mpiprocs=1` allocations in `small-g`, source
  commit `1d4ff72f46766082e77de95fff21d6ab130d83ac`.
- Expected: both launchers pass source preflight and begin the 5000-step distributed
  training runs.
- Actual: both exited with status 2 after two seconds, before MPI, torchrun, model load,
  or GPU training. PBS reported no abnormal nodes and zero GPU memory use. Both stdout
  files contain `Formal torch baseline requires a clean source commit`.
- Confirmed cause: while the jobs were queued, the requested GLM-5.2 and DeepSeek V4
  Flash review reports were created as untracked files under `reports/`. Source identity
  fingerprints only `fs_diloco`, `configs`, `scripts`, and environment metadata, but its
  `git_dirty` flag incorrectly inspected the entire worktree. The unrelated review
  evidence therefore rejected otherwise unchanged runtime sources.
- Evidence: `torch_ddp_gpt2_5k.o2497995`, `torch_pavg_gpt2_5k.o2497996`, `tracejob`
  terminal records, and both failed run roots' `source_identity.json` files.
- Next change: scope the source-dirty check to the same runtime source paths included in
  the fingerprint, add a regression proving an untracked `reports/` file neither marks
  runtime source dirty nor changes the fingerprint, then rerun static and compute-node
  validation before resubmitting with fresh run IDs.

## 2026-08-06 21:43 JST — formal-8node-launch-02 (consecutive failure 1)

- Jobs and runs: DDP `2500440.opbs` /
  `20260806_210001_torch_ddp_gpt2_wikitext2_8n_5k`; periodic average
  `2500441.opbs` /
  `20260806_210001_torch_periodic_average_gpt2_wikitext2_8n_5k`.
- Environment: two independent `select=8:mpiprocs=1` allocations in `small-g`, source
  commit `051061f46ebf4c889118fcf39088edc8e8743eb7` and runtime fingerprint
  `sha256:f15b030f14b021916f4865562e991c2346c5f8d796b810bcbe78815e97f473b0`.
- Expected: both launchers expose one local GPU per rank, initialize their eight-rank
  NCCL process groups, and begin real GPT-2/WikiText-2 training.
- Actual: both passed clean-source preflight and rendezvous, then every training process
  failed in `_runtime_device` with `RuntimeError: NCCL baseline requires CUDA`. Both
  jobs exited 1 after 14 seconds; PBS reported no abnormal nodes.
- Confirmed cause: each launcher captured the allocation shell's ambient
  `CUDA_VISIBLE_DEVICES` GPU UUID and explicitly sent that same node-specific UUID to
  all eight MPI children. The UUID is valid only on its owning node, so remote ranks
  could not enumerate a CUDA device. The repository's established multi-node launchers
  instead pass a logical local ordinal (`0`) and export it independently in every MPI
  child.
- Evidence: both `train.log` files under `logs/qsub_20260806_210001_*`, PBS stdout
  `torch_ddp_gpt2_5k.o2500440` / `torch_pavg_gpt2_5k.o2500441`, and terminal
  `tracejob` records.
- Next change: pass `WORKER_CUDA_VISIBLE_DEVICES=0` through MPI, export it only inside
  each node-local supervisor, and verify both algorithms for at least 50 real-model,
  real-dataset steps on interactive one-node and two-node allocations before another
  formal submission.
