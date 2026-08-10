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
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Literal, Sequence
import xml.etree.ElementTree as ET

from fs_diloco.core.source_identity import capture_source_identity


REQUIREMENTS = ("UNIT-01", "HARNESS-01")
FOCUSED_TESTS = (
    "tests/architecture",
    "tests/harness/test_full_protocol_harness.py",
    "tests/harness/test_plan_completion.py",
    "tests/harness/test_validation_suite.py",
    "tests/runtime/test_learner_entrypoint.py",
    "tests/runtime/test_syncer_fault_boundary.py",
    "tests/runtime/test_syncer_startup_admission.py",
    "tests/runtime/test_terminal_service.py",
    "tests/storage/test_authority_operational.py",
    "tests/storage/test_contributor_progress.py",
    "tests/storage/test_dynamic_admission_request.py",
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
    result_kind: Literal["command", "pytest"]

    def __post_init__(self) -> None:
        if not self.name or any(
            not (character.isalnum() or character in "._-") for character in self.name
        ):
            raise ValueError("validation step name is not a safe identifier")
        if not self.argv:
            raise ValueError("validation step argv must not be empty")
        if self.result_kind not in {"command", "pytest"}:
            raise ValueError("validation step result kind is invalid")
        if self.result_kind == "pytest" and any(
            argument == "--junitxml" or argument.startswith("--junitxml=") for argument in self.argv
        ):
            raise ValueError("pytest validation steps reserve --junitxml for the evidence runner")


def default_steps() -> tuple[ValidationStep, ...]:
    return (
        ValidationStep(
            "ruff-format",
            (sys.executable, "-m", "ruff", "format", "--check", "."),
            "command",
        ),
        ValidationStep("ruff-lint", (sys.executable, "-m", "ruff", "check", "."), "command"),
        ValidationStep(
            "focused-pytest",
            (sys.executable, "-m", "pytest", "-q", *FOCUSED_TESTS),
            "pytest",
        ),
        ValidationStep("full-pytest", (sys.executable, "-m", "pytest", "-q"), "pytest"),
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


def _junit_path(raw_log: Path, step: ValidationStep) -> Path:
    return raw_log.with_name(f"{raw_log.name}.{step.name}.junit.xml")


def _parse_junit(path: Path) -> dict[str, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"pytest JUnit evidence is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"pytest JUnit evidence is not a regular file: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"pytest JUnit evidence is malformed: {path}") from exc
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        raise RuntimeError(f"pytest JUnit evidence has an invalid root: {path}")
    if not suites:
        raise RuntimeError(f"pytest JUnit evidence contains no test suite: {path}")
    totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for name in totals:
            value = suite.get(name)
            if value is None or not value.isdigit():
                raise RuntimeError(f"pytest JUnit {name} count is invalid: {path}")
            totals[name] += int(value)
    return totals


def _validate_artifact(payload: dict[str, Any], *, raw_log: Path, output: Path) -> None:
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
    expected_evidence = [str(raw_log.resolve())]
    steps = payload["metrics"].get("steps")
    if not isinstance(steps, list):
        raise RuntimeError("validation artifact steps are invalid")
    for step in steps:
        common_fields = {
            "name",
            "argv",
            "result_kind",
            "returncode",
            "elapsed_seconds",
        }
        if not isinstance(step, dict) or step.get("result_kind") not in {"command", "pytest"}:
            raise RuntimeError("validation artifact step identity is invalid")
        expected_fields = (
            common_fields
            if step["result_kind"] == "command"
            else common_fields | {"junit_xml", "tests", "failures", "errors", "skipped"}
        )
        if set(step) != expected_fields:
            raise RuntimeError("validation artifact step schema is invalid")
        if step["result_kind"] == "pytest":
            junit_xml = step["junit_xml"]
            if junit_xml is not None:
                if not isinstance(junit_xml, str):
                    raise RuntimeError("validation artifact JUnit path is invalid")
                expected_evidence.append(str(Path(junit_xml).resolve()))
    if evidence != expected_evidence or len(set(evidence)) != len(evidence):
        raise RuntimeError("validation artifact evidence ownership is invalid")
    for item in evidence:
        evidence_path = Path(item)
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
        or not steps
        or any(step.get("returncode") != 0 for step in steps)
        or any(
            step["result_kind"] == "pytest"
            and (
                step["junit_xml"] is None
                or step["tests"] < 1
                or step["failures"] != 0
                or step["errors"] != 0
                or step["skipped"] != 0
            )
            for step in steps
        )
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
    selected_steps = tuple(steps if steps is not None else default_steps())
    if not selected_steps or len({step.name for step in selected_steps}) != len(selected_steps):
        raise ValueError("validation steps must have unique names")
    junit_paths = {
        step.name: _junit_path(raw_log, step)
        for step in selected_steps
        if step.result_kind == "pytest"
    }
    if raw_log.exists() or output.exists() or any(path.exists() for path in junit_paths.values()):
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
            for step in selected_steps:
                junit_path = junit_paths.get(step.name)
                argv = step.argv if junit_path is None else (*step.argv, f"--junitxml={junit_path}")
                started = time.monotonic()
                completed = subprocess.run(
                    argv,
                    cwd=project_root,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                elapsed = time.monotonic() - started
                metric: dict[str, Any] = {
                    "name": step.name,
                    "argv": list(argv),
                    "result_kind": step.result_kind,
                    "returncode": completed.returncode,
                    "elapsed_seconds": elapsed,
                }
                junit_error: str | None = None
                if junit_path is not None:
                    metric["junit_xml"] = str(junit_path)
                    try:
                        metric.update(_parse_junit(junit_path))
                    except RuntimeError as exc:
                        junit_error = str(exc)
                        if not junit_path.is_file() or junit_path.is_symlink():
                            metric["junit_xml"] = None
                        metric.update(
                            {name: -1 for name in ("tests", "failures", "errors", "skipped")}
                        )
                metrics.append(metric)
                log_lines.extend(
                    (
                        f"step={step.name}",
                        f"argv={json.dumps(argv)}",
                        f"result_kind={step.result_kind}",
                        f"returncode={completed.returncode}",
                        f"elapsed_seconds={elapsed}",
                        completed.stdout,
                    )
                )
                if junit_path is not None:
                    log_lines.append(f"junit={json.dumps(metric, sort_keys=True)}")
                if completed.returncode != 0:
                    errors.append(
                        f"validation step failed: {step.name} (exit={completed.returncode})"
                    )
                    status = "FAIL"
                    break
                if junit_error is not None:
                    errors.append(junit_error)
                    status = "FAIL"
                    break
                if junit_path is not None and (
                    metric["tests"] < 1
                    or metric["failures"] != 0
                    or metric["errors"] != 0
                    or metric["skipped"] != 0
                ):
                    errors.append(
                        "pytest machine result is not an exact all-pass suite: "
                        f"{step.name} (tests={metric['tests']}, failures={metric['failures']}, "
                        f"errors={metric['errors']}, skipped={metric['skipped']})"
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
        "experiment_id": "plan03-1-current-suite",
        "requirements_covered": list(REQUIREMENTS),
        "source_identity": None if source_before is None else _source_projection(source_before),
        "config_schema_identity": None,
        "protocol_schema_identity": None,
        "environment": environment,
        "workload_identity": None,
        "metrics": {"steps": metrics},
        "errors": errors,
        "evidence_paths": [
            str(raw_log),
            *(
                step["junit_xml"]
                for step in metrics
                if step["result_kind"] == "pytest" and step["junit_xml"] is not None
            ),
        ],
        "cleanup": {"owner": "validation_suite", "eligible": False, "targets": []},
    }
    _validate_artifact(artifact, raw_log=raw_log, output=output)
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
