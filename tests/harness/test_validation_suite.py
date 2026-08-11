"""Verify the one-node validation runner and its evidence contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/miyabi/agent/run_validation_suite.py"


def _module():
    specification = importlib.util.spec_from_file_location("run_validation_suite", RUNNER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _identity(*, dirty: bool = False) -> dict[str, object]:
    return {
        "git_commit": "1" * 40,
        "git_dirty": dirty,
        "source_scopes": ["fs_diloco", "tests"],
        "source_fingerprint": "sha256:" + "2" * 64,
    }


def _environment() -> dict[str, object]:
    return {
        "interpreter": {"executable": sys.executable, "version": sys.version},
        "packages": {"pytest": "test", "pytest-timeout": "test", "ruff": "test", "torch": "test"},
        "pbs_job_id": "fixture.opbs",
        "nodes": ["mg0001"],
        "topology": {"validation_runner": "mg0001"},
    }


def test_validation_runner_publishes_create_only_schema_complete_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    identity = _identity()
    monkeypatch.setattr(module, "capture_source_identity", lambda _root: identity.copy())
    monkeypatch.setattr(module, "_environment", _environment)
    raw_log = tmp_path / "validation.log"
    output = tmp_path / "validation.json"
    steps = (
        module.ValidationStep(
            "probe",
            (sys.executable, "-c", "print('validation-probe-pass')"),
            "command",
        ),
    )

    artifact = module.run_validation(
        project_root=ROOT,
        raw_log=raw_log,
        output=output,
        steps=steps,
    )

    assert artifact == json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "PASS"
    assert artifact["requirements_covered"] == ["UNIT-01", "HARNESS-01"]
    assert artifact["source_identity"]["fingerprint"] == identity["source_fingerprint"]
    assert artifact["metrics"]["steps"][0]["argv"] == list(steps[0].argv)
    assert artifact["metrics"]["steps"][0]["result_kind"] == "command"
    assert artifact["metrics"]["steps"][0]["returncode"] == 0
    assert "validation-probe-pass" in raw_log.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="create-only"):
        module.run_validation(
            project_root=ROOT,
            raw_log=raw_log,
            output=output,
            steps=steps,
        )


def test_validation_runner_classifies_command_failure_as_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "capture_source_identity", lambda _root: _identity())
    monkeypatch.setattr(module, "_environment", _environment)

    artifact = module.run_validation(
        project_root=ROOT,
        raw_log=tmp_path / "validation.log",
        output=tmp_path / "validation.json",
        steps=(
            module.ValidationStep("red", (sys.executable, "-c", "raise SystemExit(7)"), "command"),
        ),
    )

    assert artifact["status"] == "FAIL"
    assert artifact["metrics"]["steps"][0]["returncode"] == 7
    assert artifact["errors"] == ["validation step failed: red (exit=7)"]


def test_validation_runner_blocks_dirty_source_without_running_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "capture_source_identity", lambda _root: _identity(dirty=True))
    monkeypatch.setattr(module, "_environment", _environment)

    artifact = module.run_validation(
        project_root=ROOT,
        raw_log=tmp_path / "validation.log",
        output=tmp_path / "validation.json",
        steps=(
            module.ValidationStep(
                "must-not-run",
                (sys.executable, "-c", "raise AssertionError('unexpected execution')"),
                "command",
            ),
        ),
    )

    assert artifact["status"] == "BLOCKED"
    assert artifact["metrics"] == {"steps": []}
    assert artifact["errors"] == ["validation source scopes are dirty"]


def test_validation_runner_rejects_duplicate_evidence_step_names(tmp_path: Path) -> None:
    module = _module()
    step = module.ValidationStep("duplicate", (sys.executable, "--version"), "command")

    with pytest.raises(ValueError, match="unique names"):
        module.run_validation(
            project_root=ROOT,
            raw_log=tmp_path / "validation.log",
            output=tmp_path / "validation.json",
            steps=(step, step),
        )


def test_validation_runner_records_machine_checkable_pytest_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "capture_source_identity", lambda _root: _identity())
    monkeypatch.setattr(module, "_environment", _environment)
    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    step = module.ValidationStep(
        "pytest-probe",
        (sys.executable, "-m", "pytest", "-q", str(probe)),
        "pytest",
    )

    artifact = module.run_validation(
        project_root=ROOT,
        raw_log=tmp_path / "validation.log",
        output=tmp_path / "validation.json",
        steps=(step,),
    )

    metric = artifact["metrics"]["steps"][0]
    assert artifact["status"] == "PASS"
    assert metric["result_kind"] == "pytest"
    assert metric["tests"] == 1
    assert metric["failures"] == metric["errors"] == metric["skipped"] == 0
    assert artifact["evidence_paths"] == [
        str((tmp_path / "validation.log").resolve()),
        metric["junit_xml"],
    ]


@pytest.mark.parametrize(("tests", "skipped"), [(0, 0), (1, 1)])
def test_validation_runner_rejects_zero_or_skipped_pytest_with_zero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tests: int,
    skipped: int,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "capture_source_identity", lambda _root: _identity())
    monkeypatch.setattr(module, "_environment", _environment)

    def fake_run(argv, **_kwargs):
        junit_argument = next(argument for argument in argv if argument.startswith("--junitxml="))
        junit_path = Path(junit_argument.split("=", 1)[1])
        junit_path.write_text(
            "<testsuites><testsuite "
            f'errors="0" failures="0" skipped="{skipped}" tests="{tests}"/>'
            "</testsuites>\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="synthetic pytest\n")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    artifact = module.run_validation(
        project_root=ROOT,
        raw_log=tmp_path / "validation.log",
        output=tmp_path / "validation.json",
        steps=(
            module.ValidationStep(
                "pytest-probe",
                (sys.executable, "-m", "pytest", "-q"),
                "pytest",
            ),
        ),
    )

    assert artifact["status"] == "FAIL"
    assert artifact["metrics"]["steps"][0]["returncode"] == 0
    assert artifact["errors"] == [
        "pytest machine result is not an exact all-pass suite: "
        f"pytest-probe (tests={tests}, failures=0, errors=0, skipped={skipped})"
    ]
