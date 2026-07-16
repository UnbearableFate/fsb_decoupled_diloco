"""Safetensors codec and flat-vector helpers for parameter fragments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import torch

from .fragment_index import fragment_by_id
from .param_index import flatten_trainable_params, load_flat_into_model
from .tensor_codec import load_safetensors, save_safetensors_atomic

FRAGMENT_TENSOR_KEY = "fragment_params"


def extract_fragment(flat: torch.Tensor, fragment_index: dict[str, Any], fragment_id: int) -> torch.Tensor:
    fragment = fragment_by_id(fragment_index, fragment_id)
    chunks = []
    for item in fragment["slices"]:
        start = int(item["flat_start"])
        end = int(item["flat_end"])
        chunks.append(flat[start:end].detach())
    if not chunks:
        return torch.empty(0, dtype=flat.dtype, device=flat.device)
    return torch.cat(chunks, dim=0).contiguous()


def scatter_fragment(
    flat: torch.Tensor,
    fragment_index: dict[str, Any],
    fragment_id: int,
    fragment_tensor: torch.Tensor,
) -> torch.Tensor:
    fragment = fragment_by_id(fragment_index, fragment_id)
    expected = int(fragment["numel"])
    if int(fragment_tensor.numel()) != expected:
        raise ValueError(f"fragment {fragment_id} has {fragment_tensor.numel()} values, expected {expected}")
    result = flat.detach().clone()
    cursor = 0
    source = fragment_tensor.to(device=result.device, dtype=result.dtype).reshape(-1)
    for item in fragment["slices"]:
        start = int(item["flat_start"])
        end = int(item["flat_end"])
        span = end - start
        result[start:end] = source[cursor : cursor + span]
        cursor += span
    return result.contiguous()


@torch.no_grad()
def load_fragment_into_model(
    model: torch.nn.Module,
    fragment_tensor: torch.Tensor,
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    fragment_id: int,
) -> None:
    flat = flatten_trainable_params(model, param_index, dtype=torch.float32)
    updated = scatter_fragment(flat, fragment_index, fragment_id, fragment_tensor)
    load_flat_into_model(model, updated, param_index)


def save_fragment_update(path: str | Path, fragment_tensor: torch.Tensor, dtype: torch.dtype) -> Path:
    return save_safetensors_atomic(
        path,
        {FRAGMENT_TENSOR_KEY: fragment_tensor.detach().cpu().to(dtype=dtype).contiguous()},
    )


def load_fragment_update(path: str | Path, device: str | torch.device = "cpu") -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    if FRAGMENT_TENSOR_KEY not in tensors:
        raise ValueError(f"{path} does not contain {FRAGMENT_TENSOR_KEY}")
    return tensors[FRAGMENT_TENSOR_KEY].detach().to(device=device, dtype=torch.float32)


def save_fragment_weight(path: str | Path, fragment_tensor: torch.Tensor) -> Path:
    return save_safetensors_atomic(path, {FRAGMENT_TENSOR_KEY: fragment_tensor.detach().cpu().contiguous()})


def load_fragment_weight(path: str | Path, device: str | torch.device = "cpu") -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    if FRAGMENT_TENSOR_KEY not in tensors:
        raise ValueError(f"{path} does not contain {FRAGMENT_TENSOR_KEY}")
    return tensors[FRAGMENT_TENSOR_KEY].detach().to(device=device, dtype=torch.float32)


def materialize_full_from_fragments(
    fragment_tensors: dict[int, torch.Tensor],
    fragment_index: dict[str, Any],
    total_numel: int,
) -> torch.Tensor:
    if not fragment_tensors:
        return torch.empty(0, dtype=torch.float32)
    first = next(iter(fragment_tensors.values()))
    flat = torch.empty(total_numel, dtype=first.dtype, device=first.device)
    for fragment in fragment_index["fragments"]:
        fragment_id = int(fragment["fragment_id"])
        if fragment_id not in fragment_tensors:
            raise ValueError(f"missing tensor for fragment {fragment_id}")
        flat = scatter_fragment(flat, fragment_index, fragment_id, fragment_tensors[fragment_id])
    return flat.contiguous()
