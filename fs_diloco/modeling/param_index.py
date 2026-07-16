"""Deterministic trainable-parameter indexing and flat-vector conversion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import torch

from ..storage.atomic_io import atomic_write_json, read_json
from ..core.constants import FORMAT_VERSION


def torch_dtype_name(dtype: torch.dtype) -> str:
    return str(dtype)


def build_param_index(model: torch.nn.Module, *, model_name_or_path: str, trainable_only: bool = True) -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    offset = 0
    for name, param in model.named_parameters():
        if trainable_only and not param.requires_grad:
            continue
        numel = int(param.numel())
        params.append(
            {
                "name": name,
                "shape": list(param.shape),
                "dtype": torch_dtype_name(param.dtype),
                "numel": numel,
                "offset": offset,
            }
        )
        offset += numel
    return {
        "format_version": FORMAT_VERSION,
        "model_name_or_path": model_name_or_path,
        "trainable_only": trainable_only,
        "total_numel": offset,
        "params": params,
    }


def save_param_index(param_index: dict[str, Any], path: str | Path) -> Path:
    return atomic_write_json(path, param_index)


def load_param_index(path: str | Path) -> dict[str, Any]:
    payload = read_json(path)
    if payload.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported param index format: {payload.get('format_version')}")
    return payload


def _named_param_map(model: torch.nn.Module) -> dict[str, torch.nn.Parameter]:
    return dict(model.named_parameters())


def flatten_trainable_params(
    model: torch.nn.Module,
    param_index: dict[str, Any],
    *,
    dtype: torch.dtype | None = torch.float32,
    device: torch.device | str = "cpu",
) -> torch.Tensor:
    named_params = _named_param_map(model)
    chunks = []
    for entry in param_index["params"]:
        param = named_params[entry["name"]]
        tensor = param.detach().reshape(-1)
        if dtype is not None:
            tensor = tensor.to(dtype=dtype)
        chunks.append(tensor.to(device=device, non_blocking=False))
    if not chunks:
        return torch.empty(0, dtype=dtype or torch.float32, device=device)
    return torch.cat(chunks, dim=0).contiguous()


@torch.no_grad()
def load_flat_into_model(
    model: torch.nn.Module,
    flat: torch.Tensor,
    param_index: dict[str, Any],
    *,
    strict_shape: bool = True,
) -> None:
    if int(flat.numel()) != int(param_index["total_numel"]):
        raise ValueError(f"flat tensor has {flat.numel()} values, expected {param_index['total_numel']}")
    named_params = _named_param_map(model)
    for entry in param_index["params"]:
        param = named_params[entry["name"]]
        offset = int(entry["offset"])
        numel = int(entry["numel"])
        shape = tuple(entry["shape"])
        if strict_shape and tuple(param.shape) != shape:
            raise ValueError(f"shape mismatch for {entry['name']}: {tuple(param.shape)} != {shape}")
        view = flat[offset : offset + numel].to(device=param.device, dtype=param.dtype).reshape(param.shape)
        param.copy_(view)


def flat_to_named_tensors(flat: torch.Tensor, param_index: dict[str, Any]) -> dict[str, torch.Tensor]:
    if int(flat.numel()) != int(param_index["total_numel"]):
        raise ValueError(f"flat tensor has {flat.numel()} values, expected {param_index['total_numel']}")
    tensors: dict[str, torch.Tensor] = {}
    for entry in param_index["params"]:
        offset = int(entry["offset"])
        numel = int(entry["numel"])
        tensors[entry["name"]] = flat[offset : offset + numel].reshape(tuple(entry["shape"])).detach().cpu()
    return tensors


def named_tensors_to_flat(
    named_tensors: dict[str, torch.Tensor],
    param_index: dict[str, Any],
    *,
    device: torch.device | str = "cpu",
) -> torch.Tensor:
    chunks = []
    for entry in param_index["params"]:
        tensor = named_tensors[entry["name"]].detach().to(device=device).reshape(-1).float()
        if int(tensor.numel()) != int(entry["numel"]):
            raise ValueError(f"tensor size mismatch for {entry['name']}")
        chunks.append(tensor)
    if not chunks:
        return torch.empty(0, dtype=torch.float32, device=device)
    return torch.cat(chunks).contiguous()


def validate_compatible_index(param_index: dict[str, Any], other: dict[str, Any]) -> None:
    keys = ["format_version", "model_name_or_path", "trainable_only", "total_numel"]
    for key in keys:
        if param_index.get(key) != other.get(key):
            raise ValueError(f"param index mismatch for {key}: {param_index.get(key)} != {other.get(key)}")
    if param_index.get("params") != other.get("params"):
        raise ValueError("parameter entries differ")
