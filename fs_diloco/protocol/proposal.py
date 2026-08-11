"""Strict wire boundary for FullUpdateProposalV2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
import posixpath
from typing import Any

from ..core.versions import PROPOSAL_FORMAT_VERSION
from ._validation import (
    decode_json_object,
    identity,
    nonempty_string,
    require_exact_fields,
    require_mapping,
    sha256,
    strict_float,
    strict_int,
    uuid4_string,
)
from .contributor import ContributorFence


def canonical_update_relative_path(stable_contributor_key: str, update_id: str) -> str:
    contributor = identity(stable_contributor_key, name="stable_contributor_key")
    update = uuid4_string(update_id, name="update_id")
    return f"updates/payloads/{contributor}/{update}.safetensors"


def _validate_relative_path(value: Any, *, expected: str) -> str:
    path = nonempty_string(value, name="payload_relative_path")
    parts = path.split("/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("payload_relative_path must be normalized and run-root-relative")
    if posixpath.normpath(path) != path or path != expected:
        raise ValueError("payload_relative_path does not match proposal identity")
    if "/staging/" in f"/{path}/" or path.startswith("staging/"):
        raise ValueError("staging paths cannot be authoritative payload paths")
    return path


@dataclass(frozen=True)
class FullUpdateProposalV2:
    """Describe one complete parameter proposal bound to a cycle receipt and fence."""

    proposal_format_version: int
    run_id: str
    stable_contributor_key: str
    cycle_seq: int
    cycle_id: str
    update_id: str
    cycle_receipt_id: str
    cycle_receipt_sha256: str
    base_global_version: int
    local_step_start: int
    local_step_end: int
    inner_steps: int
    processed_tokens_this_cycle: int
    effective_tokens_this_update: int
    local_discarded_tokens_this_cycle: int
    retained_tokens_since_base: int
    data_cursor_start: int
    data_cursor_end: int
    contributor_fence: ContributorFence
    payload_relative_path: str
    payload_size: int
    payload_sha256: str
    tensor_schema_sha256: str
    tensor_dtype: str
    tensor_numel: int
    created_at: float

    def __post_init__(self) -> None:
        """Reject proposals with inconsistent work, cursor, payload, or membership identity."""

        version = strict_int(
            self.proposal_format_version, name="proposal_format_version", minimum=1
        )
        if version != PROPOSAL_FORMAT_VERSION:
            raise ValueError(f"unsupported proposal_format_version: {version}")
        identity(self.run_id, name="run_id")
        stable_key = identity(self.stable_contributor_key, name="stable_contributor_key")
        strict_int(self.cycle_seq, name="cycle_seq", minimum=1)
        uuid4_string(self.cycle_id, name="cycle_id")
        update_id = uuid4_string(self.update_id, name="update_id")
        identity(self.cycle_receipt_id, name="cycle_receipt_id")
        sha256(self.cycle_receipt_sha256, name="cycle_receipt_sha256")
        strict_int(self.base_global_version, name="base_global_version", minimum=0)
        local_start = strict_int(self.local_step_start, name="local_step_start", minimum=0)
        local_end = strict_int(self.local_step_end, name="local_step_end", minimum=1)
        inner_steps = strict_int(self.inner_steps, name="inner_steps", minimum=1)
        if local_end != local_start + inner_steps:
            raise ValueError("local_step_end must equal local_step_start + inner_steps")
        processed = strict_int(
            self.processed_tokens_this_cycle,
            name="processed_tokens_this_cycle",
            minimum=1,
        )
        effective = strict_int(
            self.effective_tokens_this_update,
            name="effective_tokens_this_update",
            minimum=1,
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
            minimum=1,
        )
        if retained < effective:
            raise ValueError("retained_tokens_since_base must cover effective tokens")
        cursor_start = strict_int(self.data_cursor_start, name="data_cursor_start", minimum=0)
        cursor_end = strict_int(self.data_cursor_end, name="data_cursor_end", minimum=1)
        if cursor_end <= cursor_start:
            raise ValueError("data_cursor_end must be greater than data_cursor_start")
        if not isinstance(self.contributor_fence, ContributorFence):
            raise ValueError("contributor_fence must be a typed contributor fence")
        if self.contributor_fence.stable_contributor_key != stable_key:
            raise ValueError("contributor fence does not match stable_contributor_key")
        _validate_relative_path(
            self.payload_relative_path,
            expected=canonical_update_relative_path(stable_key, update_id),
        )
        strict_int(self.payload_size, name="payload_size", minimum=1)
        sha256(self.payload_sha256, name="payload_sha256")
        sha256(self.tensor_schema_sha256, name="tensor_schema_sha256")
        tensor_dtype = nonempty_string(self.tensor_dtype, name="tensor_dtype")
        if tensor_dtype not in {"float32", "bfloat16"}:
            raise ValueError(f"unsupported tensor_dtype: {tensor_dtype}")
        strict_int(self.tensor_numel, name="tensor_numel", minimum=1)
        created_at = strict_float(self.created_at, name="created_at")
        if created_at < 0.0:
            raise ValueError("created_at must be >= 0")
        object.__setattr__(self, "created_at", created_at)

    @classmethod
    def from_json(cls, raw: bytes | str, *, max_bytes: int = 256 * 1024) -> "FullUpdateProposalV2":
        return cls.from_dict(
            decode_json_object(raw, name="FullUpdateProposalV2", max_bytes=max_bytes)
        )

    @classmethod
    def from_dict(cls, value: Any) -> "FullUpdateProposalV2":
        """Decode the exact current proposal schema without accepting legacy fields."""

        payload = require_mapping(value, name="FullUpdateProposalV2")
        fields = {
            "proposal_format_version",
            "run_id",
            "stable_contributor_key",
            "cycle_seq",
            "cycle_id",
            "update_id",
            "cycle_receipt_id",
            "cycle_receipt_sha256",
            "base_global_version",
            "local_step_start",
            "local_step_end",
            "inner_steps",
            "processed_tokens_this_cycle",
            "effective_tokens_this_update",
            "local_discarded_tokens_this_cycle",
            "retained_tokens_since_base",
            "data_cursor_start",
            "data_cursor_end",
            "contributor_fence",
            "payload_relative_path",
            "payload_size",
            "payload_sha256",
            "tensor_schema_sha256",
            "tensor_dtype",
            "tensor_numel",
            "created_at",
        }
        require_exact_fields(payload, required=fields, name="FullUpdateProposalV2")
        version = strict_int(
            payload["proposal_format_version"], name="proposal_format_version", minimum=1
        )
        if version != PROPOSAL_FORMAT_VERSION:
            raise ValueError(f"unsupported proposal_format_version: {version}")
        fence = ContributorFence.from_dict(payload["contributor_fence"])
        stable_key = identity(payload["stable_contributor_key"], name="stable_contributor_key")
        if fence.stable_contributor_key != stable_key:
            raise ValueError("contributor fence does not match stable_contributor_key")
        update_id = uuid4_string(payload["update_id"], name="update_id")
        cycle_seq = strict_int(payload["cycle_seq"], name="cycle_seq", minimum=1)
        local_start = strict_int(payload["local_step_start"], name="local_step_start", minimum=0)
        local_end = strict_int(payload["local_step_end"], name="local_step_end", minimum=1)
        inner_steps = strict_int(payload["inner_steps"], name="inner_steps", minimum=1)
        if local_end != local_start + inner_steps:
            raise ValueError("local_step_end must equal local_step_start + inner_steps")
        processed = strict_int(
            payload["processed_tokens_this_cycle"],
            name="processed_tokens_this_cycle",
            minimum=1,
        )
        effective = strict_int(
            payload["effective_tokens_this_update"],
            name="effective_tokens_this_update",
            minimum=1,
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
            minimum=1,
        )
        if retained < effective:
            raise ValueError("retained_tokens_since_base must cover effective tokens")
        cursor_start = strict_int(payload["data_cursor_start"], name="data_cursor_start", minimum=0)
        cursor_end = strict_int(payload["data_cursor_end"], name="data_cursor_end", minimum=1)
        if cursor_end <= cursor_start:
            raise ValueError("data_cursor_end must be greater than data_cursor_start")
        created_at = strict_float(payload["created_at"], name="created_at")
        if created_at < 0.0:
            raise ValueError("created_at must be >= 0")
        tensor_dtype = nonempty_string(payload["tensor_dtype"], name="tensor_dtype")
        if tensor_dtype not in {"float32", "bfloat16"}:
            raise ValueError(f"unsupported tensor_dtype: {tensor_dtype}")
        result = cls(
            proposal_format_version=version,
            run_id=identity(payload["run_id"], name="run_id"),
            stable_contributor_key=stable_key,
            cycle_seq=cycle_seq,
            cycle_id=uuid4_string(payload["cycle_id"], name="cycle_id"),
            update_id=update_id,
            cycle_receipt_id=identity(payload["cycle_receipt_id"], name="cycle_receipt_id"),
            cycle_receipt_sha256=sha256(
                payload["cycle_receipt_sha256"], name="cycle_receipt_sha256"
            ),
            base_global_version=strict_int(
                payload["base_global_version"], name="base_global_version", minimum=0
            ),
            local_step_start=local_start,
            local_step_end=local_end,
            inner_steps=inner_steps,
            processed_tokens_this_cycle=processed,
            effective_tokens_this_update=effective,
            local_discarded_tokens_this_cycle=discarded,
            retained_tokens_since_base=retained,
            data_cursor_start=cursor_start,
            data_cursor_end=cursor_end,
            contributor_fence=fence,
            payload_relative_path=_validate_relative_path(
                payload["payload_relative_path"],
                expected=canonical_update_relative_path(stable_key, update_id),
            ),
            payload_size=strict_int(payload["payload_size"], name="payload_size", minimum=1),
            payload_sha256=sha256(payload["payload_sha256"], name="payload_sha256"),
            tensor_schema_sha256=sha256(
                payload["tensor_schema_sha256"], name="tensor_schema_sha256"
            ),
            tensor_dtype=tensor_dtype,
            tensor_numel=strict_int(payload["tensor_numel"], name="tensor_numel", minimum=1),
            created_at=created_at,
        )
        return result

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
