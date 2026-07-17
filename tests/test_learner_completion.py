import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.learner import stop_requested
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
