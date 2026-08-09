#!/usr/bin/env python3
"""Record the P6 correctness-quality boundary and its non-blocking follow-up."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"artifact is not an object: {path}")
    return value


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--g2", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    g2 = _read(args.g2.resolve())
    errors: list[str] = []
    if g2.get("status") != "PASS":
        errors.append("G2 full regression is not PASS")
    full = g2.get("metrics", {}).get("full", {})
    if int(full.get("failures", 1)) or int(full.get("errors", 1)):
        errors.append("G2 full regression contains failures/errors")
    required_tests = (
        "tests/protocol/test_p3_unified_v4_golden.py",
        "tests/reference/test_plan03_classic_static_oracle.py",
        "tests/test_torch_baseline_health.py",
    )
    missing = [path for path in required_tests if not (project_root / path).is_file()]
    if missing:
        errors.append(f"quality regression owners are missing: {missing}")
    source_commit = g2.get("source_commit")
    payload = {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "gate": "P6-quality-boundary",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source_commit,
        "source_identity": {
            "git_commit": source_commit,
            "git_dirty": g2.get("source_identity", {}).get("git_dirty"),
        },
        "requirements_covered": ["P6-QUALITY"],
        "correctness_quality_evidence": {
            "deterministic_oracle": "verified by G2 full regression",
            "finite_numeric_regressions": "verified by G2 full regression",
            "test_owners": list(required_tests),
            "g2_artifact": str(args.g2.resolve()),
        },
        "non_blocking_follow_up": {
            "status": "NOT_RUN",
            "scope": "three-seed unique-token training-quality study",
            "reason": "pre-declared non-goal; no statistical quality claim is made",
        },
        "errors": errors,
    }
    _write(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
