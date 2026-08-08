from __future__ import annotations

import math
from dataclasses import replace

import pytest

from fs_diloco.protocol.contributor import StaticContributorFence
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from tests.support.v4_protocol import receipt_payload


def test_cycle_receipt_v1_strict_round_trip() -> None:
    receipt = CycleReceiptV1.from_dict(receipt_payload())

    assert CycleReceiptV1.from_json(receipt.canonical_bytes()) == receipt
    assert len(receipt.immutable_sha256()) == 64


def test_zero_effective_cycle_is_valid_only_without_a_proposal() -> None:
    payload = receipt_payload()
    payload.update(
        {
            "effective_tokens_this_cycle": 0,
            "local_discarded_tokens_this_cycle": 8,
            "retained_tokens_since_base": 0,
            "proposal_expected": False,
            "planned_update_id": None,
            "planned_payload_sha256": None,
        }
    )

    assert CycleReceiptV1.from_dict(payload).effective_tokens_this_cycle == 0

    payload["proposal_expected"] = True
    payload["planned_update_id"] = "00000000-0000-4000-8000-000000000001"
    payload["planned_payload_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="zero-effective"):
        CycleReceiptV1.from_dict(payload)


@pytest.mark.parametrize("value", [-1, True])
def test_cycle_receipt_rejects_invalid_token_types(value: object) -> None:
    payload = receipt_payload()
    payload["local_discarded_tokens_this_cycle"] = value
    with pytest.raises(ValueError):
        CycleReceiptV1.from_dict(payload)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_cycle_receipt_rejects_nonfinite_time(value: float) -> None:
    payload = receipt_payload()
    payload["created_at"] = value
    with pytest.raises(ValueError, match="finite"):
        CycleReceiptV1.from_dict(payload)


def test_cycle_receipt_requires_contiguous_hash_link_shape() -> None:
    missing = receipt_payload(cycle_seq=2, cursor_start=8, cursor_end=16)
    with pytest.raises(ValueError, match="previous receipt"):
        CycleReceiptV1.from_dict(missing)

    first = receipt_payload(previous_receipt_id="receipt-old", previous_receipt_sha256="a" * 64)
    with pytest.raises(ValueError, match="cycle 1"):
        CycleReceiptV1.from_dict(first)


def test_cycle_receipt_rejects_unknown_field_and_version() -> None:
    unknown = receipt_payload()
    unknown["unknown"] = None
    with pytest.raises(ValueError, match="unknown fields"):
        CycleReceiptV1.from_dict(unknown)

    version = receipt_payload()
    version["cycle_receipt_format_version"] = 2
    with pytest.raises(ValueError, match="unsupported cycle_receipt_format_version"):
        CycleReceiptV1.from_dict(version)


def test_direct_receipt_and_fence_construction_cannot_bypass_validation() -> None:
    receipt = CycleReceiptV1.from_dict(receipt_payload())

    with pytest.raises(ValueError, match="processed tokens"):
        replace(receipt, processed_tokens_this_cycle=9)
    with pytest.raises(ValueError, match="planned_update_id"):
        replace(receipt, planned_update_id="not-a-uuid")
    with pytest.raises(ValueError, match="finite"):
        replace(receipt, created_at=math.nan)
    with pytest.raises(ValueError, match="kind"):
        StaticContributorFence("dynamic", "learner-0", "launch-0", "attempt-1", 1)
