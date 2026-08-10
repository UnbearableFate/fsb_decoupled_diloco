#!/usr/bin/env python3
"""Aggregate the registered plan03-1 ladder without self-proof or source drift."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any

from fs_diloco.core.source_identity import SOURCE_SCOPES, capture_source_identity


PLAN_ID = "plan03-1"
MATRIX_FIELDS = (
    "requirement_id",
    "phase",
    "plan_source",
    "current_design_requirement",
    "verification",
    "required_evidence",
    "status",
)
REQUIREMENT_IDS = {
    "SURFACE-01",
    "CONFIG-01",
    "SCHEMA-01",
    "CLEAN-01",
    "ARCH-01",
    "UNIT-01",
    "HARNESS-01",
    "FUNC-4L1S-01",
    "FAULT-4L1S-01",
    "FORMAL-8L1S-01",
    "DOCS-01",
    "FINAL-01",
}
GATE_CONTRACTS = {
    "U1-final-one-node-validation": {
        "producer": "scripts/miyabi/run_validation_suite.py",
        "requirements": ["UNIT-01", "HARNESS-01"],
        "kind": "validation",
        "artifact_gate": "U1-one-node-validation",
        "nodes": 1,
        "topology": {
            "queue": "interact-g",
            "nodes": 1,
            "select": "1:ncpus=8:mem=16gb",
            "walltime": "01:00:00",
        },
        "workload": {
            "commands": [
                "ruff-format",
                "ruff-lint",
                "focused-pytest",
                "full-pytest",
            ],
            "pytest_policy": ("tests > 0 and failures = errors = skipped = 0 for each suite"),
        },
    },
    "F1-final-functional-normal": {
        "producer": "scripts/miyabi/check_full_protocol_run.py",
        "requirements": ["FUNC-4L1S-01"],
        "kind": "runtime",
        "artifact_gate": "F1-final-functional-normal",
        "nodes": 5,
        "fault_scenario": "none",
        "contributors": 4,
        "inner_steps": 20,
        "global_steps": 4,
        "direct_tokens": 5120,
        "topology": {
            "queue": "debug-g",
            "nodes": 5,
            "select": "5:ncpus=8:mpiprocs=1:mem=16gb",
            "walltime": "00:10:00",
        },
        "config": "configs/full_protocol_functional.yaml",
    },
    "F1-final-learner-replacement": {
        "producer": "scripts/miyabi/check_full_protocol_run.py",
        "requirements": ["FAULT-4L1S-01"],
        "kind": "runtime",
        "artifact_gate": "F1-final-learner-replacement",
        "nodes": 5,
        "fault_scenario": "learner_replacement",
        "contributors": 4,
        "inner_steps": 20,
        "global_steps": 4,
        "direct_tokens": 5120,
        "topology": {
            "queue": "debug-g",
            "nodes": 5,
            "select": "5:ncpus=8:mpiprocs=1:mem=16gb",
            "walltime": "00:10:00",
        },
        "config": "configs/full_protocol_functional.yaml",
    },
    "F1-final-syncer-takeover": {
        "producer": "scripts/miyabi/check_full_protocol_run.py",
        "requirements": ["FAULT-4L1S-01"],
        "kind": "runtime",
        "artifact_gate": "F1-final-syncer-takeover",
        "nodes": 5,
        "fault_scenario": "syncer_takeover",
        "contributors": 4,
        "inner_steps": 20,
        "global_steps": 4,
        "direct_tokens": 5120,
        "topology": {
            "queue": "debug-g",
            "nodes": 5,
            "select": "5:ncpus=8:mpiprocs=1:mem=16gb",
            "walltime": "00:10:00",
        },
        "config": "configs/full_protocol_functional.yaml",
    },
    "G1-final-formal-8l1s": {
        "producer": "scripts/miyabi/check_full_protocol_run.py",
        "requirements": ["FORMAL-8L1S-01"],
        "kind": "runtime",
        "artifact_gate": "G1-final-formal-8l1s",
        "nodes": 9,
        "fault_scenario": "none",
        "contributors": 8,
        "inner_steps": 50,
        "global_steps": 10,
        "direct_tokens": 64000,
        "topology": {
            "queue": "regular-g",
            "nodes": 9,
            "select": "9:ncpus=8:mpiprocs=1:mem=16gb",
            "walltime": "00:10:00",
        },
        "config": "configs/full_protocol_static.yaml",
    },
}


def _manifest_workload(contract: dict[str, Any]) -> dict[str, Any]:
    if contract["kind"] == "validation":
        return contract["workload"]
    return {
        "config": contract["config"],
        "fault_scenario": contract["fault_scenario"],
        "learners": contract["contributors"],
        "local_optimizer_steps_per_cycle": contract["inner_steps"],
        "committed_global_steps": contract["global_steps"],
        "direct_weight_tokens_applied": contract["direct_tokens"],
    }


REVIEW_CONTRACTS = {
    "R2-preformal-current-state": {
        "review_kind": "preformal-current-state",
        "requirements": ["SURFACE-01", "CONFIG-01", "SCHEMA-01", "CLEAN-01", "ARCH-01"],
    },
    "R3-final-evidence": {
        "review_kind": "final-evidence",
        "requirements": ["DOCS-01"],
    },
}


def _is_lower_hex(value: Any, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_lower_hex(value.removeprefix("sha256:"), length=64)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular(path, label=label)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object: {path}")
    return value


def _project_path(project_root: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "\\" in value or not value or ".." in candidate.parts:
        raise RuntimeError(f"{label} is not a canonical repository-relative path: {value}")
    resolved = (project_root / candidate).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes the project root: {value}") from exc
    return resolved


def _require_tracked(project_root: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside the project root: {path}") from exc
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=project_root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} is not tracked by Git: {relative}")


def _validate_registered_manifest(
    manifest: dict[str, Any],
    *,
    project_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if set(manifest) != {
        "manifest_version",
        "plan_id",
        "state",
        "source_scopes",
        "source_identity",
        "gates",
        "reviews",
        "aggregate",
        "retention",
    }:
        raise RuntimeError("formal ladder manifest top-level schema is invalid")
    if manifest["manifest_version"] != 1 or manifest["plan_id"] != PLAN_ID:
        raise RuntimeError("formal ladder manifest identity is invalid")
    if manifest["state"] != "registered":
        raise RuntimeError("formal ladder manifest is not source-bound and registered")
    if manifest["source_scopes"] != list(SOURCE_SCOPES):
        raise RuntimeError("formal ladder source scopes are not canonical")
    source = manifest["source_identity"]
    if (
        not isinstance(source, dict)
        or set(source) != {"commit", "dirty", "fingerprint"}
        or not _is_lower_hex(source["commit"], length=40)
        or source["dirty"] is not False
        or not _is_fingerprint(source["fingerprint"])
    ):
        raise RuntimeError("formal ladder source identity is invalid")
    gates = manifest["gates"]
    if not isinstance(gates, list) or {item.get("id") for item in gates} != set(GATE_CONTRACTS):
        raise RuntimeError("formal ladder gate set is not exact")
    registered_paths: set[str] = set()
    supporting_paths: set[str] = set()
    for gate in gates:
        if set(gate) != {
            "id",
            "producer",
            "requirements",
            "artifact_path",
            "artifact_sha256",
            "supporting_evidence",
            "topology",
            "workload",
            "pass_formula",
            "cleanup",
        }:
            raise RuntimeError("formal ladder gate schema is invalid")
        contract = GATE_CONTRACTS[gate["id"]]
        if (
            gate["producer"] != contract["producer"]
            or gate["requirements"] != contract["requirements"]
            or gate["topology"] != contract["topology"]
            or gate["workload"] != _manifest_workload(contract)
            or not isinstance(gate["pass_formula"], str)
            or not gate["pass_formula"].strip()
            or not isinstance(gate["cleanup"], str)
            or not gate["cleanup"].strip()
            or not isinstance(gate["artifact_path"], str)
            or not _is_lower_hex(gate["artifact_sha256"], length=64)
            or not isinstance(gate["supporting_evidence"], list)
            or not gate["supporting_evidence"]
        ):
            raise RuntimeError(f"formal ladder gate registration is invalid: {gate['id']}")
        if gate["artifact_path"] in registered_paths:
            raise RuntimeError("formal ladder repeats a registered artifact path")
        registered_paths.add(gate["artifact_path"])
        for evidence in gate["supporting_evidence"]:
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"path", "sha256"}
                or not isinstance(evidence["path"], str)
                or not _is_lower_hex(evidence["sha256"], length=64)
                or evidence["path"] in supporting_paths
            ):
                raise RuntimeError(f"formal ladder supporting evidence is invalid: {gate['id']}")
            supporting_paths.add(evidence["path"])
    reviews = manifest["reviews"]
    if not isinstance(reviews, list) or {item.get("id") for item in reviews} != set(
        REVIEW_CONTRACTS
    ):
        raise RuntimeError("formal ladder review set is not exact")
    for review in reviews:
        if set(review) != {
            "id",
            "review_kind",
            "requirements",
            "artifact_path",
            "artifact_sha256",
        }:
            raise RuntimeError("formal ladder review schema is invalid")
        contract = REVIEW_CONTRACTS[review["id"]]
        if (
            review["review_kind"] != contract["review_kind"]
            or review["requirements"] != contract["requirements"]
            or not isinstance(review["artifact_path"], str)
            or not _is_lower_hex(review["artifact_sha256"], length=64)
        ):
            raise RuntimeError(f"formal ladder review registration is invalid: {review['id']}")
        if review["artifact_path"] in registered_paths:
            raise RuntimeError("formal ladder repeats a registered artifact path")
        registered_paths.add(review["artifact_path"])
    if supporting_paths & registered_paths:
        raise RuntimeError("formal ladder supporting evidence is not independent")
    aggregate = manifest["aggregate"]
    if aggregate != {
        "producer": "scripts/miyabi/check_plan_completion.py",
        "matrix_path": "reports/DOING/plan03-1/requirement-matrix.csv",
        "staged_final_requirement": "FINAL-01",
    }:
        raise RuntimeError("formal ladder aggregate registration is invalid")
    retention = manifest["retention"]
    if (
        not isinstance(retention, dict)
        or set(retention) != {"success", "failure", "cleanup_owner"}
        or any(not isinstance(value, str) or not value.strip() for value in retention.values())
    ):
        raise RuntimeError("formal ladder retention policy is invalid")
    return source, gates, reviews


def _validate_source(
    project_root: Path,
    registered_source: dict[str, Any],
) -> dict[str, Any]:
    current = capture_source_identity(project_root)
    if current["git_dirty"]:
        raise RuntimeError("formal source scopes are dirty")
    if current["source_fingerprint"] != registered_source["fingerprint"]:
        raise RuntimeError("current source fingerprint differs from the formal target")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", registered_source["commit"], "HEAD"],
        cwd=project_root,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("formal source commit is not an ancestor of the completion target")
    return {
        "commit": registered_source["commit"],
        "dirty": False,
        "scopes": list(SOURCE_SCOPES),
        "fingerprint": registered_source["fingerprint"],
    }


def _validate_artifact_evidence_paths(
    payload: dict[str, Any],
    *,
    project_root: Path,
    output: Path,
) -> set[Path]:
    evidence = payload.get("evidence_paths")
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("gate artifact has no independent evidence paths")
    resolved_paths: set[Path] = set()
    for value in evidence:
        if not isinstance(value, str):
            raise RuntimeError("gate artifact evidence path is not a string")
        path = Path(value)
        resolved = path.resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise RuntimeError(f"gate artifact evidence escapes the project root: {value}") from exc
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path != resolved
            or resolved == output.resolve()
        ):
            raise RuntimeError(f"gate artifact evidence path is invalid: {value}")
        if resolved in resolved_paths:
            raise RuntimeError(f"gate artifact repeats an evidence path: {value}")
        resolved_paths.add(resolved)
    return resolved_paths


def _validate_validation_gate(payload: dict[str, Any], *, evidence_paths: set[Path]) -> None:
    steps = payload.get("metrics", {}).get("steps")
    expected_kinds = {
        "ruff-format": "command",
        "ruff-lint": "command",
        "focused-pytest": "pytest",
        "full-pytest": "pytest",
    }
    if not isinstance(steps, list) or [step.get("name") for step in steps] != list(expected_kinds):
        raise RuntimeError("one-node validation step set is not exact")
    junit_paths: set[Path] = set()
    for step in steps:
        name = step["name"]
        if (
            step.get("returncode") != 0
            or step.get("result_kind") != expected_kinds[name]
            or not isinstance(step.get("argv"), list)
            or not step["argv"]
        ):
            raise RuntimeError("one-node validation command identity is invalid")
        if expected_kinds[name] == "pytest" and (
            not isinstance(step.get("junit_xml"), str)
            or Path(step["junit_xml"]).resolve() not in evidence_paths
            or not isinstance(step.get("tests"), int)
            or step["tests"] < 1
            or step.get("failures") != 0
            or step.get("errors") != 0
            or step.get("skipped") != 0
        ):
            raise RuntimeError("one-node pytest result is not an exact all-pass suite")
        if expected_kinds[name] == "pytest":
            junit_paths.add(Path(step["junit_xml"]).resolve())
    if len(junit_paths) != 2:
        raise RuntimeError("one-node pytest suites do not have distinct JUnit evidence")
    environment = payload.get("environment")
    if (
        not isinstance(environment, dict)
        or not isinstance(environment.get("pbs_job_id"), str)
        or not environment["pbs_job_id"]
        or not isinstance(environment.get("nodes"), list)
        or len(environment["nodes"]) != 1
        or not isinstance(environment["nodes"][0], str)
        or not environment["nodes"][0].startswith("mg")
    ):
        raise RuntimeError("one-node validation topology is invalid")


def _validate_runtime_gate(payload: dict[str, Any], contract: dict[str, Any]) -> None:
    if payload.get("fault_scenario") != contract["fault_scenario"]:
        raise RuntimeError("runtime gate fault scenario is not exact")
    environment = payload.get("environment")
    workload = payload.get("workload_identity")
    metrics = payload.get("metrics")
    authority = payload.get("authority")
    if not all(isinstance(item, dict) for item in (environment, workload, metrics, authority)):
        raise RuntimeError("runtime gate projections are incomplete")
    nodes = environment.get("nodes")
    if (
        not isinstance(nodes, list)
        or len(nodes) != contract["nodes"]
        or len(set(nodes)) != contract["nodes"]
        or any(not isinstance(node, str) or not node.startswith("mg") for node in nodes)
        or not isinstance(environment.get("pbs_job_id"), str)
        or not environment["pbs_job_id"]
    ):
        raise RuntimeError("runtime gate node count is not exact")
    if (
        workload.get("configured_local_steps") != contract["inner_steps"]
        or workload.get("committed_global_steps") != contract["global_steps"]
        or workload.get("direct_weight_tokens_applied") != contract["direct_tokens"]
        or metrics.get("contributors") != contract["contributors"]
        or metrics.get("expected_direct_tokens") != contract["direct_tokens"]
        or metrics.get("token_balance") != 0
        or authority.get("final_version") != contract["global_steps"]
        or authority.get("integrity") != ["ok"]
    ):
        raise RuntimeError("runtime gate workload or authority result is not exact")


def _validate_gate(
    gate: dict[str, Any],
    *,
    project_root: Path,
    source: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    contract = GATE_CONTRACTS[gate["id"]]
    artifact_path = _project_path(
        project_root,
        gate["artifact_path"],
        label=f"{gate['id']} artifact",
    )
    _require_regular(artifact_path, label=f"{gate['id']} artifact")
    _require_tracked(project_root, artifact_path, label=f"{gate['id']} artifact")
    if _sha256(artifact_path) != gate["artifact_sha256"]:
        raise RuntimeError(f"formal ladder artifact hash differs: {gate['id']}")
    payload = _read_json(artifact_path, label=f"{gate['id']} artifact")
    if (
        payload.get("artifact_version") != 1
        or payload.get("status") != "PASS"
        or payload.get("errors") != []
        or payload.get("gate") != contract["artifact_gate"]
        or not isinstance(payload.get("experiment_id"), str)
        or not payload["experiment_id"]
        or payload.get("requirements_covered") != contract["requirements"]
        or payload.get("source_identity") != source
    ):
        raise RuntimeError(f"formal ladder artifact acceptance identity differs: {gate['id']}")
    evidence_paths = _validate_artifact_evidence_paths(
        payload,
        project_root=project_root,
        output=output,
    )
    supporting_paths: set[Path] = set()
    for evidence in gate["supporting_evidence"]:
        if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
            raise RuntimeError(f"supporting evidence schema is invalid: {gate['id']}")
        path = _project_path(project_root, evidence["path"], label="supporting evidence")
        if path in supporting_paths or path in {artifact_path, output}:
            raise RuntimeError(f"supporting evidence is not independent: {evidence['path']}")
        _require_regular(path, label="supporting evidence")
        _require_tracked(project_root, path, label="supporting evidence")
        if _sha256(path) != evidence["sha256"]:
            raise RuntimeError(f"supporting evidence hash differs: {evidence['path']}")
        supporting_paths.add(path)
    if not supporting_paths <= evidence_paths:
        raise RuntimeError(f"supporting evidence is not named by its gate artifact: {gate['id']}")
    if contract["kind"] == "validation":
        if supporting_paths != evidence_paths:
            raise RuntimeError(
                "one-node validation must hash-bind its raw log and both JUnit files"
            )
        if payload.get("cleanup") != {
            "owner": "validation_suite",
            "eligible": False,
            "targets": [],
        }:
            raise RuntimeError("one-node validation cleanup identity is invalid")
        _validate_validation_gate(payload, evidence_paths=evidence_paths)
    else:
        cleanup = payload.get("cleanup")
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("owner") != "full_protocol_harness"
            or cleanup.get("eligible") is not True
            or not isinstance(cleanup.get("targets"), list)
            or len(cleanup["targets"]) != 1
        ):
            raise RuntimeError("runtime gate cleanup identity is invalid")
        _validate_runtime_gate(payload, contract)
    return {"path": gate["artifact_path"], "sha256": gate["artifact_sha256"]}


def _validate_review(
    review: dict[str, Any],
    *,
    project_root: Path,
    source: dict[str, Any],
) -> dict[str, Any]:
    contract = REVIEW_CONTRACTS[review["id"]]
    artifact_path = _project_path(project_root, review["artifact_path"], label="review artifact")
    _require_regular(artifact_path, label="review artifact")
    _require_tracked(project_root, artifact_path, label="review artifact")
    if _sha256(artifact_path) != review["artifact_sha256"]:
        raise RuntimeError(f"review artifact hash differs: {review['id']}")
    payload = _read_json(artifact_path, label="review artifact")
    if set(payload) != {
        "review_artifact_version",
        "status",
        "plan_id",
        "reviewer",
        "review_execution",
        "external_review_status",
        "review_kind",
        "target_commit",
        "source_fingerprint",
        "source_scopes",
        "verdict",
        "requirements_covered",
        "report_path",
        "report_sha256",
        "reviewed_inputs",
        "findings",
    }:
        raise RuntimeError(f"review artifact schema is invalid: {review['id']}")
    if (
        payload["review_artifact_version"] != 1
        or payload["status"] != "PASS"
        or payload["plan_id"] != PLAN_ID
        or payload["reviewer"] != "Codex"
        or payload["review_execution"] != "internal"
        or payload["external_review_status"] != "skipped-by-user"
        or payload["review_kind"] != contract["review_kind"]
        or payload["target_commit"] != source["commit"]
        or payload["source_fingerprint"] != source["fingerprint"]
        or payload["source_scopes"] != source["scopes"]
        or payload["verdict"] != "APPROVE"
        or payload["requirements_covered"] != contract["requirements"]
        or payload["findings"] != []
        or not isinstance(payload["reviewed_inputs"], list)
        or not payload["reviewed_inputs"]
        or any(
            not isinstance(value, str) or not value.strip() for value in payload["reviewed_inputs"]
        )
        or len(set(payload["reviewed_inputs"])) != len(payload["reviewed_inputs"])
    ):
        raise RuntimeError(f"review artifact acceptance identity differs: {review['id']}")
    report_path = _project_path(project_root, payload["report_path"], label="review report")
    if report_path == artifact_path:
        raise RuntimeError(f"review report is not independent: {review['id']}")
    _require_regular(report_path, label="review report")
    _require_tracked(project_root, report_path, label="review report")
    if _sha256(report_path) != payload["report_sha256"]:
        raise RuntimeError(f"review report hash differs: {review['id']}")
    return {"path": review["artifact_path"], "sha256": review["artifact_sha256"]}


def _validate_matrix(
    matrix_path: Path,
    *,
    project_root: Path,
    mode: str,
    staged_artifact: Path | None,
    required_bindings: dict[str, set[str]],
    manifest_relative: str,
) -> tuple[dict[str, str], str | None]:
    _require_regular(matrix_path, label="requirement matrix")
    _require_tracked(project_root, matrix_path, label="requirement matrix")
    with matrix_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != MATRIX_FIELDS:
            raise RuntimeError("requirement matrix columns are invalid")
        rows = list(reader)
    if {row["requirement_id"] for row in rows} != REQUIREMENT_IDS or len(rows) != len(
        REQUIREMENT_IDS
    ):
        raise RuntimeError("requirement matrix requirement set is not exact")
    statuses = {row["requirement_id"]: row["status"] for row in rows}
    if any(statuses[item] != "complete" for item in REQUIREMENT_IDS - {"FINAL-01"}):
        raise RuntimeError("requirement matrix is incomplete before FINAL-01")
    staged_relative = None
    if mode == "staged":
        if statuses["FINAL-01"] != "pending" or staged_artifact is not None:
            raise RuntimeError("staged completion requires FINAL-01 pending and no staged input")
        required_bindings["FINAL-01"] = {manifest_relative}
    else:
        if statuses["FINAL-01"] != "complete" or staged_artifact is None:
            raise RuntimeError("completed mode requires a complete FINAL-01 and staged input")
        _require_regular(staged_artifact, label="staged completion artifact")
        _require_tracked(project_root, staged_artifact, label="staged completion artifact")
        staged_relative = staged_artifact.relative_to(project_root).as_posix()
        final_evidence = next(
            row["required_evidence"] for row in rows if row["requirement_id"] == "FINAL-01"
        )
        if staged_relative not in final_evidence.split("; "):
            raise RuntimeError("FINAL-01 does not bind the staged completion artifact")
        required_bindings["FINAL-01"] = {staged_relative}
    for row in rows:
        evidence_values = row["required_evidence"].split("; ")
        if (
            not evidence_values
            or any(not value for value in evidence_values)
            or len(set(evidence_values)) != len(evidence_values)
        ):
            raise RuntimeError(f"requirement evidence is empty: {row['requirement_id']}")
        missing = required_bindings[row["requirement_id"]] - set(evidence_values)
        if missing:
            raise RuntimeError(
                f"requirement is not bound to its final evidence: {row['requirement_id']}"
            )
        for value in evidence_values:
            path = _project_path(project_root, value, label="requirement evidence")
            _require_regular(path, label="requirement evidence")
            _require_tracked(project_root, path, label="requirement evidence")
    return statuses, staged_relative


def _matrix_bindings(
    gate_results: dict[str, dict[str, str]],
    review_results: dict[str, dict[str, str]],
) -> dict[str, set[str]]:
    bindings = {requirement: set() for requirement in REQUIREMENT_IDS}
    for gate_id, result in gate_results.items():
        for requirement in GATE_CONTRACTS[gate_id]["requirements"]:
            bindings[requirement].add(result["path"])
    for review_id, result in review_results.items():
        for requirement in REVIEW_CONTRACTS[review_id]["requirements"]:
            bindings[requirement].add(result["path"])
    return bindings


def _publish_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"completion artifact is create-only: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def check_completion(
    *,
    project_root: Path,
    manifest_path: Path,
    matrix_path: Path,
    mode: str,
    output: Path,
    staged_artifact: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    manifest_path = manifest_path.resolve()
    matrix_path = matrix_path.resolve()
    output = output.resolve()
    if output in {manifest_path, matrix_path} or (
        staged_artifact is not None and output == staged_artifact.resolve()
    ):
        raise RuntimeError("completion output cannot be one of its own inputs")
    manifest = _read_json(manifest_path, label="formal ladder manifest")
    _require_tracked(project_root, manifest_path, label="formal ladder manifest")
    registered_source, gates, reviews = _validate_registered_manifest(
        manifest,
        project_root=project_root,
    )
    source = _validate_source(project_root, registered_source)
    gate_results = {
        gate["id"]: _validate_gate(
            gate,
            project_root=project_root,
            source=source,
            output=output,
        )
        for gate in gates
    }
    review_results = {
        review["id"]: _validate_review(review, project_root=project_root, source=source)
        for review in reviews
    }
    manifest_relative = manifest_path.relative_to(project_root).as_posix()
    matrix_relative = matrix_path.relative_to(project_root).as_posix()
    resolved_staged = None if staged_artifact is None else staged_artifact.resolve()
    statuses, staged_relative = _validate_matrix(
        matrix_path,
        project_root=project_root,
        mode=mode,
        staged_artifact=resolved_staged,
        required_bindings=_matrix_bindings(gate_results, review_results),
        manifest_relative=manifest_relative,
    )
    if resolved_staged is not None:
        staged_payload = _read_json(resolved_staged, label="staged completion artifact")
        if (
            staged_payload.get("artifact_version") != 1
            or staged_payload.get("status") != "PASS"
            or staged_payload.get("gate") != "P4-plan-completion-staged"
            or staged_payload.get("requirements_covered") != ["FINAL-01"]
            or staged_payload.get("source_identity") != source
            or staged_payload.get("manifest")
            != {"path": manifest_relative, "sha256": _sha256(manifest_path)}
            or staged_payload.get("matrix", {}).get("path") != matrix_relative
            or staged_payload.get("matrix", {}).get("statuses")
            != {
                requirement: "pending" if requirement == "FINAL-01" else "complete"
                for requirement in REQUIREMENT_IDS
            }
            or staged_payload.get("gate_artifacts") != gate_results
            or staged_payload.get("review_artifacts") != review_results
            or staged_payload.get("staged_artifact") is not None
            or staged_payload.get("errors") != []
            or staged_payload.get("cleanup")
            != {"owner": "plan_completion", "eligible": False, "targets": []}
        ):
            raise RuntimeError("staged completion artifact identity is invalid")
    evidence_paths = [
        str(manifest_path),
        str(matrix_path),
        *(
            str(_project_path(project_root, item["path"], label="gate artifact"))
            for item in gate_results.values()
        ),
        *(
            str(_project_path(project_root, item["path"], label="review artifact"))
            for item in review_results.values()
        ),
    ]
    if resolved_staged is not None:
        evidence_paths.append(str(resolved_staged))
    artifact = {
        "artifact_version": 1,
        "status": "PASS",
        "gate": f"P4-plan-completion-{mode}",
        "experiment_id": f"plan03-1-{mode}-completion",
        "requirements_covered": ["FINAL-01"],
        "source_identity": source,
        "manifest": {
            "path": manifest_relative,
            "sha256": _sha256(manifest_path),
        },
        "matrix": {
            "path": matrix_relative,
            "sha256": _sha256(matrix_path),
            "statuses": statuses,
        },
        "gate_artifacts": gate_results,
        "review_artifacts": review_results,
        "staged_artifact": staged_relative,
        "errors": [],
        "evidence_paths": evidence_paths,
        "cleanup": {"owner": "plan_completion", "eligible": False, "targets": []},
    }
    _publish_new(output, artifact)
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--mode", choices=("staged", "completed"), required=True)
    parser.add_argument("--staged-artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.mode == "completed") != (args.staged_artifact is not None):
        parser.error("completed mode requires --staged-artifact; staged mode forbids it")
    artifact = check_completion(
        project_root=args.project_root,
        manifest_path=args.manifest,
        matrix_path=args.matrix,
        mode=args.mode,
        output=args.output,
        staged_artifact=args.staged_artifact,
    )
    print(artifact["status"])


if __name__ == "__main__":
    main()
