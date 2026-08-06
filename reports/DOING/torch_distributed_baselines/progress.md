# Progress

## 2026-08-06 10:57:20 JST — static-source-and-pbs validation PASS

- Scope: new baseline Python sources, shared config extension, health-check wrapper,
  and all repository PBS scripts on the Miyabi login/control plane.
- Changes covered: DDP/periodic protocol, run artifacts, training CLI, health checker,
  formal/tiny configs, console entry point, and both 8-node launchers.
- Commands:
  - `python -m py_compile fs_diloco/baselines/__init__.py
    fs_diloco/baselines/artifacts.py fs_diloco/baselines/protocol.py
    fs_diloco/baselines/train.py fs_diloco/baselines/health.py
    scripts/miyabi/check_torch_baseline_health.py`
  - `bash -n scripts/miyabi/*.pbs`
  - `/work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/ruff check
    fs_diloco/baselines fs_diloco/core/config.py tests
    scripts/miyabi/check_torch_baseline_health.py`
  - literal `#PBS -W group_list=xg24i002` and new-script placeholder inspection.
- Result: all commands passed; 17 PBS scripts use the literal project group and neither
  new launcher contains a template placeholder.
- Evidence: `artifacts/20260806-105720_static-source-and-pbs_pass.txt`.
- Remaining risk: no Python runtime import, unit test, Gloo process group, CUDA/NCCL,
  real GPT-2/WikiText-2, multi-node, review-gate, or formal 8-node behavior has yet been
  exercised.

## 2026-08-06 11:09:58 JST — focused unit and 2-process Gloo tests PASS

- Scope: DDP accumulation/no-sync boundaries, non-finite loss failure, two-rank DDP
  equivalence to a clipped combined-batch update, BF16 periodic arithmetic averaging,
  optimizer/scheduler retention, 100-step schedule, config resolution, exclusive run
  initialization, rank data sharding, and health-check pending/PASS/failure cases.
- Environment: confirmed PBS compute node `mg0004`, allocation `2497687.opbs`,
  `select=1`, modules `nvidia/25.9` and `nv-hpcx/25.9`; 00:04:08 of 00:30:00 used.
- Command: `PYTHONPATH=/work/xg24i002/x10041/fsb_decoupled_diloco-master
  /work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/python -m pytest -q
  tests/test_torch_baseline_protocol.py
  tests/test_torch_baseline_artifacts_and_data.py
  tests/test_torch_baseline_health.py`.
- Result: `15 passed in 8.86s`. Both distributed protocol tests created real two-process
  Gloo groups; DDP ranks matched the combined clipped gradient and periodic ranks reached
  the BF16 arithmetic mean without changing local AdamW moments or scheduler state.
- Evidence: `artifacts/20260806-110958_focused-pytest_pass.txt`.
- Remaining risk: the complete repository test suite, CLI torchrun smoke, CUDA/NCCL,
  real model/data, multi-node launch, review gate, and formal experiments remain.

## 2026-08-06 11:15:10 JST — complete repository regression suite PASS

- Scope: all repository tests after integrating the dedicated
  `runs/torch_baselines/{run_id}` namespace into the existing configuration invariant.
- Environment: confirmed compute node `mg0004`, allocation `2497687.opbs`, modules
  `nvidia/25.9` and `nv-hpcx/25.9`; 00:08:37 of 00:30:00 used.
- Command: `PYTHONPATH=/work/xg24i002/x10041/fsb_decoupled_diloco-master
  /work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/python -m pytest -q`.
- Result: `375 passed in 18.15s`.
- Evidence: `artifacts/20260806-111510_full-pytest_pass.txt`.
- Remaining risk: CLI torchrun smoke, CUDA/NCCL, real GPT-2/WikiText-2, multi-node,
  review gate, and formal experiments remain.

## 2026-08-06 11:18:11 JST — two-process torchrun/Gloo CLI smoke PASS

- Scope: real CLI/process lifecycle for both modes with two local processes, per-rank
  artifacts, synchronization accounting, terminal summaries, and distributed refusal to
  overwrite an existing run.
- Environment: compute node `mg0004`, allocation `2497687.opbs`; CPU/Gloo through the
  repository virtualenv, with the plan worktree explicitly on `PYTHONPATH`; 00:11:28 of
  00:30:00 used.
- Commands: `python -m torch.distributed.run --standalone --nproc-per-node=2 -m
  fs_diloco.baselines.train` against `configs/torch_baseline_tiny_2rank.yaml`, once with
  `--mode ddp --max-steps 4` and once with `--mode periodic_average --max-steps 4
  --average-interval 2`; then the DDP command was repeated against its existing run root.
- Result: both fresh runs exited 0. DDP recorded four gradient synchronization events;
  periodic average recorded BF16 parameter averages at steps 2 and 4; both summaries
  report `status=completed`, `final_step=4`, `exit_status=0`. The repeat launch failed on
  both ranks and torchrun exited 1 with the expected `FileExistsError`.
- Run roots:
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/smoke_ddp_20260806_1116`
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/smoke_periodic_20260806_1116`
- Evidence: `artifacts/20260806-111811_torchrun-gloo-smoke_pass.txt`; authoritative
  metrics and logs remain in the two run roots.
- Remaining risk: single-GPU real GPT-2/WikiText-2, NCCL, two-node launch, review gate,
  and formal experiments remain.

## 2026-08-06 11:22:40 JST — 1-node real GPT-2/WikiText-2 NCCL paths PASS

- Scope: actual Hugging Face GPT-2 checkpoint/tokenizer, repository WikiText-2 block
  pipeline, BF16 model/autocast, micro-batch 2, eight-way accumulation, AdamW/cosine,
  gradient clipping, CUDA/NCCL, DDP construction, and periodic BF16 parameter all-reduce.
- Environment: compute node `mg0004`, allocation `2497687.opbs`, NVIDIA GH200 120GB,
  modules `nvidia/25.9` and `nv-hpcx/25.9`; 00:16:30 of 00:30:00 used.
- Commands: one-process torchrun with
  `configs/torch_baseline_gpt2_wikitext2_1rank_debug.yaml`, `--backend nccl`, and
  `--max-steps 2`, once per mode; the periodic run used `--average-interval 2`.
- Result: both runs exited 0 with finite losses and completed summaries. DDP losses were
  3.834762/3.972530 and recorded two gradient syncs; periodic losses were
  3.834762/3.971846 and recorded one 124,439,808-element BF16 parameter average at step
  2. The initial model download populated the shared Hugging Face cache.
- Run roots:
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/real1n_ddp_20260806_1119`
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/real1n_periodic_20260806_1121`
- Evidence: `artifacts/20260806-112240_real1n-nccl_pass.txt`; full rank logs and metrics
  remain under the run roots.
- Remaining risk: two-node rendezvous/NCCL, review gate, and formal experiments remain.

## 2026-08-06 11:26:46 JST — 2-node MPI→torchrun/NCCL smoke PASS

- Scope: the formal launch topology (one unbound MPI supervisor per PBS node, one
  torchrun worker/GPU per supervisor), elastic c10d rendezvous, rank/host discovery,
  CUDA/NCCL DDP, and periodic BF16 parameter averaging across nodes.
- Environment: interactive allocation `2497844.opbs`, `select=2:mpiprocs=1`, nodes
  `mg0004` and `mg0021`, one NVIDIA GH200 120GB per rank, modules `nvidia/25.9` and
  `nv-hpcx/25.9`; 00:02:32 of 00:10:00 used after both runs.
- Commands: `mpirun --map-by ppr:1:node --bind-to none -np 2 /usr/bin/env ... bash
  -lc '... python -m torch.distributed.run ... -m fs_diloco.baselines.train'` with the
  two-rank tiny config, NCCL, four optimizer steps, first DDP and then periodic average
  at interval 2.
- Result: both runs exited 0. Manifests prove ranks 0/1 on distinct nodes with
  `backend=nccl`, `device_type=cuda`, and world size 2. DDP recorded four optimizer-step
  gradient syncs; periodic recorded all-rank parameter averages at steps 2 and 4.
- Run roots:
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/real2n_ddp_20260806_1124`
  - `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/real2n_periodic_20260806_1125`
- Evidence: `artifacts/20260806-112646_real2n-nccl_pass.txt`; full manifests, logs,
  metrics, heartbeats, and summaries remain under the run roots.
- Remaining risk: review gate and formal 8-node/200-step acceptance remain.

## 2026-08-06 11:32:42 JST — remediation static and full regression PASS

- Scope: post-ladder hardening for CUDA synchronization timing, immutable source
  identity agreement, PBS launcher preflight refusal, and early-ended PBS health failure.
- Static environment: `miyabi-g1`; `py_compile`, Ruff, `bash -n
  scripts/miyabi/*.pbs`, and `git diff --check` all passed.
- Runtime environment: compute node `mg0012`, allocation `2497879.opbs`, modules
  `nvidia/25.9` and `nv-hpcx/25.9`; 00:01:06 of 00:30:00 used.
- Runtime command: `PYTHONPATH=/work/xg24i002/x10041/fsb_decoupled_diloco-master
  /work/xg24i002/x10041/fsb_decoupled_diloco/.venv/bin/python -m pytest -q`.
- Result: `379 passed in 23.06s`. The suite includes real two-process Gloo protocol
  groups and the new negative checks for source mismatch and a PBS job ending before
  target step.
- Evidence: `artifacts/20260806-113242_remediation-regression_pass.txt`.
- Remaining risk: independent review gate and formal 8-node/200-step acceptance remain.

## 2026-08-06 11:51:26 JST — review findings remediated and verified PASS

- Review target/base: `e30d49f102ca91c44af5e5700457c98a6e26de6e` /
  `c1c61153548ff7b2543d3ce1bc764c19432b138e`.
- Codex independent report:
  `reports/DOING/code_review/torch_distributed_baselines/implementation-and-formal-validation/gpt-5-codex_e30d49f102ca91c44af5e5700457c98a6e26de6e.md`.
- Claude reviewer disposition: `skipped-session-limit`. A fresh `claude --print`
  invocation requested `claude-opus-5` with session
  `fd9b2759-4dff-4ea0-8757-ab12807b3e4c`, output format JSON, permission mode
  `bypassPermissions`, and dangerous-skip enabled. Machine-readable result metadata
  matched the requested session and returned HTTP 429 with the explicit, verifiable
  message `You've hit your session limit · resets 1:30pm (Asia/Tokyo)`, zero model
  usage, and no fallback. Per the completion-gate exception, no Claude report was
  created and the skip does not block remediation or the phase.
- Finding dispositions:
  - `High` checker false-positive for short/wrong-interval runs: **fixed**. Formal
    health now requires manifest `max_steps=5000` and `average_interval=100`; RED tests
    cover a declared 200-step completion and interval 200.
  - `Medium` feature-worktree source selection: **fixed**. Both PBS launchers pass
    `PYTHONPATH=$PROJECT_ROOT` through MPI. A fresh one-node GPT-2/WikiText-2 NCCL
    torchrun started with ambient `PYTHONPATH` unset and completed through the explicit
    project-root pin.
  - `Medium` untested terminal checkpoint publication: **fixed**. New tests cover model
    and tokenizer publication through staging, atomic final-directory rename, and
    staging cleanup after injected tokenizer failure.
  - `Low` checker numeric CLI validation: **fixed** for expected world size, target
    step, polling interval, and timeout.
- Static commands: Python compile, Ruff, `bash -n scripts/miyabi/*.pbs`, and `git diff
  --check`; all passed on `miyabi-g1`.
- Runtime environment: node `mg0007`, allocation `2497967.opbs`, modules
  `nvidia/25.9` and `nv-hpcx/25.9`; 00:03:08 of 00:30:00 used.
- Runtime results: complete repository suite `383 passed in 21.06s`; source-pin smoke
  run `sourcepin_1n_20260806_1150` exited 0 with NCCL/CUDA, GPT-2/WikiText-2, one finite
  optimizer step, and completed local artifacts.
- Evidence: `artifacts/20260806-115126_review-remediation_pass.txt`; source-pin run root
  `/work/xg24i002/x10041/fsb_decoupled_diloco-master/runs/torch_baselines/sourcepin_1n_20260806_1150`.
- Remaining risk: formal 8-node/200-step acceptance and continued 5000-step handoff.

## 2026-08-06 20:57:00 JST — scoped source-identity regression PASS

- Scope: correction for the two formal jobs that rejected unchanged runtime sources
  after unrelated review reports were added while queued.
- Change: `capture_source_identity.py` now computes `git_dirty` over the same runtime
  source scopes used by its fingerprint instead of the entire worktree. A new regression
  proves that an untracked file under `reports/` leaves both runtime dirty state and the
  source fingerprint unchanged, while tracked and untracked files inside `fs_diloco/`
  still mark the source dirty and change the fingerprint.
- Static validation on `miyabi-g1`: `git diff --check`, Python compile, `bash -n
  scripts/miyabi/*.pbs`, literal group scan, and placeholder scan all passed.
- Runtime environment: compute node `mg0011`, interactive allocation `2500425.opbs`,
  `select=1`, modules `nvidia/25.9` and `nv-hpcx/25.9`.
- Runtime commands: focused capture test (`1 passed`) followed by
  `tests/test_capture_source_identity.py tests/test_source_identity.py
  tests/test_torch_baseline_artifacts_and_data.py` (`9 passed in 4.04s`).
- Result: the failure cause is covered and the baseline manifest/source-identity
  integration remains passing. The allocation used 00:01:51 of 00:15:00 and exited
  normally.
- Remaining risk: the fresh 8-node jobs must pass source preflight, reach 200 steps with
  declining finite loss, and continue toward their 5000-step final checkpoints.
