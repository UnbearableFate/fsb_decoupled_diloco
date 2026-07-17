import pytest
import torch
from safetensors.torch import load_file

from fs_diloco.core.config import resolve_config
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime.syncer import (
    align_state_to_publication_dtype,
    initialize_fragment_run,
    initialize_run,
    resolve_syncer_device,
    resume_run,
)
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _runtime(tmp_path, config_name, run_id):
    config = resolve_config(
        config_name,
        run_id=run_id,
        shared_root=str(tmp_path),
    )
    config.syncer.device = "cpu"
    config.syncer.compute_dtype = "bfloat16"
    config.syncer.publish_dtype = "bfloat16"
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, config.sync.num_learners)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    return config, paths, store, logger


def _assert_floating_tensors_are_bfloat16(path):
    tensors = load_file(str(path))
    assert tensors
    assert all(
        tensor.dtype == torch.bfloat16 for tensor in tensors.values() if tensor.is_floating_point()
    )


def test_full_syncer_computes_publishes_and_resumes_bfloat16_on_cpu(tmp_path):
    config, paths, store, logger = _runtime(
        tmp_path,
        "configs/fs_diloco_tiny_local.yaml",
        "syncer_bf16_full",
    )

    version, theta, state, _param_index, _tokens = initialize_run(
        config,
        paths,
        store,
        logger,
        device=torch.device("cpu"),
    )

    assert version == 0
    assert theta.device.type == "cpu"
    assert theta.dtype == torch.bfloat16
    assert state["momentum"].dtype == torch.bfloat16
    _assert_floating_tensors_are_bfloat16(paths.global_weight_path(0))
    _assert_floating_tensors_are_bfloat16(paths.outer_optim_path(0))
    config.init.resume = True
    resumed = resume_run(config, paths, store, logger, device=torch.device("cpu"))
    assert resumed[1].dtype == torch.bfloat16
    assert resumed[2]["momentum"].dtype == torch.bfloat16
    store.close()


def test_fragment_syncer_computes_and_publishes_bfloat16_on_cpu(tmp_path):
    config, paths, store, logger = _runtime(
        tmp_path,
        "configs/fs_diloco_tiny_fragment_local.yaml",
        "syncer_bf16_fragment",
    )

    initialized = initialize_fragment_run(
        config,
        paths,
        store,
        logger,
        device=torch.device("cpu"),
    )
    fragment_thetas = initialized[1]
    outer_states = initialized[2]

    assert all(theta.dtype == torch.bfloat16 for theta in fragment_thetas.values())
    assert all(state["momentum"].dtype == torch.bfloat16 for state in outer_states.values())
    for fragment_id in fragment_thetas:
        _assert_floating_tensors_are_bfloat16(paths.fragment_weight_path(fragment_id, 0))
        _assert_floating_tensors_are_bfloat16(paths.fragment_outer_optim_path(fragment_id, 0))
    _assert_floating_tensors_are_bfloat16(paths.global_weight_path(0))
    store.close()


def test_explicit_cpu_device_is_honored():
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.syncer.device = "cpu"
    assert resolve_syncer_device(config) == torch.device("cpu")


def test_publication_dtype_is_the_authoritative_quantization_boundary():
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.syncer.compute_dtype = "float32"
    config.syncer.publish_dtype = "bfloat16"
    theta = torch.tensor([1.001, -2.003], dtype=torch.float32)
    state = {
        "step": torch.tensor(7, dtype=torch.int64),
        "momentum": torch.tensor([0.1234, -0.5678], dtype=torch.float32),
    }

    aligned_theta, aligned_state = align_state_to_publication_dtype(
        config,
        theta,
        state,
    )

    assert aligned_theta.dtype == torch.float32
    assert torch.equal(aligned_theta, theta.bfloat16().float())
    assert aligned_state["momentum"].dtype == torch.float32
    assert torch.equal(aligned_state["momentum"], state["momentum"].bfloat16().float())
    assert aligned_state["step"].dtype == torch.int64
    assert int(aligned_state["step"].item()) == 7


def test_explicit_cuda_device_fails_when_cuda_is_unavailable(monkeypatch):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.syncer.device = "cuda"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="requires an available CUDA device"):
        resolve_syncer_device(config)
