"""Factories for strict Full Protocol unit tests."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import torch

from fs_diloco.protocol.contributor import ContributorFence
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path
from fs_diloco.core.versions import CYCLE_RECEIPT_FORMAT_VERSION, PROPOSAL_FORMAT_VERSION
from fs_diloco.storage.atomic_io import publish_immutable_bytes
from fs_diloco.storage.object_store import tensor_schema_sha256
from fs_diloco.storage.tensor_codec import publish_safetensors_immutable, tensor_content_sha256


def safetensors_update_payload(value: float = 1.0) -> bytes:
    """Build one minimal valid safetensors update payload for protocol tests."""

    header = json.dumps(
        {"flat_update": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}},
        separators=(",", ":"),
    ).encode("utf-8")
    padded = header + b" " * ((8 - len(header) % 8) % 8)
    return len(padded).to_bytes(8, "little") + padded + struct.pack("<f", value)


ZERO_DIGEST = "0" * 64
DEFAULT_PAYLOAD = safetensors_update_payload()
PAYLOAD_DIGEST = hashlib.sha256(DEFAULT_PAYLOAD).hexdigest()
SCHEMA_DIGEST = tensor_schema_sha256([{"key": "flat_update", "dtype": "float32", "shape": [1]}])
RECEIPT_DIGEST = "c" * 64


def admit_contributor(
    leader: Any,
    *,
    stream_id: int = 0,
    instance_id: str | None = None,
) -> ContributorFence:
    """Initialize membership and admit one bootstrap instance through production authority."""

    resolved_instance_id = instance_id or f"instance-{stream_id}"
    leader.initialize_membership(command_id="initialize-membership")
    admission = leader.admit_incarnation(
        command_id=f"admit-{resolved_instance_id}",
        instance_id=resolved_instance_id,
        placement_id=f"placement-{resolved_instance_id}",
        stream_id=stream_id,
        bootstrap_slot=stream_id,
        admission_token_sha256=hashlib.sha256(
            f"token-{resolved_instance_id}".encode("utf-8")
        ).hexdigest(),
        hostname="host",
        pid=stream_id + 1,
        pbs_job_id=f"bootstrap-{stream_id}.opbs",
    )
    return admission.fence


def contributor_fence(
    *,
    stream_id: int = 0,
    stream_epoch: int = 1,
    admission_generation: int = 1,
    instance_id: str = "instance-1",
) -> dict[str, Any]:
    """Return the canonical current fence shape without a membership discriminator."""

    return {
        "instance_id": instance_id,
        "placement_id": f"placement-{instance_id}",
        "placement_epoch": 1,
        "stream_id": stream_id,
        "stream_epoch": stream_epoch,
        "admission_generation": admission_generation,
        "admission_token_sha256": "a" * 64,
    }


def receipt_payload(
    *,
    cycle_seq: int = 1,
    previous_receipt_id: str | None = None,
    previous_receipt_sha256: str | None = None,
    cursor_start: int = 0,
    cursor_end: int = 8,
    fence: dict[str, Any] | None = None,
    stable_contributor_key: str = "0",
    update_id: str | None = None,
    run_id: str = "run-current",
) -> dict[str, Any]:
    """Build one internally consistent current cycle-receipt payload."""

    update_id = update_id or f"00000000-0000-4000-8000-{cycle_seq:012d}"
    return {
        "cycle_receipt_format_version": CYCLE_RECEIPT_FORMAT_VERSION,
        "run_id": run_id,
        "stable_contributor_key": stable_contributor_key,
        "cycle_seq": cycle_seq,
        "cycle_id": f"10000000-0000-4000-8000-{cycle_seq:012d}",
        "receipt_id": f"receipt-{stable_contributor_key}-{cycle_seq}",
        "previous_receipt_id": previous_receipt_id,
        "previous_receipt_sha256": previous_receipt_sha256,
        "processed_tokens_this_cycle": 8,
        "effective_tokens_this_cycle": 6,
        "local_discarded_tokens_this_cycle": 2,
        "retained_tokens_since_base": 6 * cycle_seq,
        "data_cursor_start": cursor_start,
        "data_cursor_end": cursor_end,
        "proposal_expected": True,
        "planned_update_id": update_id,
        "planned_payload_sha256": PAYLOAD_DIGEST,
        "contributor_fence": fence or contributor_fence(),
        "created_at": 100.0 + cycle_seq,
    }


def proposal_payload(
    *,
    cycle_seq: int = 1,
    receipt_sha256: str = RECEIPT_DIGEST,
    payload_sha256: str = PAYLOAD_DIGEST,
    payload_size: int = len(DEFAULT_PAYLOAD),
    fence: dict[str, Any] | None = None,
    stable_contributor_key: str = "0",
    update_id: str | None = None,
    run_id: str = "run-current",
) -> dict[str, Any]:
    """Build one internally consistent current proposal payload."""

    update_id = update_id or f"00000000-0000-4000-8000-{cycle_seq:012d}"
    return {
        "proposal_format_version": PROPOSAL_FORMAT_VERSION,
        "run_id": run_id,
        "stable_contributor_key": stable_contributor_key,
        "cycle_seq": cycle_seq,
        "cycle_id": f"10000000-0000-4000-8000-{cycle_seq:012d}",
        "update_id": update_id,
        "cycle_receipt_id": f"receipt-{stable_contributor_key}-{cycle_seq}",
        "cycle_receipt_sha256": receipt_sha256,
        "base_global_version": 0,
        "local_step_start": cycle_seq - 1,
        "local_step_end": cycle_seq,
        "inner_steps": 1,
        "processed_tokens_this_cycle": 8,
        "effective_tokens_this_update": 6,
        "local_discarded_tokens_this_cycle": 2,
        "retained_tokens_since_base": 6 * cycle_seq,
        "data_cursor_start": 8 * (cycle_seq - 1),
        "data_cursor_end": 8 * cycle_seq,
        "contributor_fence": fence or contributor_fence(),
        "payload_relative_path": canonical_update_relative_path(stable_contributor_key, update_id),
        "payload_size": payload_size,
        "payload_sha256": payload_sha256,
        "tensor_schema_sha256": SCHEMA_DIGEST,
        "tensor_dtype": "float32",
        "tensor_numel": 1,
        "created_at": 100.0 + cycle_seq,
    }


def receipt(**kwargs: Any) -> CycleReceiptV1:
    """Decode a generated receipt through the production strict parser."""

    return CycleReceiptV1.from_dict(receipt_payload(**kwargs))


def proposal(**kwargs: Any) -> FullUpdateProposalV2:
    """Decode a generated proposal through the production strict parser."""

    return FullUpdateProposalV2.from_dict(proposal_payload(**kwargs))


def publish_proposal_payload(run_root: Path, proposal: FullUpdateProposalV2) -> None:
    """Publish the payload bytes named by a generated proposal."""

    publication = publish_immutable_bytes(
        run_root / proposal.payload_relative_path, DEFAULT_PAYLOAD
    )
    assert publication.size_bytes == proposal.payload_size
    assert publication.sha256 == proposal.payload_sha256


def publish_checkpoint_pair(
    run_root: Path,
    *,
    version: int,
    epoch: int = 1,
) -> dict[str, Any]:
    """Publish a matching immutable model and optimizer checkpoint pair."""

    theta = torch.tensor([float(version)], dtype=torch.float32)
    theta_sha256 = tensor_content_sha256(theta)
    metadata = {"fs_diloco_theta_sha256": theta_sha256}
    weight_relative_path = f"weights/epochs/e{epoch}/v{version}.safetensors"
    optim_relative_path = f"optim/epochs/e{epoch}/v{version}.safetensors"
    weight = publish_safetensors_immutable(
        run_root / weight_relative_path,
        {"parameter": theta},
        metadata={**metadata, "fs_diloco_theta_order": '["parameter"]'},
    )
    optim = publish_safetensors_immutable(
        run_root / optim_relative_path,
        {"theta": theta, "step": torch.tensor(version, dtype=torch.int64)},
        metadata=metadata,
    )
    return {
        "weight_relative_path": weight_relative_path,
        "weight_size": weight.size_bytes,
        "weight_sha256": weight.sha256,
        "optim_relative_path": optim_relative_path,
        "optim_size": optim.size_bytes,
        "optim_sha256": optim.sha256,
        "weight_theta_sha256": theta_sha256,
        "optim_theta_sha256": theta_sha256,
    }
