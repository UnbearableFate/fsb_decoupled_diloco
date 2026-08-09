"""Safetensors storage for global weights, update vectors, and outer optimizer state."""

from __future__ import annotations

from pathlib import Path
import json

import torch
from safetensors import safe_open
from safetensors.torch import load_file, save, save_file

from .atomic_io import (
    ImmutablePublication,
    atomic_write_with_writer,
    publish_immutable_bytes,
    publish_immutable_with_writer,
)
from .tensor_identity import tensor_content_sha256
from ..modeling.outer_optim import state_from_tensors, state_to_tensors
from ..modeling.param_index import flat_to_named_tensors, named_tensors_to_flat


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


def publish_safetensors_immutable(
    path: str | Path,
    tensors: dict[str, torch.Tensor],
    *,
    metadata: dict[str, str] | None = None,
) -> ImmutablePublication:
    cpu_tensors = {key: value.detach().cpu().contiguous() for key, value in tensors.items()}

    def writer(temporary: Path) -> None:
        save_file(cpu_tensors, str(temporary), metadata=metadata)

    return publish_immutable_with_writer(path, writer)


def encode_global_weights(
    theta: torch.Tensor,
    param_index: dict,
    *,
    dtype: torch.dtype | None = None,
) -> tuple[bytes, str]:
    """Serialize deterministic weight bytes before reserving their publication."""

    published = theta.detach().cpu().contiguous()
    if dtype is not None:
        published = published.to(dtype=dtype)
    theta_sha256 = tensor_content_sha256(published)
    named_tensors = flat_to_named_tensors(published, param_index)
    payload = save(
        named_tensors,
        metadata={
            "fs_diloco_theta_sha256": theta_sha256,
            "fs_diloco_theta_order": json.dumps(list(named_tensors), separators=(",", ":")),
        },
    )
    return payload, theta_sha256


def encode_outer_state(
    theta: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    dtype: torch.dtype | None = None,
) -> tuple[bytes, str]:
    """Serialize deterministic outer-state bytes before publication I/O."""

    published = theta.detach().cpu().contiguous()
    if dtype is not None:
        published = published.to(dtype=dtype)
    theta_sha256 = tensor_content_sha256(published)
    payload = save(
        state_to_tensors(published, state, dtype=dtype),
        metadata={"fs_diloco_theta_sha256": theta_sha256},
    )
    return payload, theta_sha256


def publish_global_weights_immutable(
    path: str | Path,
    theta: torch.Tensor,
    param_index: dict,
    *,
    dtype: torch.dtype | None = None,
) -> tuple[ImmutablePublication, str]:
    payload, theta_sha256 = encode_global_weights(theta, param_index, dtype=dtype)
    publication = publish_immutable_bytes(path, payload)
    return publication, theta_sha256


def publish_outer_state_immutable(
    path: str | Path,
    theta: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    dtype: torch.dtype | None = None,
) -> tuple[ImmutablePublication, str]:
    payload, theta_sha256 = encode_outer_state(theta, state, dtype=dtype)
    publication = publish_immutable_bytes(path, payload)
    return publication, theta_sha256


def load_safetensors(
    path: str | Path, *, device: str | torch.device = "cpu"
) -> dict[str, torch.Tensor]:
    return load_file(str(path), device=str(device))


def save_update_vector(
    path: str | Path, flat: torch.Tensor, *, dtype: torch.dtype = torch.float32
) -> Path:
    return save_safetensors_atomic(
        path, {"local_params": flat.detach().cpu().to(dtype=dtype).contiguous()}
    )


def load_update_vector(
    path: str | Path,
    *,
    device: str | torch.device = "cpu",
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    if "local_params" not in tensors:
        raise ValueError(f"{path} does not contain local_params")
    return tensors["local_params"].detach().to(device=device, dtype=dtype)


def save_global_weights(
    path: str | Path,
    theta: torch.Tensor,
    param_index: dict,
    *,
    dtype: torch.dtype | None = None,
) -> Path:
    published = theta.detach().cpu()
    if dtype is not None:
        published = published.to(dtype=dtype)
    return save_safetensors_atomic(path, flat_to_named_tensors(published, param_index))


def load_global_weights_flat(
    path: str | Path,
    param_index: dict,
    *,
    device: str | torch.device = "cpu",
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    tensors = load_safetensors(path, device=device)
    return named_tensors_to_flat(tensors, param_index, device=device, dtype=dtype)


@torch.no_grad()
def load_global_weights_into_model(
    path: str | Path,
    model: torch.nn.Module,
    param_index: dict,
    *,
    strict_shape: bool = True,
) -> None:
    """Stream a named checkpoint directly into the model's parameter dtype.

    Direct adoption does not perform any arithmetic, so flattening a BF16
    checkpoint into a CPU FP32 vector only to cast it back to a BF16 model is
    both unnecessary and expensive.  Loading one named tensor at a time also
    avoids materializing a second full flat checkpoint in host memory.
    """

    named_params = dict(model.named_parameters())
    with safe_open(str(path), framework="pt", device="cpu") as checkpoint:
        checkpoint_names = set(checkpoint.keys())
        for entry in param_index["params"]:
            name = entry["name"]
            if name not in named_params:
                raise ValueError(f"model does not contain parameter {name}")
            if name not in checkpoint_names:
                raise ValueError(f"{path} does not contain parameter {name}")

            param = named_params[name]
            expected_shape = tuple(entry["shape"])
            if strict_shape and tuple(param.shape) != expected_shape:
                raise ValueError(
                    f"shape mismatch for {name}: {tuple(param.shape)} != {expected_shape}"
                )

            tensor = checkpoint.get_tensor(name)
            if int(tensor.numel()) != int(entry["numel"]):
                raise ValueError(f"tensor size mismatch for {name}")
            if strict_shape and tuple(tensor.shape) != expected_shape:
                raise ValueError(
                    f"checkpoint shape mismatch for {name}: "
                    f"{tuple(tensor.shape)} != {expected_shape}"
                )

            param.copy_(tensor.to(device=param.device, dtype=param.dtype))


def save_outer_state(
    path: str | Path,
    theta: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    dtype: torch.dtype | None = None,
) -> Path:
    return save_safetensors_atomic(path, state_to_tensors(theta, state, dtype=dtype))


def load_outer_state(
    path: str | Path,
    *,
    device: str | torch.device = "cpu",
    dtype: torch.dtype | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    return state_from_tensors(
        load_safetensors(path, device=device),
        device=device,
        dtype=dtype,
    )
