from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.learner import fragment_stop_requested, stop_requested
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
    assert not fragment_stop_requested(paths, 5100, config)

    paths.control.mkdir(parents=True, exist_ok=True)
    paths.stop_json.write_text("{}", encoding="utf-8")
    assert stop_requested(paths, 5100, config)
    assert fragment_stop_requested(paths, 5100, config)


def test_default_completion_keeps_local_or_global_behavior(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="local_or_global_completion",
        shared_root=str(tmp_path),
    )
    paths = RunPaths(tmp_path)

    assert not stop_requested(paths, 7, config)
    assert stop_requested(paths, 8, config)
