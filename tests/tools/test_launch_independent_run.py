"""Exercise independent Full Protocol initialization and PBS submission commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fs_diloco.core.config import Config
from fs_diloco.tools import launch_independent_run


def test_one_command_submission_wrapper_freezes_the_validated_actor_shape() -> None:
    """The login-node wrapper must retain one 8+1 debug launch with safe walltimes."""

    script = Path("scripts/miyabi/agent/submit_independent_8l1s_50x10.sh").read_text(
        encoding="utf-8"
    )

    assert 'LAUNCHER_QUEUE="debug-g"' in script
    assert 'ACTOR_QUEUE="debug-g"' in script
    assert 'LAUNCHER_WALLTIME="00:10:00"' in script
    assert 'SYNCER_WALLTIME="00:10:00"' in script
    assert 'LEARNER_WALLTIME="00:10:00"' in script
    assert "run_independent_launcher.pbs" in script


def _config() -> Config:
    """Return one four-stream config with two scalar bootstrap learners."""

    config = Config()
    config.model.name_or_path = "synthetic-tiny"
    config.data.dataset_name = "synthetic"
    config.run.run_id = "run"
    config.run.shared_root = "/runs/run"
    config.sync.quorum_min = 2
    config.sync.quorum_max = 4
    config.membership.stream_pool_size = 4
    config.membership.bootstrap_instances = 2
    return config


@pytest.mark.parametrize("value", [None, "00:09:59", "invalid", "00:00:00"])
def test_submission_walltime_is_explicit_and_at_least_ten_minutes(value: str | None) -> None:
    """Actor submission rejects missing, malformed, and sub-ten-minute walltimes."""

    with pytest.raises(ValueError):
        launch_independent_run._walltime_resource(value, required=True)


@pytest.mark.parametrize("value", [None, "", "bad queue", "-debug-g"])
def test_submission_queue_is_explicit_and_safe(value: str | None) -> None:
    """Actor submission requires a nonempty shell-safe queue name."""

    with pytest.raises(ValueError):
        launch_independent_run._queue_resource(value, required=True)


def test_launch_uses_one_syncer_and_scalar_learner_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch construction emits one syncer and one scalar job per bootstrap slot."""

    config = _config()
    descriptor = {
        "shared_root": config.run.shared_root,
        "descriptor_sha256": "d" * 64,
    }
    monkeypatch.setattr(launch_independent_run, "resolve_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        launch_independent_run,
        "bind_source_identity",
        lambda *_args, **_kwargs: {
            "git_commit": "a" * 40,
            "git_dirty": False,
            "source_fingerprint": "sha256:" + "b" * 64,
        },
    )
    monkeypatch.setattr(
        launch_independent_run,
        "initialize_run",
        lambda *args, **kwargs: {"descriptor": descriptor, "bootstrap": {}},
    )

    result = launch_independent_run.launch(
        config_path=tmp_path / "config.yaml",
        run_id="run",
        shared_root=config.run.shared_root,
        project_root=tmp_path,
        submit=False,
        allow_dirty_snapshot=False,
        actor_queue="debug-g",
    )

    assert Path(result["syncer_qsub"][-1]).name == "run_syncer.pbs"
    assert result["syncer_qsub"][result["syncer_qsub"].index("-q") + 1] == "debug-g"
    assert all(command[command.index("-q") + 1] == "debug-g" for command in result["learner_qsubs"])
    assert {Path(command[-1]).name for command in result["learner_qsubs"]} == {"run_learner.pbs"}
    assert len(result["learner_qsubs"]) == config.membership.bootstrap_instances
    variables = [command[command.index("-v") + 1] for command in result["learner_qsubs"]]
    assert {item.rsplit("BOOTSTRAP_SLOT=", 1)[1] for item in variables} == {
        str(index) for index in range(config.membership.bootstrap_instances)
    }
    assert all("-J" not in command and "-r" not in command for command in result["learner_qsubs"])


def test_submit_returns_every_scalar_actor_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitted run returns every independently accepted scalar actor job ID."""

    config = _config()
    descriptor = {
        "shared_root": config.run.shared_root,
        "descriptor_sha256": "d" * 64,
    }
    monkeypatch.setattr(launch_independent_run, "resolve_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        launch_independent_run,
        "bind_source_identity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        launch_independent_run,
        "initialize_run",
        lambda *args, **kwargs: {"descriptor": descriptor, "bootstrap": {}},
    )
    submitted: list[list[str]] = []

    def fake_qsub(command: list[str]) -> dict[str, object]:
        """Record one qsub command and assign a deterministic job ID."""

        submitted.append(command)
        return {"status": "submitted", "job_id": f"job-{len(submitted)}"}

    monkeypatch.setattr(launch_independent_run, "_qsub", fake_qsub)

    result = launch_independent_run.launch(
        config_path=tmp_path / "config.yaml",
        run_id="run",
        shared_root=config.run.shared_root,
        project_root=tmp_path,
        submit=True,
        allow_dirty_snapshot=False,
        syncer_walltime="00:10:00",
        learner_walltime="00:10:00",
        log_root=tmp_path / "logs",
        actor_queue="debug-g",
    )

    assert len(submitted) == 1 + config.membership.bootstrap_instances
    assert result["syncer_job_id"] == "job-1"
    assert result["actor_queue"] == "debug-g"
    assert all(command[command.index("-q") + 1] == "debug-g" for command in submitted)
    assert result["learner_job_ids"] == [
        f"job-{index}" for index in range(2, config.membership.bootstrap_instances + 2)
    ]
    receipt = json.loads((tmp_path / "logs/submission_receipt.json").read_text(encoding="utf-8"))
    assert receipt["submission_status"] == "submitted"
    assert len(tuple((tmp_path / "logs/submission_receipts").glob("*.json"))) == (
        1 + config.membership.bootstrap_instances
    )


def test_submission_receipts_are_create_only_and_preserve_partial_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create-once staged receipts preserve partial scheduler acceptance."""

    config = _config()
    descriptor = {
        "shared_root": config.run.shared_root,
        "descriptor_sha256": "d" * 64,
    }
    monkeypatch.setattr(launch_independent_run, "resolve_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        launch_independent_run, "bind_source_identity", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        launch_independent_run,
        "initialize_run",
        lambda *args, **kwargs: {"descriptor": descriptor, "bootstrap": {}},
    )
    call_count = 0

    def fake_qsub(command: list[str]) -> dict[str, object]:
        """Reject the second learner while assigning deterministic accepted IDs."""

        nonlocal call_count
        call_count += 1
        if call_count == 3:
            return {"status": "failed", "returncode": 1, "stderr": "fixture rejection"}
        return {"status": "submitted", "job_id": f"job-{call_count}"}

    monkeypatch.setattr(launch_independent_run, "_qsub", fake_qsub)

    result = launch_independent_run.launch(
        config_path=tmp_path / "config.yaml",
        run_id="run",
        shared_root=config.run.shared_root,
        project_root=tmp_path,
        submit=True,
        allow_dirty_snapshot=False,
        syncer_walltime="00:10:00",
        learner_walltime="00:10:00",
        log_root=tmp_path / "logs",
        actor_queue="debug-g",
    )

    assert result["submission_status"] == "partial"
    assert result["accepted_learner_job_ids"] == ["job-2"]
    stages = sorted((tmp_path / "logs/submission_receipts").glob("*.json"))
    assert [path.name for path in stages] == [
        "000-syncer.json",
        "001-learner_000.json",
        "002-learner_001.json",
    ]
    final_path = tmp_path / "logs/submission_receipt.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert final["submission_status"] == "partial"
    assert final["accepted_learner_job_ids"] == ["job-2"]
    with pytest.raises(FileExistsError):
        launch_independent_run._publish_submission_receipt(final_path, final)
    with pytest.raises(FileExistsError):
        launch_independent_run.launch(
            config_path=tmp_path / "config.yaml",
            run_id="run",
            shared_root=config.run.shared_root,
            project_root=tmp_path,
            submit=True,
            allow_dirty_snapshot=False,
            syncer_walltime="00:10:00",
            learner_walltime="00:10:00",
            log_root=tmp_path / "logs",
            actor_queue="debug-g",
        )
