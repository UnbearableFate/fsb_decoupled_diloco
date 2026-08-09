#!/usr/bin/env python3
"""Run and record the formal P6 G0/G1 static or G2 compute test gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
P5_FINAL = "7f797e47d983878e25f9c48c1fddbeb9f0b2ea4f"
FOCUSED_TARGETS = (
    "tests/architecture",
    "tests/legacy",
    "tests/observability",
    "tests/protocol",
    "tests/runtime",
    "tests/storage",
    "tests/tools",
    "tests/test_clean_run.py",
    "tests/test_config.py",
    "tests/test_plan03_checker.py",
    "tests/test_torch_baseline_artifacts_and_data.py",
    "tests/test_torch_baseline_health.py",
    "tests/test_torch_baseline_protocol.py",
)
PBS_COST = (
    {"gate": "G2", "jobs": 1, "maximum_nodes": 1, "walltime": "00:10:00"},
    {"gate": "G3/G4", "jobs": 1, "maximum_nodes": 1, "walltime": "00:20:00"},
    {"gate": "G5", "jobs": 3, "maximum_nodes": 2, "walltime": "00:20:00"},
    {"gate": "G6", "jobs": 1, "maximum_nodes": 1, "walltime": "00:30:00"},
    {"gate": "G7", "jobs": 1, "maximum_nodes": 2, "walltime": "00:10:00"},
    {"gate": "G8", "jobs": 1, "maximum_nodes": 9, "walltime": "00:20:00"},
    {"gate": "G9", "jobs": 10, "maximum_nodes": 9, "walltime": "00:30:00"},
    {"gate": "G10", "jobs": 1, "maximum_nodes": 1, "walltime": "00:30:00"},
)


def _source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load source identity helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.capture(project_root)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run(
    command: list[str], *, project_root: Path, log: Path, environment: dict[str, str]
) -> dict[str, Any]:
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
    return {
        "command": command,
        "returncode": int(result.returncode),
        "elapsed_seconds": time.monotonic() - started,
        "log": str(log),
    }


def _modified_python(project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{P5_FINAL}..HEAD", "--"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        path
        for path in result.stdout.splitlines()
        if path.endswith(".py") and (project_root / path).is_file()
    )


def _junit_metrics(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }


def run_static(project_root: Path, python: Path, ruff: Path, log_root: Path) -> dict[str, Any]:
    source = _source(project_root)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    modified = _modified_python(project_root)
    pbs = sorted(
        str(path.relative_to(project_root))
        for root in ("scripts/miyabi", "scripts/local")
        for suffix in ("*.pbs", "*.sh")
        for path in (project_root / root).glob(suffix)
    )
    checks = [
        _run(
            ["git", "diff", "--check"],
            project_root=project_root,
            log=log_root / "git-diff-check.log",
            environment=environment,
        ),
        _run(
            [str(python), "-m", "compileall", "-q", "fs_diloco"],
            project_root=project_root,
            log=log_root / "compileall.log",
            environment=environment,
        ),
        _run(
            [str(ruff), "check", "fs_diloco", "tests", "scripts/miyabi"],
            project_root=project_root,
            log=log_root / "ruff-check.log",
            environment=environment,
        ),
        _run(
            [str(ruff), "format", "--check", *modified],
            project_root=project_root,
            log=log_root / "ruff-format.log",
            environment=environment,
        ),
        _run(
            ["bash", "-n", *pbs],
            project_root=project_root,
            log=log_root / "bash-n.log",
            environment=environment,
        ),
        _run(
            [
                str(python),
                "scripts/miyabi/check_plan03.py",
                "--root",
                str(project_root),
                "--expect",
                "reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/20260808-223500_p0-runtime-surface-inventory_review.json",
                "--verify-boundaries",
                "--verify-p3-operational-contracts",
                "--verify-p5-contracts",
                "--inventory-output",
                str(log_root / "checker.json"),
            ],
            project_root=project_root,
            log=log_root / "checker.log",
            environment=environment,
        ),
    ]
    errors = [
        f"static command returned {item['returncode']}: {item['command']}"
        for item in checks
        if item["returncode"] != 0
    ]
    matrix_path = (
        project_root
        / "plans/DOING/plans/fsb_decoupled_diloco_plan_03_unified_ha-requirement-matrix.csv"
    )
    with matrix_path.open(newline="", encoding="utf-8") as handle:
        phase_requirements = {
            row["invariant_id"]
            for row in csv.DictReader(handle)
            if row["phase"] == "P6-acceptance-final-review"
        }
    expected_requirements = {
        "P6-ACCEPTANCE",
        "P6-STATIC-9N",
        "P6-DYNAMIC-9N",
        "P6-PERF-CLASSIC",
        "P6-PERF-DYNAMIC",
        "P6-QUALITY",
        "P6-DOCS",
    }
    if phase_requirements != expected_requirements:
        errors.append(f"P6 requirement matrix mismatch: {sorted(phase_requirements)}")
    invalid_groups = []
    for path in (project_root / "scripts/miyabi").glob("*.pbs"):
        group_lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("#PBS -W group_list=")
        ]
        if group_lines != ["#PBS -W group_list=xg24i002"]:
            invalid_groups.append(str(path.relative_to(project_root)))
    if invalid_groups:
        errors.append(f"PBS group IDs are missing or non-literal: {invalid_groups}")
    if source["git_dirty"]:
        errors.append("formal executable source scope is dirty")
    return {
        "gate": "G0-G1-freeze-static",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source["git_commit"],
        "source_identity": source,
        "frozen_identity": {
            relative: _sha256(project_root / relative)
            for relative in (
                "configs/fs_diloco_tiny_ha_static_acceptance.yaml",
                "configs/fs_diloco_tiny_ha_dynamic_acceptance.yaml",
                "fs_diloco/storage/schema_v4.sql",
                "fs_diloco/storage/schema_v4_dynamic.sql",
                "uv.lock",
            )
        },
        "cost": {"estimated_total_jobs": 19, "gates": PBS_COST},
        "modified_python_scope": modified,
        "phase_requirements": sorted(phase_requirements),
        "checks": checks,
        "errors": errors,
    }


def run_compute(project_root: Path, python: Path, log_root: Path) -> dict[str, Any]:
    if not os.environ.get("PBS_JOBID"):
        raise RuntimeError("formal G2 tests must run inside a PBS allocation")
    source = _source(project_root)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    collect = _run(
        [str(python), "-m", "pytest", "--collect-only", "-q"],
        project_root=project_root,
        log=log_root / "collect.log",
        environment=environment,
    )
    focused_xml = log_root / "focused.xml"
    focused = _run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            *FOCUSED_TARGETS,
            f"--junitxml={focused_xml}",
        ],
        project_root=project_root,
        log=log_root / "focused.log",
        environment=environment,
    )
    full_xml = log_root / "full.xml"
    full = _run(
        [str(python), "-m", "pytest", "-q", f"--junitxml={full_xml}"],
        project_root=project_root,
        log=log_root / "full.log",
        environment=environment,
    )
    metrics = {
        "focused": _junit_metrics(focused_xml) if focused_xml.is_file() else {},
        "full": _junit_metrics(full_xml) if full_xml.is_file() else {},
    }
    errors = [
        f"test command returned {item['returncode']}: {item['command']}"
        for item in (collect, focused, full)
        if item["returncode"] != 0
    ]
    for name, values in metrics.items():
        if values.get("failures", 0) or values.get("errors", 0):
            errors.append(f"{name} suite has failures/errors: {values}")
        log_text = (log_root / f"{name}.log").read_text(encoding="utf-8", errors="replace")
        if " xfailed" in log_text:
            errors.append(f"{name} suite retained xfailed tests")
    if source["git_dirty"]:
        errors.append("formal executable source scope is dirty")
    return {
        "gate": "G2-focused-full-tests",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source["git_commit"],
        "source_identity": source,
        "environment": {"pbs_job_id": os.environ["PBS_JOBID"]},
        "commands": {"collect": collect, "focused": focused, "full": full},
        "metrics": metrics,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("static", "compute"), required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--ruff", type=Path)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    payload: dict[str, Any] = {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "requirements_covered": [],
    }
    try:
        result = (
            run_static(
                project_root,
                args.python.resolve(),
                (args.ruff or project_root / ".venv/bin/ruff").resolve(),
                args.log_root.resolve(),
            )
            if args.mode == "static"
            else run_compute(
                project_root,
                args.python.resolve(),
                args.log_root.resolve(),
            )
        )
        payload.update(result)
    except Exception as exc:
        payload.update(
            status="BLOCKED",
            gate="G0-G1" if args.mode == "static" else "G2",
            errors=[f"{type(exc).__name__}: {exc}"],
        )
    _atomic_json(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
