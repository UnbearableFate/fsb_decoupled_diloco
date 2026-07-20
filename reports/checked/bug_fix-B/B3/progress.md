# B3 completion record

## 2026-07-17 — delegated S5 implementation audit

- Baseline for the delegated implementation was `77edc08`; implementation commit `c53f14d` removed `sync.upload_mode`, `liveness.quorum_policy`, and `inner_optimizer.reset_on_global_update` from dataclasses and all repository YAMLs.
- The B3 plan was corrected before closeout because its old DCF-04 demanded zero source-string matches while DCF-01 simultaneously required parser tombstones to produce actionable “字段已移除” errors. The authoritative invariant is now: no YAML/dataclass/runtime consumer; the three strings are allowed only in `REMOVED_CONFIG_KEYS`, rejection tests, and documentation.
- DCF-01/02/03/04 are covered by `tests/test_config.py`, including path-aware unknown-key rejection and parameterized resolution of every repository YAML. The S5 report and artifacts are under `reports/imp_plans/bug_fixing/S5/`; its one-node replace/rebase/predict runs and invariant checkers passed.
- The current post-B2 full compute suite also passed 207 tests (`../B2/artifacts/20260717-full-pytest.log`), all repository configs resolve, and static lint/diff checks pass. Historical resolved snapshots are intentionally not rewritten; `docs/06-configuration.md` states that the three fields never controlled runtime behavior.
- B3 completion predicate is satisfied by the delegated S5 implementation; no duplicate code change is required.
