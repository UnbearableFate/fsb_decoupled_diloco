#!/usr/bin/env python3
"""Run the current one-node validation ladder and publish one strict artifact."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

from fs_diloco.core.source_identity import capture_source_identity


REQUIREMENTS = ("UNIT-01", "HARNESS-01")
FOCUSED_TESTS = (
    "tests/architecture",
    "tests/harness/test_full_protocol_harness.py",
    "tests/harness/test_validation_suite.py",
    "tests/runtime/test_learner_entrypoint.py",
    "tests/runtime/test_syncer_fault_boundary.py",
    "tests/runtime/test_syncer_startup_admission.py",
    "tests/runtime/test_terminal_service.py",
    "tests/test_capture_source_identity.py",
    "tests/test_clean_run.py",
    "tests/test_cli.py",
    "tests/test_config.py",
    "tests/tools/test_analysis.py",
    "tests/tools/test_launch_independent_run.py",
    "tests/tools/test_request_terminal_close.py",
)


@dataclass(frozen=True)
class ValidationStep:
    name: str
    argv: tuple[str, ...]


def default_steps() -> tuple[ValidationStep, ...]:
    return (
        ValidationStep("ruff-format", (sys.executable, "-m", "ruff", "format", "--check", ".")),
        ValidationStep("ruff-lint", (sys.executable, "-m", "ruff", "check", ".")),
        ValidationStep(
            "focused-pytest",
            (sys.executable, "-m", "pytest", "-q", *FOCUSED_TESTS),
        ),
        ValidationStep("full-pytest", (sys.executable, "-m", "pytest", "-q")),
    )


def _source_projection(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "commit": identity["git_commit"],
        "dirty": identity["git_dirty"],
        "scopes": identity["source_scopes"],
        "fingerprint": identity["source_fingerprint"],
    }


def _packages() -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in ("pytest", "pytest-timeout", "ruff", "torch"):
        try:
            result[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            result[distribution] = "not-installed"
    return result


def _pbs_nodes() -> list[str]:
    value = os.environ.get("PBS_NODEFILE")
    if not value:
        return []
    path = Path(value)
    if not path.is_file() or path.is_symlink():
        return []
    return sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line})


def _environment() -> dict[str, Any]:
    hostname = socket.gethostname()
    return {
        "interpreter": {"executable": sys.executable, "version": sys.version},
        "packages": _packages(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "nodes": _pbs_nodes(),
        "topology": {"validation_runner": hostname},
    }


def _environment_error(environment: dict[str, Any]) -> str | None:
    hostname = str(environment["topology"]["validation_runner"])
    if not hostname.startswith("mg") or not hostname[2:].isdigit():
        return f"validation must run on a Miyabi compute node, got {hostname!r}"
    if not environment["pbs_job_id"]:
        return "validation requires PBS_JOBID"
    if environment["nodes"] != [hostname]:
        return "validation requires an exact one-node PBS_NODEFILE matching the compute host"
    missing = [
        name for name, version in environment["packages"].items() if version == "not-installed"
    ]
    if missing:
        return f"validation packages are missing: {', '.join(missing)}"
    return None


def _publish_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_artifact(payload: dict[str, Any], *, output: Path) -> None:
    required = {
        "artifact_version",
        "status",
        "gate",
        "experiment_id",
        "requirements_covered",
        "source_identity",
        "config_schema_identity",
        "protocol_schema_identity",
        "environment",
        "workload_identity",
        "metrics",
        "errors",
        "evidence_paths",
        "cleanup",
    }
    if set(payload) != required:
        raise RuntimeError("validation artifact has an unexpected top-level schema")
    if payload["artifact_version"] != 1 or payload["status"] not in {
        "PASS",
        "FAIL",
        "BLOCKED",
    }:
        raise RuntimeError("validation artifact identity is invalid")
    if payload["requirements_covered"] != list(REQUIREMENTS):
        raise RuntimeError("validation artifact requirement ownership is invalid")
    if payload["config_schema_identity"] is not None:
        raise RuntimeError("validation artifact must not claim one runtime config")
    if payload["protocol_schema_identity"] is not None:
        raise RuntimeError("validation artifact must not claim one runtime schema")
    if payload["workload_identity"] is not None:
        raise RuntimeError("validation artifact must not claim one runtime workload")
    evidence = payload["evidence_paths"]
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise RuntimeError("validation artifact must name exactly one raw command log")
    evidence_path = Path(evidence[0])
    if (
        not evidence_path.is_absolute()
        or evidence_path.is_symlink()
        or not evidence_path.is_file()
        or evidence_path.resolve() == output.resolve()
    ):
        raise RuntimeError("validation artifact raw evidence is invalid")
    cleanup = payload["cleanup"]
    if cleanup != {"owner": "validation_suite", "eligible": False, "targets": []}:
        raise RuntimeError("validation artifact cleanup projection is invalid")
    errors = payload["errors"]
    if not isinstance(errors, list) or any(not isinstance(error, str) for error in errors):
        raise RuntimeError("validation artifact errors are invalid")
    source = payload["source_identity"]
    if payload["status"] == "PASS" and (
        errors
        or not isinstance(source, dict)
        or source.get("dirty") is not False
        or not source.get("commit")
        or not source.get("scopes")
        or not source.get("fingerprint")
        or any(step.get("returncode") != 0 for step in payload["metrics"].get("steps", []))
    ):
        raise RuntimeError("PASS validation artifact has incomplete acceptance identity")


def run_validation(
    *,
    project_root: Path,
    raw_log: Path,
    output: Path,
    steps: Sequence[ValidationStep] | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    raw_log = raw_log.resolve()
    output = output.resolve()
    if raw_log == output:
        raise ValueError("raw log and artifact paths must differ")
    if raw_log.exists() or output.exists():
        raise FileExistsError("validation outputs are create-only")

    environment = _environment()
    errors: list[str] = []
    metrics: list[dict[str, Any]] = []
    log_lines = [f"project_root={project_root}", f"started_at={time.time()}"]
    source_before: dict[str, Any] | None = None
    status = "BLOCKED"
    try:
        source_before = capture_source_identity(project_root)
        prerequisite_error = _environment_error(environment)
        if prerequisite_error is not None:
            errors.append(prerequisite_error)
        elif source_before["git_dirty"]:
            errors.append("validation source scopes are dirty")
        else:
            status = "PASS"
            for step in steps if steps is not None else default_steps():
                started = time.monotonic()
                completed = subprocess.run(
                    step.argv,
                    cwd=project_root,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                elapsed = time.monotonic() - started
                metrics.append(
                    {
                        "name": step.name,
                        "argv": list(step.argv),
                        "returncode": completed.returncode,
                        "elapsed_seconds": elapsed,
                    }
                )
                log_lines.extend(
                    (
                        f"step={step.name}",
                        f"argv={json.dumps(step.argv)}",
                        f"returncode={completed.returncode}",
                        f"elapsed_seconds={elapsed}",
                        completed.stdout,
                    )
                )
                if completed.returncode != 0:
                    errors.append(
                        f"validation step failed: {step.name} (exit={completed.returncode})"
                    )
                    status = "FAIL"
                    break
            source_after = capture_source_identity(project_root)
            if source_after != source_before:
                errors.append("source identity changed during validation")
                status = "FAIL"
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        status = "BLOCKED"

    log_lines.append(f"finished_at={time.time()}")
    _publish_new(raw_log, "\n".join(log_lines) + "\n")
    artifact = {
        "artifact_version": 1,
        "status": status,
        "gate": "U1-one-node-validation",
        "experiment_id": "p2-current-suite",
        "requirements_covered": list(REQUIREMENTS),
        "source_identity": None if source_before is None else _source_projection(source_before),
        "config_schema_identity": None,
        "protocol_schema_identity": None,
        "environment": environment,
        "workload_identity": None,
        "metrics": {"steps": metrics},
        "errors": errors,
        "evidence_paths": [str(raw_log)],
        "cleanup": {"owner": "validation_suite", "eligible": False, "targets": []},
    }
    _validate_artifact(artifact, output=output)
    _publish_new(output, json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--raw-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    artifact = run_validation(
        project_root=args.project_root,
        raw_log=args.raw_log,
        output=args.output,
    )
    print(artifact["status"])
    if artifact["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
