"""Low-level tensor identity helpers with no storage/modeling dependencies."""

from __future__ import annotations

import hashlib
import json

import torch


def tensor_content_sha256(tensor: torch.Tensor) -> str:
    """Digest one tensor's exact dtype, shape, and contiguous value bytes."""

    value = tensor.detach().cpu().contiguous()
    digest = hashlib.sha256()
    digest.update(str(value.dtype).encode("ascii"))
    digest.update(b"\0")
    digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode("ascii"))
    digest.update(b"\0")
    digest.update(value.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()
