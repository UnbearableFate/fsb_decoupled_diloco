import pytest
import torch

from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
)
from fs_diloco.runtime.learner import adopt_global, load_or_refresh_latest
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.tensor_codec import save_global_weights


def test_load_or_refresh_latest_retries_whole_callback_on_newer_pointer(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    old_latest = {"version": 1, "weight_path": "collected-v1"}
    new_latest = {"version": 2, "weight_path": "present-v2"}
    atomic_write_json(paths.latest_json, new_latest)
    attempts = []

    def load(candidate):
        attempts.append(candidate["version"])
        if candidate["version"] == 1:
            raise FileNotFoundError(candidate["weight_path"])
        return f"loaded-v{candidate['version']}"

    result = load_or_refresh_latest(
        paths=paths,
        latest=old_latest,
        version_field="version",
        load_fn=load,
        wait_seconds=0.0,
        poll_seconds=0.01,
    )

    assert result.value == "loaded-v2"
    assert result.latest == new_latest
    assert result.retry_count == 1
    assert result.missing_path == "collected-v1"
    assert attempts == [1, 2]


def test_load_or_refresh_latest_exhaustion_preserves_file_error_chain(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    latest = {"version": 1, "weight_path": "collected-v1"}
    atomic_write_json(paths.latest_json, latest)

    with pytest.raises(FileNotFoundError) as exc_info:
        load_or_refresh_latest(
            paths=paths,
            latest=latest,
            version_field="version",
            load_fn=lambda candidate: (_ for _ in ()).throw(
                FileNotFoundError(candidate["weight_path"])
            ),
            wait_seconds=0.0,
            poll_seconds=0.01,
        )

    assert "collected-v1" in str(exc_info.value)
    assert any("latest load retries exhausted" in note for note in exc_info.value.__notes__)


def test_full_initial_style_adoption_loads_actual_recovered_version(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    source = TinyCausalLM(vocab_size=16, hidden_size=8)
    target = TinyCausalLM(vocab_size=16, hidden_size=8)
    param_index = build_param_index(source, model_name_or_path="synthetic-tiny")
    expected = flatten_trainable_params(source, param_index) + 0.5
    load_flat_into_model(source, expected, param_index)
    valid_path = tmp_path / "global_v2.safetensors"
    save_global_weights(valid_path, expected, param_index)
    old_latest = {"version": 1, "weight_path": str(tmp_path / "collected-v1")}
    new_latest = {"version": 2, "weight_path": str(valid_path)}
    atomic_write_json(paths.latest_json, new_latest)

    result = load_or_refresh_latest(
        paths=paths,
        latest=old_latest,
        version_field="version",
        load_fn=lambda candidate: adopt_global(
            model=target,
            latest=candidate,
            param_index=param_index,
            device=torch.device("cpu"),
        ),
        wait_seconds=0.0,
        poll_seconds=0.01,
    )

    assert result.value == 2
    assert result.latest == new_latest
    assert torch.equal(flatten_trainable_params(target, param_index), expected)
