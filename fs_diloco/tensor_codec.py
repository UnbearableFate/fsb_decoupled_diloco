"""Safetensors storage for global weights, update vectors, and outer optimizer state."""

from __future__ import annotations

from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

from .atomic_io import atomic_write_with_writer
from .outer_optim import state_from_tensors, state_to_tensors
from .param_index import flat_to_named_tensors, named_tensors_to_flat


def dtype_from_name(name: str) -> torch.dtype:
    normalized = name.lower().replace("torch.", "")
    mapping = {
        "float32": torch.float32,
        "fp32": torch.float32,
        "float": torch.float32,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported tensor dtype: {name}")
    return mapping[normalized]


def save_safetensors_atomic(path: str | Path, tensors: dict[str, torch.Tensor]) -> Path:
    path = Path(path)

    def writer(tmp_path: Path) -> None:
        cpu_tensors = {key: value.detach().cpu().contiguous() for key, value in tensors.items()}
        save_file(cpu_tensors, str(tmp_path))

    return atomic_write_with_writer(path, writer)


def load_safetensors(path: str | Path, *, device: str | torch.device = "cpu") -> dict[str, torch.Tensor]:
    return load_file(str(path), device=str(device))


def save_update_vector(path: str | Path, flat: torch.Tensor, *, dtype: torch.dtype = torch.float32) -> Path:
    return save_safetensors_atomic(path, {"local_params": flat.detach().cpu().to(dtype=dtype).contiguous()})


def load_update_vector(path: str | Path, *, device: str | torch.device = "cpu") -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    if "local_params" not in tensors:
        raise ValueError(f"{path} does not contain local_params")
    return tensors["local_params"].detach().to(device=device, dtype=torch.float32)


def save_global_weights(path: str | Path, theta: torch.Tensor, param_index: dict) -> Path:
    return save_safetensors_atomic(path, flat_to_named_tensors(theta.detach().cpu(), param_index))


def load_global_weights_flat(
    path: str | Path,
    param_index: dict,
    *,
    device: str | torch.device = "cpu",
) -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    return named_tensors_to_flat(tensors, param_index, device=device)


def save_outer_state(path: str | Path, theta: torch.Tensor, state: dict[str, torch.Tensor]) -> Path:
    return save_safetensors_atomic(path, state_to_tensors(theta, state))


def load_outer_state(
    path: str | Path,
    *,
    device: str | torch.device = "cpu",
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    return state_from_tensors(load_safetensors(path, device=device), device=device)
