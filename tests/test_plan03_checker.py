from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import subprocess
import sys

from scripts.miyabi.check_plan03 import inventory, verify_inventory


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
    assert result.stderr == ""


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
            assert discoverable, (row["invariant_id"], evidence_path)
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
