"""Strict wire boundary for CycleReceiptV1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from ..core.versions import CYCLE_RECEIPT_FORMAT_VERSION
from ._validation import (
    decode_json_object,
    identity,
    require_exact_fields,
    require_mapping,
    sha256,
    strict_bool,
    strict_float,
    strict_int,
    uuid4_string,
)
from .contributor import ContributorFence


def canonical_receipt_id(stable_contributor_key: str, cycle_seq: int) -> str:
    """Return the protocol identity shared by a receipt and its proposal reference."""

    stable_key = identity(stable_contributor_key, name="stable_contributor_key")
    sequence = strict_int(cycle_seq, name="cycle_seq", minimum=1)
    return f"receipt-{stable_key}-{sequence}"


def contributor_fence_namespace(fence: ContributorFence) -> str:
    """Return a safe, deterministic namespace for one contributor incarnation."""

    if not isinstance(fence, ContributorFence):
        raise ValueError("fence must be a typed contributor fence")
    canonical_fence = json.dumps(
        fence.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical_fence).hexdigest()


def canonical_receipt_relative_path(fence: ContributorFence, cycle_seq: int) -> str:
    """Derive an immutable receipt path that cannot collide across incarnations."""

    stable_key = identity(fence.stable_contributor_key, name="stable_contributor_key")
    receipt_id = canonical_receipt_id(stable_key, cycle_seq)
    namespace = contributor_fence_namespace(fence)
    return f"updates/receipts/{stable_key}/{namespace}/{receipt_id}.json"


@dataclass(frozen=True)
class CycleReceiptV1:
    """Record one contiguous learner cycle and its optional promised proposal."""

    cycle_receipt_format_version: int
    run_id: str
    stable_contributor_key: str
    cycle_seq: int
    cycle_id: str
    receipt_id: str
    previous_receipt_id: str | None
    previous_receipt_sha256: str | None
    processed_tokens_this_cycle: int
    effective_tokens_this_cycle: int
    local_discarded_tokens_this_cycle: int
    retained_tokens_since_base: int
    data_cursor_start: int
    data_cursor_end: int
    proposal_expected: bool
    planned_update_id: str | None
    planned_payload_sha256: str | None
    contributor_fence: ContributorFence
    created_at: float

    def __post_init__(self) -> None:
        """Reject any receipt whose identity, accounting, cursor, or fence is inconsistent."""

        version = strict_int(
            self.cycle_receipt_format_version,
            name="cycle_receipt_format_version",
            minimum=1,
        )
        if version != CYCLE_RECEIPT_FORMAT_VERSION:
            raise ValueError(f"unsupported cycle_receipt_format_version: {version}")
        identity(self.run_id, name="run_id")
        stable_key = identity(self.stable_contributor_key, name="stable_contributor_key")
        sequence = strict_int(self.cycle_seq, name="cycle_seq", minimum=1)
        uuid4_string(self.cycle_id, name="cycle_id")
        receipt_id = identity(self.receipt_id, name="receipt_id")
        if receipt_id != canonical_receipt_id(stable_key, sequence):
            raise ValueError("receipt_id does not match stable contributor and cycle sequence")
        if sequence == 1:
            if self.previous_receipt_id is not None or self.previous_receipt_sha256 is not None:
                raise ValueError("cycle 1 must not name a previous receipt")
        else:
            identity(self.previous_receipt_id, name="previous_receipt_id")
            sha256(self.previous_receipt_sha256, name="previous_receipt_sha256")
        processed = strict_int(
            self.processed_tokens_this_cycle,
            name="processed_tokens_this_cycle",
            minimum=1,
        )
        effective = strict_int(
            self.effective_tokens_this_cycle,
            name="effective_tokens_this_cycle",
            minimum=0,
        )
        discarded = strict_int(
            self.local_discarded_tokens_this_cycle,
            name="local_discarded_tokens_this_cycle",
            minimum=0,
        )
        if processed != effective + discarded:
            raise ValueError("processed tokens must equal effective plus local-discarded tokens")
        retained = strict_int(
            self.retained_tokens_since_base,
            name="retained_tokens_since_base",
            minimum=0,
        )
        if retained < effective:
            raise ValueError("retained_tokens_since_base must cover effective tokens")
        cursor_start = strict_int(self.data_cursor_start, name="data_cursor_start", minimum=0)
        cursor_end = strict_int(self.data_cursor_end, name="data_cursor_end", minimum=1)
        if cursor_end <= cursor_start:
            raise ValueError("data_cursor_end must be greater than data_cursor_start")
        expected = strict_bool(self.proposal_expected, name="proposal_expected")
        if expected:
            uuid4_string(self.planned_update_id, name="planned_update_id")
            sha256(self.planned_payload_sha256, name="planned_payload_sha256")
            if effective <= 0:
                raise ValueError("a proposal cannot be planned for a zero-effective cycle")
        elif self.planned_update_id is not None or self.planned_payload_sha256 is not None:
            raise ValueError("an upload-skipped receipt must not name a planned proposal")
        if not isinstance(self.contributor_fence, ContributorFence):
            raise ValueError("contributor_fence must be a typed contributor fence")
        if self.contributor_fence.stable_contributor_key != stable_key:
            raise ValueError("contributor fence does not match stable_contributor_key")
        created_at = strict_float(self.created_at, name="created_at")
        if created_at < 0.0:
            raise ValueError("created_at must be >= 0")
        object.__setattr__(self, "created_at", created_at)

    @classmethod
    def from_json(cls, raw: bytes | str, *, max_bytes: int = 256 * 1024) -> "CycleReceiptV1":
        return cls.from_dict(decode_json_object(raw, name="CycleReceiptV1", max_bytes=max_bytes))

    @classmethod
    def from_dict(cls, value: Any) -> "CycleReceiptV1":
        """Decode the exact current receipt schema without accepting legacy fields."""

        payload = require_mapping(value, name="CycleReceiptV1")
        fields = {
            "cycle_receipt_format_version",
            "run_id",
            "stable_contributor_key",
            "cycle_seq",
            "cycle_id",
            "receipt_id",
            "previous_receipt_id",
            "previous_receipt_sha256",
            "processed_tokens_this_cycle",
            "effective_tokens_this_cycle",
            "local_discarded_tokens_this_cycle",
            "retained_tokens_since_base",
            "data_cursor_start",
            "data_cursor_end",
            "proposal_expected",
            "planned_update_id",
            "planned_payload_sha256",
            "contributor_fence",
            "created_at",
        }
        require_exact_fields(payload, required=fields, name="CycleReceiptV1")
        version = strict_int(
            payload["cycle_receipt_format_version"],
            name="cycle_receipt_format_version",
            minimum=1,
        )
        if version != CYCLE_RECEIPT_FORMAT_VERSION:
            raise ValueError(f"unsupported cycle_receipt_format_version: {version}")
        sequence = strict_int(payload["cycle_seq"], name="cycle_seq", minimum=1)
        previous_id = payload["previous_receipt_id"]
        previous_digest = payload["previous_receipt_sha256"]
        if sequence == 1:
            if previous_id is not None or previous_digest is not None:
                raise ValueError("cycle 1 must not name a previous receipt")
        elif previous_id is None or previous_digest is None:
            raise ValueError("cycle sequence > 1 requires the previous receipt identity and hash")
        if previous_id is not None:
            previous_id = identity(previous_id, name="previous_receipt_id")
            previous_digest = sha256(previous_digest, name="previous_receipt_sha256")
        processed = strict_int(
            payload["processed_tokens_this_cycle"],
            name="processed_tokens_this_cycle",
            minimum=1,
        )
        effective = strict_int(
            payload["effective_tokens_this_cycle"],
            name="effective_tokens_this_cycle",
            minimum=0,
        )
        discarded = strict_int(
            payload["local_discarded_tokens_this_cycle"],
            name="local_discarded_tokens_this_cycle",
            minimum=0,
        )
        if processed != effective + discarded:
            raise ValueError("processed tokens must equal effective plus local-discarded tokens")
        retained = strict_int(
            payload["retained_tokens_since_base"],
            name="retained_tokens_since_base",
            minimum=0,
        )
        if retained < effective:
            raise ValueError("retained_tokens_since_base must cover effective tokens")
        cursor_start = strict_int(payload["data_cursor_start"], name="data_cursor_start", minimum=0)
        cursor_end = strict_int(payload["data_cursor_end"], name="data_cursor_end", minimum=1)
        if cursor_end <= cursor_start:
            raise ValueError("data_cursor_end must be greater than data_cursor_start")
        expected = strict_bool(payload["proposal_expected"], name="proposal_expected")
        update_id = payload["planned_update_id"]
        payload_digest = payload["planned_payload_sha256"]
        if expected:
            update_id = uuid4_string(update_id, name="planned_update_id")
            payload_digest = sha256(payload_digest, name="planned_payload_sha256")
            if effective <= 0:
                raise ValueError("a proposal cannot be planned for a zero-effective cycle")
        elif update_id is not None or payload_digest is not None:
            raise ValueError("an upload-skipped receipt must not name a planned proposal")
        fence = ContributorFence.from_dict(payload["contributor_fence"])
        stable_key = identity(payload["stable_contributor_key"], name="stable_contributor_key")
        if fence.stable_contributor_key != stable_key:
            raise ValueError("contributor fence does not match stable_contributor_key")
        created_at = strict_float(payload["created_at"], name="created_at")
        if created_at < 0.0:
            raise ValueError("created_at must be >= 0")
        return cls(
            cycle_receipt_format_version=version,
            run_id=identity(payload["run_id"], name="run_id"),
            stable_contributor_key=stable_key,
            cycle_seq=sequence,
            cycle_id=uuid4_string(payload["cycle_id"], name="cycle_id"),
            receipt_id=identity(payload["receipt_id"], name="receipt_id"),
            previous_receipt_id=previous_id,
            previous_receipt_sha256=previous_digest,
            processed_tokens_this_cycle=processed,
            effective_tokens_this_cycle=effective,
            local_discarded_tokens_this_cycle=discarded,
            retained_tokens_since_base=retained,
            data_cursor_start=cursor_start,
            data_cursor_end=cursor_end,
            proposal_expected=expected,
            planned_update_id=update_id,
            planned_payload_sha256=payload_digest,
            contributor_fence=fence,
            created_at=created_at,
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["contributor_fence"] = self.contributor_fence.as_dict()
        return result

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def immutable_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
