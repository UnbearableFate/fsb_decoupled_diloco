#!/usr/bin/env python3
"""Execute the frozen P6 pure/SQLite generated-state profiles."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
NODEIDS = (
    "tests/gates/test_plan03_p6_state_machines.py::test_p6_gate_pure_protocol_model",
    "tests/gates/test_plan03_p6_state_machines.py::test_p6_gate_sqlite_adapter",
)


def _source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load source identity helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.capture(project_root)


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    source = _source(project_root)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["FS_DILOCO_RUN_P6_FORMAL_GATES"] = "1"
    command = [str(args.python.resolve()), "-m", "pytest", "-q", *NODEIDS]
    started = time.monotonic()
    with args.log.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
    errors: list[str] = []
    if result.returncode != 0:
        errors.append(f"pytest returned {result.returncode}")
    if source["git_dirty"]:
        errors.append("formal state-machine source is dirty")
    payload = {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "gate": "G3-generated-state-machines",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source["git_commit"],
        "source_identity": {
            "git_commit": source["git_commit"],
            "git_dirty": source["git_dirty"],
            "source_fingerprint": source["source_fingerprint"],
        },
        "requirements_covered": ["P6-ACCEPTANCE"],
        "environment": {"pbs_job_id": os.environ.get("PBS_JOBID")},
        "metrics": {
            "pure_examples": 1000,
            "pure_max_transitions": 300,
            "sqlite_examples": 200,
            "sqlite_max_transitions": 150,
            "action_count": 13,
            "elapsed_seconds": time.monotonic() - started,
        },
        "evidence_paths": [str(args.log.resolve())],
        "errors": errors,
    }
    _write(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
