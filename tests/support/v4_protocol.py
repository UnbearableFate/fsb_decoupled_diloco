"""Factories for strict Full Protocol v4 unit tests."""

from __future__ import annotations

from typing import Any

from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path


ZERO_DIGEST = "0" * 64
PAYLOAD_DIGEST = "a" * 64
SCHEMA_DIGEST = "b" * 64
RECEIPT_DIGEST = "c" * 64


def static_fence(*, generation: int = 1, attempt_id: str = "attempt-1") -> dict[str, Any]:
    return {
        "kind": "static",
        "learner_id": "learner-0",
        "logical_launch_id": "launch-0",
        "attempt_id": attempt_id,
        "binding_generation": generation,
    }


def receipt_payload(
    *,
    cycle_seq: int = 1,
    previous_receipt_id: str | None = None,
    previous_receipt_sha256: str | None = None,
    cursor_start: int = 0,
    cursor_end: int = 8,
    fence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    update_id = f"00000000-0000-4000-8000-{cycle_seq:012d}"
    return {
        "cycle_receipt_format_version": 1,
        "run_id": "run-v4",
        "stable_contributor_key": "learner-0",
        "cycle_seq": cycle_seq,
        "cycle_id": f"10000000-0000-4000-8000-{cycle_seq:012d}",
        "receipt_id": f"receipt-learner-0-{cycle_seq}",
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
        "contributor_fence": fence or static_fence(),
        "created_at": 100.0 + cycle_seq,
    }


def proposal_payload(
    *,
    cycle_seq: int = 1,
    receipt_sha256: str = RECEIPT_DIGEST,
    payload_sha256: str = PAYLOAD_DIGEST,
    payload_size: int = 4,
    fence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    update_id = f"00000000-0000-4000-8000-{cycle_seq:012d}"
    return {
        "proposal_format_version": 2,
        "run_id": "run-v4",
        "stable_contributor_key": "learner-0",
        "cycle_seq": cycle_seq,
        "cycle_id": f"10000000-0000-4000-8000-{cycle_seq:012d}",
        "update_id": update_id,
        "cycle_receipt_id": f"receipt-learner-0-{cycle_seq}",
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
        "contributor_fence": fence or static_fence(),
        "payload_relative_path": canonical_update_relative_path("learner-0", update_id),
        "payload_size": payload_size,
        "payload_sha256": payload_sha256,
        "tensor_schema_sha256": SCHEMA_DIGEST,
        "tensor_dtype": "float32",
        "tensor_numel": 1,
        "created_at": 100.0 + cycle_seq,
    }


def receipt(**kwargs: Any) -> CycleReceiptV1:
    return CycleReceiptV1.from_dict(receipt_payload(**kwargs))


def proposal(**kwargs: Any) -> FullUpdateProposalV2:
    return FullUpdateProposalV2.from_dict(proposal_payload(**kwargs))
