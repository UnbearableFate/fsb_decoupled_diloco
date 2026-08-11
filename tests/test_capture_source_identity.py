"""Verify canonical source identity capture across tracked runtime scopes."""

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "miyabi" / "agent" / "capture_source_identity.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _capture(repo: Path, suffix: str) -> dict:
    output_json = repo / f"identity-{suffix}.json"
    output_env = repo / f"identity-{suffix}.env"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-root",
            str(repo),
            "--output-json",
            str(output_json),
            "--output-env",
            str(output_env),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    env_text = output_env.read_text(encoding="utf-8")
    assert f"FS_DILOCO_GIT_COMMIT={payload['git_commit']}" in env_text
    assert f"FS_DILOCO_SOURCE_FINGERPRINT={payload['source_fingerprint']}" in env_text
    return payload


def test_capture_source_identity_hashes_tracked_and_untracked_runtime_sources(tmp_path):
    """Runtime source changes must alter identity while evidence-only changes do not."""

    repo = tmp_path / "repo"
    (repo / "fs_diloco").mkdir(parents=True)
    (repo / "configs").mkdir()
    (repo / "docs").mkdir()
    (repo / "scripts" / "miyabi" / "agent").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "fs_diloco" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "configs" / "run.yaml").write_text("run: {}\n", encoding="utf-8")
    (repo / "docs" / "contract.md").write_text("# Contract\n", encoding="utf-8")
    (repo / "tests" / "test_module.py").write_text("def test_value(): pass\n", encoding="utf-8")
    (repo / "scripts" / "miyabi" / "agent" / "run.pbs").write_text(
        "#!/bin/bash\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Project\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (repo / ".gitignore").write_text("uv.lock\n", encoding="utf-8")
    (repo / "uv.lock").write_text("dependency = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    _git(
        repo,
        "add",
        ".gitignore",
        "fs_diloco/module.py",
        "configs/run.yaml",
        "docs/contract.md",
        "tests/test_module.py",
        "scripts/miyabi/agent/run.pbs",
        "README.md",
        "pyproject.toml",
    )
    _git(repo, "commit", "-qm", "initial")

    clean = _capture(repo, "clean")
    assert clean["git_dirty"] is False
    assert clean["source_fingerprint"].startswith("sha256:")
    assert {entry["path"] for entry in clean["source_files"]} == {
        "configs/run.yaml",
        "docs/contract.md",
        "fs_diloco/module.py",
        "pyproject.toml",
        "README.md",
        "scripts/miyabi/agent/run.pbs",
        "tests/test_module.py",
    }

    (repo / "reports").mkdir()
    (repo / "reports" / "review.md").write_text("review evidence\n", encoding="utf-8")
    out_of_scope = _capture(repo, "out-of-scope")
    assert out_of_scope["git_dirty"] is False
    assert out_of_scope["source_fingerprint"] == clean["source_fingerprint"]

    (repo / "tests" / "test_module.py").write_text(
        "def test_value(): assert False\n", encoding="utf-8"
    )
    dirty_test = _capture(repo, "dirty-test")
    assert dirty_test["git_dirty"] is True
    assert dirty_test["source_fingerprint"] != clean["source_fingerprint"]
    _git(repo, "restore", "tests/test_module.py")

    (repo / "docs" / "contract.md").write_text("# Changed contract\n", encoding="utf-8")
    dirty_docs = _capture(repo, "dirty-docs")
    assert dirty_docs["git_dirty"] is True
    assert dirty_docs["source_fingerprint"] != clean["source_fingerprint"]
    _git(repo, "restore", "docs/contract.md")

    (repo / "fs_diloco" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracked_edit = _capture(repo, "tracked")
    assert tracked_edit["git_dirty"] is True
    assert tracked_edit["git_commit"] == clean["git_commit"]
    assert tracked_edit["source_fingerprint"] != clean["source_fingerprint"]

    (repo / "fs_diloco" / "new_module.py").write_text("NEW = 1\n", encoding="utf-8")
    untracked_edit = _capture(repo, "untracked")
    assert untracked_edit["source_fingerprint"] != tracked_edit["source_fingerprint"]
    assert "fs_diloco/new_module.py" in {entry["path"] for entry in untracked_edit["source_files"]}

    (repo / "uv.lock").write_text("dependency = 2\n", encoding="utf-8")
    ignored_lock_edit = _capture(repo, "ignored-lock")
    assert ignored_lock_edit["source_fingerprint"] == untracked_edit["source_fingerprint"]
