"""Typed scheduler-uncertainty state and operator request boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from ._validation import identity, require_exact_fields, require_mapping, sha256, strict_float


class SchedulerLaunchState(str, Enum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    SUBMISSION_UNKNOWN = "submission_unknown"
    SUBMITTED = "submitted"
    STARTED = "started"
    TERMINAL_UNCERTAIN = "terminal_uncertain"
    ADMITTED = "admitted"
    FAILED = "failed"
    EXPIRED = "expired"
    MANUAL_REVIEW = "manual_review"


class SchedulerOperatorAction(str, Enum):
    CONFIRM_JOB_ID = "confirm_job_id"
    MARK_FAILED = "mark_failed"
    MARK_EXPIRED = "mark_expired"
    RECORD_EXTERNAL_CANCEL_EVIDENCE = "record_external_cancel_evidence"


@dataclass(frozen=True)
class SchedulerOperatorRequest:
    format_version: int
    request_id: str
    launch_request_id: str
    action: SchedulerOperatorAction
    expected_state_sha256: str
    reason: str
    created_at: float
    scheduler_job_id: str | None = None
    evidence_source: str | None = None

    def __post_init__(self) -> None:
        if self.format_version != 1:
            raise ValueError("unsupported scheduler operator request version")
        identity(self.request_id, name="request_id")
        identity(self.launch_request_id, name="launch_request_id")
        if not isinstance(self.action, SchedulerOperatorAction):
            raise ValueError("action must be a SchedulerOperatorAction")
        sha256(self.expected_state_sha256, name="expected_state_sha256")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("operator reason must not be empty")
        timestamp = strict_float(self.created_at, name="created_at")
        if timestamp < 0.0:
            raise ValueError("created_at must be non-negative")
        if self.action is SchedulerOperatorAction.CONFIRM_JOB_ID:
            identity(self.scheduler_job_id, name="scheduler_job_id")
        elif self.scheduler_job_id is not None:
            raise ValueError("scheduler_job_id is only valid for confirm_job_id")
        if self.evidence_source is not None and not self.evidence_source.strip():
            raise ValueError("evidence_source must not be empty")
        object.__setattr__(self, "created_at", timestamp)

    @classmethod
    def from_dict(cls, value: Any) -> "SchedulerOperatorRequest":
        payload = require_mapping(value, name="SchedulerOperatorRequest")
        fields = {
            "format_version",
            "request_id",
            "launch_request_id",
            "action",
            "expected_state_sha256",
            "reason",
            "created_at",
            "scheduler_job_id",
            "evidence_source",
        }
        require_exact_fields(payload, required=fields, name="SchedulerOperatorRequest")
        return cls(
            format_version=payload["format_version"],
            request_id=payload["request_id"],
            launch_request_id=payload["launch_request_id"],
            action=SchedulerOperatorAction(payload["action"]),
            expected_state_sha256=payload["expected_state_sha256"],
            reason=payload["reason"],
            created_at=payload["created_at"],
            scheduler_job_id=payload["scheduler_job_id"],
            evidence_source=payload["evidence_source"],
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["action"] = self.action.value
        return result

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def immutable_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def scheduler_state_sha256(row: dict[str, Any]) -> str:
    """Hash the operator-relevant persisted state, excluding display-only data."""

    projection = {
        key: row.get(key)
        for key in (
            "request_id",
            "state",
            "scheduler_state",
            "first_uncertain_at",
            "last_positive_evidence_at",
            "uncertainty_deadline",
            "evidence_source",
            "manual_reason",
            "admitted_instance_id",
        )
    }
    projection["pbs_job_id"] = row.get("pbs_job_id", row.get("scheduler_job_id"))
    raw = json.dumps(projection, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
