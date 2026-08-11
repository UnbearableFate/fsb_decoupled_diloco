"""The sole stream-incarnation membership types for the Full Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._validation import (
    identity,
    require_exact_fields,
    require_mapping,
    sha256,
    strict_int,
)


@dataclass(frozen=True)
class ContributorFence:
    """Prove that one admitted instance owns the current stream incarnation."""

    instance_id: str  # Concrete learner process identity.
    placement_id: str  # Scheduler placement identity for the process.
    placement_epoch: int  # Monotonic incarnation of the placement.
    stream_id: int  # Stable logical contributor and data-shard identity.
    stream_epoch: int  # Monotonic incarnation of the stream.
    admission_generation: int  # Monotonic admission grant generation.
    admission_token_sha256: str  # Digest of the unpersisted admission capability.

    def __post_init__(self) -> None:
        """Reject incomplete or structurally invalid ownership credentials."""

        identity(self.instance_id, name="instance_id")
        identity(self.placement_id, name="placement_id")
        strict_int(self.placement_epoch, name="placement_epoch", minimum=1)
        strict_int(self.stream_id, name="stream_id", minimum=0)
        strict_int(self.stream_epoch, name="stream_epoch", minimum=1)
        strict_int(self.admission_generation, name="admission_generation", minimum=1)
        sha256(self.admission_token_sha256, name="admission_token_sha256")

    @property
    def stable_contributor_key(self) -> str:
        """Return the stable contributor key used by proposals and progress."""

        return str(self.stream_id)

    @classmethod
    def from_dict(cls, value: Any) -> "ContributorFence":
        """Decode the exact current fence shape and reject all legacy fields."""

        payload = require_mapping(value, name="contributor fence")
        require_exact_fields(
            payload,
            required={
                "instance_id",
                "placement_id",
                "placement_epoch",
                "stream_id",
                "stream_epoch",
                "admission_generation",
                "admission_token_sha256",
            },
            name="contributor fence",
        )
        return cls(
            instance_id=identity(payload["instance_id"], name="instance_id"),
            placement_id=identity(payload["placement_id"], name="placement_id"),
            placement_epoch=strict_int(
                payload["placement_epoch"], name="placement_epoch", minimum=1
            ),
            stream_id=strict_int(payload["stream_id"], name="stream_id", minimum=0),
            stream_epoch=strict_int(payload["stream_epoch"], name="stream_epoch", minimum=1),
            admission_generation=strict_int(
                payload["admission_generation"], name="admission_generation", minimum=1
            ),
            admission_token_sha256=sha256(
                payload["admission_token_sha256"], name="admission_token_sha256"
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical wire representation without a mode discriminator."""

        return {
            "instance_id": self.instance_id,
            "placement_id": self.placement_id,
            "placement_epoch": self.placement_epoch,
            "stream_id": self.stream_id,
            "stream_epoch": self.stream_epoch,
            "admission_generation": self.admission_generation,
            "admission_token_sha256": self.admission_token_sha256,
        }


@dataclass(frozen=True)
class MembershipScope:
    """Describe the immutable logical stream pool for one run."""

    stream_pool_size: int  # Total logical contributors and dataset shards.

    def __post_init__(self) -> None:
        """Require at least one stream in every run."""

        strict_int(self.stream_pool_size, name="stream_pool_size", minimum=1)
