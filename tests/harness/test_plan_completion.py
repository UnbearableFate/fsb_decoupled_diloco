from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/miyabi/check_plan_completion.py"
MANIFEST = ROOT / "reports/DOING/plan03-1/formal-ladder-manifest.json"


def _module():
    specification = importlib.util.spec_from_file_location("check_plan_completion", CHECKER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source() -> dict[str, object]:
    return {
        "commit": "1" * 40,
        "dirty": False,
        "scopes": [
            "fs_diloco",
            "configs",
            "scripts/miyabi",
            "tests",
            "pyproject.toml",
            "README.md",
            "docs",
        ],
        "fingerprint": "sha256:" + "2" * 64,
    }


def _registered_manifest(project_root: Path) -> dict[str, object]:
    manifest = copy.deepcopy(json.loads(MANIFEST.read_text(encoding="utf-8")))
    source = _source()
    manifest["state"] = "registered"
    manifest["source_identity"] = {
        "commit": source["commit"],
        "dirty": False,
        "fingerprint": source["fingerprint"],
    }
    for gate in manifest["gates"]:
        supporting = project_root / f"evidence/{gate['id']}.log"
        supporting.parent.mkdir(parents=True, exist_ok=True)
        supporting.write_text(f"independent evidence for {gate['id']}\n", encoding="utf-8")
        gate["supporting_evidence"] = [
            {
                "path": supporting.relative_to(project_root).as_posix(),
                "sha256": _sha256(supporting),
            }
        ]
    return manifest


def _gate_payload(contract: dict[str, object], source: dict[str, object], raw: Path):
    common = {
        "artifact_version": 1,
        "status": "PASS",
        "gate": contract["artifact_gate"],
        "experiment_id": "fixture",
        "requirements_covered": contract["requirements"],
        "source_identity": source,
        "config_schema_identity": {"version": 1},
        "protocol_schema_identity": {"version": 1, "mode": "static"},
        "environment": {"nodes": [f"mg{index:04d}" for index in range(contract["nodes"])]},
        "workload_identity": None,
        "metrics": {},
        "errors": [],
        "evidence_paths": [str(raw.resolve())],
        "cleanup": {
            "owner": "full_protocol_harness",
            "eligible": True,
            "targets": [str((raw.parent / "run").resolve())],
        },
    }
    if contract["kind"] == "validation":
        focused_junit = raw.with_name("focused.junit.xml")
        full_junit = raw.with_name("full.junit.xml")
        focused_junit.write_text("<testsuites/>\n", encoding="utf-8")
        full_junit.write_text("<testsuites/>\n", encoding="utf-8")
        common["evidence_paths"] = [
            str(raw.resolve()),
            str(focused_junit.resolve()),
            str(full_junit.resolve()),
        ]
        common["gate"] = "U1-one-node-validation"
        common["config_schema_identity"] = None
        common["protocol_schema_identity"] = None
        common["cleanup"] = {
            "owner": "validation_suite",
            "eligible": False,
            "targets": [],
        }
        common["metrics"] = {
            "steps": [
                {
                    "name": "ruff-format",
                    "result_kind": "command",
                    "returncode": 0,
                },
                {"name": "ruff-lint", "result_kind": "command", "returncode": 0},
                {
                    "name": "focused-pytest",
                    "result_kind": "pytest",
                    "returncode": 0,
                    "junit_xml": str(focused_junit.resolve()),
                    "tests": 2,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                {
                    "name": "full-pytest",
                    "result_kind": "pytest",
                    "returncode": 0,
                    "junit_xml": str(full_junit.resolve()),
                    "tests": 4,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                },
            ]
        }
    else:
        common["fault_scenario"] = contract["fault_scenario"]
        common["workload_identity"] = {
            "configured_local_steps": contract["inner_steps"],
            "committed_global_steps": contract["global_steps"],
            "direct_weight_tokens_applied": contract["direct_tokens"],
        }
        common["metrics"] = {
            "contributors": contract["contributors"],
            "expected_direct_tokens": contract["direct_tokens"],
            "token_balance": 0,
        }
        common["authority"] = {
            "final_version": contract["global_steps"],
            "integrity": ["ok"],
        }
    return common


def _write_matrix(
    module,
    path: Path,
    bindings: dict[str, set[str]],
    *,
    final_status: str,
    final_evidence: str | None = None,
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.MATRIX_FIELDS)
        writer.writeheader()
        for requirement in sorted(module.REQUIREMENT_IDS):
            evidence = sorted(bindings[requirement])
            if requirement == "FINAL-01" and final_evidence is not None:
                evidence = [final_evidence]
            writer.writerow(
                {
                    "requirement_id": requirement,
                    "phase": "fixture",
                    "plan_source": "fixture",
                    "current_design_requirement": "fixture",
                    "verification": "fixture",
                    "required_evidence": "; ".join(evidence),
                    "status": final_status if requirement == "FINAL-01" else "complete",
                }
            )


def _materialize_registered_inputs(module, tmp_path: Path):
    source = _source()
    manifest = _registered_manifest(tmp_path)
    raw = tmp_path / "evidence/raw.log"
    raw.write_text("raw gate evidence\n", encoding="utf-8")
    for gate in manifest["gates"]:
        contract = module.GATE_CONTRACTS[gate["id"]]
        artifact_path = tmp_path / f"artifacts/{gate['id']}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(_gate_payload(contract, source, raw)) + "\n",
            encoding="utf-8",
        )
        gate["artifact_path"] = artifact_path.relative_to(tmp_path).as_posix()
        gate["artifact_sha256"] = _sha256(artifact_path)
    for review in manifest["reviews"]:
        contract = module.REVIEW_CONTRACTS[review["id"]]
        report_path = tmp_path / f"reviews/{review['id']}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("Codex internal review\n\nAPPROVE\n", encoding="utf-8")
        artifact_path = tmp_path / f"reviews/{review['id']}.json"
        artifact = {
            "review_artifact_version": 1,
            "status": "PASS",
            "plan_id": "plan03-1",
            "reviewer": "Codex",
            "review_execution": "internal",
            "external_review_status": "skipped-by-user",
            "review_kind": contract["review_kind"],
            "target_commit": source["commit"],
            "source_fingerprint": source["fingerprint"],
            "source_scopes": source["scopes"],
            "verdict": "APPROVE",
            "requirements_covered": contract["requirements"],
            "report_path": report_path.relative_to(tmp_path).as_posix(),
            "report_sha256": _sha256(report_path),
            "reviewed_inputs": ["fixture"],
            "findings": [],
        }
        artifact_path.write_text(json.dumps(artifact) + "\n", encoding="utf-8")
        review["artifact_path"] = artifact_path.relative_to(tmp_path).as_posix()
        review["artifact_sha256"] = _sha256(artifact_path)
    manifest_path = tmp_path / "formal-ladder-manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    gate_results = {
        gate["id"]: {"path": gate["artifact_path"], "sha256": gate["artifact_sha256"]}
        for gate in manifest["gates"]
    }
    review_results = {
        review["id"]: {
            "path": review["artifact_path"],
            "sha256": review["artifact_sha256"],
        }
        for review in manifest["reviews"]
    }
    bindings = module._matrix_bindings(gate_results, review_results)
    bindings["FINAL-01"] = {manifest_path.relative_to(tmp_path).as_posix()}
    matrix_path = tmp_path / "requirement-matrix.csv"
    _write_matrix(
        module,
        matrix_path,
        bindings,
        final_status="pending",
    )
    return source, manifest, manifest_path, matrix_path


def test_candidate_manifest_registers_exact_final_ladder() -> None:
    module = _module()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["state"] == "candidate"
    assert manifest["source_identity"] is None
    assert {gate["id"] for gate in manifest["gates"]} == set(module.GATE_CONTRACTS)
    assert all(gate["artifact_path"] is None for gate in manifest["gates"])
    for gate in manifest["gates"]:
        contract = module.GATE_CONTRACTS[gate["id"]]
        assert gate["topology"] == contract["topology"]
        assert gate["workload"] == module._manifest_workload(contract)
    formal = next(gate for gate in manifest["gates"] if gate["id"] == "G1-final-formal-8l1s")
    assert formal["topology"]["nodes"] == 9
    assert formal["topology"]["walltime"] == "00:10:00"
    assert formal["workload"]["local_optimizer_steps_per_cycle"] == 50
    assert formal["workload"]["committed_global_steps"] == 10
    assert formal["workload"]["direct_weight_tokens_applied"] == 64000


def test_registered_manifest_rejects_extra_or_missing_gate(tmp_path: Path) -> None:
    module = _module()
    manifest = _registered_manifest(tmp_path)
    manifest["gates"].pop()

    with pytest.raises(RuntimeError, match="gate set is not exact"):
        module._validate_registered_manifest(manifest, project_root=tmp_path)


def test_completion_aggregate_accepts_registered_staged_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    source, _manifest, manifest_path, matrix_path = _materialize_registered_inputs(module, tmp_path)
    monkeypatch.setattr(module, "_require_tracked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_validate_source", lambda *_args, **_kwargs: source)
    output = tmp_path / "staged.json"

    artifact = module.check_completion(
        project_root=tmp_path,
        manifest_path=manifest_path,
        matrix_path=matrix_path,
        mode="staged",
        output=output,
    )

    assert artifact == json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "PASS"
    assert artifact["gate"] == "P4-plan-completion-staged"
    assert set(artifact["gate_artifacts"]) == set(module.GATE_CONTRACTS)
    assert set(artifact["review_artifacts"]) == set(module.REVIEW_CONTRACTS)


def test_completion_aggregate_rejects_wrong_source_and_self_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    source, manifest, manifest_path, matrix_path = _materialize_registered_inputs(module, tmp_path)
    monkeypatch.setattr(module, "_require_tracked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_validate_source", lambda *_args, **_kwargs: source)

    with pytest.raises(RuntimeError, match="cannot be one of its own inputs"):
        module.check_completion(
            project_root=tmp_path,
            manifest_path=manifest_path,
            matrix_path=matrix_path,
            mode="staged",
            output=manifest_path,
        )

    gate = manifest["gates"][0]
    gate_path = tmp_path / gate["artifact_path"]
    payload = json.loads(gate_path.read_text(encoding="utf-8"))
    payload["source_identity"]["fingerprint"] = "sha256:" + "9" * 64
    gate_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    gate["artifact_sha256"] = _sha256(gate_path)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="acceptance identity differs"):
        module.check_completion(
            project_root=tmp_path,
            manifest_path=manifest_path,
            matrix_path=matrix_path,
            mode="staged",
            output=tmp_path / "staged.json",
        )


def test_completion_rejects_unbound_matrix_and_non_internal_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    source, manifest, manifest_path, matrix_path = _materialize_registered_inputs(module, tmp_path)
    monkeypatch.setattr(module, "_require_tracked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_validate_source", lambda *_args, **_kwargs: source)
    original_matrix = matrix_path.read_text(encoding="utf-8")
    with matrix_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["requirement_id"] == "FAULT-4L1S-01":
            row["required_evidence"] = manifest["gates"][0]["supporting_evidence"][0]["path"]
    with matrix_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.MATRIX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(RuntimeError, match="not bound to its final evidence"):
        module.check_completion(
            project_root=tmp_path,
            manifest_path=manifest_path,
            matrix_path=matrix_path,
            mode="staged",
            output=tmp_path / "unbound.json",
        )

    matrix_path.write_text(original_matrix, encoding="utf-8")
    review = manifest["reviews"][0]
    review_path = tmp_path / review["artifact_path"]
    review_payload = json.loads(review_path.read_text(encoding="utf-8"))
    review_payload["review_execution"] = "external"
    review_path.write_text(json.dumps(review_payload) + "\n", encoding="utf-8")
    review["artifact_sha256"] = _sha256(review_path)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="review artifact acceptance identity differs"):
        module.check_completion(
            project_root=tmp_path,
            manifest_path=manifest_path,
            matrix_path=matrix_path,
            mode="staged",
            output=tmp_path / "external.json",
        )


def test_completed_mode_requires_tracked_staged_artifact_in_final_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_require_tracked", lambda *_args, **_kwargs: None)
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence\n", encoding="utf-8")
    staged = tmp_path / "staged.json"
    staged.write_text("{}\n", encoding="utf-8")
    matrix = tmp_path / "matrix.csv"
    bindings = {requirement: {"evidence.txt"} for requirement in module.REQUIREMENT_IDS}
    bindings["FINAL-01"] = {"staged.json"}
    _write_matrix(
        module,
        matrix,
        bindings,
        final_status="complete",
        final_evidence="staged.json",
    )

    statuses, staged_relative = module._validate_matrix(
        matrix,
        project_root=tmp_path,
        mode="completed",
        staged_artifact=staged,
        required_bindings=bindings,
        manifest_relative="manifest.json",
    )

    assert set(statuses) == module.REQUIREMENT_IDS
    assert staged_relative == "staged.json"


def test_tracked_evidence_check_rejects_untracked_file(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "untracked.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not tracked"):
        module._require_tracked(tmp_path, path, label="fixture")
