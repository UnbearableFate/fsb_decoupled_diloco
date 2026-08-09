"""Typed leader-lease values and the local monotonic safety boundary."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


PLAN03_REQUIREMENTS = frozenset({"CLOCK-01"})


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
    """Exact lease lifetime committed by the v4 authority transaction."""

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


class LeaseSafetyTracker:
    """Thread-safe local monotonic boundary shared by renewer and business writes."""

    def __init__(
        self,
        token: LeaderToken,
        *,
        lease_duration_seconds: float,
        max_clock_skew_seconds: float,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token = token
        self.safe_duration_seconds = float(lease_duration_seconds) - float(max_clock_skew_seconds)
        if self.safe_duration_seconds <= 0.0:
            raise ValueError("lease safety duration must be > 0")
        self._monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._last_successful_renew = float(monotonic_clock())

    def mark_renewed(self, token: LeaderToken) -> None:
        self._check_token(token)
        with self._lock:
            self._last_successful_renew = float(self._monotonic_clock())

    def assert_safe(self, token: LeaderToken) -> None:
        self._check_token(token)
        now = float(self._monotonic_clock())
        with self._lock:
            elapsed = now - self._last_successful_renew
        if elapsed < 0.0 or elapsed > self.safe_duration_seconds:
            raise StaleLeaderTokenError("leader token crossed its local monotonic safety boundary")

    def remaining_safe_seconds(self, token: LeaderToken) -> float:
        self._check_token(token)
        now = float(self._monotonic_clock())
        with self._lock:
            elapsed = now - self._last_successful_renew
        if elapsed < 0.0:
            return 0.0
        return max(0.0, self.safe_duration_seconds - elapsed)

    def _check_token(self, token: LeaderToken) -> None:
        if token != self.token:
            raise StaleLeaderTokenError("lease safety tracker token mismatch")
