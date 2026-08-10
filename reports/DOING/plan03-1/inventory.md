# plan03-1 startup inventory

Inventory source: tracked tree at
`7d4a607b753744d9b57b54fe0400d1267b13cc40` on 2026-08-10. The entries
below separate observed current state from implementation decisions.

## Observed current state

| Surface | Observed fact |
|---|---|
| Python package | 83 tracked files and 27,221 lines across `fs_diloco/`, root `baselines/`, and `main.py`. |
| Configuration | `core/config.py` defines a shared `Config`; `core/config_v4.py` wraps it in `ConfigV4` and owns the strict production loader/resolver. |
| Runtime | Top-level learner/syncer shims lead through `runtime/*_entrypoint.py` into `runtime/learner_v4.py` and `runtime/syncer_v4.py`. |
| Storage | Static and dynamic DDL are `schema_v4.sql` and `schema_v4_dynamic.sql`; bootstrap symbols and durable directories also carry v4 names. |
| Compatibility | `fs_diloco/legacy/` retains v1-v3 and Fragment V0 query paths; current analysis/evaluation tools import them. |
| Alternative runtime | Root `baselines/` remains runnable, while the package entry point still names the removed `fs_diloco.baselines.train` module. |
| Tools | `fs_diloco/tools/` has 18 modules, including previous-plan comparison, replacement authorization, workload-equivalence, and scheduler-uncertainty utilities in addition to current operator tools. |
| Tests/configs | There are zero tracked files under `tests/` and zero tracked files under `configs/`. |
| PBS/runtime scripts | Only `scripts/miyabi/codex/run_multi_agent_review.pbs` remains; documentation names deleted learner/syncer/debug/9-node scripts. |
| Packaging | `pyproject.toml` exposes a deleted v3-to-v4 migration module, a nonexistent package baseline, and three schema package-data names although only the two v4 files exist. |
| Documentation | README and 18 docs files describe Full Protocol v4, legacy query compatibility, baselines, migration, versioned files/paths, and scripts/configs absent from the tree. |
| Git/workflow | Worktree was clean. `plan03-1` was created as the dedicated branch at the plan-bearing commit above. |

## Current implementation surface

- Core: configuration, independent artifact versions, immutable descriptor, and
  adoption policy.
- Protocol: pure typed contributor, receipt, proposal, accounting, selection,
  merge, scheduler, and authority values.
- Storage: immutable file publication, SQLite leader/authority, admission,
  control, object reads, run initialization, artifact policy, audit maintenance,
  tensor identity, and terminal requests.
- Runtime: admission-gated learner, lease-gated syncer, dynamic capacity/PBS,
  merge, maintenance, terminal, adoption, and training/model/data composition.
- Observability: actor JSONL events, resources, and W&B projection.
- Operator surface: initialize/launch, inspect/export metrics, manual terminal
  close, evaluation, quality gate, and cleanup.

## Decisions frozen for this plan

- Retain only the Full Protocol behavior currently implemented by the v4 path,
  but remove the generation suffix everywhere it identifies the product rather
  than an independently versioned wire artifact.
- Retain independent receipt/proposal/artifact/schema integer versions where
  they are data-contract versions. Names such as `CycleReceiptV1` and
  `FullUpdateProposalV2` are therefore not product-generation suffixes.
- Delete compatibility and migration code instead of adapting it.
- Delete the runnable baseline and former-plan evidence tools; they are not part
  of the single current filesystem protocol.
- Recreate one current config family, one PBS launch family, and a current test
  suite. Deleted historic scripts/configs/tests are not compatibility targets.
- Preserve checked historical reports as immutable evidence. Repository-wide
  obsolete-name checks exclude `plans/DONE/**` and `reports/checked/**` because
  rewriting archived evidence would falsify its source identity.

## Non-goals

- Reading, resuming, or migrating old configs, schemas, run roots, checkpoints,
  metadata, or query-only output.
- Preserving old Python imports, CLI flags, entry points, environment variables,
  filenames, path layouts, protocol product labels, or test fixtures.
- Maintaining the DDP/periodic-average baseline or reproducing previous plan
  performance comparisons.
- Changing Full Protocol semantics except where simplification reveals a defect
  that prevents the current requirements or reviewed tests from passing.
