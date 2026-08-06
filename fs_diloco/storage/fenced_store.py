"""Explicit legacy, fenced HA, and read-only SQLite store surfaces."""

from __future__ import annotations

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
        self._wall_clock = wall_clock
        self.gc_grace_seconds = float(gc_grace_seconds)
        self.max_retained_epoch_dirs = int(max_retained_epoch_dirs)

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
        return self._mutate(
            token,
            "commit_full_merge",
            commit_epoch=token.epoch,
            commit_owner_id=token.owner_id,
            publication_id=publication_id,
            weight_size_bytes=weight_size_bytes,
            optim_size_bytes=optim_size_bytes,
            **kwargs,
        )

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
        return bool(self._mutate(token, "insert_update_metadata", metadata, **kwargs))

    def mark_updates_selected(
        self, token: LeaderToken, update_ids: list[str], selected_by_run: str
    ) -> None:
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
    "claim_gc_candidate",
    "claim_ready_gc_candidates",
    "clear_gc_pending_paths",
    "commit_full_merge",
    "complete_gc_candidate",
    "delete_archived_rows",
    "delete_archived_ha_history",
    "drop_ineligible_updates",
    "drop_obsolete_updates",
    "drop_superseded_updates",
    "drop_updates",
    "expedite_terminal_gc_candidates",
    "finalize_terminal_state",
    "finalize_unconsumed_updates",
    "initialize_full_run",
    "insert_update_metadata",
    "mark_updates_applied",
    "mark_updates_selected",
    "prepare_full_resume",
    "record_control_publication",
    "register_orphan_gc_candidate",
    "reset_all_selected_to_pending",
    "reset_selected_to_pending",
    "set_controller_state",
    "set_run_state",
    "update_learner_status",
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
