#!/usr/bin/env python3
"""Run the P6 crash-boundary mapping ten times and retain structured coverage."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
REPETITIONS = 10
BOUNDARIES: dict[str, dict[str, Any]] = {
    "tensor_temp_fsync_create": {
        "nodeid": "tests/storage/test_publication_v4.py::test_immutable_publication_crash_boundaries_replay_exactly",
        "lifecycles": ["v0", "N>0"],
    },
    "proposal_metadata_temp_fsync_create": {
        "nodeid": "tests/storage/test_publication_v4.py::test_immutable_publication_crash_boundaries_replay_exactly",
        "lifecycles": ["N>0"],
    },
    "cycle_receipt": {
        "nodeid": "tests/storage/test_contributor_progress.py::test_contributor_progress_advances_only_contiguous_receipt_chain",
        "lifecycles": ["N>0"],
    },
    "progress_pointer": {
        "nodeid": "tests/runtime/test_p4_mandatory_runtime.py::test_indexed_runtime_data_resumes_without_replaying_prefix",
        "lifecycles": ["N>0"],
    },
    "proposal_pointer": {
        "nodeid": "tests/storage/test_visibility_v4.py::test_visibility_requires_receipt_and_pointer_sequence_collision_fails_closed",
        "lifecycles": ["N>0"],
    },
    "static_binding_control": {
        "nodeid": "tests/runtime/test_p4_mandatory_runtime.py::test_admission_response_replay_is_byte_idempotent",
        "lifecycles": ["N>0"],
    },
    "selection_batch": {
        "nodeid": "tests/storage/test_authority_p3_operational.py::test_abandoned_selection_does_not_consume_persistent_service_credit",
        "lifecycles": ["N>0"],
    },
    "publication_intent": {
        "nodeid": "tests/storage/test_publication_v4.py::test_prepared_intent_precedes_io_and_commit_verifies_exact_theta_pair",
        "lifecycles": ["v0", "N>0"],
    },
    "weight": {
        "nodeid": "tests/storage/test_publication_v4.py::test_publication_crash_prefix_is_reconciled_idempotently",
        "lifecycles": ["v0", "N>0"],
    },
    "outer": {
        "nodeid": "tests/storage/test_publication_v4.py::test_publication_crash_prefix_is_reconciled_idempotently",
        "lifecycles": ["v0", "N>0"],
    },
    "precommit_fence": {
        "nodeid": "tests/storage/test_authority_p2_dynamic.py::test_revoke_after_selection_returns_per_row_conflict_then_retry_commits",
        "lifecycles": ["N>0"],
    },
    "version_insert": {
        "nodeid": "tests/storage/test_leader_authority_commands.py::test_commit_fault_boundaries_roll_back_and_exact_retry",
        "lifecycles": ["v0", "N>0"],
    },
    "proposal_transition": {
        "nodeid": "tests/storage/test_leader_authority_commands.py::test_commit_fault_boundaries_roll_back_and_exact_retry",
        "lifecycles": ["N>0"],
    },
    "db_commit": {
        "nodeid": "tests/storage/test_leader_authority_commands.py::test_commit_fault_boundaries_roll_back_and_exact_retry",
        "lifecycles": ["v0", "N>0"],
    },
    "canonical_control": {
        "nodeid": "tests/runtime/test_p4_mandatory_runtime.py::test_receipt_ack_is_current_epoch_fenced_and_byte_idempotent",
        "lifecycles": ["N>0"],
    },
    "fixed_cache": {
        "nodeid": "tests/runtime/test_p4_mandatory_runtime.py::test_epoch_control_ignores_polluted_fixed_cache_and_repairs_it",
        "lifecycles": ["v0", "N>0"],
    },
    "archive_batch": {
        "nodeid": "tests/storage/test_authority_p3_operational.py::test_immutable_audit_batch_precedes_exact_history_prune_and_preserves_rollup",
        "lifecycles": ["N>0"],
    },
    "archive_manifest": {
        "nodeid": "tests/storage/test_audit_archive_p3.py::test_partition_is_published_before_hashed_manifest_and_validates",
        "lifecycles": ["N>0"],
    },
}


def _source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load source helper: {helper}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.capture(project_root)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(project_root: Path, python: Path, log_path: Path) -> dict[str, Any]:
    source = _source(project_root)
    nodeids = sorted({str(item["nodeid"]) for item in BOUNDARIES.values()})
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        for repetition in range(REPETITIONS):
            started = time.monotonic()
            command = [str(python), "-m", "pytest", "-q", *nodeids]
            completed = subprocess.run(
                command,
                cwd=project_root,
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log.flush()
            result = {
                "repetition": repetition,
                "returncode": completed.returncode,
                "elapsed_seconds": time.monotonic() - started,
            }
            results.append(result)
            if completed.returncode != 0:
                errors.append(f"repetition {repetition} returned {completed.returncode}")
                break
    coverage = {
        name: {
            **contract,
            "required_repetitions": REPETITIONS,
            "completed_repetitions": sum(row["returncode"] == 0 for row in results),
            "status": (
                "PASS"
                if len(results) == REPETITIONS and all(row["returncode"] == 0 for row in results)
                else "BLOCKED"
            ),
        }
        for name, contract in BOUNDARIES.items()
    }
    if source["git_dirty"]:
        errors.append("formal crash matrix source is dirty")
    if len(coverage) != 18 or any(item["status"] != "PASS" for item in coverage.values()):
        errors.append("crash boundary coverage is incomplete")
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase": "P6-acceptance-final-review",
        "experiment_id": "p6-g4-publication-crash-matrix",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source["git_commit"],
        "source_identity": {
            "git_commit": source["git_commit"],
            "git_dirty": source["git_dirty"],
            "source_fingerprint": source["source_fingerprint"],
        },
        "requirements_covered": ["P6-ACCEPTANCE"],
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "required_boundaries": 18,
        "required_repetitions_per_boundary": REPETITIONS,
        "coverage": coverage,
        "runs": results,
        "log_path": str(log_path),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = run(
            args.project_root.resolve(),
            args.python.absolute(),
            args.log.resolve(),
        )
    except Exception as exc:
        payload = {
            "artifact_version": 1,
            "plan_id": PLAN_ID,
            "phase": "P6-acceptance-final-review",
            "experiment_id": "p6-g4-publication-crash-matrix",
            "status": "BLOCKED",
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    _atomic_json(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
