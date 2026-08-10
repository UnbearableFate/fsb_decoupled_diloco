from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/miyabi/check_full_protocol_run.py"


def _checker_module():
    specification = importlib.util.spec_from_file_location("check_full_protocol_run", CHECKER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_checker_parser_freezes_topology_workload_and_fault_oracles(tmp_path: Path) -> None:
    module = _checker_module()
    args = module.build_parser().parse_args(
        [
            "--gate",
            "F1-functional",
            "--experiment-id",
            "functional-normal",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "run"),
            "--log-root",
            str(tmp_path / "logs"),
            "--expected-global-steps",
            "4",
            "--expected-inner-steps",
            "20",
            "--expected-contributors",
            "4",
            "--expected-hosts",
            "5",
            "--expected-syncer-epochs",
            "2",
            "--expected-replaced-learner",
            "learner_000",
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    assert vars(args) == {
        "gate": "F1-functional",
        "experiment_id": "functional-normal",
        "requirement_id": "FUNC-4L1S-01",
        "project_root": ROOT,
        "run_root": tmp_path / "run",
        "log_root": tmp_path / "logs",
        "expected_global_steps": 4,
        "expected_inner_steps": 20,
        "expected_contributors": 4,
        "expected_hosts": 5,
        "expected_syncer_epochs": 2,
        "expected_replaced_learner": "learner_000",
        "output": tmp_path / "evidence.json",
    }


def test_checker_classifies_missing_run_as_structured_blocked_artifact(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "invocation.log").write_text("missing run fixture\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--gate",
            "F1-functional",
            "--experiment-id",
            "functional-missing-run",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "missing"),
            "--log-root",
            str(log_root),
            "--expected-global-steps",
            "4",
            "--expected-inner-steps",
            "20",
            "--expected-contributors",
            "4",
            "--expected-hosts",
            "5",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert artifact["artifact_version"] == 1
    assert artifact["status"] == "BLOCKED"
    assert artifact["gate"] == "F1-functional"
    assert artifact["experiment_id"] == "functional-missing-run"
    assert artifact["requirements_covered"] == ["FUNC-4L1S-01"]
    assert artifact["source_identity"]["scopes"]
    assert artifact["environment"]["interpreter"]["executable"] == sys.executable
    assert artifact["cleanup"]["eligible"] is False
    assert artifact["evidence_paths"] == [str((log_root / "invocation.log").resolve())]
    assert len(artifact["errors"]) == 1


def test_checker_verifies_publication_size_and_hash(tmp_path: Path) -> None:
    module = _checker_module()
    weight = tmp_path / "weights/global.safetensors"
    outer = tmp_path / "optim/global.safetensors"
    weight.parent.mkdir(parents=True)
    outer.parent.mkdir(parents=True)
    weight.write_bytes(b"weight")
    outer.write_bytes(b"outer")
    version = {
        "version": 0,
        "weight_relative_path": weight.relative_to(tmp_path).as_posix(),
        "weight_size": weight.stat().st_size,
        "weight_sha256": hashlib.sha256(b"weight").hexdigest(),
        "optim_relative_path": outer.relative_to(tmp_path).as_posix(),
        "optim_size": outer.stat().st_size,
        "optim_sha256": hashlib.sha256(b"outer").hexdigest(),
    }
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, [version], errors)

    assert errors == []
    assert {item["status"] for item in evidence} == {"ok"}
    weight.write_bytes(b"changed")
    module._verify_publication_objects(tmp_path, [version], errors)
    assert errors == ["publication object identity mismatch: weights/global.safetensors"]


def test_checker_rejects_escaped_or_symlinked_publication_objects(tmp_path: Path) -> None:
    module = _checker_module()
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"outside")
    linked = tmp_path / "weights/linked.safetensors"
    linked.parent.mkdir()
    linked.symlink_to(outside)
    digest = hashlib.sha256(b"outside").hexdigest()
    versions = [
        {
            "version": 1,
            "weight_relative_path": "../outside.safetensors",
            "weight_size": 7,
            "weight_sha256": digest,
            "optim_relative_path": "weights/linked.safetensors",
            "optim_size": 7,
            "optim_sha256": digest,
        }
    ]
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, versions, errors)

    assert {item["status"] for item in evidence} == {"invalid_path", "invalid_type"}
    assert len(errors) == 2


def test_checker_rejects_publication_below_symlinked_parent(tmp_path: Path) -> None:
    module = _checker_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "global.safetensors").write_bytes(b"weight")
    (tmp_path / "weights").symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(b"weight").hexdigest()
    version = {
        "version": 1,
        "weight_relative_path": "weights/global.safetensors",
        "weight_size": 6,
        "weight_sha256": digest,
        "optim_relative_path": "weights/global.safetensors",
        "optim_size": 6,
        "optim_sha256": digest,
    }
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, [version], errors)

    assert {item["status"] for item in evidence} == {"invalid_path"}
    assert len(errors) == 2


def test_gate_artifact_validator_rejects_self_proof(tmp_path: Path) -> None:
    module = _checker_module()
    output = tmp_path / "evidence.json"
    output.write_text("{}\n", encoding="utf-8")
    payload = {
        "artifact_version": 1,
        "status": "BLOCKED",
        "gate": "F1-functional",
        "experiment_id": "self-proof",
        "requirements_covered": ["FUNC-4L1S-01"],
        "source_identity": None,
        "config_schema_identity": None,
        "protocol_schema_identity": None,
        "environment": {},
        "workload_identity": None,
        "metrics": {},
        "errors": ["missing run"],
        "evidence_paths": [str(output)],
        "cleanup": {"owner": "full_protocol_harness", "eligible": False, "targets": []},
    }

    with pytest.raises(RuntimeError, match="evidence path is invalid"):
        module.validate_gate_artifact(payload, output=output)


def test_token_balance_oracle_classifies_every_direct_fate() -> None:
    module = _checker_module()
    rollup = {
        "adjudicated_processed": 21,
        "local_discarded": 1,
        "direct_applied": 10,
        "direct_dropped": 2,
        "direct_quarantined_or_conflicted": 3,
        "direct_reported_unpublished": 4,
        "direct_outstanding": 1,
    }

    assert module._token_balance(rollup) == 0
    rollup["direct_applied"] = 9
    assert module._token_balance(rollup) == 1


def test_pbs_scripts_bind_literal_group_minimum_walltime_and_one_current_runner() -> None:
    pbs_scripts = tuple(sorted((ROOT / "scripts/miyabi").glob("*.pbs")))
    assert pbs_scripts
    for path in pbs_scripts:
        source = path.read_text(encoding="utf-8")
        assert "#PBS -W group_list=xg24i002" in source
        assert "group_list=<" not in source
    wrapper = (ROOT / "scripts/miyabi/run_full_protocol.pbs").read_text(encoding="utf-8")
    assert "#PBS -l walltime=00:15:00" in wrapper
    assert all(
        f'${{{name}:?{name} is required}}' in wrapper
        for name in ("GATE", "EXPERIMENT_ID", "REQUIREMENT_ID")
    )
    assert wrapper.count("run_full_protocol_allocation.sh") == 1
    allocation = (ROOT / "scripts/miyabi/run_full_protocol_allocation.sh").read_text(
        encoding="utf-8"
    )
    assert '"RUN_ID=$RUN_ID"' in allocation
    assert "--map-by ppr:1:node" in allocation
    rank_runner = (ROOT / "scripts/miyabi/run_full_protocol_rank.sh").read_text(
        encoding="utf-8"
    )
    assert "request_static_replacement" in rank_runner
    assert "FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION=2" in rank_runner
    assert "kill -KILL \"$primary_pid\"" in rank_runner
