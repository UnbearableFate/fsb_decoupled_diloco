"""Verify identity-bound tensor consumption for updates and checkpoints."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import torch

from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.outer_optim import init_outer_state
from fs_diloco.modeling.param_index import build_param_index, flatten_trainable_params
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.atomic_io import publish_immutable_bytes
from fs_diloco.storage.object_store import ArtifactIdentityError, tensor_schema_sha256
from fs_diloco.storage.tensor_codec import (
    encode_global_weights,
    encode_outer_state,
    load_global_weights_into_model,
    load_outer_state,
    load_update_vector,
    publish_safetensors_immutable,
)
from tests.support.protocol import proposal_payload


def _update_proposal(run_root: Path, value: float = 1.0) -> FullUpdateProposalV2:
    """Publish one BF16 update and return its exact accepted proposal identity."""

    payload = proposal_payload()
    publication = publish_safetensors_immutable(
        run_root / str(payload["payload_relative_path"]),
        {"local_params": torch.tensor([value], dtype=torch.bfloat16)},
    )
    payload.update(
        {
            "payload_size": publication.size_bytes,
            "payload_sha256": publication.sha256,
            "tensor_schema_sha256": tensor_schema_sha256(
                [{"key": "local_params", "dtype": "bfloat16", "shape": [1]}]
            ),
            "tensor_dtype": "bfloat16",
            "tensor_numel": 1,
        }
    )
    return FullUpdateProposalV2.from_dict(payload)


def _replace_immutable(path: Path, payload: bytes) -> None:
    """Simulate owner-level pathname replacement after authority acceptance."""

    path.chmod(0o644)
    path.unlink()
    publish_immutable_bytes(path, payload)


def test_update_reader_preserves_requested_dtype_for_exact_accepted_bytes(tmp_path: Path) -> None:
    """Merge computation may cast only after proposal identity and schema are proven."""

    proposal = _update_proposal(tmp_path, 3.5)

    loaded = load_update_vector(tmp_path, proposal)
    loaded_bf16 = load_update_vector(tmp_path, proposal, dtype=torch.bfloat16)

    assert loaded.dtype == torch.float32
    assert torch.equal(loaded, torch.tensor([3.5]))
    assert loaded_bf16.dtype == torch.bfloat16


def test_update_reader_rejects_same_shaped_path_replacement(tmp_path: Path) -> None:
    """A valid tensor replacing an accepted name must never enter a merge."""

    proposal = _update_proposal(tmp_path, 1.0)
    path = tmp_path / proposal.payload_relative_path
    replacement = tmp_path / "replacement.safetensors"
    replacement_publication = publish_safetensors_immutable(
        replacement,
        {"local_params": torch.tensor([9.0], dtype=torch.bfloat16)},
    )
    _replace_immutable(path, replacement.read_bytes())

    assert replacement_publication.sha256 != proposal.payload_sha256
    with pytest.raises(ArtifactIdentityError, match="SHA-256"):
        load_update_vector(tmp_path, proposal)


def test_checkpoint_readers_bind_bytes_and_theta_before_model_mutation(tmp_path: Path) -> None:
    """Every checkpoint consumer must prove committed bytes and theta identity first."""

    source_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    param_index = build_param_index(source_model, model_name_or_path="synthetic-tiny")
    theta = flatten_trainable_params(source_model, param_index, dtype=torch.bfloat16)
    outer_state = init_outer_state(theta, type("Outer", (), {"name": "nesterov"})())
    weight_bytes, theta_sha256 = encode_global_weights(
        theta,
        param_index,
        dtype=torch.bfloat16,
    )
    outer_bytes, outer_theta_sha256 = encode_outer_state(
        theta,
        outer_state,
        dtype=torch.bfloat16,
    )
    assert outer_theta_sha256 == theta_sha256
    weight_relative = "weights/weight.safetensors"
    outer_relative = "optim/outer.safetensors"
    weight_publication = publish_immutable_bytes(tmp_path / weight_relative, weight_bytes)
    outer_publication = publish_immutable_bytes(tmp_path / outer_relative, outer_bytes)

    target_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    with torch.no_grad():
        for parameter in target_model.parameters():
            parameter.zero_()
    load_global_weights_into_model(
        tmp_path,
        weight_relative,
        target_model,
        param_index,
        expected_size=weight_publication.size_bytes,
        expected_sha256=weight_publication.sha256,
        expected_theta_sha256=theta_sha256,
    )
    loaded_theta, _loaded_state = load_outer_state(
        tmp_path,
        outer_relative,
        expected_size=outer_publication.size_bytes,
        expected_sha256=outer_publication.sha256,
        expected_theta_sha256=theta_sha256,
        dtype=torch.bfloat16,
    )
    assert torch.equal(
        flatten_trainable_params(target_model, param_index, dtype=torch.bfloat16),
        theta,
    )
    assert torch.equal(loaded_theta, theta)

    changed = theta.add(1)
    replacement_bytes, replacement_theta = encode_global_weights(
        changed,
        param_index,
        dtype=torch.bfloat16,
    )
    assert replacement_theta != theta_sha256
    before = flatten_trainable_params(target_model, param_index, dtype=torch.bfloat16).clone()
    _replace_immutable(tmp_path / weight_relative, replacement_bytes)

    with pytest.raises(ArtifactIdentityError, match="SHA-256"):
        load_global_weights_into_model(
            tmp_path,
            weight_relative,
            target_model,
            param_index,
            expected_size=weight_publication.size_bytes,
            expected_sha256=weight_publication.sha256,
            expected_theta_sha256=theta_sha256,
        )
    assert torch.equal(
        flatten_trainable_params(target_model, param_index, dtype=torch.bfloat16),
        before,
    )
    assert hashlib.sha256(replacement_bytes).hexdigest() != weight_publication.sha256
