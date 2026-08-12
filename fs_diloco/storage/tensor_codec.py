"""Identity-bound safetensors codecs for protocol updates and checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import torch
from safetensors import safe_open
from safetensors.torch import save, save_file

from ..modeling.outer_optim import state_from_tensors, state_to_tensors
from ..modeling.param_index import flat_to_named_tensors, load_flat_into_model
from ..protocol.proposal import FullUpdateProposalV2
from .atomic_io import ImmutablePublication, publish_immutable_with_writer
from .object_store import consume_verified_artifact, tensor_schema_sha256
from .tensor_identity import tensor_content_sha256


def dtype_from_name(name: str) -> torch.dtype:
    """Decode the exact floating-point dtype names allowed by the protocol."""

    mapping = {
        "float32": torch.float32,
        "bfloat16": torch.bfloat16,
    }
    if name not in mapping:
        raise ValueError(f"unsupported tensor dtype: {name}")
    return mapping[name]


def publish_safetensors_immutable(
    path: str | Path,
    tensors: dict[str, torch.Tensor],
    *,
    metadata: dict[str, str] | None = None,
) -> ImmutablePublication:
    """Publish one create-no-replace safetensors object from contiguous CPU tensors."""

    cpu_tensors = {key: value.detach().cpu().contiguous() for key, value in tensors.items()}

    def writer(temporary: Path) -> None:
        """Serialize the prepared tensors into the publication temporary file."""

        save_file(cpu_tensors, str(temporary), metadata=metadata)

    return publish_immutable_with_writer(path, writer)


def encode_global_weights(
    theta: torch.Tensor,
    param_index: dict[str, Any],
    *,
    dtype: torch.dtype | None = None,
) -> tuple[bytes, str]:
    """Serialize deterministic weight bytes and their exact flat-theta identity."""

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
    """Serialize deterministic outer-state bytes bound to their exact theta."""

    published = theta.detach().cpu().contiguous()
    if dtype is not None:
        published = published.to(dtype=dtype)
    theta_sha256 = tensor_content_sha256(published)
    payload = save(
        state_to_tensors(published, state, dtype=dtype),
        metadata={"fs_diloco_theta_sha256": theta_sha256},
    )
    return payload, theta_sha256


def load_update_vector(
    run_root: str | Path,
    proposal: FullUpdateProposalV2,
    *,
    device: str | torch.device = "cpu",
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """Load the exact finite update bytes accepted for one proposal."""

    tensors, _metadata = _load_verified_tensors(
        run_root,
        proposal.payload_relative_path,
        expected_size=proposal.payload_size,
        expected_sha256=proposal.payload_sha256,
    )
    if set(tensors) != {"local_params"}:
        raise ValueError("update artifact must contain only local_params")
    tensor = tensors["local_params"]
    tensor_dtype = _protocol_dtype_name(tensor.dtype)
    schema = tensor_schema_sha256(
        [{"key": "local_params", "dtype": tensor_dtype, "shape": list(tensor.shape)}]
    )
    if schema != proposal.tensor_schema_sha256:
        raise ValueError("update tensor schema does not match the accepted proposal")
    if tensor_dtype != proposal.tensor_dtype:
        raise ValueError("update tensor dtype does not match the accepted proposal")
    if int(tensor.numel()) != proposal.tensor_numel:
        raise ValueError("update tensor size does not match the accepted proposal")
    if not bool(torch.isfinite(tensor).all().item()):
        raise ValueError("update tensor contains non-finite values")
    return tensor.detach().to(device=device, dtype=dtype)


def load_global_weights_flat(
    run_root: str | Path,
    relative_path: str,
    param_index: dict[str, Any],
    *,
    expected_size: int,
    expected_sha256: str,
    expected_theta_sha256: str,
    device: str | torch.device = "cpu",
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """Load one exact committed weight artifact as a validated flat tensor."""

    tensors, metadata = _load_verified_tensors(
        run_root,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    theta = _validated_global_theta(
        tensors,
        metadata,
        param_index,
        expected_theta_sha256=expected_theta_sha256,
    )
    return theta.to(device=device, dtype=dtype)


@torch.no_grad()
def load_global_weights_into_model(
    run_root: str | Path,
    relative_path: str,
    model: torch.nn.Module,
    param_index: dict[str, Any],
    *,
    expected_size: int,
    expected_sha256: str,
    expected_theta_sha256: str,
) -> None:
    """Validate one committed weight identity before replacing model parameters."""

    tensors, metadata = _load_verified_tensors(
        run_root,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    theta = _validated_global_theta(
        tensors,
        metadata,
        param_index,
        expected_theta_sha256=expected_theta_sha256,
    )
    load_flat_into_model(model, theta, param_index)


def load_outer_state(
    run_root: str | Path,
    relative_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_theta_sha256: str,
    device: str | torch.device = "cpu",
    dtype: torch.dtype | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    """Load one exact committed outer state and verify its bound theta."""

    tensors, metadata = _load_verified_tensors(
        run_root,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    if metadata.get("fs_diloco_theta_sha256") != expected_theta_sha256:
        raise ValueError("outer-state metadata does not match committed theta identity")
    if "theta" not in tensors:
        raise ValueError("outer-state artifact does not contain theta")
    if tensor_content_sha256(tensors["theta"]) != expected_theta_sha256:
        raise ValueError("outer-state tensor does not match committed theta identity")
    return state_from_tensors(tensors, device=device, dtype=dtype)


def _load_verified_tensors(
    run_root: str | Path,
    relative_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> tuple[dict[str, torch.Tensor], dict[str, str]]:
    """Deserialize tensors only through the descriptor whose bytes were verified."""

    def consume(descriptor_path: str) -> tuple[dict[str, torch.Tensor], dict[str, str]]:
        """Copy all tensors and metadata from the already verified descriptor."""

        with safe_open(descriptor_path, framework="pt", device="cpu") as checkpoint:
            metadata = checkpoint.metadata() or {}
            tensors = {key: checkpoint.get_tensor(key) for key in checkpoint.keys()}
        if not tensors:
            raise ValueError("safetensors artifact contains no tensors")
        return tensors, metadata

    return consume_verified_artifact(
        run_root,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        consumer=consume,
    )


def _validated_global_theta(
    tensors: dict[str, torch.Tensor],
    metadata: dict[str, str],
    param_index: dict[str, Any],
    *,
    expected_theta_sha256: str,
) -> torch.Tensor:
    """Reconstruct and validate the exact flat theta represented by named weights."""

    expected_names = tuple(str(entry["name"]) for entry in param_index["params"])
    try:
        stored_order = json.loads(metadata["fs_diloco_theta_order"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("weight artifact lacks valid theta order metadata") from exc
    if stored_order != list(expected_names) or set(tensors) != set(expected_names):
        raise ValueError("weight artifact tensor names or order do not match the parameter index")
    chunks: list[torch.Tensor] = []
    published_dtype: torch.dtype | None = None
    for entry in param_index["params"]:
        tensor = tensors[str(entry["name"])]
        if tuple(tensor.shape) != tuple(entry["shape"]):
            raise ValueError(f"weight shape mismatch for {entry['name']}")
        if int(tensor.numel()) != int(entry["numel"]):
            raise ValueError(f"weight size mismatch for {entry['name']}")
        if published_dtype is not None and tensor.dtype != published_dtype:
            raise ValueError("weight artifact contains mixed tensor dtypes")
        published_dtype = tensor.dtype
        chunks.append(tensor.reshape(-1))
    if not chunks:
        raise ValueError("weight artifact cannot represent an empty parameter index")
    theta = torch.cat(chunks).contiguous()
    if metadata.get("fs_diloco_theta_sha256") != expected_theta_sha256:
        raise ValueError("weight metadata does not match committed theta identity")
    if tensor_content_sha256(theta) != expected_theta_sha256:
        raise ValueError("weight tensors do not match committed theta identity")
    return theta


def _protocol_dtype_name(dtype: torch.dtype) -> str:
    """Encode a tensor dtype using the exact proposal vocabulary."""

    mapping = {
        torch.float32: "float32",
        torch.bfloat16: "bfloat16",
    }
    try:
        return mapping[dtype]
    except KeyError as exc:
        raise ValueError(f"unsupported protocol tensor dtype: {dtype}") from exc
