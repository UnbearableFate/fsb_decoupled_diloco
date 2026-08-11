"""Exercise safe PBS command construction and scheduler observation parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fs_diloco.runtime.pbs_scheduler import PBSScheduler


def test_historical_request_scan_uses_qstat_history_and_matches_exact_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        if command == ["qstat-safe", "-H"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=("JOB_ID JOB_NAME STATUS\n122 other FINISH\n123 target FINISH\n"),
                stderr="",
            )
        assert command == ["qstat-safe", "-H", "-f", "123"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "Job Id: 123.opbs\n"
                "    job_state = F\n"
                "    Variable_List = FS_DILOCO_LAUNCH_REQUEST_ID=launch-exact,OTHER=value\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    observation = PBSScheduler(qstat_binary="qstat-safe").find_by_launch_request(
        "launch-exact", historical=True
    )

    assert commands == [
        ["qstat-safe", "-H"],
        ["qstat-safe", "-H", "-f", "123"],
    ]
    assert observation is not None
    assert observation.job_id == "123"
    assert observation.classification == "finished"


def test_request_scan_fails_closed_when_job_listing_is_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="job listing failed")

    monkeypatch.setattr(subprocess, "run", run)
    assert PBSScheduler().find_by_launch_request("launch-exact") is None


def test_submit_passes_exact_environment_as_one_argument_and_rejects_unsafe_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Learner submission must preserve exact variables and reject unsafe values."""

    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="456.opbs\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    scheduler = PBSScheduler(qsub_binary="qsub-safe")
    result = scheduler.submit_learner(
        script="scripts/miyabi/agent/run_learner.pbs",
        launch_request_id="launch-exact",
        stream_id=3,
        replace_instance_id="old-instance",
        shared_root=tmp_path,
        descriptor_sha256="d" * 64,
        walltime="00:10:00",
        queue="debug-g",
    )

    command = commands[0]
    variables = command[command.index("-v") + 1]
    assert command[0] == "qsub-safe"
    assert variables.split(",") == [
        f"FS_DILOCO_SHARED_ROOT={tmp_path.resolve()}",
        "FS_DILOCO_LAUNCH_REQUEST_ID=launch-exact",
        "FS_DILOCO_STREAM_ID=3",
        f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={'d' * 64}",
        "FS_DILOCO_REPLACE_INSTANCE_ID=old-instance",
    ]
    assert result["job_id_normalized"] == "456"

    with pytest.raises(ValueError, match="unsafe PBS variable"):
        scheduler.submit_learner(
            script="scripts/miyabi/agent/run_learner.pbs",
            launch_request_id="launch,unsafe",
            stream_id=3,
            replace_instance_id=None,
            shared_root=tmp_path,
            descriptor_sha256="d" * 64,
            walltime="00:10:00",
        )
