"""Explicit legacy, fenced HA, and read-only SQLite store surfaces."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from .leader_lease import LeaderToken, StaleLeaderTokenError
from .schema_bootstrap import BootstrapIdentity, open_existing, open_readonly
from .sqlite_store import SQLiteStore


LegacySQLiteStore = SQLiteStore


_DDL_KEYWORDS = {"ALTER", "ATTACH", "CREATE", "DETACH", "DROP", "REINDEX", "VACUUM"}
_MUTATING_KEYWORDS = {"DELETE", "INSERT", "REPLACE", "UPDATE"}
_READ_KEYWORDS = {"EXPLAIN", "SELECT"}
_TRANSACTION_KEYWORDS = {"BEGIN", "SAVEPOINT"}
_READ_ONLY_PRAGMAS = {
    "BUSY_TIMEOUT",
    "FOREIGN_KEY_CHECK",
    "FREELIST_COUNT",
    "INTEGRITY_CHECK",
    "JOURNAL_MODE",
    "PAGE_COUNT",
    "QUERY_ONLY",
    "QUICK_CHECK",
    "SYNCHRONOUS",
    "TABLE_INFO",
    "USER_VERSION",
}
_READ_ONLY_ARGUMENT_PRAGMAS = {
    "FOREIGN_KEY_CHECK",
    "INTEGRITY_CHECK",
    "QUICK_CHECK",
    "TABLE_INFO",
}
_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(?:(?:main|temp)\.)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*\((?P<argument>[^()]*)\))?\s*;?\s*$",
    re.IGNORECASE,
)


def _pbs_job_identity(value: Any) -> str:
    return str(value).strip().split(".", 1)[0]


def _keyword(sql: str) -> str:
    statement = sql.lstrip()
    while statement.startswith("--"):
        _, _, statement = statement.partition("\n")
        statement = statement.lstrip()
    return statement.split(None, 1)[0].upper() if statement else ""


def _read_only_pragma(sql: str) -> bool:
    if "=" in sql:
        return False
    match = _PRAGMA_RE.fullmatch(sql)
    if match is None:
        return False
    pragma_name = match.group("name").upper()
    if pragma_name not in _READ_ONLY_PRAGMAS:
        return False
    argument = match.group("argument")
    return argument is None or pragma_name in _READ_ONLY_ARGUMENT_PRAGMAS


def _parameters(
    values: Iterable[Any] | Mapping[str, Any],
) -> tuple[Any, ...] | Mapping[str, Any]:
    return values if isinstance(values, Mapping) else tuple(values)


class _FencedConnection:
    """Connection proxy that injects a lease check into every write transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        max_clock_skew_seconds: float,
        wall_clock: Callable[[], float],
        lease_safety_check: Callable[[LeaderToken], None] | None,
    ) -> None:
        self._connection = connection
        self._run_id = run_id
        self._max_clock_skew_seconds = float(max_clock_skew_seconds)
        self._wall_clock = wall_clock
        self._lease_safety_check = lease_safety_check
        self._active_token: LeaderToken | None = None
        connection.create_function("current_leader_epoch", 0, self._current_epoch)
        connection.create_function("current_leader_owner", 0, self._current_owner)

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    @property
    def total_changes(self) -> int:
        return self._connection.total_changes

    @property
    def row_factory(self) -> Any:
        return self._connection.row_factory

    def activate(self, token: LeaderToken) -> None:
        if self._active_token is not None:
            raise RuntimeError("nested fenced store mutation is not supported")
        if token.run_id != self._run_id:
            raise StaleLeaderTokenError("leader token belongs to another run")
        self._active_token = token

    def deactivate(self) -> None:
        self._active_token = None

    def preflight(self) -> None:
        self._verify_token()

    def execute(
        self,
        sql: str,
        parameters: Iterable[Any] | Mapping[str, Any] = (),
    ) -> sqlite3.Cursor:
        keyword = _keyword(sql)
        if keyword in _DDL_KEYWORDS:
            raise RuntimeError("DDL is forbidden after HA bootstrap")
        if keyword in _TRANSACTION_KEYWORDS:
            self._require_active_token()
            cursor = self._connection.execute(sql, _parameters(parameters))
            self._verify_token()
            return cursor
        if keyword in _MUTATING_KEYWORDS:
            self._ensure_write_transaction()
            self._verify_token()
        elif keyword not in _READ_KEYWORDS and not (keyword == "PRAGMA" and _read_only_pragma(sql)):
            raise RuntimeError(f"unrecognized SQL statement is forbidden by the fence: {keyword}")
        return self._connection.execute(sql, _parameters(parameters))

    def executemany(
        self,
        sql: str,
        seq_of_parameters: Iterable[Iterable[Any] | Mapping[str, Any]],
    ) -> sqlite3.Cursor:
        keyword = _keyword(sql)
        if keyword in _DDL_KEYWORDS:
            raise RuntimeError("DDL is forbidden after HA bootstrap")
        if keyword not in _MUTATING_KEYWORDS:
            raise RuntimeError("executemany is restricted to fenced mutations")
        self._ensure_write_transaction()
        self._verify_token()
        return self._connection.executemany(sql, seq_of_parameters)

    def commit(self) -> None:
        if self._connection.in_transaction:
            self._verify_token()
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def _ensure_write_transaction(self) -> None:
        self._require_active_token()
        if not self._connection.in_transaction:
            self._connection.execute("BEGIN IMMEDIATE")
            self._verify_token()

    def _require_active_token(self) -> LeaderToken:
        if self._active_token is None:
            raise StaleLeaderTokenError("HA mutation requires a LeaderToken")
        return self._active_token

    def _verify_token(self) -> sqlite3.Row:
        token = self._require_active_token()
        if self._lease_safety_check is not None:
            self._lease_safety_check(token)
        row = self._connection.execute(
            """
            SELECT * FROM syncer_leader
            WHERE singleton = 1 AND epoch = ? AND owner_id = ? AND state = 'active'
            """,
            (token.epoch, token.owner_id),
        ).fetchone()
        if row is None:
            raise StaleLeaderTokenError("leader token has been superseded or released")
        safe_expiry = float(row["lease_expires_at"]) - self._max_clock_skew_seconds
        if float(self._wall_clock()) > safe_expiry:
            raise StaleLeaderTokenError("leader token crossed the lease safety boundary")
        return row

    def _current_epoch(self) -> int:
        return self._require_active_token().epoch

    def _current_owner(self) -> str:
        return self._require_active_token().owner_id


_READ_METHODS = {
    "active_payload_paths",
    "committed_global_count",
    "current_fragment_versions",
    "eligible_updates",
    "fragment_proposal_frontiers",
    "gc_pending_count",
    "gc_pending_paths",
    "get_global_version",
    "get_run_state",
    "get_update",
    "historical_version_rows",
    "integrity_check",
    "ha_gc_candidate_paths",
    "latest_global_version",
    "learner_resource_peaks",
    "list_learners",
    "list_fragment_versions",
    "pending_updates",
    "pointer_signature_is_cached",
    "pragma_settings",
    "proposal_frontiers",
    "terminal_update_rows",
}


class FencedSQLiteStore:
    """HA full-mode store whose public mutators all require a leader token."""

    def __init__(
        self,
        database_path: str | Path,
        identity: BootstrapIdentity | Mapping[str, Any],
        *,
        marker_path: str | Path | None = None,
        max_clock_skew_seconds: float = 2.0,
        busy_timeout_ms: int = 60_000,
        gc_grace_seconds: float = 92.0,
        max_retained_epoch_dirs: int = 32,
        wall_clock: Callable[[], float] = time.time,
        lease_safety_check: Callable[[LeaderToken], None],
    ) -> None:
        identity_payload = (
            identity.as_dict() if isinstance(identity, BootstrapIdentity) else dict(identity)
        )
        raw = open_existing(
            database_path,
            identity_payload,
            marker_path=marker_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        guarded = _FencedConnection(
            raw,
            run_id=str(identity_payload["run_id"]),
            max_clock_skew_seconds=max_clock_skew_seconds,
            wall_clock=wall_clock,
            lease_safety_check=lease_safety_check,
        )
        legacy = SQLiteStore.__new__(SQLiteStore)
        legacy.path = Path(database_path)
        legacy.conn = guarded
        legacy._pointer_signatures = {}
        self.path = Path(database_path)
        self._legacy = legacy
        self._connection = guarded
        self._mutation_lock = threading.RLock()
        self._business_metrics_lock = threading.Lock()
        self._business_transaction_seconds: deque[float] = deque(maxlen=10_000)
        self._business_transaction_count = 0
        self._business_transaction_failure_count = 0
        self._inline_capacity_thread_id: int | None = None
        self._wall_clock = wall_clock
        self.gc_grace_seconds = float(gc_grace_seconds)
        self.max_retained_epoch_dirs = int(max_retained_epoch_dirs)
        self.run_id = str(identity_payload["run_id"])
        self.config_sha256 = str(identity_payload["config_sha256"])
        self.dynamic_mode = str(identity_payload["mode"]) == "full_dynamic"

    @property
    def conn(self) -> None:
        raise AttributeError("FencedSQLiteStore does not expose a writable connection")

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AttributeError("raw execute is not part of the fenced store API")

    def close(self) -> None:
        self._connection.close()

    def bind(self, token: LeaderToken) -> "LeaderBoundSQLiteStore":
        return LeaderBoundSQLiteStore(self, token)

    def __getattr__(self, name: str) -> Any:
        if name in _READ_METHODS:
            return getattr(self._legacy, name)
        raise AttributeError(name)

    def cache_pointer_signature(
        self, path: str | Path, signature: tuple[int, int, int, int]
    ) -> None:
        self._legacy.cache_pointer_signature(path, signature)

    def controller_state(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM controller_state WHERE singleton = 1"
        ).fetchone()
        return None if row is None else dict(row)

    def terminal_state(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM terminal_state WHERE singleton = 1"
        ).fetchone()
        return None if row is None else dict(row)

    def current_instances(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT * FROM learner_instances
            WHERE status IN ('admitted', 'draining', 'drained')
            ORDER BY stream_id, instance_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def streams(self) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM streams ORDER BY stream_id").fetchall()
        return [dict(row) for row in rows]

    def launch_requests(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        where = (
            "WHERE state IN ('planned','submitting','submitted','started',"
            "'external_submitted','submission_unknown','retryable')"
            if active_only
            else ""
        )
        rows = self._connection.execute(
            f"SELECT * FROM launch_requests {where} ORDER BY created_at, request_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def registration_requests(self, *, unresolved_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE state='pending'" if unresolved_only else ""
        rows = self._connection.execute(
            f"SELECT * FROM registration_requests {where} ORDER BY created_at, instance_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def capacity_observations(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM capacity_observations ORDER BY observation_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def dynamic_state_counts(self) -> dict[str, int]:
        if not self.dynamic_mode:
            return {}
        return {
            "current_instances": int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM learner_instances
                    WHERE status IN ('admitted','draining','drained')
                    """
                ).fetchone()[0]
            ),
            "registration_requests": int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM registration_requests WHERE state='pending'"
                ).fetchone()[0]
            ),
            "active_launch_requests": int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM launch_requests
                    WHERE state IN ('planned','submitting','submitted','started',
                                    'external_submitted','submission_unknown','retryable')
                    """
                ).fetchone()[0]
            ),
            "capacity_observations": int(
                self._connection.execute("SELECT COUNT(*) FROM capacity_observations").fetchone()[0]
            ),
        }

    def dynamic_history_to_archive(
        self,
        *,
        now: float,
        expired_retention_seconds: float,
        max_active_instance_records: int,
        capacity_observation_retention_count: int,
    ) -> dict[str, list[dict[str, Any]]]:
        cutoff = float(now) - float(expired_retention_seconds)
        excess_instances = max(
            0,
            int(self._connection.execute("SELECT COUNT(*) FROM learner_instances").fetchone()[0])
            - int(max_active_instance_records),
        )
        terminal_instances = self._connection.execute(
            """
            SELECT * FROM learner_instances AS li
            WHERE li.status IN ('revoked','stopped','expired')
              AND NOT EXISTS (
                  SELECT 1 FROM updates AS u
                  WHERE u.learner_instance_id=li.instance_id
              )
            ORDER BY COALESCE(expired_at, stopped_at, last_seen, registered_at), instance_id
            """
        ).fetchall()
        instances = [
            row
            for index, row in enumerate(terminal_instances)
            if index < excess_instances
            or float(
                row["expired_at"] or row["stopped_at"] or row["last_seen"] or row["registered_at"]
            )
            <= cutoff
        ]
        registrations_all = self._connection.execute(
            """
            SELECT * FROM registration_requests
            WHERE state IN ('admitted','rejected','expired','capacity_fulfilled')
            ORDER BY created_at, instance_id
            """
        ).fetchall()
        excess_registrations = max(
            0,
            len(registrations_all) - max(1, 2 * int(max_active_instance_records)),
        )
        registrations = [
            row
            for index, row in enumerate(registrations_all)
            if index < excess_registrations
            or float(row["expires_at"] or row["created_at"]) <= cutoff
        ]
        launches = self._connection.execute(
            """
            SELECT * FROM launch_requests
            WHERE state IN ('completed','failed','expired','cancelled','capacity_fulfilled')
              AND reason != 'bootstrap'
              AND updated_at <= ?
            ORDER BY updated_at, request_id
            """,
            (cutoff,),
        ).fetchall()
        observations = self._connection.execute(
            """
            SELECT * FROM capacity_observations
            WHERE observation_seq <= (
                SELECT COALESCE(MAX(observation_seq), 0) - ? FROM capacity_observations
            )
            ORDER BY observation_seq
            """,
            (int(capacity_observation_retention_count),),
        ).fetchall()
        registration_ids = {str(row["instance_id"]) for row in (*instances, *registrations)}
        registration_publications: list[sqlite3.Row] = []
        for instance_id in sorted(registration_ids):
            registration_publications.extend(
                self._connection.execute(
                    """
                    SELECT * FROM control_publications
                    WHERE kind=?
                    ORDER BY published_by_epoch, logical_generation
                    """,
                    (f"registration:{instance_id}",),
                ).fetchall()
            )
        return {
            "learner_instances": [dict(row) for row in instances],
            "registration_requests": [dict(row) for row in registrations],
            "launch_requests": [dict(row) for row in launches],
            "capacity_observations": [dict(row) for row in observations],
            "registration_publications": [dict(row) for row in registration_publications],
        }

    def delete_archived_dynamic_history(
        self,
        token: LeaderToken,
        *,
        instance_ids: list[str],
        registration_instance_ids: list[str],
        launch_request_ids: list[str],
        observation_keys: list[str],
        registration_publication_kinds: list[str] = (),
    ) -> None:
        def operation(conn: _FencedConnection) -> None:
            for instance_id in instance_ids:
                row = conn.execute(
                    "SELECT status FROM learner_instances WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
                if row is None:
                    continue
                if str(row["status"]) not in {"revoked", "stopped", "expired"}:
                    raise RuntimeError("cannot archive a current learner instance")
                referenced = conn.execute(
                    "SELECT 1 FROM updates WHERE learner_instance_id=? LIMIT 1",
                    (instance_id,),
                ).fetchone()
                if referenced is not None:
                    raise RuntimeError("cannot archive an instance referenced by an update")
                conn.execute("DELETE FROM learner_instances WHERE instance_id=?", (instance_id,))
                conn.execute("DELETE FROM proposal_frontiers WHERE learner_id=?", (instance_id,))
            for instance_id in registration_instance_ids:
                conn.execute(
                    """
                    DELETE FROM registration_requests
                    WHERE instance_id=?
                      AND state IN ('admitted','rejected','expired','capacity_fulfilled')
                    """,
                    (instance_id,),
                )
            for request_id in launch_request_ids:
                conn.execute(
                    """
                    DELETE FROM launch_requests
                    WHERE request_id=?
                      AND state IN ('completed','failed','expired','cancelled','capacity_fulfilled')
                    """,
                    (request_id,),
                )
            for observation_key in observation_keys:
                conn.execute(
                    "DELETE FROM capacity_observations WHERE observation_key=?",
                    (observation_key,),
                )
            for kind in registration_publication_kinds:
                conn.execute(
                    "DELETE FROM control_publications WHERE kind=?",
                    (kind,),
                )

        self._transaction(token, operation)

    def archivable_ha_history(self, *, before_epoch: int) -> dict[str, Any]:
        epochs = self._connection.execute(
            "SELECT * FROM syncer_epochs WHERE epoch < ? ORDER BY epoch",
            (int(before_epoch),),
        ).fetchall()
        publications = self._connection.execute(
            """
            SELECT * FROM control_publications
            WHERE published_by_epoch < ?
            ORDER BY published_by_epoch, kind, logical_generation
            """,
            (int(before_epoch),),
        ).fetchall()
        return {
            "epochs": [dict(row) for row in epochs],
            "control_publications": [dict(row) for row in publications],
        }

    def ha_gc_candidate_paths(self) -> set[str]:
        rows = self._connection.execute("SELECT relative_path FROM gc_candidates").fetchall()
        return {str(row["relative_path"]) for row in rows}

    def ready_gc_candidates(self, *, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else float(now)
        rows = self._connection.execute(
            """
            SELECT * FROM gc_candidates
            WHERE state IN ('pending', 'deleting') AND not_before <= ?
            ORDER BY recorded_at, relative_path
            """,
            (timestamp,),
        ).fetchall()
        return [dict(row) for row in rows]

    def business_transaction_metrics(self) -> dict[str, Any]:
        with self._business_metrics_lock:
            samples = list(self._business_transaction_seconds)
            return {
                "business_transaction_count": self._business_transaction_count,
                "business_transaction_failure_count": (self._business_transaction_failure_count),
                "business_transaction_captured_count": len(samples),
                "business_transaction_seconds": samples,
            }

    def _record_business_transaction(self, started_at: float, *, failed: bool) -> None:
        duration = max(0.0, time.monotonic() - started_at)
        with self._business_metrics_lock:
            self._business_transaction_count += 1
            self._business_transaction_failure_count += int(failed)
            self._business_transaction_seconds.append(duration)

    def _mutate(
        self,
        token: LeaderToken,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not isinstance(token, LeaderToken):
            raise TypeError("HA mutation requires a LeaderToken as its first argument")
        started_at = time.monotonic()
        failed = True
        with self._mutation_lock:
            self._connection.activate(token)
            try:
                self._connection.preflight()
                result = getattr(self._legacy, method_name)(*args, **kwargs)
                failed = False
                return result
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            finally:
                self._connection.deactivate()
                self._record_business_transaction(started_at, failed=failed)

    def _transaction(
        self,
        token: LeaderToken,
        operation: Callable[[_FencedConnection], Any],
    ) -> Any:
        if not isinstance(token, LeaderToken):
            raise TypeError("HA mutation requires a LeaderToken as its first argument")
        started_at = time.monotonic()
        failed = True
        with self._mutation_lock:
            self._connection.activate(token)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = operation(self._connection)
                self._connection.commit()
                failed = False
                return result
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            finally:
                self._connection.deactivate()
                self._record_business_transaction(started_at, failed=failed)

    def _record_capacity_observation_in_active_transaction(
        self,
        token: LeaderToken,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._connection.in_transaction:
            raise RuntimeError("inline capacity observation requires an active transaction")
        thread_id = threading.get_ident()
        if self._inline_capacity_thread_id is not None:
            raise RuntimeError("nested inline capacity observation is not supported")
        self._inline_capacity_thread_id = thread_id
        try:
            return self.record_capacity_observation(token, **kwargs)
        finally:
            self._inline_capacity_thread_id = None

    def set_run_state(self, token: LeaderToken, key: str, value: Any) -> None:
        self._mutate(token, "set_run_state", key, value)

    def upsert_global_version(
        self,
        token: LeaderToken,
        version: int,
        weight_path: str,
        optim_path: str,
        *,
        publication_id: str,
        weight_size_bytes: int,
        optim_size_bytes: int,
        **kwargs: Any,
    ) -> None:
        self._mutate(
            token,
            "upsert_global_version",
            version,
            weight_path,
            optim_path,
            commit_epoch=token.epoch,
            commit_owner_id=token.owner_id,
            publication_id=publication_id,
            weight_size_bytes=weight_size_bytes,
            optim_size_bytes=optim_size_bytes,
            **kwargs,
        )

    def initialize_full_run(
        self,
        token: LeaderToken,
        *,
        publication_id: str,
        weight_size_bytes: int,
        optim_size_bytes: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._mutate(
            token,
            "initialize_full_run",
            commit_epoch=token.epoch,
            commit_owner_id=token.owner_id,
            publication_id=publication_id,
            weight_size_bytes=weight_size_bytes,
            optim_size_bytes=optim_size_bytes,
            **kwargs,
        )

    def commit_full_merge(
        self,
        token: LeaderToken,
        *,
        publication_id: str,
        weight_size_bytes: int,
        optim_size_bytes: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        capacity_observation = kwargs.pop("capacity_observation", None)
        if self.dynamic_mode and capacity_observation is None:
            raise RuntimeError("dynamic full merge requires an atomic capacity observation")
        caller_before_commit = kwargs.pop("before_commit", None)
        capacity_result: dict[str, Any] | None = None

        def before_commit() -> None:
            nonlocal capacity_result
            if capacity_observation is not None:
                if not self.dynamic_mode:
                    raise RuntimeError("capacity observation cannot be attached to a static merge")
                specification = dict(capacity_observation)
                observed_at = float(specification.pop("now", self._wall_clock()))
                capacity_result = self._record_capacity_observation_in_active_transaction(
                    token,
                    now=observed_at,
                    **specification,
                )
            if caller_before_commit is not None:
                caller_before_commit()

        row = self._mutate(
            token,
            "commit_full_merge",
            commit_epoch=token.epoch,
            commit_owner_id=token.owner_id,
            publication_id=publication_id,
            weight_size_bytes=weight_size_bytes,
            optim_size_bytes=optim_size_bytes,
            require_membership_fence=self.dynamic_mode,
            before_commit=before_commit,
            **kwargs,
        )
        if capacity_result is None:
            return row
        return {**row, "_capacity_observation_result": capacity_result}

    def upsert_learner(self, token: LeaderToken, learner_id: str, **kwargs: Any) -> None:
        self._mutate(token, "upsert_learner", learner_id, **kwargs)

    def update_learner_status(
        self,
        token: LeaderToken,
        learner_id: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        self._mutate(token, "update_learner_status", learner_id, status, reason)

    def insert_update_metadata(
        self, token: LeaderToken, metadata: dict[str, Any], **kwargs: Any
    ) -> bool:
        if self.dynamic_mode:
            return bool(
                self._mutate(
                    token,
                    "insert_update_metadata",
                    metadata,
                    require_membership_fence=True,
                    **kwargs,
                )
            )
        return bool(self._mutate(token, "insert_update_metadata", metadata, **kwargs))

    def initialize_dynamic_membership(
        self,
        token: LeaderToken,
        *,
        stream_pool_size: int,
        bootstrap_instances: int,
        config_fingerprint: str,
        created_at: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self.dynamic_mode:
            raise RuntimeError("dynamic membership requires schema v3")
        timestamp = self._wall_clock() if created_at is None else float(created_at)

        def operation(conn: _FencedConnection) -> list[dict[str, Any]]:
            state_row = conn.execute(
                "SELECT value FROM run_state WHERE key='stream_pool_size'"
            ).fetchone()
            if state_row is not None and int(json.loads(state_row["value"])) != int(
                stream_pool_size
            ):
                raise RuntimeError("stream_pool_size is immutable after initialization")
            if state_row is None:
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                    ("stream_pool_size", json.dumps(int(stream_pool_size)), timestamp),
                )
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                    ("dynamic_initialized_at", json.dumps(timestamp), timestamp),
                )
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                    ("consecutive_low_count", json.dumps(0), timestamp),
                )
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        "dynamic_config_fingerprint",
                        json.dumps(config_fingerprint),
                        timestamp,
                    ),
                )
            fingerprint_row = conn.execute(
                "SELECT value FROM run_state WHERE key='dynamic_config_fingerprint'"
            ).fetchone()
            if (
                fingerprint_row is None
                or str(json.loads(fingerprint_row["value"])) != config_fingerprint
            ):
                raise RuntimeError("dynamic config fingerprint is immutable")
            admission_max = int(
                conn.execute(
                    "SELECT COALESCE(MAX(admission_generation), 0) FROM learner_instances"
                ).fetchone()[0]
            )
            scale_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM launch_requests WHERE reason='scale_out'"
                ).fetchone()[0]
            )
            for key, value in (
                ("admission_generation", admission_max),
                ("scale_launch_request_count", scale_count),
            ):
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (key, json.dumps(value), timestamp),
                )
            for stream_id in range(int(stream_pool_size)):
                conn.execute(
                    """
                    INSERT INTO streams(
                        stream_id, current_stream_epoch, current_instance_id, state, updated_at
                    ) VALUES (?, 0, NULL, 'free', ?)
                    ON CONFLICT(stream_id) DO NOTHING
                    """,
                    (stream_id, timestamp),
                )
            actual_streams = int(conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0])
            if actual_streams != int(stream_pool_size):
                raise RuntimeError("stream table does not match the immutable pool size")
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if controller is None:
                conn.execute(
                    """
                    INSERT INTO controller_state(
                        singleton, state, generation, reason, requested_at,
                        max_terminal_version, updated_by_epoch, updated_by_owner_id
                    ) VALUES (1, 'open', 0, NULL, ?, NULL, ?, ?)
                    """,
                    (timestamp, token.epoch, token.owner_id),
                )
            for slot in range(int(bootstrap_instances)):
                encoded = json.dumps(
                    [self.run_id, "bootstrap", slot, config_fingerprint],
                    separators=(",", ":"),
                ).encode("utf-8")
                request_id = hashlib.sha256(encoded).hexdigest()
                conn.execute(
                    """
                    INSERT INTO launch_requests(
                        request_id, observation_key, bootstrap_slot, role, reason,
                        requested_by_epoch, state, created_at, updated_at, not_before,
                        submission_attempts, expires_at
                    ) VALUES (?, NULL, ?, 'learner', 'bootstrap', ?,
                              'external_submitted', ?, ?, ?, 0, NULL)
                    ON CONFLICT(request_id) DO NOTHING
                    """,
                    (request_id, slot, token.epoch, timestamp, timestamp, timestamp),
                )
            rows = conn.execute(
                """
                SELECT * FROM launch_requests WHERE reason='bootstrap'
                ORDER BY bootstrap_slot
                """
            ).fetchall()
            if len(rows) != int(bootstrap_instances):
                raise RuntimeError("bootstrap request set does not match configured slots")
            return [dict(row) for row in rows]

        return self._transaction(token, operation)

    def reject_registration(
        self,
        token: LeaderToken,
        *,
        instance_id: str,
        request_sha256: str,
        launch_request_id: str | None,
        placement_id: str,
        created_at: float,
        expires_at: float,
        reason: str,
    ) -> dict[str, Any]:
        def operation(conn: _FencedConnection) -> dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM registration_requests WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if existing is not None and str(existing["request_sha256"]) != request_sha256:
                if str(existing["state"]) == "pending":
                    result = {
                        "instance_id": instance_id,
                        "placement_id": str(existing["placement_id"]),
                        "launch_request_id": existing["launch_request_id"],
                        "state": "rejected",
                        "rejection_reason": reason,
                    }
                    conn.execute(
                        """
                        UPDATE registration_requests SET state='rejected',
                            processed_by_epoch=?, rejection_reason=?, result_json=?
                        WHERE instance_id=? AND state='pending'
                        """,
                        (
                            token.epoch,
                            reason,
                            json.dumps(result, sort_keys=True),
                            instance_id,
                        ),
                    )
                    return result
                # Never replace an already published canonical decision with a
                # result derived from conflicting replay content.
                result_json = existing["result_json"]
                if result_json:
                    result = json.loads(result_json)
                    result["_preserve_existing_publication"] = True
                    return result
                if str(existing["state"]) == "admitted":
                    instance = conn.execute(
                        "SELECT * FROM learner_instances WHERE instance_id=?",
                        (instance_id,),
                    ).fetchone()
                    if instance is not None:
                        result = self._admission_result(
                            dict(instance),
                            request_sha256=str(existing["request_sha256"]),
                        )
                        result["_preserve_existing_publication"] = True
                        return result
                result = {
                    "instance_id": instance_id,
                    "placement_id": str(existing["placement_id"]),
                    "launch_request_id": existing["launch_request_id"],
                    "state": str(existing["state"]),
                    "rejection_reason": existing["rejection_reason"],
                }
                result["_preserve_existing_publication"] = True
                return result
            state = "expired" if "expired" in reason.lower() else "rejected"
            result = {
                "instance_id": instance_id,
                "placement_id": placement_id,
                "launch_request_id": launch_request_id,
                "state": state,
                "rejection_reason": reason,
            }
            conn.execute(
                """
                INSERT INTO registration_requests(
                    instance_id, request_sha256, launch_request_id, placement_id,
                    state, created_at, expires_at, processed_by_epoch,
                    rejection_reason, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    state=excluded.state,
                    processed_by_epoch=excluded.processed_by_epoch,
                    rejection_reason=excluded.rejection_reason,
                    result_json=excluded.result_json
                """,
                (
                    instance_id,
                    request_sha256,
                    launch_request_id,
                    placement_id,
                    state,
                    float(created_at),
                    float(expires_at),
                    token.epoch,
                    reason,
                    json.dumps(result, sort_keys=True),
                ),
            )
            return result

        return self._transaction(token, operation)

    def record_external_launch_jobs(
        self,
        token: LeaderToken,
        jobs: list[dict[str, Any]],
        *,
        observed_at: float | None = None,
    ) -> int:
        """Bind externally submitted bootstrap requests to durable PBS job IDs."""

        if not self.dynamic_mode:
            raise RuntimeError("external launch reconciliation requires schema v3")
        timestamp = self._wall_clock() if observed_at is None else float(observed_at)

        def operation(conn: _FencedConnection) -> int:
            changed = 0
            for job in jobs:
                request_id = str(job["request_id"])
                bootstrap_slot = int(job["bootstrap_slot"])
                pbs_job_id = str(job["pbs_job_id"])
                launch = conn.execute(
                    "SELECT * FROM launch_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if (
                    launch is None
                    or str(launch["reason"]) != "bootstrap"
                    or int(launch["bootstrap_slot"]) != bootstrap_slot
                ):
                    raise RuntimeError("bootstrap scheduler receipt has no matching request")
                existing_job_id = launch["pbs_job_id"]
                if existing_job_id is not None and _pbs_job_identity(
                    existing_job_id
                ) != _pbs_job_identity(pbs_job_id):
                    raise RuntimeError("bootstrap launch request changed PBS job identity")
                if str(launch["state"]) in {
                    "failed",
                    "expired",
                    "cancelled",
                    "capacity_fulfilled",
                    "completed",
                }:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE launch_requests
                    SET pbs_job_id=COALESCE(pbs_job_id, ?),
                        scheduler_state=COALESCE(scheduler_state, 'submitted'),
                        scheduler_observed_at=COALESCE(scheduler_observed_at, ?),
                        updated_at=MAX(updated_at, ?)
                    WHERE request_id=?
                    """,
                    (pbs_job_id, timestamp, timestamp, request_id),
                )
                changed += int(cursor.rowcount)
            return changed

        return int(self._transaction(token, operation))

    def admit_registration(
        self,
        token: LeaderToken,
        request: dict[str, Any],
        *,
        stream_pool_size: int,
        desired_contributors: int,
        allow_unsolicited_registration: bool,
        allow_healthy_placement_replacement: bool,
        reuse_stream_for_same_placement: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.dynamic_mode:
            raise RuntimeError("dynamic registration requires schema v3")
        timestamp = self._wall_clock() if now is None else float(now)
        instance_id = str(request["instance_id"])
        placement_id = str(request["placement_id"])
        launch_request_id = str(request.get("launch_request_id") or "")
        request_sha256 = str(request["request_sha256"])

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            existing_request = conn.execute(
                "SELECT * FROM registration_requests WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if existing_request is not None:
                if str(existing_request["request_sha256"]) != request_sha256:
                    raise RuntimeError("registration replay changed its request checksum")
                if existing_request["state"] == "admitted":
                    row = conn.execute(
                        "SELECT * FROM learner_instances WHERE instance_id=?", (instance_id,)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("admitted registration lost its instance row")
                    return self._admission_result(dict(row), request_sha256=request_sha256)
                if existing_request["state"] in {
                    "rejected",
                    "expired",
                    "capacity_fulfilled",
                }:
                    result_json = existing_request["result_json"]
                    return (
                        json.loads(result_json)
                        if result_json
                        else {
                            "instance_id": instance_id,
                            "state": str(existing_request["state"]),
                        }
                    )
            if timestamp > float(request["expires_at"]):
                raise RuntimeError("registration request expired")
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if controller is None or str(controller["state"]) != "open":
                raise RuntimeError("dynamic admission is closed")
            launch = None
            if launch_request_id:
                launch = conn.execute(
                    "SELECT * FROM launch_requests WHERE request_id=?", (launch_request_id,)
                ).fetchone()
            if launch is None and not allow_unsolicited_registration:
                raise RuntimeError("registration has no authorized launch request")
            if launch is not None:
                if launch["admitted_instance_id"] is not None:
                    if str(launch["admitted_instance_id"]) != instance_id:
                        raise RuntimeError(
                            "logical launch request already admitted another instance"
                        )
                launch_job_id = launch["pbs_job_id"]
                request_job_id = request.get("pbs_job_id")
                if str(launch["state"]) in {
                    "failed",
                    "expired",
                    "cancelled",
                    "capacity_fulfilled",
                    "completed",
                }:
                    raise RuntimeError(f"launch request is terminal: {launch['state']}")
                if launch_job_id is None and request_job_id is not None:
                    conn.execute(
                        """
                        INSERT INTO registration_requests(
                            instance_id, request_sha256, launch_request_id, placement_id,
                            state, created_at, expires_at, processed_by_epoch,
                            rejection_reason, result_json
                        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL)
                        ON CONFLICT(instance_id) DO NOTHING
                        """,
                        (
                            instance_id,
                            request_sha256,
                            launch_request_id,
                            placement_id,
                            float(request["created_at"]),
                            float(request["expires_at"]),
                        ),
                    )
                    return {
                        "instance_id": instance_id,
                        "placement_id": placement_id,
                        "launch_request_id": launch_request_id,
                        "state": "pending_scheduler_authorization",
                    }
                if launch_job_id is not None and _pbs_job_identity(
                    request_job_id or ""
                ) != _pbs_job_identity(launch_job_id):
                    raise RuntimeError("registration PBS job does not match launch request")
            current_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM learner_instances
                    WHERE status IN ('admitted','draining','drained')
                    """
                ).fetchone()[0]
            )
            reserved_states = (
                "planned",
                "submitting",
                "submitted",
                "started",
                "external_submitted",
                "submission_unknown",
                "retryable",
            )
            placeholders = ",".join("?" for _ in reserved_states)
            reserved = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM launch_requests
                    WHERE state IN ({placeholders}) AND admitted_instance_id IS NULL
                      AND request_id != ?
                    """,
                    (*reserved_states, launch_request_id),
                ).fetchone()[0]
            )
            if (
                launch is not None
                and str(launch["reason"])
                not in {
                    "bootstrap",
                    "operator_replacement",
                }
                and (
                    current_count >= int(desired_contributors)
                    or current_count + reserved >= int(desired_contributors)
                    or current_count + reserved >= int(stream_pool_size)
                )
            ):
                result = {
                    "instance_id": instance_id,
                    "placement_id": placement_id,
                    "launch_request_id": launch_request_id,
                    "state": "capacity_fulfilled",
                    "rejection_reason": "current or reserved capacity already satisfies the request",
                }
                conn.execute(
                    """
                    UPDATE launch_requests SET state='capacity_fulfilled', updated_at=?,
                        reservation_released_at=? WHERE request_id=?
                    """,
                    (timestamp, timestamp, launch_request_id),
                )
                conn.execute(
                    """
                    INSERT INTO registration_requests(
                        instance_id, request_sha256, launch_request_id, placement_id,
                        state, created_at, expires_at, processed_by_epoch,
                        rejection_reason, result_json
                    ) VALUES (?, ?, ?, ?, 'capacity_fulfilled', ?, ?, ?, ?, ?)
                    """,
                    (
                        instance_id,
                        request_sha256,
                        launch_request_id,
                        placement_id,
                        float(request["created_at"]),
                        float(request["expires_at"]),
                        token.epoch,
                        result["rejection_reason"],
                        json.dumps(result, sort_keys=True),
                    ),
                )
                return result
            replacement_request = (
                launch is not None and str(launch["reason"]) == "operator_replacement"
            )
            if current_count >= int(stream_pool_size) and not replacement_request:
                raise RuntimeError("stream pool is exhausted")
            placement = conn.execute(
                "SELECT * FROM placements WHERE placement_id=?", (placement_id,)
            ).fetchone()
            previous_instance = None
            if placement is not None and placement["current_instance_id"] is not None:
                previous_instance = conn.execute(
                    "SELECT * FROM learner_instances WHERE instance_id=?",
                    (placement["current_instance_id"],),
                ).fetchone()
            authorized_epoch = None if launch is None else launch["authorized_placement_epoch"]
            authorized = (
                authorized_epoch is not None
                and str(launch["authorized_placement_id"] or "") == placement_id
                and placement is not None
                and int(authorized_epoch) == int(placement["current_placement_epoch"]) + 1
            )
            if previous_instance is not None and str(previous_instance["status"]) in {
                "admitted",
                "draining",
                "drained",
            }:
                if not (authorized or allow_healthy_placement_replacement):
                    raise RuntimeError("healthy placement replacement is not authorized")
                conn.execute(
                    """
                    UPDATE learner_instances SET status='revoked', expired_at=?,
                        status_reason='authorized_replacement'
                    WHERE instance_id=?
                    """,
                    (timestamp, previous_instance["instance_id"]),
                )
                conn.execute(
                    """
                    UPDATE launch_requests SET state='failed', updated_at=?,
                        reservation_released_at=COALESCE(reservation_released_at, ?),
                        last_error='instance_replaced'
                    WHERE admitted_instance_id=? AND state='admitted'
                    """,
                    (timestamp, timestamp, previous_instance["instance_id"]),
                )
            placement_epoch = (
                0 if placement is None else int(placement["current_placement_epoch"]) + 1
            )
            reusable_stream = None
            if reuse_stream_for_same_placement and placement is not None:
                reusable_stream = placement["reusable_stream_id"]
                if reusable_stream is None and previous_instance is not None:
                    reusable_stream = previous_instance["stream_id"]
            stream = None
            if reusable_stream is not None:
                stream = conn.execute(
                    "SELECT * FROM streams WHERE stream_id=?", (int(reusable_stream),)
                ).fetchone()
                if stream is not None and stream["current_instance_id"] not in (
                    None,
                    None if previous_instance is None else previous_instance["instance_id"],
                ):
                    stream = None
            if stream is None:
                stream = conn.execute(
                    """
                    SELECT * FROM streams WHERE current_instance_id IS NULL OR state='free'
                    ORDER BY stream_id LIMIT 1
                    """
                ).fetchone()
            if stream is None:
                raise RuntimeError("stream pool is exhausted")
            stream_id = int(stream["stream_id"])
            stream_restarted = str(stream["state"]) != "free" or (
                placement is not None and reusable_stream == stream_id
            )
            stream_epoch = int(stream["current_stream_epoch"]) + int(stream_restarted)
            generation_row = conn.execute(
                "SELECT value FROM run_state WHERE key='admission_generation'"
            ).fetchone()
            admission_generation = (
                1 if generation_row is None else int(json.loads(generation_row["value"])) + 1
            )
            conn.execute(
                """
                INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (
                    "admission_generation",
                    json.dumps(admission_generation),
                    timestamp,
                ),
            )
            admission_token = self._admission_token(instance_id, request_sha256)
            admission_token_hash = hashlib.sha256(admission_token.encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO learner_instances(
                    instance_id, placement_id, placement_epoch, stream_id, stream_epoch,
                    admission_generation, admission_token_hash, launch_request_id,
                    pbs_job_id, hostname, pid, gpu_identity, status, registered_at,
                    admitted_at, last_seen, admitted_by_epoch, stream_restarted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    placement_id,
                    placement_epoch,
                    stream_id,
                    stream_epoch,
                    admission_generation,
                    admission_token_hash,
                    launch_request_id or None,
                    request.get("pbs_job_id"),
                    str(request["hostname"]),
                    int(request["pid"]),
                    request.get("gpu_identity"),
                    timestamp,
                    timestamp,
                    timestamp,
                    token.epoch,
                    int(stream_restarted),
                ),
            )
            conn.execute(
                """
                INSERT INTO placements(
                    placement_id, current_placement_epoch, current_instance_id,
                    reusable_stream_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(placement_id) DO UPDATE SET
                    current_placement_epoch=excluded.current_placement_epoch,
                    current_instance_id=excluded.current_instance_id,
                    reusable_stream_id=excluded.reusable_stream_id,
                    updated_at=excluded.updated_at
                """,
                (placement_id, placement_epoch, instance_id, stream_id, timestamp),
            )
            conn.execute(
                """
                UPDATE streams SET current_stream_epoch=?, current_instance_id=?,
                    state='assigned', updated_at=? WHERE stream_id=?
                """,
                (stream_epoch, instance_id, timestamp, stream_id),
            )
            if previous_instance is not None and int(previous_instance["stream_id"]) != stream_id:
                conn.execute(
                    """
                    UPDATE streams SET current_instance_id=NULL, state='reusable', updated_at=?
                    WHERE stream_id=? AND current_instance_id=?
                    """,
                    (
                        timestamp,
                        int(previous_instance["stream_id"]),
                        previous_instance["instance_id"],
                    ),
                )
            if launch is not None:
                conn.execute(
                    """
                    UPDATE launch_requests SET state='admitted', updated_at=?,
                        admitted_instance_id=?, pbs_job_id=COALESCE(pbs_job_id, ?)
                    WHERE request_id=?
                    """,
                    (
                        timestamp,
                        instance_id,
                        request.get("pbs_job_id"),
                        launch_request_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM learner_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            assert row is not None
            result = self._admission_result(dict(row), request_sha256=request_sha256)
            conn.execute(
                """
                INSERT INTO registration_requests(
                    instance_id, request_sha256, launch_request_id, placement_id,
                    state, created_at, expires_at, processed_by_epoch,
                    rejection_reason, result_json
                ) VALUES (?, ?, ?, ?, 'admitted', ?, ?, ?, NULL, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    state='admitted', processed_by_epoch=excluded.processed_by_epoch,
                    rejection_reason=NULL, result_json=excluded.result_json
                """,
                (
                    instance_id,
                    request_sha256,
                    launch_request_id or None,
                    placement_id,
                    float(request["created_at"]),
                    float(request["expires_at"]),
                    token.epoch,
                    json.dumps(
                        {key: value for key, value in result.items() if key != "admission_token"},
                        sort_keys=True,
                    ),
                ),
            )
            return result

        return self._transaction(token, operation)

    def authorize_placement_replacement(
        self,
        token: LeaderToken,
        *,
        placement_id: str,
        reason: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.dynamic_mode:
            raise RuntimeError("dynamic replacement authorization requires schema v3")
        if not reason:
            raise ValueError("replacement authorization requires an audit reason")
        if ttl_seconds <= 0.0:
            raise ValueError("replacement authorization TTL must be > 0")
        timestamp = self._wall_clock() if now is None else float(now)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if controller is None or str(controller["state"]) != "open":
                raise RuntimeError("dynamic admission is closed")
            placement = conn.execute(
                "SELECT * FROM placements WHERE placement_id=?", (placement_id,)
            ).fetchone()
            if placement is None:
                raise RuntimeError("replacement placement has no prior incarnation")
            next_epoch = int(placement["current_placement_epoch"]) + 1
            encoded = json.dumps(
                [
                    self.run_id,
                    "operator_replacement",
                    placement_id,
                    next_epoch,
                    reason,
                ],
                separators=(",", ":"),
            ).encode("utf-8")
            request_id = hashlib.sha256(encoded).hexdigest()
            conn.execute(
                """
                INSERT INTO launch_requests(
                    request_id, observation_key, bootstrap_slot, role, reason,
                    requested_by_epoch, state, created_at, updated_at, not_before,
                    submission_attempts, expires_at, authorized_placement_id,
                    authorized_placement_epoch
                ) VALUES (?, NULL, NULL, 'learner', 'operator_replacement', ?,
                          'external_submitted', ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (
                    request_id,
                    token.epoch,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp + float(ttl_seconds),
                    placement_id,
                    next_epoch,
                ),
            )
            row = conn.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._transaction(token, operation)

    def _admission_token(self, instance_id: str, request_sha256: str) -> str:
        encoded = f"{self.run_id}\0{self.config_sha256}\0{instance_id}\0{request_sha256}"
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _admission_result(self, instance: dict[str, Any], *, request_sha256: str) -> dict[str, Any]:
        return {
            "state": "admitted",
            "instance_id": str(instance["instance_id"]),
            "placement_id": str(instance["placement_id"]),
            "placement_epoch": int(instance["placement_epoch"]),
            "stream_id": int(instance["stream_id"]),
            "stream_epoch": int(instance["stream_epoch"]),
            "admission_generation": int(instance["admission_generation"]),
            "admission_token": self._admission_token(str(instance["instance_id"]), request_sha256),
            "launch_request_id": str(instance["launch_request_id"] or ""),
            "stream_restarted": bool(instance["stream_restarted"]),
        }

    def _update_instance_heartbeat_row(
        self,
        conn: _FencedConnection,
        payload: dict[str, Any],
        *,
        heartbeat_path: str,
    ) -> bool:
        instance_id = str(payload["instance_id"])
        admission_token_hash = hashlib.sha256(
            str(payload["admission_token"]).encode("utf-8")
        ).hexdigest()
        row = conn.execute(
            """
            SELECT li.*, p.current_instance_id AS placement_current_instance_id,
                p.current_placement_epoch, s.current_instance_id AS stream_current_instance_id,
                s.current_stream_epoch
            FROM learner_instances AS li
            JOIN placements AS p ON p.placement_id=li.placement_id
            JOIN streams AS s ON s.stream_id=li.stream_id
            WHERE li.instance_id=?
            """,
            (instance_id,),
        ).fetchone()
        if row is None:
            return False
        observed = (
            str(payload["placement_id"]),
            int(payload["placement_epoch"]),
            int(payload["stream_id"]),
            int(payload["stream_epoch"]),
            int(payload["admission_generation"]),
            admission_token_hash,
        )
        expected = (
            str(row["placement_id"]),
            int(row["placement_epoch"]),
            int(row["stream_id"]),
            int(row["stream_epoch"]),
            int(row["admission_generation"]),
            str(row["admission_token_hash"]),
        )
        if observed != expected:
            return False
        if (
            str(row["placement_current_instance_id"]) != instance_id
            or int(row["current_placement_epoch"]) != int(row["placement_epoch"])
            or str(row["stream_current_instance_id"]) != instance_id
            or int(row["current_stream_epoch"]) != int(row["stream_epoch"])
        ):
            return False
        heartbeat_status = str(payload.get("status") or "active")
        if heartbeat_status == "drained":
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if (
                controller is None
                or str(controller["state"]) not in {"draining", "closed"}
                or int(payload.get("close_generation") or -1) != int(controller["generation"])
            ):
                return False
            next_status = "drained"
        elif heartbeat_status == "stopped":
            next_status = "stopped"
        elif str(row["status"]) in {"draining", "drained"}:
            next_status = str(row["status"])
        elif str(row["status"]) == "admitted":
            next_status = "admitted"
        else:
            return False
        conn.execute(
            """
            UPDATE learner_instances SET
                last_seen=?, last_proposal_at=COALESCE(?, last_proposal_at),
                last_cycle_step_time_seconds_mean=COALESCE(
                    ?, last_cycle_step_time_seconds_mean
                ),
                last_cycle_step_count=COALESCE(?, last_cycle_step_count),
                drained_generation=COALESCE(?, drained_generation),
                final_update_id=COALESCE(?, final_update_id),
                stopped_at=CASE WHEN ?='stopped' THEN ? ELSE stopped_at END,
                status=?, status_reason=?, pbs_job_id=COALESCE(?, pbs_job_id)
            WHERE instance_id=?
            """,
            (
                float(payload["timestamp"]),
                payload.get("last_proposal_at"),
                payload.get("local_cycle_step_time_seconds_mean"),
                payload.get("local_cycle_step_count"),
                payload.get("close_generation"),
                payload.get("final_update_id"),
                heartbeat_status,
                float(payload["timestamp"]),
                next_status,
                str(payload.get("status_reason") or heartbeat_path),
                payload.get("pbs_job_id"),
                instance_id,
            ),
        )
        if next_status == "stopped":
            conn.execute(
                """
                UPDATE placements SET current_instance_id=NULL,
                    reusable_stream_id=?, updated_at=?
                WHERE placement_id=? AND current_instance_id=?
                """,
                (
                    row["stream_id"],
                    float(payload["timestamp"]),
                    row["placement_id"],
                    instance_id,
                ),
            )
            conn.execute(
                """
                UPDATE streams SET current_instance_id=NULL, state='reusable', updated_at=?
                WHERE stream_id=? AND current_instance_id=?
                """,
                (float(payload["timestamp"]), row["stream_id"], instance_id),
            )
            conn.execute(
                """
                UPDATE launch_requests SET state='completed', updated_at=?
                WHERE admitted_instance_id=? AND state='admitted'
                """,
                (float(payload["timestamp"]), instance_id),
            )
        return True

    def update_instance_heartbeat(
        self,
        token: LeaderToken,
        payload: dict[str, Any],
        *,
        heartbeat_path: str,
    ) -> bool:
        if not self.dynamic_mode:
            raise RuntimeError("dynamic heartbeat requires schema v3")

        def operation(conn: _FencedConnection) -> bool:
            return self._update_instance_heartbeat_row(
                conn,
                payload,
                heartbeat_path=heartbeat_path,
            )

        return bool(self._transaction(token, operation))

    def update_instance_heartbeats(
        self,
        token: LeaderToken,
        heartbeats: list[tuple[dict[str, Any], str]],
    ) -> int:
        """Ingest one discovery scan under a single fenced transaction."""

        if not self.dynamic_mode:
            raise RuntimeError("dynamic heartbeat requires schema v3")
        if not heartbeats:
            return 0

        def operation(conn: _FencedConnection) -> int:
            return sum(
                self._update_instance_heartbeat_row(
                    conn,
                    payload,
                    heartbeat_path=heartbeat_path,
                )
                for payload, heartbeat_path in heartbeats
            )

        return int(self._transaction(token, operation))

    def begin_dynamic_drain(
        self,
        token: LeaderToken,
        *,
        reason: str,
        current_version: int,
        global_target: int | None,
        max_terminal_merges: int,
        requested_at: float | None = None,
    ) -> dict[str, Any]:
        if not self.dynamic_mode:
            raise RuntimeError("dynamic drain requires schema v3")
        timestamp = self._wall_clock() if requested_at is None else float(requested_at)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            current = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if current is None:
                raise RuntimeError("dynamic controller state is missing")
            if str(current["state"]) in {"draining", "closed", "terminal"}:
                return dict(current)
            if str(current["state"]) != "open":
                raise RuntimeError(f"cannot drain controller state {current['state']}")
            generation = int(current["generation"]) + 1
            if reason == "stop_after_outer_steps":
                maximum = int(global_target) if global_target is not None else int(current_version)
            elif reason == "stop_after_global_tokens":
                maximum = int(current_version)
            else:
                requested_maximum = int(current_version) + int(max_terminal_merges)
                maximum = (
                    requested_maximum
                    if global_target is None
                    else min(int(global_target), requested_maximum)
                )
            maximum = max(int(current_version), maximum)
            conn.execute(
                """
                UPDATE controller_state SET state='draining', generation=?, reason=?,
                    requested_at=?, max_terminal_version=?, updated_by_epoch=?,
                    updated_by_owner_id=? WHERE singleton=1
                """,
                (generation, reason, timestamp, maximum, token.epoch, token.owner_id),
            )
            conn.execute(
                """
                UPDATE launch_requests SET state='cancelled', updated_at=?,
                    reservation_released_at=?, last_error='admission_closed'
                WHERE admitted_instance_id IS NULL
                  AND state IN ('planned','submitting','submitted','started',
                                'external_submitted','submission_unknown','retryable')
                """,
                (timestamp, timestamp),
            )
            conn.execute(
                """
                UPDATE learner_instances SET status='draining', status_reason=?
                WHERE status='admitted'
                """,
                (f"drain_generation={generation}",),
            )
            conn.execute("DELETE FROM run_state WHERE key='drain_visibility_started_at'")
            row = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            assert row is not None
            return dict(row)

        return self._transaction(token, operation)

    def advance_dynamic_drain(
        self,
        token: LeaderToken,
        *,
        drain_ack_timeout_seconds: float,
        registration_visibility_grace_seconds: float,
        proposal_visibility_grace_seconds: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = self._wall_clock() if now is None else float(now)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if controller is None or str(controller["state"]) not in {"draining", "closed"}:
                raise RuntimeError("dynamic drain is not active")
            generation = int(controller["generation"])
            requested_at = float(controller["requested_at"])
            conn.execute(
                """
                UPDATE registration_requests SET state='rejected',
                    processed_by_epoch=?, rejection_reason='admission_closed'
                WHERE state='pending'
                """,
                (token.epoch,),
            )
            if timestamp >= requested_at + float(drain_ack_timeout_seconds):
                stale = conn.execute(
                    """
                    SELECT * FROM learner_instances
                    WHERE status IN ('admitted','draining')
                    ORDER BY instance_id
                    """
                ).fetchall()
                for instance in stale:
                    conn.execute(
                        """
                        UPDATE learner_instances SET status='revoked', expired_at=?,
                            status_reason='drain_ack_timeout' WHERE instance_id=?
                        """,
                        (timestamp, instance["instance_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE placements SET current_instance_id=NULL,
                            reusable_stream_id=?, updated_at=?
                        WHERE placement_id=? AND current_instance_id=?
                        """,
                        (
                            instance["stream_id"],
                            timestamp,
                            instance["placement_id"],
                            instance["instance_id"],
                        ),
                    )
                    conn.execute(
                        """
                            UPDATE streams SET current_instance_id=NULL, state='reusable', updated_at=?
                        WHERE stream_id=? AND current_instance_id=?
                        """,
                        (timestamp, instance["stream_id"], instance["instance_id"]),
                    )
            unsettled_instances = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM learner_instances
                    WHERE status IN ('admitted','draining')
                    """
                ).fetchone()[0]
            )
            active_requests = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM launch_requests
                    WHERE admitted_instance_id IS NULL
                      AND state IN ('planned','submitting','submitted','started',
                                    'external_submitted','submission_unknown','retryable')
                    """
                ).fetchone()[0]
            )
            pending_registrations = int(
                conn.execute(
                    "SELECT COUNT(*) FROM registration_requests WHERE state='pending'"
                ).fetchone()[0]
            )
            pending_final_pointers = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM learner_instances AS li
                    WHERE li.status='drained'
                      AND li.final_update_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM proposal_frontiers AS pf
                          WHERE pf.learner_id=li.instance_id
                            AND pf.last_observed_update_id=li.final_update_id
                      )
                    """
                ).fetchone()[0]
            )
            visibility_started = conn.execute(
                "SELECT value FROM run_state WHERE key='drain_visibility_started_at'"
            ).fetchone()
            settled = (
                unsettled_instances == 0
                and active_requests == 0
                and pending_registrations == 0
                and pending_final_pointers == 0
            )
            conn.execute(
                """
                UPDATE launch_requests SET state='completed', updated_at=?
                WHERE state='admitted' AND admitted_instance_id IN (
                    SELECT instance_id FROM learner_instances
                    WHERE status IN ('drained','stopped','revoked','expired')
                )
                """,
                (timestamp,),
            )
            if settled and visibility_started is None:
                conn.execute(
                    "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        "drain_visibility_started_at",
                        json.dumps(timestamp),
                        timestamp,
                    ),
                )
                visibility_at = timestamp
            elif visibility_started is None:
                visibility_at = None
            else:
                visibility_at = float(json.loads(visibility_started["value"]))
            grace = max(
                float(registration_visibility_grace_seconds),
                float(proposal_visibility_grace_seconds),
            )
            if (
                settled
                and visibility_at is not None
                and timestamp >= visibility_at + grace
                and str(controller["state"]) != "closed"
            ):
                conn.execute(
                    """
                    UPDATE controller_state SET state='closed', updated_by_epoch=?,
                        updated_by_owner_id=? WHERE singleton=1 AND generation=?
                    """,
                    (token.epoch, token.owner_id, generation),
                )
            row = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            assert row is not None
            result = dict(row)
            result.update(
                unsettled_instances=unsettled_instances,
                active_launch_requests=active_requests,
                pending_registration_requests=pending_registrations,
                pending_final_pointers=pending_final_pointers,
                visibility_started_at=visibility_at,
                input_closed=(str(row["state"]) == "closed" and settled),
            )
            return result

        return self._transaction(token, operation)

    def dynamic_input_closed(self) -> bool:
        if not self.dynamic_mode:
            return False
        controller = self.controller_state()
        if controller is None or str(controller["state"]) != "closed":
            return False
        return (
            int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM learner_instances
                    WHERE status IN ('admitted','draining')
                    """
                ).fetchone()[0]
            )
            == 0
            and int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM registration_requests WHERE state='pending'"
                ).fetchone()[0]
            )
            == 0
            and int(
                self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM learner_instances AS li
                    WHERE li.status='drained'
                      AND li.final_update_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM proposal_frontiers AS pf
                          WHERE pf.learner_id=li.instance_id
                            AND pf.last_observed_update_id=li.final_update_id
                      )
                    """
                ).fetchone()[0]
            )
            == 0
            and int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM launch_requests
                    WHERE admitted_instance_id IS NULL
                      AND state IN ('planned','submitting','submitted','started',
                                    'external_submitted','submission_unknown','retryable')
                    """
                ).fetchone()[0]
            )
            == 0
        )

    def revoke_dead_instances(
        self,
        token: LeaderToken,
        *,
        heartbeat_dead_after_seconds: float,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = self._wall_clock() if now is None else float(now)

        def operation(conn: _FencedConnection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT * FROM learner_instances
                WHERE status='admitted'
                  AND ? - COALESCE(last_seen, admitted_at) > ?
                ORDER BY instance_id
                """,
                (timestamp, float(heartbeat_dead_after_seconds)),
            ).fetchall()
            revoked: list[dict[str, Any]] = []
            for row in rows:
                conn.execute(
                    """
                    UPDATE learner_instances SET status='revoked', expired_at=?,
                        status_reason='heartbeat_dead'
                    WHERE instance_id=? AND status='admitted'
                    """,
                    (timestamp, row["instance_id"]),
                )
                conn.execute(
                    """
                    UPDATE placements SET current_instance_id=NULL,
                        reusable_stream_id=?, updated_at=?
                    WHERE placement_id=? AND current_instance_id=?
                    """,
                    (
                        row["stream_id"],
                        timestamp,
                        row["placement_id"],
                        row["instance_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE streams SET current_instance_id=NULL, state='reusable', updated_at=?
                    WHERE stream_id=? AND current_instance_id=?
                    """,
                    (timestamp, row["stream_id"], row["instance_id"]),
                )
                conn.execute(
                    """
                    UPDATE launch_requests SET state='failed', updated_at=?,
                        reservation_released_at=?, last_error='heartbeat_dead'
                    WHERE admitted_instance_id=? AND state='admitted'
                    """,
                    (timestamp, timestamp, row["instance_id"]),
                )
                revoked.append(dict(row))
            return revoked

        return self._transaction(token, operation)

    def record_capacity_observation(
        self,
        token: LeaderToken,
        *,
        observation_key: str,
        kind: str,
        global_version: int,
        eligible_contributors: int,
        selected_instance_ids: list[str],
        low_contributor_threshold: int,
        consecutive_low_windows: int,
        productive_window_count: int,
        startup_grace_seconds: float,
        heartbeat_stale_after_seconds: float,
        productive_upload_grace_factor: float,
        productive_upload_grace_min_seconds: float,
        productive_upload_grace_max_seconds: float,
        desired_contributors: int,
        stream_pool_size: int,
        scaling_enabled: bool,
        initial_membership_deadline_seconds: float,
        cooldown_seconds: float,
        max_pending_launch_requests: int,
        max_total_launch_requests: int,
        launch_request_ttl_seconds: float,
        config_fingerprint: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.dynamic_mode:
            raise RuntimeError("capacity observations require schema v3")
        timestamp = self._wall_clock() if now is None else float(now)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM capacity_observations WHERE observation_key=?",
                (observation_key,),
            ).fetchone()
            if existing is not None:
                request = conn.execute(
                    "SELECT * FROM launch_requests WHERE observation_key=?",
                    (observation_key,),
                ).fetchone()
                return {
                    "inserted": False,
                    "observation": dict(existing),
                    "launch_request": None if request is None else dict(request),
                }
            observation_seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(observation_seq), 0) + 1 FROM capacity_observations"
                ).fetchone()[0]
            )
            reserved_states = (
                "planned",
                "submitting",
                "submitted",
                "started",
                "external_submitted",
                "submission_unknown",
                "retryable",
            )
            placeholders = ",".join("?" for _ in reserved_states)
            reserved = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM launch_requests
                    WHERE state IN ({placeholders}) AND admitted_instance_id IS NULL
                    """,
                    reserved_states,
                ).fetchone()[0]
            )
            instances = conn.execute(
                """
                SELECT * FROM learner_instances
                WHERE status IN ('admitted','draining','drained')
                ORDER BY instance_id
                """
            ).fetchall()
            productive = 0
            for instance in instances:
                recent = instance["last_contributed_observation_seq"] is not None and int(
                    instance["last_contributed_observation_seq"]
                ) >= max(1, observation_seq - int(productive_window_count))
                startup = timestamp - float(instance["admitted_at"]) <= float(startup_grace_seconds)
                heartbeat_fresh = instance["last_seen"] is not None and timestamp - float(
                    instance["last_seen"]
                ) <= float(heartbeat_stale_after_seconds)
                upload_fresh = False
                if (
                    heartbeat_fresh
                    and instance["last_proposal_at"] is not None
                    and instance["last_cycle_step_time_seconds_mean"] is not None
                    and instance["last_cycle_step_count"] is not None
                ):
                    grace = (
                        float(instance["last_cycle_step_time_seconds_mean"])
                        * max(1, int(instance["last_cycle_step_count"]))
                        * float(productive_upload_grace_factor)
                    )
                    grace = min(
                        float(productive_upload_grace_max_seconds),
                        max(float(productive_upload_grace_min_seconds), grace),
                    )
                    upload_fresh = timestamp - float(instance["last_proposal_at"]) <= grace
                productive += int(recent or startup or upload_fresh)
            low_capacity = int(int(eligible_contributors) <= int(low_contributor_threshold))
            low_row = conn.execute(
                "SELECT value FROM run_state WHERE key='consecutive_low_count'"
            ).fetchone()
            previous_low = 0 if low_row is None else int(json.loads(low_row["value"]))
            consecutive_low = previous_low + 1 if low_capacity else 0
            conn.execute(
                """
                INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("consecutive_low_count", json.dumps(consecutive_low), timestamp),
            )
            controller = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            close_generation = 0 if controller is None else int(controller["generation"])
            conn.execute(
                """
                INSERT INTO capacity_observations(
                    observation_key, observation_seq, kind, global_version, observed_at,
                    eligible_contributors, selected_contributors, productive_instances,
                    reserved_launch_capacity, low_capacity, close_generation,
                    recorded_by_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_key,
                    observation_seq,
                    kind,
                    int(global_version),
                    timestamp,
                    int(eligible_contributors),
                    len(selected_instance_ids),
                    productive,
                    reserved,
                    low_capacity,
                    close_generation,
                    token.epoch,
                ),
            )
            for instance_id in sorted(set(selected_instance_ids)):
                conn.execute(
                    """
                    UPDATE learner_instances SET last_contributed_observation_seq=?
                    WHERE instance_id=?
                    """,
                    (observation_seq, instance_id),
                )
            launch_request = None
            initialized = conn.execute(
                "SELECT value FROM run_state WHERE key='dynamic_initialized_at'"
            ).fetchone()
            initialized_at = (
                timestamp if initialized is None else float(json.loads(initialized["value"]))
            )
            bootstrap_open = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM launch_requests
                    WHERE reason='bootstrap' AND state NOT IN (
                        'admitted','completed','failed','expired','cancelled','capacity_fulfilled'
                    )
                    """
                ).fetchone()[0]
            )
            initial_gate = bootstrap_open == 0 or (
                timestamp >= initialized_at + float(initial_membership_deadline_seconds)
            )
            active_count = len(instances)
            pending_scale = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM launch_requests
                    WHERE reason='scale_out' AND state IN ({placeholders})
                      AND admitted_instance_id IS NULL
                    """,
                    reserved_states,
                ).fetchone()[0]
            )
            scale_count_row = conn.execute(
                "SELECT value FROM run_state WHERE key='scale_launch_request_count'"
            ).fetchone()
            total_scale = (
                0 if scale_count_row is None else int(json.loads(scale_count_row["value"]))
            )
            last_scale = conn.execute(
                "SELECT value FROM run_state WHERE key='last_scale_launch_at'"
            ).fetchone()
            cooldown_ready = last_scale is None or timestamp >= float(
                json.loads(last_scale["value"])
            ) + float(cooldown_seconds)
            can_scale = (
                bool(scaling_enabled)
                and controller is not None
                and str(controller["state"]) == "open"
                and initial_gate
                and consecutive_low >= int(consecutive_low_windows)
                and productive + reserved < int(desired_contributors)
                and pending_scale < int(max_pending_launch_requests)
                and total_scale < int(max_total_launch_requests)
                and cooldown_ready
                and active_count + reserved < int(stream_pool_size)
            )
            if can_scale:
                ordinal = total_scale + 1
                encoded = json.dumps(
                    [self.run_id, observation_key, ordinal, config_fingerprint],
                    separators=(",", ":"),
                ).encode("utf-8")
                request_id = hashlib.sha256(encoded).hexdigest()
                conn.execute(
                    """
                    INSERT INTO launch_requests(
                        request_id, observation_key, bootstrap_slot, role, reason,
                        requested_by_epoch, state, created_at, updated_at, not_before,
                        submission_attempts, expires_at
                    ) VALUES (?, ?, NULL, 'learner', 'scale_out', ?, 'planned',
                              ?, ?, ?, 0, ?)
                    ON CONFLICT(request_id) DO NOTHING
                    """,
                    (
                        request_id,
                        observation_key,
                        token.epoch,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp + float(launch_request_ttl_seconds),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (
                        "scale_launch_request_count",
                        json.dumps(ordinal),
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (
                        "last_scale_launch_at",
                        json.dumps(timestamp),
                        timestamp,
                    ),
                )
                request = conn.execute(
                    "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
                ).fetchone()
                launch_request = None if request is None else dict(request)
            observation = conn.execute(
                "SELECT * FROM capacity_observations WHERE observation_key=?",
                (observation_key,),
            ).fetchone()
            assert observation is not None
            return {
                "inserted": True,
                "observation": dict(observation),
                "consecutive_low_count": consecutive_low,
                "launch_request": launch_request,
            }

        if self._inline_capacity_thread_id == threading.get_ident():
            return operation(self._connection)
        return self._transaction(token, operation)

    def record_starvation_capacity_observation(
        self,
        token: LeaderToken,
        *,
        interval_seconds: float,
        capacity_observation: Mapping[str, Any] | None = None,
        now: float | None = None,
        before_commit: Callable[[], None] | None = None,
        **capacity_fields: Any,
    ) -> dict[str, Any] | None:
        """Allocate and record one starvation window in the same transaction."""

        if not self.dynamic_mode:
            raise RuntimeError("starvation observations require schema v3")
        if float(interval_seconds) <= 0.0:
            raise ValueError("starvation observation interval must be > 0")
        timestamp = self._wall_clock() if now is None else float(now)

        def operation(conn: _FencedConnection) -> dict[str, Any] | None:
            next_row = conn.execute(
                "SELECT value FROM run_state WHERE key='next_starvation_observation_at'"
            ).fetchone()
            if next_row is not None and timestamp < float(json.loads(next_row["value"])):
                return None
            generation_row = conn.execute(
                "SELECT value FROM run_state WHERE key='starvation_generation'"
            ).fetchone()
            generation = (
                1 if generation_row is None else int(json.loads(generation_row["value"])) + 1
            )
            for key, value in (
                ("starvation_generation", generation),
                ("next_starvation_observation_at", timestamp + float(interval_seconds)),
            ):
                conn.execute(
                    """
                    INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(value), timestamp),
                )
            specification = dict(capacity_observation or {})
            specification.update(capacity_fields)
            specification.pop("observation_key", None)
            specification.pop("kind", None)
            specification.pop("now", None)
            selected = specification.get("selected_instance_ids")
            if selected not in (None, []):
                raise ValueError("starvation observation cannot select contributors")
            specification.update(
                observation_key=f"starvation:{generation}",
                kind="starvation",
                selected_instance_ids=[],
            )
            result = self._record_capacity_observation_in_active_transaction(
                token,
                now=timestamp,
                **specification,
            )
            if before_commit is not None:
                before_commit()
            return result

        return self._transaction(token, operation)

    def update_launch_request(
        self,
        token: LeaderToken,
        *,
        request_id: str,
        expected_states: set[str],
        state: str,
        pbs_job_id: str | None = None,
        scheduler_state: str | None = None,
        last_error: str | None = None,
        increment_submission_attempts: bool = False,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        timestamp = self._wall_clock() if observed_at is None else float(observed_at)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"unknown launch request: {request_id}")
            if str(row["state"]) not in expected_states:
                raise RuntimeError(f"launch request {request_id} state changed: {row['state']}")
            stored_job_id = row["pbs_job_id"]
            if pbs_job_id is not None and stored_job_id is not None:
                if _pbs_job_identity(pbs_job_id) != _pbs_job_identity(stored_job_id):
                    raise RuntimeError(f"launch request {request_id} changed PBS job identity")
                # Preserve the first auditable spelling (normally qsub's raw
                # receipt) when qstat returns the same ID without its server
                # suffix.
                effective_pbs_job_id = None
            else:
                effective_pbs_job_id = pbs_job_id
            conn.execute(
                """
                UPDATE launch_requests SET state=?, updated_at=?,
                    submission_attempts=submission_attempts+?,
                    pbs_job_id=COALESCE(?, pbs_job_id),
                    scheduler_state=COALESCE(?, scheduler_state),
                    scheduler_observed_at=CASE WHEN ? IS NULL
                        THEN scheduler_observed_at ELSE ? END,
                    last_error=?,
                    reservation_released_at=CASE
                        WHEN ? IN ('failed','expired','cancelled','capacity_fulfilled','completed')
                        THEN COALESCE(reservation_released_at, ?)
                        ELSE reservation_released_at
                    END
                WHERE request_id=?
                """,
                (
                    state,
                    timestamp,
                    int(increment_submission_attempts),
                    effective_pbs_job_id,
                    scheduler_state,
                    scheduler_state,
                    timestamp,
                    last_error,
                    state,
                    timestamp,
                    request_id,
                ),
            )
            result = conn.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert result is not None
            return dict(result)

        return self._transaction(token, operation)

    def mark_updates_selected(
        self, token: LeaderToken, update_ids: list[str], selected_by_run: str
    ) -> None:
        if self.dynamic_mode:
            if not update_ids:
                return

            def operation(conn: _FencedConnection) -> None:
                for update_id in update_ids:
                    cursor = conn.execute(
                        """
                        UPDATE updates
                        SET status='selected', selected_at=?, selected_by_run=?,
                            selected_membership_generation=admission_generation
                        WHERE update_id=? AND status='pending'
                          AND EXISTS (
                              SELECT 1
                              FROM learner_instances AS li
                              JOIN placements AS p ON p.placement_id=li.placement_id
                              JOIN streams AS s ON s.stream_id=li.stream_id
                              WHERE li.instance_id=updates.learner_instance_id
                                AND li.status IN ('admitted','draining','drained')
                                AND p.current_instance_id=li.instance_id
                                AND p.current_placement_epoch=li.placement_epoch
                                AND s.current_instance_id=li.instance_id
                                AND s.current_stream_epoch=li.stream_epoch
                          )
                        """,
                        (float(self._wall_clock()), selected_by_run, update_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"dynamic update is not pending/current at selection: {update_id}"
                        )

            self._transaction(token, operation)
            return
        self._mutate(token, "mark_updates_selected", update_ids, selected_by_run)

    def mark_updates_applied(
        self, token: LeaderToken, updates: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        self._mutate(token, "mark_updates_applied", updates, **kwargs)

    def reset_selected_to_pending(self, token: LeaderToken, update_ids: list[str]) -> None:
        self._mutate(token, "reset_selected_to_pending", update_ids)

    def reset_all_selected_to_pending(self, token: LeaderToken) -> int:
        return int(self._mutate(token, "reset_all_selected_to_pending"))

    def prepare_full_resume(self, token: LeaderToken, **kwargs: Any) -> dict[str, Any]:
        return self._mutate(token, "prepare_full_resume", **kwargs)

    def drop_updates(self, token: LeaderToken, update_ids: list[str], reason: str) -> None:
        self._mutate(token, "drop_updates", update_ids, reason)

    def drop_obsolete_updates(
        self,
        token: LeaderToken,
        current_version: int,
        max_staleness_versions: int,
    ) -> int:
        return int(
            self._mutate(
                token,
                "drop_obsolete_updates",
                current_version,
                max_staleness_versions,
            )
        )

    def drop_ineligible_updates(
        self,
        token: LeaderToken,
        current_version: int,
        max_staleness_versions: int,
    ) -> int:
        return int(
            self._mutate(
                token,
                "drop_ineligible_updates",
                current_version,
                max_staleness_versions,
            )
        )

    def finalize_unconsumed_updates(
        self, token: LeaderToken, *, fragment_mode: bool, reason: str
    ) -> int:
        if fragment_mode:
            raise ValueError("FencedSQLiteStore supports full mode only")
        return int(
            self._mutate(
                token,
                "finalize_unconsumed_updates",
                fragment_mode=False,
                reason=reason,
            )
        )

    def drop_superseded_updates(
        self,
        token: LeaderToken,
        selected_updates: list[dict[str, Any]],
        reason: str = "superseded",
    ) -> int:
        return int(self._mutate(token, "drop_superseded_updates", selected_updates, reason))

    def delete_archived_rows(
        self,
        token: LeaderToken,
        *,
        update_rows: list[dict[str, Any]],
        version_rows: list[dict[str, Any]],
    ) -> None:
        if any(row.get("update_kind") == "fragment" for row in update_rows):
            raise ValueError("FencedSQLiteStore cannot mutate fragment rows")
        if any(row.get("version_kind") == "fragment" for row in version_rows):
            raise ValueError("FencedSQLiteStore cannot mutate fragment rows")
        now = float(self._wall_clock())

        def operation(conn: _FencedConnection) -> None:
            pending_paths = {
                str(Path(str(row["file_path"])).resolve(strict=False))
                for row in update_rows
                if row.get("file_path")
            }
            conn.executemany(
                """
                INSERT INTO gc_pending(file_path, archived_at)
                VALUES (?, ?)
                ON CONFLICT(file_path) DO UPDATE SET archived_at=excluded.archived_at
                """,
                [(path, now) for path in sorted(pending_paths)],
            )
            for row in update_rows:
                conn.execute(
                    "DELETE FROM updates WHERE update_id = ? AND status IN (?, ?)",
                    (row["update_id"], "applied", "dropped"),
                )
            for row in version_rows:
                for artifact_kind, path_field in (
                    ("weight", "weight_path"),
                    ("optim", "optim_path"),
                ):
                    conn.execute(
                        """
                        INSERT INTO gc_candidates(
                            relative_path, artifact_kind, owning_epoch, publication_id,
                            state, not_before, recorded_by_epoch, recorded_at
                        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                        ON CONFLICT(relative_path) DO NOTHING
                        """,
                        (
                            str(row[path_field]),
                            artifact_kind,
                            int(row["commit_epoch"]),
                            str(row["publication_id"]),
                            now + self.gc_grace_seconds,
                            token.epoch,
                            now,
                        ),
                    )
                conn.execute(
                    "DELETE FROM global_versions WHERE version = ?",
                    (int(row["version"]),),
                )

        self._transaction(token, operation)

    def claim_ready_gc_candidates(
        self, token: LeaderToken, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else float(now)

        def operation(conn: _FencedConnection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT * FROM gc_candidates
                WHERE state IN ('pending', 'deleting') AND not_before <= ?
                ORDER BY recorded_at, relative_path
                """,
                (timestamp,),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                referenced = conn.execute(
                    """
                    SELECT 1 FROM global_versions
                    WHERE weight_path = ? OR optim_path = ? LIMIT 1
                    """,
                    (row["relative_path"], row["relative_path"]),
                ).fetchone()
                if referenced is not None:
                    raise RuntimeError(f"GC candidate became referenced: {row['relative_path']}")
                conn.execute(
                    "UPDATE gc_candidates SET state='deleting' WHERE relative_path=?",
                    (row["relative_path"],),
                )
                claimed.append(dict(row))
            return claimed

        return self._transaction(token, operation)

    def register_orphan_gc_candidate(
        self,
        token: LeaderToken,
        *,
        relative_path: str,
        artifact_kind: str,
        owning_epoch: int,
        publication_id: str,
        not_before: float,
        recorded_at: float,
    ) -> None:
        if int(owning_epoch) >= token.epoch:
            raise RuntimeError("only a prior epoch artifact can be registered as an orphan")

        def operation(conn: _FencedConnection) -> None:
            referenced = conn.execute(
                """
                SELECT 1 FROM global_versions
                WHERE weight_path = ? OR optim_path = ? LIMIT 1
                """,
                (relative_path, relative_path),
            ).fetchone()
            if referenced is not None:
                raise RuntimeError(f"cannot register referenced GC path: {relative_path}")
            conn.execute(
                """
                INSERT INTO gc_candidates(
                    relative_path, artifact_kind, owning_epoch, publication_id,
                    state, not_before, recorded_by_epoch, recorded_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(relative_path) DO NOTHING
                """,
                (
                    relative_path,
                    artifact_kind,
                    int(owning_epoch),
                    publication_id,
                    float(not_before),
                    token.epoch,
                    float(recorded_at),
                ),
            )

        self._transaction(token, operation)

    def claim_gc_candidate(
        self,
        token: LeaderToken,
        relative_path: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = time.time() if now is None else float(now)

        def operation(conn: _FencedConnection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT * FROM gc_candidates
                WHERE relative_path=? AND state IN ('pending', 'deleting') AND not_before <= ?
                """,
                (relative_path, timestamp),
            ).fetchone()
            if row is None:
                return None
            referenced = conn.execute(
                """
                SELECT 1 FROM global_versions
                WHERE weight_path = ? OR optim_path = ? LIMIT 1
                """,
                (relative_path, relative_path),
            ).fetchone()
            if referenced is not None:
                raise RuntimeError(f"GC candidate became referenced: {relative_path}")
            conn.execute(
                "UPDATE gc_candidates SET state='deleting' WHERE relative_path=?",
                (relative_path,),
            )
            return dict(row)

        return self._transaction(token, operation)

    def expedite_terminal_gc_candidates(
        self,
        token: LeaderToken,
        *,
        now: float | None = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)

        def operation(conn: _FencedConnection) -> int:
            cursor = conn.execute(
                """
                UPDATE gc_candidates SET not_before=?
                WHERE state IN ('pending', 'deleting') AND not_before > ?
                """,
                (timestamp, timestamp),
            )
            return int(cursor.rowcount)

        return int(self._transaction(token, operation))

    def complete_gc_candidate(
        self,
        token: LeaderToken,
        relative_path: str,
        *,
        deleted_at: float | None = None,
    ) -> None:
        timestamp = time.time() if deleted_at is None else float(deleted_at)

        def operation(conn: _FencedConnection) -> None:
            cursor = conn.execute(
                """
                UPDATE gc_candidates SET state='deleted', deleted_at=?
                WHERE relative_path=? AND state='deleting'
                """,
                (timestamp, relative_path),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"GC candidate is not claimed: {relative_path}")
            conn.execute(
                "DELETE FROM gc_candidates WHERE relative_path=? AND state='deleted'",
                (relative_path,),
            )

        self._transaction(token, operation)

    def delete_archived_ha_history(self, token: LeaderToken, *, before_epoch: int) -> None:
        def operation(conn: _FencedConnection) -> None:
            conn.execute(
                "DELETE FROM control_publications WHERE published_by_epoch < ?",
                (int(before_epoch),),
            )
            conn.execute(
                "DELETE FROM syncer_epochs WHERE epoch < ?",
                (int(before_epoch),),
            )

        self._transaction(token, operation)

    def clear_gc_pending_paths(self, token: LeaderToken, paths: Iterable[str | Path]) -> int:
        return int(self._mutate(token, "clear_gc_pending_paths", paths))

    def record_control_publication(
        self,
        token: LeaderToken,
        *,
        kind: str,
        logical_generation: int,
        relative_path: str,
        sha256: str,
        created_at: float | None = None,
    ) -> None:
        timestamp = time.time() if created_at is None else float(created_at)

        def operation(conn: _FencedConnection) -> None:
            existing = conn.execute(
                """
                SELECT * FROM control_publications
                WHERE kind = ? AND logical_generation = ? AND published_by_epoch = ?
                """,
                (kind, int(logical_generation), token.epoch),
            ).fetchone()
            expected = (token.owner_id, relative_path, sha256)
            if existing is not None:
                observed = (
                    str(existing["published_by_owner_id"]),
                    str(existing["relative_path"]),
                    str(existing["sha256"]),
                )
                if observed != expected:
                    raise RuntimeError(
                        f"control publication collision for {kind}/g{logical_generation}: "
                        f"{observed} != {expected}"
                    )
                return
            conn.execute(
                """
                INSERT INTO control_publications(
                    kind, logical_generation, published_by_epoch,
                    published_by_owner_id, relative_path, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    int(logical_generation),
                    token.epoch,
                    token.owner_id,
                    relative_path,
                    sha256,
                    timestamp,
                ),
            )

        self._transaction(token, operation)

    def set_controller_state(
        self,
        token: LeaderToken,
        *,
        state: str,
        generation: int,
        reason: str | None = None,
        requested_at: float | None = None,
        max_terminal_version: int | None = None,
    ) -> dict[str, Any]:
        def operation(conn: _FencedConnection) -> dict[str, Any]:
            current = conn.execute("SELECT * FROM controller_state WHERE singleton = 1").fetchone()
            if current is not None and int(current["generation"]) > int(generation):
                raise RuntimeError("controller generation cannot move backwards")
            conn.execute(
                """
                INSERT INTO controller_state(
                    singleton, state, generation, reason, requested_at,
                    max_terminal_version, updated_by_epoch, updated_by_owner_id
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    state=excluded.state,
                    generation=excluded.generation,
                    reason=excluded.reason,
                    requested_at=excluded.requested_at,
                    max_terminal_version=excluded.max_terminal_version,
                    updated_by_epoch=excluded.updated_by_epoch,
                    updated_by_owner_id=excluded.updated_by_owner_id
                """,
                (
                    state,
                    int(generation),
                    reason,
                    requested_at,
                    max_terminal_version,
                    token.epoch,
                    token.owner_id,
                ),
            )
            row = conn.execute("SELECT * FROM controller_state WHERE singleton = 1").fetchone()
            assert row is not None
            return dict(row)

        return self._transaction(token, operation)

    def finalize_terminal_state(
        self,
        token: LeaderToken,
        *,
        generation: int,
        stop_reason: str,
        final_version: int,
        total_seen_tokens: int,
        finalized_at: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if finalized_at is None else float(finalized_at)

        def operation(conn: _FencedConnection) -> dict[str, Any]:
            current = conn.execute("SELECT * FROM terminal_state WHERE singleton = 1").fetchone()
            if current is not None:
                if int(current["generation"]) > int(generation):
                    raise RuntimeError("terminal generation cannot move backwards")
                if int(current["generation"]) == int(generation):
                    expected = (stop_reason, int(final_version), int(total_seen_tokens))
                    observed = (
                        str(current["stop_reason"]),
                        int(current["final_version"]),
                        int(current["total_seen_tokens"]),
                    )
                    if observed != expected:
                        raise RuntimeError("terminal generation collision")
                    return dict(current)
            conn.execute(
                """
                INSERT INTO terminal_state(
                    singleton, generation, stop_reason, final_version,
                    total_seen_tokens, finalized_by_epoch, finalized_by_owner_id,
                    finalized_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    generation=excluded.generation,
                    stop_reason=excluded.stop_reason,
                    final_version=excluded.final_version,
                    total_seen_tokens=excluded.total_seen_tokens,
                    finalized_by_epoch=excluded.finalized_by_epoch,
                    finalized_by_owner_id=excluded.finalized_by_owner_id,
                    finalized_at=excluded.finalized_at
                """,
                (
                    int(generation),
                    stop_reason,
                    int(final_version),
                    int(total_seen_tokens),
                    token.epoch,
                    token.owner_id,
                    timestamp,
                ),
            )
            row = conn.execute("SELECT * FROM terminal_state WHERE singleton = 1").fetchone()
            assert row is not None
            return dict(row)

        return self._transaction(token, operation)


_BOUND_MUTATORS = {
    "admit_registration",
    "advance_dynamic_drain",
    "begin_dynamic_drain",
    "authorize_placement_replacement",
    "claim_gc_candidate",
    "claim_ready_gc_candidates",
    "clear_gc_pending_paths",
    "commit_full_merge",
    "complete_gc_candidate",
    "delete_archived_rows",
    "delete_archived_ha_history",
    "delete_archived_dynamic_history",
    "drop_ineligible_updates",
    "drop_obsolete_updates",
    "drop_superseded_updates",
    "drop_updates",
    "expedite_terminal_gc_candidates",
    "finalize_terminal_state",
    "finalize_unconsumed_updates",
    "initialize_full_run",
    "initialize_dynamic_membership",
    "insert_update_metadata",
    "mark_updates_applied",
    "mark_updates_selected",
    "prepare_full_resume",
    "record_control_publication",
    "record_capacity_observation",
    "record_starvation_capacity_observation",
    "record_external_launch_jobs",
    "reject_registration",
    "revoke_dead_instances",
    "register_orphan_gc_candidate",
    "reset_all_selected_to_pending",
    "reset_selected_to_pending",
    "set_controller_state",
    "set_run_state",
    "update_learner_status",
    "update_instance_heartbeat",
    "update_instance_heartbeats",
    "update_launch_request",
    "upsert_global_version",
    "upsert_learner",
}


class LeaderBoundSQLiteStore:
    """Runtime adapter binding one immutable token to the explicit fenced API."""

    def __init__(self, store: FencedSQLiteStore, token: LeaderToken) -> None:
        self.fenced_store = store
        self.token = token
        self.path = store.path

    def close(self) -> None:
        self.fenced_store.close()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.fenced_store, name)
        if name not in _BOUND_MUTATORS:
            return target

        def bound(*args: Any, **kwargs: Any) -> Any:
            return target(self.token, *args, **kwargs)

        return bound


class ReadOnlySQLiteStore:
    """Analysis/checker store backed by SQLite ``mode=ro`` and ``query_only``."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self._connection = open_readonly(self.path)
        store = SQLiteStore.__new__(SQLiteStore)
        store.path = self.path
        store.conn = self._connection
        store._pointer_signatures = {}
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    @property
    def dynamic_mode(self) -> bool:
        row = self._connection.execute("SELECT mode FROM schema_meta WHERE singleton=1").fetchone()
        return row is not None and str(row["mode"]) == "full_dynamic"

    def current_instances(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT * FROM learner_instances
            WHERE status IN ('admitted','draining','drained')
            ORDER BY stream_id, instance_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def streams(self) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM streams ORDER BY stream_id").fetchall()
        return [dict(row) for row in rows]

    def launch_requests(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        where = (
            "WHERE state IN ('planned','submitting','submitted','started',"
            "'external_submitted','submission_unknown','retryable')"
            if active_only
            else ""
        )
        rows = self._connection.execute(
            f"SELECT * FROM launch_requests {where} ORDER BY created_at, request_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def registration_requests(self, *, unresolved_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE state='pending'" if unresolved_only else ""
        rows = self._connection.execute(
            f"SELECT * FROM registration_requests {where} ORDER BY created_at, instance_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def capacity_observations(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM capacity_observations ORDER BY observation_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def dynamic_state_counts(self) -> dict[str, int]:
        if not self.dynamic_mode:
            return {}
        return {
            "current_instances": len(self.current_instances()),
            "registration_requests": len(self.registration_requests(unresolved_only=True)),
            "active_launch_requests": len(self.launch_requests(active_only=True)),
            "capacity_observations": len(self.capacity_observations()),
        }

    def __getattr__(self, name: str) -> Any:
        if name in _READ_METHODS:
            return getattr(self._store, name)
        raise AttributeError(name)

    def execute(
        self,
        sql: str,
        parameters: Iterable[Any] | Mapping[str, Any] = (),
    ) -> sqlite3.Cursor:
        keyword = _keyword(sql)
        if keyword in _DDL_KEYWORDS or keyword in _MUTATING_KEYWORDS:
            raise sqlite3.OperationalError("ReadOnlySQLiteStore accepts queries only")
        if keyword not in _READ_KEYWORDS and not (keyword == "PRAGMA" and _read_only_pragma(sql)):
            raise sqlite3.OperationalError("ReadOnlySQLiteStore rejects unrecognized SQL")
        return self._connection.execute(sql, _parameters(parameters))
