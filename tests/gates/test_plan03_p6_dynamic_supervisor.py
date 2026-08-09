from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HARNESS = PROJECT_ROOT / "scripts/miyabi/plan03_p6_dynamic_supervisor.py"


def _load_harness():
    specification = importlib.util.spec_from_file_location("plan03_p6_dynamic_supervisor", HARNESS)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_bootstrap_jobs_wait_at_pretorch_gate_without_early_loss(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_harness()
    commands: list[list[str]] = []

    def fake_run(command, *, environment=None):
        assert environment is None
        commands.append(command)
        return f"{1000 + len(commands)}.opbs"

    monkeypatch.setattr(module, "_run", fake_run)
    release = tmp_path / "release.json"
    duplicate = tmp_path / "duplicate.json"
    job_ids = module._qsub_learners(
        project_root=PROJECT_ROOT,
        shared_root=tmp_path / "run",
        descriptor={"descriptor_sha256": "a" * 64},
        duplicate_result=duplicate,
        bootstrap_release=release,
    )

    assert len(job_ids) == 8
    variables = [command[command.index("-v") + 1] for command in commands]
    assert all("FS_DILOCO_TEST_TERMINATE_AFTER_ADMISSION_SECONDS" not in row for row in variables)
    assert all(f"FS_DILOCO_TEST_PRETORCH_RELEASE_MARKER_PATH={release}" in row for row in variables)
    assert "FS_DILOCO_TEST_SPAWN_DUPLICATE_PRETORCH=1" in variables[1]
    assert all(
        "FS_DILOCO_TEST_SPAWN_DUPLICATE_PRETORCH" not in row
        for index, row in enumerate(variables)
        if index != 1
    )


def test_bootstrap_admission_join_normalizes_full_pbs_ids(tmp_path: Path) -> None:
    module = _load_harness()
    database = tmp_path / "authority.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE learner_instances ("
            "instance_id TEXT, pbs_job_id TEXT, status TEXT, stream_id INTEGER, "
            "stream_epoch INTEGER, admitted_at REAL, launch_request_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO learner_instances VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("bootstrap-0", "100.opbs", "admitted", 0, 1, 1.0, None),
                ("bootstrap-1", "101.opbs", "admitted", 1, 1, 1.0, None),
                ("replacement", "102.opbs", "admitted", 0, 2, 2.0, "launch-1"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    admissions = module._bootstrap_admissions(database, ["100", "101"])

    assert set(admissions) == {"100", "101"}
    assert {row["instance_id"] for row in admissions.values()} == {
        "bootstrap-0",
        "bootstrap-1",
    }
