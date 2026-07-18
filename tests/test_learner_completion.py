import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.learner import (
    SyncerProgressWatchdog,
    confirm_syncer_unresponsive,
    stop_requested,
)
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths


def test_global_only_completion_ignores_local_horizon_until_stop_is_published(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml",
        run_id="global_only_completion",
        shared_root=str(tmp_path),
    )
    paths = RunPaths(tmp_path)

    assert not stop_requested(paths, 5000, config)
    assert not stop_requested(paths, 5100, config)

    paths.control.mkdir(parents=True, exist_ok=True)
    paths.stop_json.write_text("{}", encoding="utf-8")
    assert stop_requested(paths, 5100, config)


def test_default_completion_keeps_local_or_global_behavior(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="local_or_global_completion",
        shared_root=str(tmp_path),
    )
    paths = RunPaths(tmp_path)

    assert not stop_requested(paths, 7, config)
    assert stop_requested(paths, 8, config)


@pytest.mark.parametrize(
    ("completion_mode", "max_local_steps", "local_step", "has_stop", "expected"),
    [
        ("local_or_global", None, 100, True, True),
        ("local_or_global", 8, 7, False, False),
        ("local_or_global", 8, 8, False, True),
        ("local_or_global", 5000, 100, True, True),
        ("global_only", 8, 8, False, False),
        ("global_only", 8, 100, True, True),
    ],
)
def test_stop_requested_truth_table(
    tmp_path,
    completion_mode,
    max_local_steps,
    local_step,
    has_stop,
    expected,
):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="stop-truth-table",
        shared_root=str(tmp_path),
    )
    config.training.completion_mode = completion_mode
    config.training.max_local_steps = max_local_steps
    paths = RunPaths(tmp_path)
    if has_stop:
        paths.control.mkdir(parents=True, exist_ok=True)
        paths.stop_json.write_text("{}", encoding="utf-8")

    assert stop_requested(paths, local_step, config) is expected


def test_fragment_stop_checks_global_stop_before_local_horizon(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fragment-stop-regression",
        shared_root=str(tmp_path),
    )
    config.training.max_local_steps = 5000
    paths = RunPaths(tmp_path)
    paths.control.mkdir(parents=True, exist_ok=True)
    paths.stop_json.write_text('{"reason": "stop_after_outer_steps"}', encoding="utf-8")

    assert stop_requested(paths, 100, config)


def test_syncer_watchdog_refresh_and_deadline_boundaries():
    watchdog = SyncerProgressWatchdog.start(
        timeout_seconds=10.0,
        initial_version=3,
        now_monotonic=100.0,
        now_wall=1000.0,
    )

    assert not watchdog.observe(3, now_monotonic=105.0, now_wall=1005.0)
    assert not watchdog.deadline_reached(now_monotonic=109.999)
    assert watchdog.deadline_reached(now_monotonic=110.0)
    assert watchdog.observe(4, now_monotonic=110.0, now_wall=1010.0)
    assert watchdog.last_observed_version == 4
    assert not watchdog.deadline_reached(now_monotonic=119.999)
    assert watchdog.deadline_reached(now_monotonic=120.0)


@pytest.mark.parametrize(
    ("version_field", "latest_payload"),
    [
        ("version", {"version": 2}),
        ("global_merge_event", {"latest_kind": "fragment", "global_merge_event": 2}),
    ],
)
def test_syncer_watchdog_confirms_latest_before_triggering(
    tmp_path, version_field, latest_payload
):
    paths = RunPaths(tmp_path)
    paths.control.mkdir(parents=True, exist_ok=True)
    watchdog = SyncerProgressWatchdog.start(
        timeout_seconds=5.0,
        initial_version=1,
        now_monotonic=10.0,
        now_wall=100.0,
    )
    atomic_write_json(paths.latest_json, latest_payload)

    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field=version_field,
        now_monotonic=15.0,
        now_wall=105.0,
    )
    assert watchdog.last_observed_version == 2
    assert confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field=version_field,
        now_monotonic=20.0,
        now_wall=110.0,
    )
    atomic_write_json(paths.stop_json, {"reason": "target"})
    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field=version_field,
        now_monotonic=21.0,
        now_wall=111.0,
    )
