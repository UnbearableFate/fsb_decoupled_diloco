#!/usr/bin/env python3
"""Validate and combine the independent P6 G0-G7 correctness artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
EXPECTED_SCENARIOS = {
    "static-1-learner-1-candidate-none",
    "static-2-learners-1-candidate-none",
    "static-2-learners-loss-rerun-old-resume",
    "static-2-learners-2-candidates-active-crash",
    "dynamic-2-learners-replacement",
    "dynamic-2-learners-2-candidates-syncer-and-learner-failure",
}
EXPECTED_PHASE_REQUIREMENTS = frozenset(
    {
        "AUTH-11",
        "P6-ACCEPTANCE",
        "P6-STATIC-9N",
        "P6-DYNAMIC-9N",
        "P6-PERF-CLASSIC",
        "P6-PERF-DYNAMIC",
        "P6-QUALITY",
        "P6-DOCS",
    }
)


def phase_requirement_error(value: Any) -> str | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return "G0 P6 requirement matrix has an invalid requirement list"
    actual = set(value)
    if len(actual) != len(value) or actual != EXPECTED_PHASE_REQUIREMENTS:
        return (
            "G0 P6 requirement matrix differs from the exact phase set: "
            f"missing={sorted(EXPECTED_PHASE_REQUIREMENTS - actual)}, "
            f"extra={sorted(actual - EXPECTED_PHASE_REQUIREMENTS)}, "
            f"duplicates={len(value) - len(actual)}"
        )
    return None


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"artifact is not an object: {path}")
    return payload


def _atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate(paths: dict[str, Path]) -> dict[str, Any]:
    artifacts = {name: _read(path) for name, path in paths.items()}
    errors: list[str] = []
    for name, payload in artifacts.items():
        if payload.get("status") != "PASS":
            errors.append(f"{name} status is not PASS")
        if payload.get("plan_id") != PLAN_ID:
            errors.append(f"{name} plan identity mismatch")
        identity = payload.get("source_identity", {})
        if identity.get("git_dirty") is not False:
            errors.append(f"{name} lacks a clean source marker")
    commit_values = [
        payload.get("source_commit") or payload.get("source_identity", {}).get("git_commit")
        for payload in artifacts.values()
    ]
    commits = {value for value in commit_values if isinstance(value, str) and value}
    if len(commits) != 1 or len(commits) != len(set(commit_values)):
        errors.append(f"G0-G7 source commits differ: {sorted(commits)}")

    g01 = artifacts["g0_g1"]
    if int(g01.get("cost", {}).get("estimated_total_jobs", -1)) != 19:
        errors.append("G0 cost/job freeze is missing")
    requirement_error = phase_requirement_error(g01.get("phase_requirements"))
    if requirement_error is not None:
        errors.append(requirement_error)
    if any(item.get("returncode") != 0 for item in g01.get("checks", [])):
        errors.append("G1 contains a failed static command")

    g2 = artifacts["g2"]
    full = g2.get("metrics", {}).get("full", {})
    if (
        int(full.get("tests", 0)) < 526
        or int(full.get("failures", 1))
        or int(full.get("errors", 1))
    ):
        errors.append(f"G2 full-suite metrics are incomplete: {full}")

    g3 = artifacts["g3"].get("metrics", {})
    expected_g3 = {
        "pure_examples": 1000,
        "pure_max_transitions": 300,
        "sqlite_examples": 200,
        "sqlite_max_transitions": 150,
        "action_count": 13,
    }
    if any(int(g3.get(key, -1)) != value for key, value in expected_g3.items()):
        errors.append(f"G3 profile mismatch: {g3}")

    g4 = artifacts["g4"]
    coverage = g4.get("coverage", {})
    if (
        int(g4.get("required_boundaries", -1)) != 18
        or int(g4.get("required_repetitions_per_boundary", -1)) != 10
        or len(coverage) != 18
        or any(
            item.get("status") != "PASS" or int(item.get("completed_repetitions", -1)) != 10
            for item in coverage.values()
        )
    ):
        errors.append("G4 does not prove eighteen crash boundaries times ten")

    g5 = artifacts["g5"]
    scenarios = g5.get("scenarios", [])
    names = {row.get("name") for row in scenarios if isinstance(row, dict)}
    if int(g5.get("scenario_count", -1)) != 6 or names != EXPECTED_SCENARIOS:
        errors.append(f"G5 scenario matrix mismatch: {sorted(str(item) for item in names)}")
    for row in scenarios:
        if (
            int(row.get("token_balance", 1)) != 0
            or not row.get("terminal_fences")
            or row.get("summary", {}).get("all_learners_stopped") is not True
        ):
            errors.append(f"G5 scenario invariant failed: {row.get('name')}")

    g6 = artifacts["g6"].get("metrics", {})
    live_upper = g6.get("live_page_slope", {}).get("one_sided_95_upper")
    file_upper = g6.get("active_file_slope", {}).get("one_sided_95_upper")
    if (
        int(g6.get("cycles", -1)) < 10_000
        or live_upper is None
        or float(live_upper) >= 0.01
        or file_upper is None
        or abs(float(file_upper)) >= 0.01
    ):
        errors.append("G6 boundedness slope/profile gate failed")

    g7 = artifacts["g7"].get("results", {})
    outside = g7.get("outside_transaction", {})
    inside = g7.get("inside_transaction", {})
    if outside.get("old_resumed", {}).get("stale_successful_commits") != 0:
        errors.append("G7 stale outside-transaction writer committed")
    if outside.get("authority", {}).get("integrity") != ["ok"]:
        errors.append("G7 outside-transaction integrity failed")
    if inside.get("authority", {}).get("integrity") != ["ok"]:
        errors.append("G7 inside-transaction integrity failed")
    if float(inside.get("successor", {}).get("wait_seconds", 0.0)) < 3.0:
        errors.append("G7 successor did not demonstrably wait for the held writer lock")

    source_commit = next(iter(commits)) if len(commits) == 1 else None
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "gate": "G0-G7-correctness-aggregate",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source_commit,
        "source_identity": {"git_commit": source_commit, "git_dirty": False},
        "requirements_covered": ["P6-ACCEPTANCE"],
        "inputs": {name: str(path) for name, path in paths.items()},
        "gate_statuses": {name: payload.get("status") for name, payload in artifacts.items()},
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("g0-g1", "g2", "g3", "g4", "g5", "g6", "g7"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = {
        "g0_g1": args.g0_g1.resolve(),
        "g2": args.g2.resolve(),
        "g3": args.g3.resolve(),
        "g4": args.g4.resolve(),
        "g5": args.g5.resolve(),
        "g6": args.g6.resolve(),
        "g7": args.g7.resolve(),
    }
    payload = validate(paths)
    _atomic(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
