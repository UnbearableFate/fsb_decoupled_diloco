from __future__ import annotations

from pathlib import Path

import pytest

from fs_diloco.core.config import Config
from fs_diloco.tools import launch_independent_run


def _config(mode: str) -> Config:
    config = Config()
    config.model.name_or_path = "synthetic-tiny"
    config.data.dataset_name = "synthetic"
    config.run.run_id = "run"
    config.run.shared_root = "/runs/run"
    config.membership.mode = mode
    if mode == "dynamic":
        config.sync.num_learners = 4
        config.sync.quorum_min = 2
        config.sync.quorum_max = 4
        config.membership.stream_pool_size = 4
        config.membership.bootstrap_instances = 2
        config.scaling.desired_contributors = 3
        config.scaling.low_contributor_threshold = 2
    return config


@pytest.mark.parametrize("value", [None, "00:09:59", "invalid", "00:00:00"])
def test_submission_walltime_is_explicit_and_at_least_ten_minutes(value: str | None) -> None:
    with pytest.raises(ValueError):
        launch_independent_run._walltime_resource(value, required=True)


@pytest.mark.parametrize("mode", ["static", "dynamic"])
def test_launch_uses_one_syncer_and_one_learner_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    config = _config(mode)
    descriptor = {
        "shared_root": config.run.shared_root,
        "descriptor_sha256": "d" * 64,
    }
    monkeypatch.setattr(launch_independent_run, "resolve_config", lambda *args, **kwargs: config)
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
    )

    assert Path(result["syncer_qsub"][-1]).name == "run_syncer.pbs"
    assert {Path(command[-1]).name for command in result["learner_qsubs"]} == {
        "run_learner.pbs"
    }
