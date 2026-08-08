from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import subprocess
import sys

from scripts.miyabi.check_plan03 import (
    inventory,
    verify_boundaries,
    verify_inventory,
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
