import json

import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.outer_optim import outer_optimizer_step
from fs_diloco.modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
    save_param_index,
)
from fs_diloco.runtime import learner as learner_runtime
from fs_diloco.runtime.learner import (
    PredictionState,
    adopt_global,
    choose_learner_compute_placement,
    prepare_prediction_or_find_newer_latest,
    predict_next_global_weight,
    rebase_local_delta_onto_global,
    reconcile_prediction,
    wait_for_latest_if_newer,
)
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.tensor_codec import save_global_weights, save_outer_state


def test_adopt_global_loads_bfloat16_checkpoint_without_fp32_flattening(
    tmp_path, monkeypatch
):
    source_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    param_index = build_param_index(source_model, model_name_or_path="synthetic-tiny")
    source_flat = flatten_trainable_params(
        source_model,
        param_index,
        dtype=torch.bfloat16,
    )
    weight_path = tmp_path / "global.safetensors"
    save_global_weights(weight_path, source_flat, param_index, dtype=torch.bfloat16)

    target_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    monkeypatch.setattr(
        learner_runtime,
        "load_global_weights_flat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct adoption must not build an FP32 flat checkpoint")
        ),
    )

    version = adopt_global(
        model=target_model,
        latest={"version": 7, "weight_path": str(weight_path)},
        param_index=param_index,
        device=torch.device("cpu"),
    )

    assert version == 7
    assert torch.equal(
        flatten_trainable_params(target_model, param_index, dtype=torch.bfloat16),
        source_flat,
    )


def test_rebase_preserves_only_progress_after_each_published_reference(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    initial = flatten_trainable_params(model, index)
    first_delta = torch.linspace(0.0, 1.0e-3, initial.numel())
    load_flat_into_model(model, initial + first_delta, index)

    global_v1 = initial - 0.25
    global_v1_path = tmp_path / "global_v1.safetensors"
    save_global_weights(global_v1_path, global_v1, index)
    version, delta_norm, compute_stats = rebase_local_delta_onto_global(
        model=model,
        latest={"version": 1, "weight_path": str(global_v1_path)},
        param_index=index,
        device=torch.device("cpu"),
        reference_flat=initial,
        config=config,
    )

    assert version == 1
    assert torch.allclose(flatten_trainable_params(model, index), global_v1 + first_delta)
    assert delta_norm == pytest.approx(
        torch.linalg.vector_norm(first_delta).item(), rel=1.0e-5, abs=1.0e-8
    )
    assert compute_stats["reconcile_compute_dtype"] == "float32"
    assert compute_stats["reconcile_compute_device"] == "cpu"

    published_reference = flatten_trainable_params(model, index)
    second_delta = torch.full_like(first_delta, 2.0e-4)
    load_flat_into_model(model, published_reference + second_delta, index)
    global_v2 = initial + 0.5
    global_v2_path = tmp_path / "global_v2.safetensors"
    save_global_weights(global_v2_path, global_v2, index)
    version, _, _compute_stats = rebase_local_delta_onto_global(
        model=model,
        latest={"version": 2, "weight_path": str(global_v2_path)},
        param_index=index,
        device=torch.device("cpu"),
        reference_flat=published_reference,
        config=config,
    )

    assert version == 2
    assert torch.allclose(
        flatten_trainable_params(model, index),
        global_v2 + second_delta,
        atol=1.0e-6,
        rtol=1.0e-5,
    )


def test_rebase_rejects_reference_with_wrong_size(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
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
            config=config,
        )


@pytest.mark.parametrize("waited_seconds", [None, 0.25])
def test_prediction_reconcile_helper_clears_state_and_preserves_event_shape(
    monkeypatch, waited_seconds
):
    reference = torch.arange(4, dtype=torch.float32)
    state = PredictionState(
        reference_flat=reference,
        carried_tokens=96,
        update_id="learner_000_00000002_deadbeef",
        base_version=3,
    )
    events = []

    class RecordingLogger:
        def event(self, event_type, **payload):
            events.append((event_type, payload))

    monkeypatch.setattr(
        learner_runtime,
        "rebase_local_delta_onto_global",
        lambda **_kwargs: (
            4,
            1.5,
            {
                "reconcile_compute_dtype": "float32",
                "reconcile_compute_device": "cpu",
            },
        ),
    )

    result = reconcile_prediction(
        model=object(),
        latest={"version": 4, "weight_path": "unused"},
        param_index={"total_numel": 4},
        device=torch.device("cpu"),
        config=resolve_config("configs/fs_diloco_tiny_predict_local.yaml"),
        logger=RecordingLogger(),
        state=state,
        previous_version=3,
        waited_seconds=waited_seconds,
    )

    assert result.version == 4
    assert result.tokens_since_global_load == 96
    assert result.state == PredictionState()
    assert events[0][0] == "global_prediction_reconciled"
    payload = events[0][1]
    assert payload["previous_version"] == 3
    assert payload["version"] == 4
    assert payload["prediction_base_version"] == 3
    assert payload["prediction_update_id"] == state.update_id
    assert payload["carried_delta_tokens"] == 96
    assert payload["post_prediction_delta_norm"] == 1.5
    if waited_seconds is None:
        assert "reconcile_waited_seconds" not in payload
    else:
        assert payload["reconcile_waited_seconds"] == waited_seconds


def test_prediction_state_rejects_partial_active_state():
    state = PredictionState(update_id="orphan")

    with pytest.raises(RuntimeError, match="incomplete"):
        state.require_active()


def _prepare_prediction_run(tmp_path):
    run_root = tmp_path / "run"
    config = resolve_config(
        "configs/fs_diloco_tiny_predict_local.yaml",
        run_id="prediction-state-test",
        shared_root=str(run_root),
        num_learners=1,
    )
    config.training.inner_steps = 2
    config.training.max_local_steps = 4
    paths = RunPaths(run_root)
    prepare_run_dirs(paths, 1)
    torch.manual_seed(config.training.seed)
    initial_model = TinyCausalLM(
        vocab_size=config.model.synthetic_vocab_size,
        hidden_size=config.model.synthetic_hidden_size,
    )
    param_index = build_param_index(initial_model, model_name_or_path="synthetic-tiny")
    initial_flat = flatten_trainable_params(initial_model, param_index)
    weight_path = paths.weights / "global_v000000.safetensors"
    save_global_weights(weight_path, initial_flat, param_index)
    save_param_index(param_index, paths.param_index_json)
    atomic_write_json(paths.latest_json, {"version": 0, "weight_path": str(weight_path)})
    return config, paths


def _mock_prediction_snapshot(**kwargs):
    reference = flatten_trainable_params(kwargs["model"], kwargs["param_index"])
    return reference, {"prediction_compute_device": "cpu"}, None, None


def test_prediction_reconcile_timeout_does_not_emit_partial_reconcile(tmp_path, monkeypatch):
    config, paths = _prepare_prediction_run(tmp_path)
    monkeypatch.setattr(learner_runtime, "choose_device", lambda: torch.device("cpu"))
    monkeypatch.setattr(learner_runtime, "read_latest_if_newer", lambda *_args: None)
    monkeypatch.setattr(
        learner_runtime,
        "prepare_prediction_or_find_newer_latest",
        _mock_prediction_snapshot,
    )
    monkeypatch.setattr(
        learner_runtime,
        "wait_for_latest_if_newer",
        lambda *_args, **_kwargs: (None, 5.0),
    )

    with pytest.raises(TimeoutError, match="timed out waiting to reconcile"):
        learner_runtime.run_learner(config, "learner_000")

    events = [
        json.loads(line) for line in (paths.logs / "learner_000.jsonl").read_text().splitlines()
    ]
    event_types = [event["event_type"] for event in events]
    assert "global_prediction_started" in event_types
    assert "global_prediction_reconcile_wait_started" in event_types
    assert "global_prediction_reconciled" not in event_types


def test_prediction_is_abandoned_when_stop_arrives_during_inner_steps(tmp_path, monkeypatch):
    config, paths = _prepare_prediction_run(tmp_path)
    monkeypatch.setattr(learner_runtime, "choose_device", lambda: torch.device("cpu"))
    monkeypatch.setattr(learner_runtime, "read_latest_if_newer", lambda *_args: None)
    monkeypatch.setattr(
        learner_runtime,
        "prepare_prediction_or_find_newer_latest",
        _mock_prediction_snapshot,
    )
    original_train_one_step = learner_runtime.train_one_step
    calls = 0

    def stop_during_second_cycle(*args, **kwargs):
        nonlocal calls
        result = original_train_one_step(*args, **kwargs)
        calls += 1
        if calls == 3:
            atomic_write_json(paths.stop_json, {"reason": "injected_stop"})
        return result

    monkeypatch.setattr(learner_runtime, "train_one_step", stop_during_second_cycle)

    learner_runtime.run_learner(config, "learner_000")

    events = [
        json.loads(line) for line in (paths.logs / "learner_000.jsonl").read_text().splitlines()
    ]
    abandoned = [
        event
        for event in events
        if event["event_type"] == "global_prediction_abandoned_on_stop"
    ]
    assert len(abandoned) == 1
    assert abandoned[0]["prediction_base_version"] == 0
    assert abandoned[0]["carried_delta_tokens"] > 0
    assert not any(event["event_type"] == "global_prediction_reconciled" for event in events)


def test_reconcile_uses_configured_bfloat16_compute_dtype(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_syncer_bf16_gpu.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    reference = flatten_trainable_params(model, index, dtype=torch.bfloat16)
    local_delta = torch.full_like(reference, 0.125)
    load_flat_into_model(model, reference + local_delta, index)
    realized_local = flatten_trainable_params(model, index, dtype=torch.bfloat16)
    realized_delta = realized_local - reference
    new_global = reference - 0.25
    weight_path = tmp_path / "global_bf16.safetensors"
    save_global_weights(weight_path, new_global, index, dtype=torch.bfloat16)

    version, delta_norm, compute_stats = rebase_local_delta_onto_global(
        model=model,
        latest={"version": 3, "weight_path": str(weight_path)},
        param_index=index,
        device=torch.device("cpu"),
        reference_flat=reference,
        config=config,
    )

    assert version == 3
    assert compute_stats["reconcile_compute_dtype"] == "bfloat16"
    assert compute_stats["reconcile_compute_device"] == "cpu"
    assert delta_norm == pytest.approx(
        torch.linalg.vector_norm(realized_delta, ord=2, dtype=torch.float32).item()
    )
    assert torch.equal(
        flatten_trainable_params(model, index, dtype=torch.bfloat16),
        new_global + realized_delta,
    )


def test_rebase_reconciliation_preserves_optimizer_and_scheduler_state(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="rebase-state-test",
        shared_root=str(run_root),
        num_learners=1,
    )
    config.training.inner_steps = 2
    config.training.max_local_steps = 3
    config.inner_optimizer.scheduler = "cosine"
    config.inner_optimizer.warmup_steps = 2
    config.learner.poll_latest_during_inner_steps = True
    config.learner.adopt_global_after_upload = True
    config.learner.global_adoption_strategy = "rebase_post_publish_delta"

    paths = RunPaths(run_root)
    prepare_run_dirs(paths, config.sync.num_learners)
    torch.manual_seed(config.training.seed)
    initial_model = TinyCausalLM(
        vocab_size=config.model.synthetic_vocab_size,
        hidden_size=config.model.synthetic_hidden_size,
    )
    param_index = build_param_index(initial_model, model_name_or_path="synthetic-tiny")
    initial_flat = flatten_trainable_params(initial_model, param_index)
    v0_path = paths.weights / "global_v000000.safetensors"
    v1_path = paths.weights / "global_v000001.safetensors"
    save_global_weights(v0_path, initial_flat, param_index)
    save_global_weights(v1_path, initial_flat + 0.05, param_index)
    save_param_index(param_index, paths.param_index_json)
    atomic_write_json(
        paths.latest_json,
        {"version": 0, "weight_path": str(v0_path)},
    )
    latest_v1 = {"version": 1, "weight_path": str(v1_path)}

    original_builder = learner_runtime.build_inner_optimizer_and_scheduler
    built_states = []

    def tracked_builder(model, resolved_config):
        state = original_builder(model, resolved_config)
        built_states.append(state)
        return state

    latest_reads = iter([None, latest_v1, None])
    monkeypatch.setattr(learner_runtime, "choose_device", lambda: torch.device("cpu"))
    monkeypatch.setattr(
        learner_runtime,
        "build_inner_optimizer_and_scheduler",
        tracked_builder,
    )
    monkeypatch.setattr(
        learner_runtime,
        "read_latest_if_newer",
        lambda *_args, **_kwargs: next(latest_reads),
    )

    learner_runtime.run_learner(config, "learner_000")

    assert len(built_states) == 1
    optimizer, scheduler = built_states[0]
    assert optimizer.state
    assert scheduler is not None
    assert scheduler.last_epoch == 3

    events = [
        json.loads(line) for line in (paths.logs / "learner_000.jsonl").read_text().splitlines()
    ]
    resets = [event for event in events if event["event_type"] == "inner_optimizer_reset"]
    preserved = [
        event for event in events if event["event_type"] == "inner_training_state_preserved"
    ]
    assert [event["version"] for event in resets] == [0]
    assert len(preserved) == 1
    assert preserved[0]["version"] == 1
    assert preserved[0]["reason"] == "global_rebased"
    assert preserved[0]["optimizer_state_preserved"] is True
    assert preserved[0]["scheduler_state_preserved"] is True
    assert preserved[0]["optimizer_state_entries"] > 0
    assert preserved[0]["scheduler_last_epoch"] == 3
    assert preserved[0]["optimizer_lrs"] == [0.0]


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


def test_prediction_preparation_recovers_collected_cached_checkpoint(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path / "run")
    prepare_run_dirs(paths, 1)
    config = resolve_config("configs/fs_diloco_tiny_predict_local.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    missing_path = str(paths.optim / "outer_v000024.safetensors")
    newer_latest = {"version": 25, "weight_path": "global_v25.safetensors"}

    monkeypatch.setattr(
        "fs_diloco.runtime.learner.read_latest_if_newer",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.predict_next_global_weight",
        lambda **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError(2, "No such file or directory", missing_path)
        ),
    )
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.wait_for_latest_if_newer",
        lambda *_args, **_kwargs: (newer_latest, 0.2),
    )

    reference, stats, latest, recovery = prepare_prediction_or_find_newer_latest(
        paths=paths,
        model=model,
        cached_latest={"version": 24},
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=32,
    )

    assert reference is None
    assert stats is None
    assert latest == newer_latest
    assert recovery == {
        "reason": "cached_checkpoint_collected",
        "cached_version": 24,
        "missing_path": missing_path,
        "waited_seconds": 0.2,
    }


def test_prediction_preparation_prefers_latest_published_during_snapshot(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path / "run")
    prepare_run_dirs(paths, 1)
    config = resolve_config("configs/fs_diloco_tiny_predict_local.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    predicted = torch.ones(index["total_numel"])
    prediction_stats = {"base_version": 24}
    newer_latest = {"version": 25, "weight_path": "global_v25.safetensors"}
    reads = iter([None, newer_latest])

    monkeypatch.setattr(
        "fs_diloco.runtime.learner.read_latest_if_newer",
        lambda *_args, **_kwargs: next(reads),
    )
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.predict_next_global_weight",
        lambda **_kwargs: (predicted, prediction_stats),
    )

    reference, stats, latest, recovery = prepare_prediction_or_find_newer_latest(
        paths=paths,
        model=model,
        cached_latest={"version": 24},
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=32,
    )

    assert reference is None
    assert stats is None
    assert latest == newer_latest
    assert recovery == {
        "reason": "newer_latest_after_prediction_snapshot",
        "cached_version": 24,
        "waited_seconds": 0.0,
    }


def test_prediction_preparation_keeps_snapshot_when_latest_is_unchanged(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path / "run")
    prepare_run_dirs(paths, 1)
    config = resolve_config("configs/fs_diloco_tiny_predict_local.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    predicted = torch.ones(index["total_numel"])
    prediction_stats = {"base_version": 24}

    monkeypatch.setattr(
        "fs_diloco.runtime.learner.read_latest_if_newer",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "fs_diloco.runtime.learner.predict_next_global_weight",
        lambda **_kwargs: (predicted, prediction_stats),
    )

    reference, stats, latest, recovery = prepare_prediction_or_find_newer_latest(
        paths=paths,
        model=model,
        cached_latest={"version": 24},
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=32,
    )

    assert reference is predicted
    assert stats is prediction_stats
    assert latest is None
    assert recovery is None


def test_compute_placement_prefers_cuda_when_estimate_fits(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        torch.cuda,
        "mem_get_info",
        lambda _device: (20 << 30, 24 << 30),
    )

    placement = choose_learner_compute_placement(
        preferred_device=torch.device("cuda"),
        dtype=torch.bfloat16,
        total_numel=1_000_000,
        working_vector_count=14,
    )

    assert placement.device.type == "cuda"
    assert placement.dtype == torch.bfloat16
    assert placement.fallback_reason is None


def test_compute_placement_falls_back_for_estimated_cuda_oom(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        torch.cuda,
        "mem_get_info",
        lambda _device: (2 << 30, 24 << 30),
    )

    placement = choose_learner_compute_placement(
        preferred_device=torch.device("cuda"),
        dtype=torch.float32,
        total_numel=100_000_000,
        working_vector_count=14,
    )

    assert placement.device.type == "cpu"
    assert placement.dtype == torch.float32
    assert placement.fallback_reason == "estimated_cuda_oom_risk"


def test_prediction_retries_on_cpu_after_cuda_oom(tmp_path, monkeypatch):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
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

    cuda_placement = learner_runtime.LearnerComputePlacement(
        device=torch.device("cuda"),
        dtype=torch.float32,
        estimated_working_bytes=1024,
        cuda_free_bytes=4096,
        cuda_total_bytes=8192,
        cuda_reserve_bytes=1024,
    )
    monkeypatch.setattr(
        learner_runtime,
        "choose_learner_compute_placement",
        lambda **_kwargs: cuda_placement,
    )
    original_attempt = learner_runtime._predict_next_global_weight_on_placement
    attempted_devices = []

    def fail_cuda_then_compute_cpu(**kwargs):
        attempted_devices.append(kwargs["placement"].device.type)
        if kwargs["placement"].device.type == "cuda":
            raise torch.OutOfMemoryError("injected CUDA OOM")
        return original_attempt(**kwargs)

    monkeypatch.setattr(
        learner_runtime,
        "_predict_next_global_weight_on_placement",
        fail_cuda_then_compute_cpu,
    )
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)

    predicted, stats = predict_next_global_weight(
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

    assert predicted.device.type == "cpu"
    assert attempted_devices == ["cuda", "cpu"]
    assert stats["prediction_fallback_reason"] == "cuda_oom"
    assert stats["prediction_compute_device"] == "cpu"


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


def test_predict_next_global_uses_configured_bfloat16_compute_dtype(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_syncer_bf16_gpu.yaml")
    model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    global_flat = flatten_trainable_params(model, index, dtype=torch.bfloat16)
    local_delta = torch.full_like(global_flat, 0.125)
    load_flat_into_model(model, global_flat + local_delta, index)
    momentum = torch.full_like(global_flat, 0.25)
    outer_state = {"step": torch.tensor(3), "momentum": momentum}
    weight_path = tmp_path / "global_bf16.safetensors"
    outer_path = tmp_path / "outer_bf16.safetensors"
    save_global_weights(weight_path, global_flat, index, dtype=torch.bfloat16)
    save_outer_state(
        outer_path,
        global_flat,
        outer_state,
        dtype=torch.bfloat16,
    )

    predicted, stats = predict_next_global_weight(
        model=model,
        latest={
            "version": 4,
            "weight_path": str(weight_path),
            "optim_path": str(outer_path),
            "total_update_tokens": 100,
        },
        param_index=index,
        device=torch.device("cpu"),
        config=config,
        local_tokens=25,
    )

    assert predicted.dtype == torch.bfloat16
    assert predicted.device.type == "cpu"
    assert stats["prediction_compute_dtype"] == "bfloat16"
    assert stats["prediction_compute_device"] == "cpu"
    assert stats["prediction_fallback_reason"] == "learner_device_not_cuda"
    assert {param.dtype for param in model.parameters()} == {torch.bfloat16}


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
