"""SQLite-backed syncer leader lease with monotonic epochs."""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .schema_bootstrap import BootstrapIdentity, open_existing


class LeaseUnavailableError(RuntimeError):
    """Raised when another non-expired owner holds the lease."""


class StaleLeaderTokenError(RuntimeError):
    """Raised when an expired or superseded token attempts an operation."""


@dataclass(frozen=True)
class LeaderToken:
    run_id: str
    epoch: int
    owner_id: str


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
        """Return the local monotonic time left before this token is unsafe."""
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


def make_owner_id(
    *,
    pbs_job_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> str:
    job = pbs_job_id or os.environ.get("PBS_JOBID") or "local"
    host = hostname or socket.gethostname()
    process_id = os.getpid() if pid is None else int(pid)
    return f"{job}:{host}:{process_id}:{uuid.uuid4()}"


class LeaderLeaseStore:
    """The only production writer for ``syncer_leader`` and epoch history."""

    def __init__(
        self,
        database_path: str | Path,
        identity: BootstrapIdentity | Mapping[str, Any],
        *,
        marker_path: str | Path | None = None,
        lease_duration_seconds: float = 90.0,
        max_clock_skew_seconds: float = 2.0,
        busy_timeout_ms: int = 5000,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_duration_seconds <= 0.0:
            raise ValueError("lease_duration_seconds must be > 0")
        if max_clock_skew_seconds < 0.0:
            raise ValueError("max_clock_skew_seconds must be >= 0")
        self.path = Path(database_path)
        self.identity = (
            identity.as_dict() if isinstance(identity, BootstrapIdentity) else dict(identity)
        )
        self.run_id = str(self.identity["run_id"])
        self.lease_duration_seconds = float(lease_duration_seconds)
        self.max_clock_skew_seconds = float(max_clock_skew_seconds)
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self.conn = open_existing(
            self.path,
            self.identity,
            marker_path=marker_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._last_successful_renew: dict[LeaderToken, float] = {}

    def close(self) -> None:
        self.conn.close()

    def observe(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM syncer_leader WHERE singleton = 1").fetchone()
        return None if row is None else dict(row)

    def terminal_state(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM terminal_state WHERE singleton = 1").fetchone()
        return None if row is None else dict(row)

    def control_publication(
        self,
        *,
        kind: str,
        logical_generation: int,
        published_by_epoch: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM control_publications
            WHERE kind = ? AND logical_generation = ? AND published_by_epoch = ?
            """,
            (kind, int(logical_generation), int(published_by_epoch)),
        ).fetchone()
        return None if row is None else dict(row)

    def acquire(
        self,
        *,
        owner_id: str,
        hostname: str,
        pid: int,
        pbs_job_id: str | None = None,
    ) -> LeaderToken:
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        now = float(self._wall_clock())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.conn.execute(
                "SELECT * FROM syncer_leader WHERE singleton = 1"
            ).fetchone()
            if current is None:
                epoch = 1
            else:
                state = str(current["state"])
                expired = now > (float(current["lease_expires_at"]) + self.max_clock_skew_seconds)
                if state != "released" and not expired:
                    raise LeaseUnavailableError(
                        "syncer lease is held by "
                        f"epoch={current['epoch']} owner={current['owner_id']}"
                    )
                epoch = int(current["epoch"]) + 1
                final_state = "released" if state == "released" else "expired"
                self.conn.execute(
                    """
                    UPDATE syncer_epochs
                    SET final_state = COALESCE(final_state, ?),
                        final_at = COALESCE(final_at, ?),
                        superseded_by_epoch = ?
                    WHERE epoch = ?
                    """,
                    (final_state, now, epoch, int(current["epoch"])),
                )
            expires_at = now + self.lease_duration_seconds
            values = (
                epoch,
                owner_id,
                "active",
                pbs_job_id,
                hostname,
                int(pid),
                now,
                now,
                expires_at,
                1,
            )
            self.conn.execute(
                """
                INSERT INTO syncer_leader(
                    singleton, epoch, owner_id, state, pbs_job_id, hostname, pid,
                    acquired_at, renewed_at, lease_expires_at, heartbeat_seq
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    epoch=excluded.epoch,
                    owner_id=excluded.owner_id,
                    state=excluded.state,
                    pbs_job_id=excluded.pbs_job_id,
                    hostname=excluded.hostname,
                    pid=excluded.pid,
                    acquired_at=excluded.acquired_at,
                    renewed_at=excluded.renewed_at,
                    lease_expires_at=excluded.lease_expires_at,
                    heartbeat_seq=excluded.heartbeat_seq
                """,
                values,
            )
            self.conn.execute(
                """
                INSERT INTO syncer_epochs(
                    epoch, owner_id, pbs_job_id, hostname, pid, acquired_at,
                    last_renewed_at, source_fingerprint, config_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    epoch,
                    owner_id,
                    pbs_job_id,
                    hostname,
                    int(pid),
                    now,
                    now,
                    str(self.identity["source_fingerprint"]),
                    str(self.identity["config_sha256"]),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        token = LeaderToken(self.run_id, epoch, owner_id)
        self._last_successful_renew[token] = float(self._monotonic_clock())
        return token

    def renew(self, token: LeaderToken) -> dict[str, Any]:
        self._check_run_id(token)
        now = float(self._wall_clock())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._matching_row(token)
            if row is None or str(row["state"]) != "active":
                raise StaleLeaderTokenError("leader token has been superseded or released")
            if now > float(row["lease_expires_at"]):
                raise StaleLeaderTokenError("expired leader token cannot be renewed")
            expires_at = now + self.lease_duration_seconds
            cursor = self.conn.execute(
                """
                UPDATE syncer_leader
                SET renewed_at = ?, lease_expires_at = ?, heartbeat_seq = heartbeat_seq + 1
                WHERE singleton = 1 AND epoch = ? AND owner_id = ? AND state = 'active'
                """,
                (now, expires_at, token.epoch, token.owner_id),
            )
            if cursor.rowcount != 1:
                raise StaleLeaderTokenError("leader token changed during renew")
            self.conn.execute(
                """
                UPDATE syncer_epochs SET last_renewed_at = ?
                WHERE epoch = ? AND owner_id = ?
                """,
                (now, token.epoch, token.owner_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._last_successful_renew[token] = float(self._monotonic_clock())
        observed = self.observe()
        assert observed is not None
        return observed

    def assert_current(self, token: LeaderToken) -> dict[str, Any]:
        """Verify ownership and the local monotonic lease safety boundary."""
        self._check_run_id(token)
        last_renew = self._last_successful_renew.get(token)
        if last_renew is None:
            raise StaleLeaderTokenError("token was not acquired by this lease store")
        elapsed = float(self._monotonic_clock()) - last_renew
        safe_duration = self.lease_duration_seconds - self.max_clock_skew_seconds
        if elapsed > safe_duration:
            raise StaleLeaderTokenError("leader token crossed its monotonic safety boundary")
        row = self._matching_row(token)
        if row is None or str(row["state"]) != "active":
            raise StaleLeaderTokenError("leader token has been superseded or released")
        return dict(row)

    def release(self, token: LeaderToken) -> None:
        self._check_run_id(token)
        now = float(self._wall_clock())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._matching_row(token)
            if row is None or str(row["state"]) != "active":
                raise StaleLeaderTokenError("only the current active owner may release")
            cursor = self.conn.execute(
                """
                UPDATE syncer_leader
                SET state = 'released', renewed_at = ?, lease_expires_at = ?
                WHERE singleton = 1 AND epoch = ? AND owner_id = ? AND state = 'active'
                """,
                (now, now, token.epoch, token.owner_id),
            )
            if cursor.rowcount != 1:
                raise StaleLeaderTokenError("leader token changed during release")
            self.conn.execute(
                """
                UPDATE syncer_epochs
                SET final_state = 'released', final_at = ?, last_renewed_at = ?
                WHERE epoch = ? AND owner_id = ?
                """,
                (now, now, token.epoch, token.owner_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._last_successful_renew.pop(token, None)

    def _check_run_id(self, token: LeaderToken) -> None:
        if token.run_id != self.run_id:
            raise StaleLeaderTokenError(
                f"token run_id {token.run_id!r} does not match {self.run_id!r}"
            )

    def _matching_row(self, token: LeaderToken) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM syncer_leader
            WHERE singleton = 1 AND epoch = ? AND owner_id = ?
            """,
            (token.epoch, token.owner_id),
        ).fetchone()
