import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.outer_optim import outer_optimizer_step
from fs_diloco.modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
)
from fs_diloco.runtime.learner import (
    predict_next_global_weight,
    rebase_local_delta_onto_global,
    wait_for_latest_if_newer,
)
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.tensor_codec import save_global_weights, save_outer_state


def test_rebase_preserves_only_progress_after_each_published_reference(tmp_path):
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    initial = flatten_trainable_params(model, index)
    first_delta = torch.linspace(0.0, 1.0e-3, initial.numel())
    load_flat_into_model(model, initial + first_delta, index)

    global_v1 = initial - 0.25
    global_v1_path = tmp_path / "global_v1.safetensors"
    save_global_weights(global_v1_path, global_v1, index)
    version, delta_norm = rebase_local_delta_onto_global(
        model=model,
        latest={"version": 1, "weight_path": str(global_v1_path)},
        param_index=index,
        device=torch.device("cpu"),
        reference_flat=initial,
    )

    assert version == 1
    assert torch.allclose(flatten_trainable_params(model, index), global_v1 + first_delta)
    assert delta_norm == pytest.approx(
        torch.linalg.vector_norm(first_delta).item(), rel=1.0e-5, abs=1.0e-8
    )

    published_reference = flatten_trainable_params(model, index)
    second_delta = torch.full_like(first_delta, 2.0e-4)
    load_flat_into_model(model, published_reference + second_delta, index)
    global_v2 = initial + 0.5
    global_v2_path = tmp_path / "global_v2.safetensors"
    save_global_weights(global_v2_path, global_v2, index)
    version, _ = rebase_local_delta_onto_global(
        model=model,
        latest={"version": 2, "weight_path": str(global_v2_path)},
        param_index=index,
        device=torch.device("cpu"),
        reference_flat=published_reference,
    )

    assert version == 2
    assert torch.allclose(
        flatten_trainable_params(model, index),
        global_v2 + second_delta,
        atol=1.0e-6,
        rtol=1.0e-5,
    )


def test_rebase_rejects_reference_with_wrong_size(tmp_path):
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    flat = flatten_trainable_params(model, index)
    path = tmp_path / "global.safetensors"
    save_global_weights(path, flat, index)

    with pytest.raises(ValueError, match="rebase reference"):
        rebase_local_delta_onto_global(
            model=model,
            latest={"version": 1, "weight_path": str(path)},
            param_index=index,
            device=torch.device("cpu"),
            reference_flat=flat[:-1],
        )


def test_post_publish_wait_polls_until_newer_latest(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path / "run")
    prepare_run_dirs(paths, 1)
    reads = iter([None, None, {"version": 2}])
    clock = [10.0]

    monkeypatch.setattr(
        "fs_diloco.runtime.learner.read_latest_if_newer",
        lambda *_args, **_kwargs: next(reads),
    )
    monkeypatch.setattr("fs_diloco.runtime.learner.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    latest, waited_seconds = wait_for_latest_if_newer(
        paths,
        1,
        wait_seconds=2.5,
        poll_seconds=0.2,
    )

    assert latest == {"version": 2}
    assert waited_seconds == pytest.approx(0.4)


def test_post_publish_wait_stops_at_deadline(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path / "run")
    prepare_run_dirs(paths, 1)
    clock = [10.0]

    monkeypatch.setattr(
        "fs_diloco.runtime.learner.read_latest_if_newer",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("fs_diloco.runtime.learner.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    latest, waited_seconds = wait_for_latest_if_newer(
        paths,
        1,
        wait_seconds=2.5,
        poll_seconds=0.2,
    )

    assert latest is None
    assert waited_seconds == pytest.approx(2.5)


def test_predict_next_global_uses_token_weighted_delta_and_outer_step(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml", num_learners=2)
    config.learner.global_adoption_strategy = "predict_post_publish_global"
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    global_flat = flatten_trainable_params(model, index)
    local_delta = torch.full_like(global_flat, 0.1)
    load_flat_into_model(model, global_flat + local_delta, index)
    momentum = torch.full_like(global_flat, 0.2)
    outer_state = {"step": torch.tensor(3), "momentum": momentum}
    weight_path = tmp_path / "global.safetensors"
    outer_path = tmp_path / "outer.safetensors"
    save_global_weights(weight_path, global_flat, index)
    save_outer_state(outer_path, global_flat, outer_state)
    latest = {
        "version": 4,
        "weight_path": str(weight_path),
        "optim_path": str(outer_path),
        "total_update_tokens": 100,
    }

    predicted, stats = predict_next_global_weight(
        model=model,
        latest=latest,
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=25,
    )

    historical_delta = momentum * -(1.0 - config.outer_optimizer.momentum)
    predicted_aggregate_delta = historical_delta * 0.75 + local_delta * 0.25
    expected, _ = outer_optimizer_step(
        global_flat,
        -predicted_aggregate_delta,
        outer_state,
        config.outer_optimizer,
    )
    assert torch.allclose(predicted, expected)
    assert torch.allclose(flatten_trainable_params(model, index), expected)
    assert stats["base_version"] == 4
    assert stats["predicted_version"] == 5
    assert stats["local_weight"] == pytest.approx(0.25)
    assert stats["estimated_total_tokens"] == 100
    assert stats["bootstrapped_total_tokens"] is False


def test_predict_next_global_bootstraps_zero_token_initial_checkpoint(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml", num_learners=2)
    config.sync.quorum_min = 2
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    global_flat = flatten_trainable_params(model, index)
    outer_state = {
        "step": torch.tensor(0),
        "momentum": torch.zeros_like(global_flat),
    }
    weight_path = tmp_path / "global.safetensors"
    outer_path = tmp_path / "outer.safetensors"
    save_global_weights(weight_path, global_flat, index)
    save_outer_state(outer_path, global_flat, outer_state)
    load_flat_into_model(model, global_flat + 0.1, index)

    _predicted, stats = predict_next_global_weight(
        model=model,
        latest={
            "version": 0,
            "weight_path": str(weight_path),
            "optim_path": str(outer_path),
            "total_update_tokens": 0,
        },
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=32,
    )

    assert stats["bootstrapped_total_tokens"] is True
    assert stats["estimated_total_tokens"] == 64
    assert stats["local_weight"] == pytest.approx(0.5)
