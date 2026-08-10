from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/miyabi/run_validation_suite.py"


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
        steps=(module.ValidationStep("red", (sys.executable, "-c", "raise SystemExit(7)")),),
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
            ),
        ),
    )

    assert artifact["status"] == "BLOCKED"
    assert artifact["metrics"] == {"steps": []}
    assert artifact["errors"] == ["validation source scopes are dirty"]
