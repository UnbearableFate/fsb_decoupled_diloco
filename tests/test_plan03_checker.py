from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.miyabi.check_plan03 import (
    inventory,
    verify_boundaries,
    verify_inventory,
    verify_p5_contracts,
    verify_phase_requirements,
    verify_p4_migration_contracts,
    verify_tracked_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
FROZEN = (
    ROOT
    / "reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts"
    / "20260808-223500_p0-runtime-surface-inventory_review.json"
)


def _expected() -> dict[str, object]:
    return json.loads(FROZEN.read_text(encoding="utf-8"))


def test_plan03_checker_verifies_frozen_commit_inventory() -> None:
    expected = _expected()
    actual = inventory(ROOT, source_ref=str(expected["source_identity"]["commit"]))

    assert verify_inventory(actual, expected) == []


def test_plan03_checker_cli_prints_only_pass_for_frozen_verification() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/miyabi/check_plan03.py",
            "--root",
            str(ROOT),
            "--expect",
            str(FROZEN),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "PASS\n"
    assert "Traceback" not in result.stderr


def test_plan03_checker_blocks_count_manifest_and_tag_drift() -> None:
    expected = _expected()
    actual = inventory(ROOT, source_ref=str(expected["source_identity"]["commit"]))

    count_drift = copy.deepcopy(actual)
    count_drift["counts"]["config_files"] += 1
    assert verify_inventory(count_drift, expected) == ["counts"]

    manifest_drift = copy.deepcopy(actual)
    first_path = next(iter(manifest_drift["manifest_sha256"]))
    manifest_drift["manifest_sha256"][first_path] = "0" * 64
    assert verify_inventory(manifest_drift, expected) == ["manifest_sha256"]

    missing_tag = copy.deepcopy(actual)
    tag = next(iter(missing_tag["source_identity"]["archive_tag_targets"]))
    missing_tag["source_identity"]["archive_tag_targets"][tag] = None
    assert verify_inventory(missing_tag, expected) == ["source_identity.archive_tag_targets"]


def test_plan03_checker_blocks_real_tracked_fragment_boundary_drift(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(clone)],
        check=True,
    )
    source = clone / "configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml"
    drift = clone / "configs/plan03-unexpected-fragment.yaml"
    drift.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "add", str(drift.relative_to(clone))], cwd=clone, check=True)

    differences = verify_boundaries(inventory(clone), _expected())

    assert "boundary_counts" in differences
    assert "migration_boundaries" in differences


def test_plan03_boundary_allows_p1_baseline_composition_but_not_protocol_drift() -> None:
    expected = _expected()
    actual = inventory(ROOT, source_ref=str(expected["source_identity"]["commit"]))
    train = "fs_diloco/baselines/train.py"
    protocol = "fs_diloco/baselines/protocol.py"

    composition_only = copy.deepcopy(actual)
    composition_only["manifest_sha256"][train] = "0" * 64
    assert verify_boundaries(composition_only, expected) == []

    protocol_drift = copy.deepcopy(actual)
    protocol_drift["manifest_sha256"][protocol] = "0" * 64
    assert verify_boundaries(protocol_drift, expected) == ["boundary_manifest_sha256"]


def test_plan03_boundary_allows_p3_store_implementation_but_not_mutator_surface_drift() -> None:
    expected = _expected()
    actual = inventory(ROOT, source_ref=str(expected["source_identity"]["commit"]))

    implementation_only = copy.deepcopy(actual)
    implementation_only["manifest_sha256"]["fs_diloco/storage/fenced_store.py"] = "0" * 64
    assert verify_boundaries(implementation_only, expected) == []

    mutator_drift = copy.deepcopy(actual)
    mutator_drift["inventory"]["bound_mutators"].append("unreviewed_mutator")
    assert verify_boundaries(mutator_drift, expected) == ["inventory.bound_mutators"]


def test_plan03_p4_semantic_migration_still_detects_post_p4_config_changes() -> None:
    expected = _expected()
    frozen = str(expected["source_identity"]["commit"])
    differences = verify_p4_migration_contracts(ROOT, frozen)
    assert "config-migration.fragment-path-inventory" in differences
    assert "config-migration.historical-path-inventory" in differences


def test_plan03_checker_guards_p5_removal_and_compatibility_contracts() -> None:
    assert verify_p5_contracts(ROOT, _expected()) == []


def test_plan03_completed_candidate_evidence_is_tracked_and_matches_contract() -> None:
    matrix = (
        ROOT / "plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv"
    )
    with matrix.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [row for row in rows if row["invariant_id"].startswith("P0-")]

    assert len(candidates) == 10
    for row in candidates:
        assert row["status"] in {"completion-candidate", "complete"}
        evidence = [item.strip() for item in row["evidence_path"].split(";") if item.strip()]
        for evidence_path in evidence:
            path = ROOT / evidence_path
            assert path.exists(), (row["invariant_id"], evidence_path)
            discoverable = subprocess.run(
                [
                    "git",
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    evidence_path,
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if evidence_path.endswith("/"):
                assert discoverable and all(
                    path.startswith(evidence_path) for path in discoverable
                ), (row["invariant_id"], evidence_path)
            else:
                assert discoverable == [evidence_path], (row["invariant_id"], evidence_path)
        contracts = [
            item.strip() for item in row["artifact_contract"].split(";") if "artifacts/<ts>" in item
        ]
        evidence_names = {Path(item).name for item in evidence}
        for contract in contracts:
            suffix = contract.split("<ts>", 1)[1]
            assert any(name.endswith(suffix) for name in evidence_names), (
                row["invariant_id"],
                contract,
            )
        if row["invariant_id"] == "P0-FS-CAP":
            assert len(evidence) == 1


def test_plan03_phase_final_gate_requires_tracked_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    tracked = repository / "tracked.json"
    tracked.write_text("{}\n", encoding="utf-8")
    tracked_dir = repository / "tracked-dir"
    tracked_dir.mkdir()
    (tracked_dir / "item.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.json", "tracked-dir/item.json"],
        cwd=repository,
        check=True,
    )
    matrix = repository / "matrix.csv"
    matrix.write_text(
        "invariant_id,status,evidence_path\nONE,complete,tracked.json\nTWO,complete,tracked-dir/\n",
        encoding="utf-8",
    )
    assert verify_tracked_evidence(repository, matrix) == []

    matrix.write_text(
        matrix.read_text(encoding="utf-8") + "THREE,complete,untracked.json\n",
        encoding="utf-8",
    )
    (repository / "untracked.json").write_text("{}\n", encoding="utf-8")
    assert verify_tracked_evidence(repository, matrix) == ["THREE:untracked.json:not-tracked"]


def test_plan03_p3_requirement_checker_binds_implementation_tests_and_evidence() -> None:
    matrix = (
        ROOT / "plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv"
    )
    with matrix.open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle) if row["phase"] == "P3-operational-robustness"
        ]
    evidence_paths = {
        item.strip()
        for row in rows
        for item in row["evidence_path"].split(";")
        if item.strip().endswith(".json")
    }
    evidence = {
        path: json.loads((ROOT / path).read_text(encoding="utf-8")) for path in evidence_paths
    }
    runtime_paths = [
        path for path, payload in evidence.items() if payload.get("requirements_covered")
    ]
    self_paths = [
        path for path, payload in evidence.items() if payload.get("checks", {}).get("requirements")
    ]
    assert len(runtime_paths) == len(self_paths) == 1
    source_commit = evidence[runtime_paths[0]]["source_commit"]
    checks, differences = verify_phase_requirements(
        ROOT,
        matrix,
        "P3-operational-robustness",
        expected_source_commit=source_commit,
        excluded_evidence_path=self_paths[0],
    )

    assert differences == []
    assert checks
    assert all(item["status"] == "PASS" for item in checks.values())
    assert all(item["structured_evidence_paths"] == runtime_paths for item in checks.values())


def test_plan03_requirement_checker_rejects_self_evidence_and_stale_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "fs_diloco").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "fs_diloco" / "owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "test_owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    matrix = tmp_path / "matrix.csv"
    matrix.write_text(
        "invariant_id,phase,artifact_contract,status,evidence_path\n"
        'REQ-1,P3,"checker requirements.REQ-1; artifacts/<ts>_runtime.json",'
        "complete,self.json;runtime.json\n",
        encoding="utf-8",
    )
    self_artifact = {
        "checks": {
            "current_migration_boundaries": {"source_commit": "target"},
            "requirements": {"REQ-1": {"status": "PASS"}},
        }
    }
    (tmp_path / "self.json").write_text(json.dumps(self_artifact), encoding="utf-8")
    stale_runtime = {
        "status": "PASS",
        "source_commit": "stale",
        "source_identity": {"git_dirty": False},
        "requirements_covered": ["REQ-1"],
    }
    (tmp_path / "runtime.json").write_text(json.dumps(stale_runtime), encoding="utf-8")

    checks, differences = verify_phase_requirements(
        tmp_path,
        matrix,
        "P3",
        expected_source_commit="target",
        excluded_evidence_path="self.json",
    )
    assert checks["REQ-1"]["status"] == "BLOCKED"
    assert differences == ["requirements.REQ-1.structured-checker-evidence"]

    stale_runtime["source_commit"] = "target"
    (tmp_path / "runtime.json").write_text(json.dumps(stale_runtime), encoding="utf-8")
    (tmp_path / "self.json").unlink()
    checks, differences = verify_phase_requirements(
        tmp_path,
        matrix,
        "P3",
        expected_source_commit="target",
        excluded_evidence_path="self.json",
    )
    assert differences == []
    assert checks["REQ-1"]["structured_evidence_paths"] == ["runtime.json"]


def test_plan03_requirement_checker_accepts_evidence_only_descendant_target(
    tmp_path: Path,
) -> None:
    (tmp_path / "fs_diloco").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "fs_diloco" / "owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "test_owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    matrix = tmp_path / "matrix.csv"
    matrix.write_text(
        "invariant_id,phase,artifact_contract,status,evidence_path\n"
        'REQ-1,P4,"checker requirements.REQ-1",complete,runtime.json\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "plan03@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Plan 03 Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "source"], cwd=tmp_path, check=True)
    source = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "runtime.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source_commit": source,
                "source_identity": {"git_dirty": False},
                "requirements_covered": ["REQ-1"],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "runtime.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "evidence"], cwd=tmp_path, check=True)
    evidence_target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    checks, differences = verify_phase_requirements(
        tmp_path,
        matrix,
        "P4",
        expected_source_commit=evidence_target,
    )
    assert differences == []
    assert checks["REQ-1"]["status"] == "PASS"

    (tmp_path / "fs_diloco" / "owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\nCHANGED = True\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "fs_diloco/owner.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "source drift"], cwd=tmp_path, check=True)
    drift_target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _checks, differences = verify_phase_requirements(
        tmp_path,
        matrix,
        "P4",
        expected_source_commit=drift_target,
    )
    assert differences == ["requirements.REQ-1.structured-checker-evidence"]


def test_plan03_p4_gate_requirements_are_owned_by_the_p4_phase() -> None:
    matrix = (
        ROOT / "plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv"
    )
    with matrix.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row["gate"] == "P4 completed"]

    assert rows
    assert {row["phase"] for row in rows} == {"P4-mandatory-fenced-runtime"}
    assert all(row["status"] == "complete" for row in rows)
    assert all(row["evidence_path"] != "TBD" for row in rows)


def test_plan03_triage_finding_ids_are_all_bound_to_matrix_requirements() -> None:
    triage = json.loads(
        (
            ROOT
            / "reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts"
            / "20260808-224500_p0-finding-triage_review.json"
        ).read_text(encoding="utf-8")
    )
    matrix = (
        ROOT / "plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv"
    )
    with matrix.open(newline="", encoding="utf-8") as handle:
        references = ",".join(row["review_finding"] for row in csv.DictReader(handle))

    for finding in triage["findings"]:
        finding_id = str(finding["id"])
        prefix, number = finding_id.split("-")
        number_value = int(number)
        directly_referenced = finding_id in references
        range_referenced = any(
            token.startswith(f"{prefix}-")
            and ".." in token
            and int(token.split("-", 1)[1].split("..", 1)[0]) <= number_value
            and int(token.rsplit("-", 1)[1]) >= number_value
            for token in references.split(",")
        )
        assert directly_referenced or range_referenced, finding_id


def _requirement_evidence_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    (tmp_path / "fs_diloco").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "fs_diloco" / "owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "test_owner.py").write_text(
        'PLAN03_REQUIREMENTS = {"REQ-1"}\n', encoding="utf-8"
    )
    matrix = tmp_path / "matrix.csv"
    matrix.write_text(
        "invariant_id,phase,artifact_contract,status,evidence_path\n"
        'REQ-1,P4,"checker requirements.REQ-1",complete,evidence.json\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "plan03@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Plan 03 Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "source"], cwd=tmp_path, check=True)
    source = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return tmp_path, matrix, source


def test_plan03_requirement_checker_rejects_dirty_runtime_evidence(tmp_path: Path) -> None:
    root, matrix, source = _requirement_evidence_repo(tmp_path)
    (root / "evidence.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source_commit": source,
                "source_identity": {"git_dirty": True},
                "requirements_covered": ["REQ-1"],
            }
        ),
        encoding="utf-8",
    )

    _checks, differences = verify_phase_requirements(
        root,
        matrix,
        "P4",
        expected_source_commit=source,
    )

    assert differences == ["requirements.REQ-1.structured-checker-evidence"]


@pytest.mark.parametrize(
    "source_identity",
    [None, {"git_dirty": None}],
    ids=["missing-marker", "null-marker"],
)
def test_plan03_requirement_checker_requires_explicit_clean_runtime_evidence(
    tmp_path: Path,
    source_identity: dict[str, object] | None,
) -> None:
    root, matrix, source = _requirement_evidence_repo(tmp_path)
    payload: dict[str, object] = {
        "status": "PASS",
        "source_commit": source,
        "requirements_covered": ["REQ-1"],
    }
    if source_identity is not None:
        payload["source_identity"] = source_identity
    (root / "evidence.json").write_text(json.dumps(payload), encoding="utf-8")

    _checks, differences = verify_phase_requirements(
        root,
        matrix,
        "P4",
        expected_source_commit=source,
    )

    assert differences == ["requirements.REQ-1.structured-checker-evidence"]


def test_plan03_evidence_source_equivalence_covers_root_entrypoint(tmp_path: Path) -> None:
    root, matrix, source = _requirement_evidence_repo(tmp_path)
    (root / "evidence.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source_commit": source,
                "source_identity": {"git_dirty": False},
                "requirements_covered": ["REQ-1"],
            }
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text("print('changed')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "entrypoint drift"], cwd=root, check=True)
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _checks, differences = verify_phase_requirements(
        root,
        matrix,
        "P4",
        expected_source_commit=target,
    )

    assert differences == ["requirements.REQ-1.structured-checker-evidence"]


def test_plan03_requirement_evidence_does_not_treat_frozen_boundary_as_source(
    tmp_path: Path,
) -> None:
    root, matrix, source = _requirement_evidence_repo(tmp_path)
    (root / "evidence.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "checks": {
                    "current_migration_boundaries": {"source_commit": source},
                    "requirements": {"REQ-1": {"status": "PASS"}},
                },
            }
        ),
        encoding="utf-8",
    )

    _checks, differences = verify_phase_requirements(
        root,
        matrix,
        "P4",
        expected_source_commit=source,
    )

    assert differences == ["requirements.REQ-1.structured-checker-evidence"]
