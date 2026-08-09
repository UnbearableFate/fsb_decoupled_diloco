"""Build auditable P5 classic/fragment test-deletion accounting."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_COMMIT = "77e047cc5e291153736f9abbffb8986e6b912330"
BASELINE_COLLECTED = 894


@dataclass(frozen=True)
class Disposition:
    classification: str
    replacements: tuple[str, ...]
    reason: str


MIGRATE = "migrate-to-unified"
RETAIN = "retain-legacy-reader"
DELETE = "delete-obsolete"


def _refs(path: str, *tests: str) -> tuple[str, ...]:
    references: list[str] = []
    current_path = path
    for item in tests:
        if item.startswith("tests/") and item.endswith(".py"):
            current_path = item
        else:
            references.append(f"{current_path}::{item}")
    return tuple(references)


FILE_DISPOSITIONS: dict[str, Disposition] = {
    "tests/reference/test_plan03_classic_static_oracle.py": Disposition(
        MIGRATE,
        _refs(
            "tests/protocol/test_p3_unified_v4_golden.py",
            "test_unified_no_change_case_is_exactly_anchored_to_both_p0_projections",
            "test_unified_v4_merge_math_generates_the_p0_semantic_projection",
        ),
        "The frozen classic arm is tag-only; its semantic projection is retained by unified v4 golden tests.",
    ),
    "tests/test_adoption_telemetry.py": Disposition(
        MIGRATE,
        _refs(
            "tests/observability/test_p3_operational_evidence.py",
            "test_actor_telemetry_claim_prevents_shared_attempt_append",
            "test_actor_telemetry_payload_cannot_override_frozen_actor_identity",
        ),
        "Shared CSV append telemetry was replaced by immutable actor-scoped JSONL telemetry.",
    ),
    "tests/test_bounded_1000_cycles.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_visibility_v4.py",
            "test_visibility_upsert_and_pointer_archive_are_bounded",
            "tests/storage/test_proposal_adjudication_v4.py",
            "test_quarantine_hot_rows_are_bounded_for_distinct_conflicts",
        ),
        "The bounded-surface invariant is covered by v4 visibility/frontier/quarantine authority.",
    ),
    "tests/test_fragment_codec.py": Disposition(
        DELETE,
        (),
        "Fragment tensor extraction and update writing are unsupported production-writer behavior.",
    ),
    "tests/test_fragment_final_wait.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p3_operational.py",
            "test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens",
            "test_terminal_close_accepts_only_one_contiguous_current_cycle_and_matching_update",
        ),
        "Heartbeat-based fragment shutdown is gone; the shared terminal drain/ack invariant is v4 authority state.",
    ),
    "tests/test_fragment_index.py": Disposition(
        RETAIN,
        _refs(
            "tests/test_fragment_analysis.py",
            "test_fragment_v0_analysis_uses_legacy_pure_decoders",
        ),
        "Historical fragment index coverage/validation remains a pure legacy decoder concern.",
    ),
    "tests/test_fragment_latest_retry.py": Disposition(
        MIGRATE,
        _refs(
            "tests/runtime/test_p4_mandatory_runtime.py",
            "test_epoch_control_ignores_polluted_fixed_cache_and_repairs_it",
            "test_latest_head_rejects_path_escape_and_payload_identity_mismatch",
        ),
        "Mixed-version retry is replaced by epoch-scoped, identity-checked v4 controls.",
    ),
    "tests/test_fragment_materialization.py": Disposition(
        DELETE,
        (),
        "Fragment materialization scheduling was part of the removed writer and has no current runtime meaning.",
    ),
    "tests/test_fragment_merge.py": Disposition(
        MIGRATE,
        _refs(
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_merge_weights_use_stable_fsum_and_reject_duplicate_ids",
            "test_full_quorum_keeps_stable_set_and_reduction_order",
        ),
        "Selection-before-weighting and deterministic reduction are retained in unified proposal selection.",
    ),
    "tests/test_fragment_pipeline_smoke.py": Disposition(
        DELETE,
        (),
        "The test asserted that unsupported fragment production artifacts existed.",
    ),
    "tests/test_fragment_pointer_discovery.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_visibility_v4.py",
            "test_visibility_upsert_and_pointer_archive_are_bounded",
            "test_visibility_requires_receipt_and_pointer_sequence_collision_fails_closed",
            "tests/storage/test_publication_v4.py",
            "test_concurrent_immutable_publishers_never_overwrite_the_winner",
        ),
        "Fixed discovery, latest-wins/frontier, boundedness, and no-overwrite moved to v4 visibility/publication.",
    ),
    "tests/test_fragment_scheduler.py": Disposition(
        RETAIN,
        _refs(
            "tests/test_fragment_analysis.py",
            "test_fragment_v0_analysis_uses_legacy_pure_decoders",
        ),
        "Only the historical event-to-version decoder remains useful for legacy analysis.",
    ),
    "tests/test_fragment_store.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_p2_state_machine.py",
            "test_proposal_state_machine_keeps_one_pending_and_monotonic_frontier",
            "tests/storage/test_authority_p3_operational.py",
            "test_terminal_close_accepts_only_one_contiguous_current_cycle_and_matching_update",
            "tests/storage/test_proposal_adjudication_v4.py",
            "test_insert_supersede_and_frontier_failures_roll_back_as_one_unit",
        ),
        "Shared lifecycle, supersession, rollback, frontier, and terminal invariants are explicit v4 tests.",
    ),
    "tests/test_interval_telemetry.py": Disposition(
        MIGRATE,
        _refs(
            "tests/observability/test_p3_operational_evidence.py",
            "test_actor_telemetry_payload_cannot_override_frozen_actor_identity",
            "tests/storage/test_authority_p3_operational.py",
            "test_telemetry_deletion_cannot_change_authoritative_token_summary",
        ),
        "Non-authoritative shared CSV intervals were replaced by actor telemetry and authority accounting.",
    ),
    "tests/test_latest_load_retry.py": Disposition(
        MIGRATE,
        _refs(
            "tests/runtime/test_p4_mandatory_runtime.py",
            "test_latest_head_rejects_path_escape_and_payload_identity_mismatch",
            "test_authority_missing_fails_closed_even_when_fixed_cache_exists",
        ),
        "Current epoch controls replace classic fixed latest retry and fail closed without authority.",
    ),
    "tests/test_learner_completion.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p3_operational.py",
            "test_terminal_close_freezes_fence_blocks_admission_and_accounts_hard_crash",
            "test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens",
        ),
        "Completion is now a fenced controller/terminal state machine rather than fixed stop/watchdog files.",
    ),
    "tests/test_learner_rebase.py": Disposition(
        MIGRATE,
        _refs(
            "tests/test_adoption_strategy.py",
            "test_rebase_strategy_owns_anchor_tokens_and_clears_after_adoption",
            "test_prediction_strategy_starts_reconciles_and_abandons_on_stop",
            "test_prediction_strategy_timeout_keeps_state_for_diagnostics",
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_predict_rebase_retains_work_without_loss_or_double_count",
        ),
        "Model adoption/rebase behavior remains shared modeling logic and v4 segment accounting.",
    ),
    "tests/test_liveness.py": Disposition(
        MIGRATE,
        _refs(
            "tests/runtime/test_p4_mandatory_runtime.py",
            "test_heartbeat_publication_uses_exact_committed_lease_and_rejects_delay",
            "test_stale_epoch_admission_response_cannot_open_torch_gate",
        ),
        "Classic heartbeat tables/pointers were replaced by leader-fenced v4 heartbeat/control evidence.",
    ),
    "tests/test_midcycle_adoption_metadata.py": Disposition(
        MIGRATE,
        _refs(
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_prepublication_replace_discards_old_segment_and_resets_effective_metrics",
            "test_predict_rebase_retains_work_without_loss_or_double_count",
        ),
        "Adoption metadata is represented by cycle-segment receipts and explicit token fate.",
    ),
    "tests/test_parallel_publication.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_publication_v4.py",
            "test_prepared_intent_precedes_io_and_commit_verifies_exact_theta_pair",
            "test_publication_crash_prefix_is_reconciled_idempotently",
        ),
        "Publication ordering/rollback moved from classic worker futures to fenced v4 publication intents.",
    ),
    "tests/test_plan01_checker.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_contributor_progress.py",
            "test_contributor_progress_advances_only_contiguous_receipt_chain",
            "test_contributor_progress_rejects_sequence_hole_and_cursor_mismatch",
        ),
        "Classic resume-progress checking is replaced by authoritative receipt hash/cursor continuity.",
    ),
    "tests/test_plan02_feasibility.py": Disposition(
        DELETE,
        (),
        "Plan02 feasibility/checker/PBS probe fixtures are frozen evidence, not current runtime tests.",
    ),
    "tests/test_plan02_phase1_ha.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_schema_v4.py",
            "test_fresh_v4_schema_initializes_reopens_and_is_integral",
            "tests/storage/test_leader_authority_commands.py",
            "test_stale_token_cannot_execute_a_named_business_command",
            "test_authority_surface_does_not_offer_direct_sql_or_raw_connection",
            "tests/storage/test_publication_v4.py",
            "test_takeover_reconciles_predecessor_intent_and_orphan_grace_is_lease_safe",
        ),
        "Lease/schema/fencing/takeover/GC invariants moved to finite v4 authority commands and tests.",
    ),
    "tests/test_plan02_phase2_dynamic.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p2_dynamic.py",
            "test_authorized_replacement_atomically_retires_old_incarnation",
            "tests/storage/test_p2_state_machine.py",
            "test_dynamic_membership_state_machine_has_one_current_incarnation",
            "tests/runtime/test_p4_mandatory_runtime.py",
            "test_dynamic_launcher_preserves_each_partial_submission_receipt",
        ),
        "Dynamic admission/replacement/scheduler identities are now fenced v4 state and current runtime tests.",
    ),
    "tests/test_plan02_phase2_review_remediation.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p2_dynamic.py",
            "test_revoke_after_selection_returns_per_row_conflict_then_retry_commits",
            "tests/storage/test_authority_p3_operational.py",
            "test_scheduler_uncertainty_deadline_survives_leader_change_and_bounds_resolution",
        ),
        "Cross-command rollback and scheduler uncertainty are retained at the v4 authority boundary.",
    ),
    "tests/test_plan03_p0_performance.py": Disposition(
        MIGRATE,
        _refs(
            "tests/test_plan03_support.py",
            "test_paired_performance_keeps_signed_deltas_and_fixed_bootstrap",
            "test_paired_performance_rejects_nonpositive_or_nonfinite_durations",
        ),
        "The classic executable is tag-only; the reusable paired-performance method remains current.",
    ),
    "tests/test_plan03_p0_red.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p2_dynamic.py",
            "test_revoke_before_selection_leaves_current_quorum_progressing",
            "test_revoke_after_selection_returns_per_row_conflict_then_retry_commits",
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_persistent_fair_selector_meets_frozen_1000_round_gate",
            "tests/storage/test_visibility_v4.py",
            "test_transient_recovery_never_drops_and_deadline_enters_manual_review",
        ),
        "All accepted P0 RED findings have direct current v4 regression tests.",
    ),
    "tests/test_resume.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_run_initializer_p3.py",
            "test_retry_and_completed_replay_bind_the_entire_resolved_config_identity",
            "tests/storage/test_contributor_progress.py",
            "test_contributor_progress_advances_only_contiguous_receipt_chain",
        ),
        "In-place classic resume is unsupported; v4 retries/restarts use immutable init plus authority cursor state.",
    ),
    "tests/test_retention.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_publication_v4.py",
            "test_takeover_reconciles_predecessor_intent_and_orphan_grace_is_lease_safe",
            "tests/storage/test_authority_p3_operational.py",
            "test_active_leader_compacts_audit_batches_before_exact_source_gc",
            "tests/test_clean_run.py",
            "test_clean_run_applies_artifact_policy_and_authority_live_references",
        ),
        "Reference-driven GC, crash recovery, audit ordering, and cleanup fail-closed behavior are v4 tests.",
    ),
    "tests/test_shared_runtime_primitives.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_visibility_v4.py",
            "test_not_found_requires_three_stable_observations_and_grace",
            "tests/test_adoption_strategy.py",
            "test_rebase_context_returns_actual_latest_from_retry_helper",
        ),
        "The useful proposal visibility/adoption primitives moved to typed v4 services and modeling strategy tests.",
    ),
    "tests/test_source_identity.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_run_initializer_p3.py",
            "test_actor_attestation_requires_explicit_runtime_resource_evidence",
            "test_completed_descriptor_rejects_identity_mode_mismatch",
        ),
        "Source identity is frozen in the v4 descriptor and actor attestations.",
    ),
    "tests/test_sqlite_store.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_schema_v4.py",
            "test_fresh_v4_schema_initializes_reopens_and_is_integral",
            "tests/storage/test_leader_authority_commands.py",
            "test_typed_receipt_proposal_selection_and_v1_commit_flow",
            "tests/storage/test_authority_p2_dynamic.py",
            "test_selection_classifies_stale_rows_without_partial_abort",
        ),
        "Shared SQLite atomicity, eligibility, merge rollback, and terminalization are v4 authority tests.",
    ),
    "tests/test_staleness_evidence.py": Disposition(
        MIGRATE,
        _refs(
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_merge_weights_reject_nonfinite_or_negative_weight_parameters",
            "test_merge_weights_use_stable_fsum_and_reject_duplicate_ids",
        ),
        "Staleness weighting is unified; fragment-specific evidence columns are archived only.",
    ),
    "tests/test_syncer_runtime.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_publication_v4.py",
            "test_prepared_intent_precedes_io_and_commit_verifies_exact_theta_pair",
            "tests/runtime/test_p4_mandatory_runtime.py",
            "test_production_v4_entrypoint_closure_has_no_classic_authority_or_shared_csv",
        ),
        "Full sync/publish and device behavior is exercised by the mandatory v4 runtime; fragment runtime is gone.",
    ),
    "tests/test_syncer_selection.py": Disposition(
        MIGRATE,
        _refs(
            "tests/protocol/test_p3_accounting_selection_cursor.py",
            "test_persistent_fair_selector_meets_frozen_1000_round_gate",
            "test_full_quorum_keeps_stable_set_and_reduction_order",
            "tests/storage/test_visibility_v4.py",
            "test_not_found_requires_three_stable_observations_and_grace",
        ),
        "Selection, grace/visibility, quorum, and drain behavior are authoritative v4 state.",
    ),
    "tests/test_terminal_predecessor_capture.py": Disposition(
        DELETE,
        (),
        "This was a classic partial-terminal-merge research writer; read-only evaluation of existing captures remains.",
    ),
    "tests/test_terminal_state_machine.py": Disposition(
        MIGRATE,
        _refs(
            "tests/storage/test_authority_p3_operational.py",
            "test_terminal_close_snapshot_cannot_be_rewritten_by_a_second_command",
            "test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens",
        ),
        "Classic discovery loops were replaced by the fenced v4 terminal state machine.",
    ),
}


TEST_OVERRIDES: dict[str, Disposition] = {
    "tests/test_fragment_scheduler.py::test_round_robin_fragment_schedule": Disposition(
        DELETE, (), "Round-robin fragment production scheduling is unsupported."
    ),
    "tests/test_learner_completion.py::test_fragment_stop_checks_global_stop_before_local_horizon": Disposition(
        DELETE, (), "The fragment learner stop branch is unsupported."
    ),
    "tests/test_shared_runtime_primitives.py::test_fragment_adoption_helper_preserves_all_four_call_contexts": Disposition(
        DELETE, (), "The fragment learner adoption call surface is unsupported."
    ),
    "tests/test_syncer_runtime.py::test_fragment_syncer_computes_and_publishes_bfloat16_on_cpu": Disposition(
        DELETE, (), "The fragment syncer writer is unsupported."
    ),
    "tests/test_syncer_selection.py::test_fragment_terminal_drain_allows_partial_quorum_and_rejects_future": Disposition(
        DELETE, (), "Fragment-specific drain selection is unsupported."
    ),
    "tests/test_terminal_state_machine.py::test_fragment_closed_empty_runs_only_terminal_discovery": Disposition(
        DELETE, (), "Fragment-specific terminal discovery is unsupported."
    ),
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _test_functions(source: str, filename: str) -> list[tuple[str, ast.AST]]:
    tree = ast.parse(source, filename=filename)
    tests: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            tests.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and child.name.startswith("test_"):
                    tests.append((f"{node.name}::{child.name}", child))
    return tests


def _literal_case_count(node: ast.AST) -> int:
    multiplier = 1
    decorators = getattr(node, "decorator_list", ())
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        function = ast.unparse(decorator.func)
        if not function.endswith(".parametrize") or len(decorator.args) < 2:
            continue
        cases = decorator.args[1]
        try:
            value = ast.literal_eval(cases)
        except (ValueError, TypeError):
            if (
                isinstance(cases, ast.Call)
                and isinstance(cases.func, ast.Name)
                and cases.func.id == "range"
            ):
                values = [ast.literal_eval(argument) for argument in cases.args]
                value = range(*values)
            else:
                continue
        multiplier *= len(value)
    return multiplier


def _current_test_index(root: Path) -> tuple[set[str], int, int]:
    refs: set[str] = set()
    functions = 0
    static_cases = 0
    for path in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        for name, node in _test_functions(path.read_text(encoding="utf-8"), relative):
            refs.add(f"{relative}::{name}")
            functions += 1
            static_cases += _literal_case_count(node)
    return refs, functions, static_cases


def build(root: Path, *, current_collected: int | None) -> dict[str, Any]:
    deleted_paths = _git(
        root,
        "diff",
        "--diff-filter=D",
        "--name-only",
        BASE_COMMIT,
        "--",
        "tests",
    ).splitlines()
    deleted_paths = [
        path for path in deleted_paths if path.endswith(".py") and "/support/" not in path
    ]
    if set(deleted_paths) != set(FILE_DISPOSITIONS):
        missing = sorted(set(deleted_paths) - set(FILE_DISPOSITIONS))
        extra = sorted(set(FILE_DISPOSITIONS) - set(deleted_paths))
        raise RuntimeError(f"test disposition file drift: missing={missing}, extra={extra}")

    current_refs, current_functions, current_static_cases = _current_test_index(root)
    rows: list[dict[str, Any]] = []
    deleted_static_cases = 0
    for path in sorted(deleted_paths):
        source = _git(root, "show", f"{BASE_COMMIT}:{path}")
        tests = _test_functions(source, path)
        if not tests:
            raise RuntimeError(f"deleted test file has no tests: {path}")
        for name, node in tests:
            old_ref = f"{path}::{name}"
            disposition = TEST_OVERRIDES.get(old_ref, FILE_DISPOSITIONS[path])
            for replacement in disposition.replacements:
                if replacement not in current_refs:
                    raise RuntimeError(f"replacement test does not exist: {replacement}")
            static_cases = _literal_case_count(node)
            deleted_static_cases += static_cases
            rows.append(
                {
                    "old_path": path,
                    "old_test": name,
                    "classification": disposition.classification,
                    "replacement_assertions": list(disposition.replacements),
                    "statically_enumerable_cases": static_cases,
                    "reason": disposition.reason,
                }
            )

    counts = {classification: 0 for classification in (MIGRATE, RETAIN, DELETE)}
    for row in rows:
        counts[row["classification"]] += 1
    replacements = sorted(
        {replacement for row in rows for replacement in row["replacement_assertions"]}
    )
    return {
        "artifact_version": 1,
        "phase": "P5-delete-classic-refactor",
        "status": "PASS" if current_collected is not None else "PENDING_COMPUTE_COUNT",
        "source_base": BASE_COMMIT,
        "scope": "every deleted test function between the P5 base and current worktree",
        "classification_vocabulary": [MIGRATE, RETAIN, DELETE],
        "summary": {
            "deleted_test_files": len(deleted_paths),
            "deleted_test_functions": len(rows),
            "classification_counts": counts,
            "unique_current_replacement_assertions": len(replacements),
            "deleted_statically_enumerable_cases": deleted_static_cases,
            "current_test_functions": current_functions,
            "current_statically_enumerable_cases": current_static_cases,
            "p4_baseline_collected": BASELINE_COLLECTED,
            "p5_current_collected": current_collected,
            "collected_case_net_change": (
                None if current_collected is None else current_collected - BASELINE_COLLECTED
            ),
            "count_explanation": (
                "The collection delta includes deletion of classic/fragment and obsolete Plan01/02 "
                "fixtures, migration into denser parameterized v4 authority tests, and new P5 "
                "architecture/legacy/config regressions. Static case counts include only literal "
                "parametrize decorators; the compute pytest collection is authoritative."
            ),
        },
        "current_replacement_assertions": replacements,
        "deleted_tests": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-collected", type=int)
    args = parser.parse_args()
    payload = build(args.root.resolve(), current_collected=args.current_collected)
    output = args.output
    if not output.is_absolute():
        output = args.root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
