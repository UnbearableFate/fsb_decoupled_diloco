from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


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
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "run"),
            "--expected-global-steps",
            "4",
            "--expected-inner-steps",
            "20",
            "--expected-contributors",
            "4",
            "--expected-hosts",
            "5",
            "--expected-min-syncer-epochs",
            "2",
            "--expected-replaced-learner",
            "learner_000",
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    assert vars(args) == {
        "project_root": ROOT,
        "run_root": tmp_path / "run",
        "expected_global_steps": 4,
        "expected_inner_steps": 20,
        "expected_contributors": 4,
        "expected_hosts": 5,
        "expected_min_syncer_epochs": 2,
        "expected_replaced_learner": "learner_000",
        "output": tmp_path / "evidence.json",
    }


def test_checker_classifies_missing_run_as_structured_blocked_artifact(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "missing"),
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
    assert artifact["requirements_covered"] == []
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
    assert wrapper.count("run_full_protocol_allocation.sh") == 1
    rank_runner = (ROOT / "scripts/miyabi/run_full_protocol_rank.sh").read_text(
        encoding="utf-8"
    )
    assert "request_static_replacement" in rank_runner
    assert "FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION=2" in rank_runner
