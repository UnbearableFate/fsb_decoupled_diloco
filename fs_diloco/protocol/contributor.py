"""Typed static and dynamic contributor fences for Full Protocol v4."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from ._validation import (
    identity,
    nonempty_string,
    require_exact_fields,
    require_mapping,
    sha256,
    strict_int,
)


@dataclass(frozen=True)
class StaticContributorFence:
    kind: Literal["static"]
    learner_id: str
    logical_launch_id: str
    attempt_id: str
    binding_generation: int

    @property
    def stable_contributor_key(self) -> str:
        return self.learner_id

    @classmethod
    def from_dict(cls, value: Any) -> "StaticContributorFence":
        payload = require_mapping(value, name="static contributor fence")
        require_exact_fields(
            payload,
            required={
                "kind",
                "learner_id",
                "logical_launch_id",
                "attempt_id",
                "binding_generation",
            },
            name="static contributor fence",
        )
        if payload["kind"] != "static":
            raise ValueError("static contributor fence kind must be 'static'")
        return cls(
            kind="static",
            learner_id=identity(payload["learner_id"], name="learner_id"),
            logical_launch_id=identity(payload["logical_launch_id"], name="logical_launch_id"),
            attempt_id=identity(payload["attempt_id"], name="attempt_id"),
            binding_generation=strict_int(
                payload["binding_generation"], name="binding_generation", minimum=1
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "learner_id": self.learner_id,
            "logical_launch_id": self.logical_launch_id,
            "attempt_id": self.attempt_id,
            "binding_generation": self.binding_generation,
        }


@dataclass(frozen=True)
class DynamicContributorFence:
    kind: Literal["dynamic"]
    instance_id: str
    placement_id: str
    placement_epoch: int
    stream_id: int
    stream_epoch: int
    admission_generation: int
    admission_token_sha256: str

    @property
    def stable_contributor_key(self) -> str:
        return str(self.stream_id)

    @classmethod
    def from_dict(cls, value: Any) -> "DynamicContributorFence":
        payload = require_mapping(value, name="dynamic contributor fence")
        require_exact_fields(
            payload,
            required={
                "kind",
                "instance_id",
                "placement_id",
                "placement_epoch",
                "stream_id",
                "stream_epoch",
                "admission_generation",
                "admission_token_sha256",
            },
            name="dynamic contributor fence",
        )
        if payload["kind"] != "dynamic":
            raise ValueError("dynamic contributor fence kind must be 'dynamic'")
        return cls(
            kind="dynamic",
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
        return {
            "kind": self.kind,
            "instance_id": self.instance_id,
            "placement_id": self.placement_id,
            "placement_epoch": self.placement_epoch,
            "stream_id": self.stream_id,
            "stream_epoch": self.stream_epoch,
            "admission_generation": self.admission_generation,
            "admission_token_sha256": self.admission_token_sha256,
        }


ContributorFence: TypeAlias = StaticContributorFence | DynamicContributorFence


def decode_contributor_fence(value: Any) -> ContributorFence:
    payload = require_mapping(value, name="contributor_fence")
    kind = nonempty_string(payload.get("kind"), name="contributor_fence.kind")
    if kind == "static":
        return StaticContributorFence.from_dict(payload)
    if kind == "dynamic":
        return DynamicContributorFence.from_dict(payload)
    raise ValueError(f"unknown contributor fence kind: {kind}")


@dataclass(frozen=True)
class StaticMembershipScope:
    learner_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.learner_ids:
            raise ValueError("static membership scope must not be empty")
        normalized = tuple(identity(item, name="learner_id") for item in self.learner_ids)
        if len(set(normalized)) != len(normalized):
            raise ValueError("static membership learner IDs must be unique")
        object.__setattr__(self, "learner_ids", normalized)


@dataclass(frozen=True)
class DynamicMembershipScope:
    stream_pool_size: int

    def __post_init__(self) -> None:
        strict_int(self.stream_pool_size, name="stream_pool_size", minimum=1)
