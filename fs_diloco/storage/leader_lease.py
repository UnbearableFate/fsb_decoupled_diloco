"""Typed leader-lease values and the local monotonic safety boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass


class LeaseUnavailableError(RuntimeError):
    """Raised when another non-expired owner holds the lease."""


class StaleLeaderTokenError(RuntimeError):
    """Raised when an expired or superseded token attempts an operation."""


@dataclass(frozen=True)
class LeaderToken:
    run_id: str
    epoch: int
    owner_id: str


@dataclass(frozen=True)
class CommittedLeaderLease:
    """Exact lease lifetime committed by the authority transaction."""

    token: LeaderToken
    renewed_at: float
    lease_expires_at: float
    heartbeat_seq: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.renewed_at) or self.renewed_at < 0.0:
            raise ValueError("leader lease renewed_at must be non-negative")
        if not math.isfinite(self.lease_expires_at) or self.lease_expires_at <= self.renewed_at:
            raise ValueError("leader lease expiry must be newer than renewal")
        if (
            isinstance(self.heartbeat_seq, bool)
            or not isinstance(self.heartbeat_seq, int)
            or self.heartbeat_seq < 1
        ):
            raise ValueError("leader lease heartbeat_seq must be a positive integer")
