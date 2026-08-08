"""Typed application objects exchanged with the v4 authority boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from ._validation import identity, sha256, strict_float, strict_int
from .contributor import ContributorFence
from .proposal import FullUpdateProposalV2


class ProposalDisposition(str, Enum):
    ACCEPTED = "accepted"
    EXACT_REPLAY = "exact_replay"
    CONFLICT = "conflict"
    IDENTITY_COLLISION = "identity_collision"
    MALFORMED = "malformed"
    STALE_FENCE = "stale_fence"
    POST_FENCE = "post_fence"


class ReadStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    TRANSIENT_IO = "transient_io"
    MALFORMED = "malformed"
    IDENTITY_MISMATCH = "identity_mismatch"


T = TypeVar("T")


@dataclass(frozen=True)
class ReadResult(Generic[T]):
    status: ReadStatus
    value: T | None = None
    diagnostic: str | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.status is ReadStatus.OK and self.value is None:
            raise ValueError("an OK read result requires a value")
        if self.status is not ReadStatus.OK and self.value is not None:
            raise ValueError("a non-OK read result cannot carry a value")


class LaunchState(str, Enum):
    REQUESTED = "requested"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    UNCERTAIN = "uncertain"
    ADMITTED = "admitted"
    FAILED = "failed"
    EXPIRED = "expired"
    MANUAL_REVIEW = "manual_review"


class TerminalState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    DRAINING = "draining"
    FINALIZED = "finalized"
    ERROR = "error"


@dataclass(frozen=True)
class SelectionCandidate:
    proposal: FullUpdateProposalV2
    selection_credit: int

    def __post_init__(self) -> None:
        strict_int(self.selection_credit, name="selection_credit", minimum=0)

    @property
    def stable_key(self) -> str:
        return self.proposal.stable_contributor_key


@dataclass(frozen=True)
class SelectionBatch:
    batch_id: str
    command_id: str
    owner_epoch: int
    target_version: int
    candidates: tuple[SelectionCandidate, ...]
    state: str = "selected"

    def __post_init__(self) -> None:
        identity(self.batch_id, name="batch_id")
        identity(self.command_id, name="command_id")
        strict_int(self.owner_epoch, name="owner_epoch", minimum=1)
        strict_int(self.target_version, name="target_version", minimum=0)
        if self.state not in {"selected", "prepared", "committed", "abandoned"}:
            raise ValueError(f"invalid selection batch state: {self.state}")
        keys = [candidate.stable_key for candidate in self.candidates]
        if len(set(keys)) != len(keys):
            raise ValueError("a selection batch cannot contain duplicate contributors")


@dataclass(frozen=True)
class PublicationIntent:
    publication_id: str
    command_id: str
    owner_epoch: int
    target_version: int
    predecessor_version: int | None
    selection_batch_id: str | None
    weight_relative_path: str
    weight_size: int
    weight_sha256: str
    optim_relative_path: str
    optim_size: int
    optim_sha256: str
    state: str = "prepared"

    def __post_init__(self) -> None:
        identity(self.publication_id, name="publication_id")
        identity(self.command_id, name="command_id")
        strict_int(self.owner_epoch, name="owner_epoch", minimum=1)
        strict_int(self.target_version, name="target_version", minimum=0)
        if self.target_version == 0:
            if self.predecessor_version is not None:
                raise ValueError("v0 publication cannot have a predecessor")
        elif self.predecessor_version != self.target_version - 1:
            raise ValueError("publication predecessor must be target_version - 1")
        if self.selection_batch_id is not None:
            identity(self.selection_batch_id, name="selection_batch_id")
        for name, path in (
            ("weight_relative_path", self.weight_relative_path),
            ("optim_relative_path", self.optim_relative_path),
        ):
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"{name} must be normalized and run-root-relative")
        strict_int(self.weight_size, name="weight_size", minimum=1)
        strict_int(self.optim_size, name="optim_size", minimum=1)
        sha256(self.weight_sha256, name="weight_sha256")
        sha256(self.optim_sha256, name="optim_sha256")
        if self.state not in {"prepared", "committed", "abandoned"}:
            raise ValueError(f"invalid publication intent state: {self.state}")


@dataclass(frozen=True)
class StaticBinding:
    learner_id: str
    logical_launch_id: str
    attempt_id: str
    binding_generation: int
    status: str

    def __post_init__(self) -> None:
        identity(self.learner_id, name="learner_id")
        identity(self.logical_launch_id, name="logical_launch_id")
        identity(self.attempt_id, name="attempt_id")
        strict_int(self.binding_generation, name="binding_generation", minimum=1)
        if self.status not in {"active", "terminal", "replaced"}:
            raise ValueError(f"invalid static binding status: {self.status}")


@dataclass(frozen=True)
class ContributorProgress:
    stable_contributor_key: str
    last_cycle_seq: int
    last_receipt_id: str | None
    last_receipt_sha256: str | None
    data_cursor: int
    updated_at: float

    def __post_init__(self) -> None:
        identity(self.stable_contributor_key, name="stable_contributor_key")
        strict_int(self.last_cycle_seq, name="last_cycle_seq", minimum=0)
        strict_int(self.data_cursor, name="data_cursor", minimum=0)
        strict_float(self.updated_at, name="updated_at")
        if self.last_cycle_seq == 0:
            if self.last_receipt_id is not None or self.last_receipt_sha256 is not None:
                raise ValueError("empty progress cannot name a last receipt")
        else:
            identity(self.last_receipt_id, name="last_receipt_id")
            sha256(self.last_receipt_sha256, name="last_receipt_sha256")


@dataclass(frozen=True)
class RetireIncarnation:
    instance_id: str
    reason: str
    fence: ContributorFence
