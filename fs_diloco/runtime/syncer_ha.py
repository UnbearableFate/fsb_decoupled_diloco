"""HA candidate acquisition and independent lease renewal utilities."""

from __future__ import annotations

import os
import random
import socket
import sqlite3
import threading
import time
from typing import Any

from ..core.config import Config
from ..observability.logging_utils import JsonlLogger
from ..protocol.control_epoch import EpochControlPublisher
from ..storage.atomic_io import sha256_file
from ..storage.fenced_store import FencedSQLiteStore, LeaderBoundSQLiteStore
from ..storage.leader_lease import (
    LeaderLeaseStore,
    LeaseSafetyTracker,
    LeaderToken,
    LeaseUnavailableError,
    StaleLeaderTokenError,
    make_owner_id,
)
from ..storage.paths import RunPaths
from ..storage.schema_bootstrap import BootstrapIdentity


def _is_sqlite_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("busy", "locked")
    )


def _completed_terminal_controls_valid(
    *,
    paths: RunPaths,
    lease: LeaderLeaseStore,
    terminal: dict[str, Any],
) -> bool:
    epoch = int(terminal["finalized_by_epoch"])
    owner_id = str(terminal["finalized_by_owner_id"])
    generation = int(terminal["generation"])
    expected_paths = {
        "stop": paths.epoch_stop_path(epoch, owner_id, generation),
        "summary": paths.epoch_summary_path(epoch, owner_id, generation),
    }
    for kind, expected_path in expected_paths.items():
        publication = lease.control_publication(
            kind=kind,
            logical_generation=generation,
            published_by_epoch=epoch,
        )
        if publication is None:
            return False
        if str(publication["published_by_owner_id"]) != owner_id:
            return False
        try:
            expected_relative = paths.relative(expected_path)
            if str(publication["relative_path"]) != expected_relative:
                return False
            if not expected_path.is_file():
                return False
            if sha256_file(expected_path) != str(publication["sha256"]):
                return False
        except (OSError, ValueError):
            return False
    return True


class LeaseRenewalThread:
    def __init__(
        self,
        *,
        paths: RunPaths,
        identity: BootstrapIdentity,
        config: Config,
        token: LeaderToken,
        fenced_store: FencedSQLiteStore,
        safety_tracker: LeaseSafetyTracker,
    ) -> None:
        self.paths = paths
        self.identity = identity
        self.config = config
        self.token = token
        self.fenced_store = fenced_store
        self.safety_tracker = safety_tracker
        self._stop = threading.Event()
        self._publish_heartbeats = threading.Event()
        self._started = threading.Event()
        self._failure: BaseException | None = None
        self.busy_retry_count = 0
        self.renew_failure_count = 0
        self._metrics_lock = threading.Lock()
        self._renew_seconds: list[float] = []
        self._renew_cpu_seconds = 0.0
        self._heartbeat_publish_count = 0
        self._heartbeat_publish_wall_seconds = 0.0
        self._heartbeat_publish_cpu_seconds = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name=f"syncer-lease-e{token.epoch:06d}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        if not self._started.wait(timeout=10.0):
            # A delayed thread may still finish opening SQLite after this wait.
            # Set the stop flag before returning so it cannot enter its renewal
            # loop later and keep an otherwise abandoned lease alive.
            self._stop.set()
            self._thread.join(
                timeout=max(
                    5.0,
                    self.config.coordination.syncer_ha.renew_interval_seconds * 2.0,
                )
            )
            raise RuntimeError("lease renewal thread did not start")
        self.raise_if_failed()

    def enable_heartbeats(self) -> None:
        self._publish_heartbeats.set()

    def observation_metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            samples = list(self._renew_seconds)
            return {
                "lease_renew_count": len(samples),
                "lease_renew_failure_count": self.renew_failure_count,
                "lease_renew_busy_retry_count": self.busy_retry_count,
                "lease_renew_seconds": samples,
                "lease_renew_wall_seconds": sum(samples),
                "lease_renew_cpu_seconds": self._renew_cpu_seconds,
                "heartbeat_publish_count": self._heartbeat_publish_count,
                "heartbeat_publish_wall_seconds": (self._heartbeat_publish_wall_seconds),
                "heartbeat_publish_cpu_seconds": (self._heartbeat_publish_cpu_seconds),
            }

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("syncer lease renewal failed") from self._failure

    def stop(self) -> None:
        self._stop.set()
        # ``Thread.start`` itself can fail.  Cleanup must remain safe for that
        # partially initialized case and preserve the original startup error.
        if self._thread.ident is None:
            return
        self._thread.join(
            timeout=max(5.0, self.config.coordination.syncer_ha.renew_interval_seconds * 2.0)
        )
        if self._thread.is_alive():
            raise RuntimeError("lease renewal thread did not stop")
        self.raise_if_failed()

    def _run(self) -> None:
        ha = self.config.coordination.syncer_ha
        lease: LeaderLeaseStore | None = None
        try:
            lease = LeaderLeaseStore(
                self.paths.sqlite_db,
                self.identity,
                marker_path=self.paths.bootstrap_complete_json,
                lease_duration_seconds=ha.lease_duration_seconds,
                max_clock_skew_seconds=ha.max_clock_skew_seconds,
                busy_timeout_ms=ha.lease_busy_timeout_ms,
            )
            publisher = EpochControlPublisher(self.paths, self.fenced_store, self.token)
            self._started.set()
            next_renew = time.monotonic() + ha.renew_interval_seconds
            next_heartbeat = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                wait_until = next_renew
                if self._publish_heartbeats.is_set():
                    wait_until = min(wait_until, next_heartbeat)
                if now < wait_until:
                    self._stop.wait(min(wait_until - now, 0.5))
                    continue
                row: dict[str, Any] | None = None
                if now >= next_renew:
                    renew_started = time.monotonic()
                    renew_cpu_started = time.process_time()
                    try:
                        row = lease.renew(self.token)
                    except sqlite3.OperationalError as exc:
                        if not _is_sqlite_busy(exc):
                            with self._metrics_lock:
                                self.renew_failure_count += 1
                            raise
                        with self._metrics_lock:
                            self.busy_retry_count += 1
                        remaining = self.safety_tracker.remaining_safe_seconds(self.token)
                        if remaining <= 0.0:
                            raise StaleLeaderTokenError(
                                "lease renewal stayed busy through the local safety budget"
                            ) from exc
                        self._stop.wait(min(random.uniform(0.05, 0.2), remaining))
                        next_renew = time.monotonic()
                        continue
                    except BaseException:
                        with self._metrics_lock:
                            self.renew_failure_count += 1
                        raise
                    renew_seconds = time.monotonic() - renew_started
                    renew_cpu_seconds = time.process_time() - renew_cpu_started
                    with self._metrics_lock:
                        self._renew_seconds.append(renew_seconds)
                        self._renew_cpu_seconds += renew_cpu_seconds
                    self.safety_tracker.mark_renewed(self.token)
                    next_renew = now + ha.renew_interval_seconds
                if self._publish_heartbeats.is_set() and now >= next_heartbeat:
                    if row is None:
                        observed = lease.observe()
                        if observed is None:
                            raise RuntimeError("leader row disappeared before heartbeat")
                        row = observed
                    heartbeat_started = time.monotonic()
                    heartbeat_cpu_started = time.process_time()
                    publisher.publish_heartbeat(row)
                    with self._metrics_lock:
                        self._heartbeat_publish_count += 1
                        self._heartbeat_publish_wall_seconds += time.monotonic() - heartbeat_started
                        self._heartbeat_publish_cpu_seconds += (
                            time.process_time() - heartbeat_cpu_started
                        )
                    next_heartbeat = now + ha.heartbeat_interval_seconds
        except BaseException as exc:
            self._failure = exc
            self._started.set()
            self._stop.set()
        finally:
            if lease is not None:
                lease.close()


def acquire_candidate(
    *,
    paths: RunPaths,
    identity: BootstrapIdentity,
    config: Config,
    owner_id: str | None = None,
) -> tuple[LeaderLeaseStore, LeaderToken, LeaseSafetyTracker, JsonlLogger]:
    """Observe without writer transactions, then acquire only after expiry/release."""
    ha = config.coordination.syncer_ha
    owner_id = owner_id or make_owner_id()
    candidate_logger = JsonlLogger(
        paths.logs / "candidates" / f"{RunPaths.owner_short(owner_id)}.jsonl",
        "syncer_candidate",
    )
    deadline = time.monotonic() + ha.candidate_wait_seconds
    hostname = socket.gethostname()
    lease: LeaderLeaseStore | None = None

    def wait_or_timeout(event_type: str, **payload: Any) -> None:
        if time.monotonic() >= deadline:
            if lease is not None:
                lease.close()
            raise TimeoutError("candidate did not acquire leadership before its wait deadline")
        candidate_logger.event(event_type, **payload)
        jitter = random.uniform(0.8, 1.2)
        time.sleep(
            min(ha.candidate_acquire_poll_seconds * jitter, max(0.0, deadline - time.monotonic()))
        )

    while lease is None:
        try:
            lease = LeaderLeaseStore(
                paths.sqlite_db,
                identity,
                marker_path=paths.bootstrap_complete_json,
                lease_duration_seconds=ha.lease_duration_seconds,
                max_clock_skew_seconds=ha.max_clock_skew_seconds,
                busy_timeout_ms=ha.lease_busy_timeout_ms,
            )
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                raise
            wait_or_timeout("candidate_store_busy", error=str(exc))

    while True:
        try:
            terminal = lease.terminal_state()
            observed = lease.observe()
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                lease.close()
                raise
            wait_or_timeout("candidate_observation_busy", error=str(exc))
            continue
        if terminal is not None and terminal.get("stop_reason") not in (
            None,
            "",
            "error",
        ):
            if _completed_terminal_controls_valid(
                paths=paths,
                lease=lease,
                terminal=terminal,
            ):
                lease.close()
                raise RuntimeError(
                    f"candidate cannot acquire a terminal run: {terminal['stop_reason']}"
                )
            candidate_logger.event(
                "terminal_repair_required",
                generation=int(terminal["generation"]),
                finalized_by_epoch=int(terminal["finalized_by_epoch"]),
            )
        now_wall = time.time()
        eligible = observed is None or str(observed["state"]) == "released"
        if observed is not None and str(observed["state"]) == "active":
            eligible = now_wall > (float(observed["lease_expires_at"]) + ha.max_clock_skew_seconds)
        if eligible:
            try:
                candidate_logger.event(
                    "candidate_writer_transaction_attempt",
                    observed_epoch=None if observed is None else int(observed["epoch"]),
                    observed_owner=None if observed is None else str(observed["owner_id"]),
                    observed_state=None if observed is None else str(observed["state"]),
                )
                token = lease.acquire(
                    owner_id=owner_id,
                    hostname=hostname,
                    pid=os.getpid(),
                    pbs_job_id=os.environ.get("PBS_JOBID"),
                )
                candidate_logger.event(
                    "leadership_acquired", epoch=token.epoch, owner_id=token.owner_id
                )
                safety_tracker = LeaseSafetyTracker(
                    token,
                    lease_duration_seconds=ha.lease_duration_seconds,
                    max_clock_skew_seconds=ha.max_clock_skew_seconds,
                )
                return lease, token, safety_tracker, candidate_logger
            except LeaseUnavailableError:
                pass
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_busy(exc):
                    lease.close()
                    raise
                wait_or_timeout(
                    "writer_lock_blocked",
                    error=str(exc),
                    observed_epoch=None if observed is None else int(observed["epoch"]),
                    observed_owner=None if observed is None else str(observed["owner_id"]),
                    observed_pbs_job_id=(None if observed is None else observed.get("pbs_job_id")),
                    observed_hostname=None if observed is None else observed.get("hostname"),
                    observed_pid=None if observed is None else observed.get("pid"),
                )
                continue
        wait_or_timeout(
            "leadership_wait",
            observed_epoch=None if observed is None else int(observed["epoch"]),
            observed_owner=None if observed is None else str(observed["owner_id"]),
            observed_state=None if observed is None else str(observed["state"]),
        )


def open_leader_store(
    *,
    paths: RunPaths,
    identity: BootstrapIdentity,
    config: Config,
    token: LeaderToken,
    safety_tracker: LeaseSafetyTracker,
) -> LeaderBoundSQLiteStore:
    ha = config.coordination.syncer_ha
    fenced = FencedSQLiteStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=ha.max_clock_skew_seconds,
        busy_timeout_ms=ha.business_busy_timeout_ms,
        gc_grace_seconds=ha.lease_duration_seconds + ha.max_clock_skew_seconds,
        max_retained_epoch_dirs=ha.max_retained_epoch_dirs,
        lease_safety_check=safety_tracker.assert_safe,
    )
    return fenced.bind(token)
