"""Persistent shared-filesystem SQLite metadata store."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .atomic_io import ensure_dir
from ..core.constants import (
    GLOBAL_STATUS_COMMITTED,
    LEARNER_STATUS_UNKNOWN,
    UPDATE_STATUS_APPLIED,
    UPDATE_STATUS_DROPPED,
    UPDATE_STATUS_PENDING,
    UPDATE_STATUS_SELECTED,
)


def _schema_text() -> str:
    return resources.files("fs_diloco.storage").joinpath("schema.sql").read_text(encoding="utf-8")


RESOURCE_COLUMNS = {
    "training_cpu_utilization_peak_percent": "REAL",
    "training_gpu_utilization_peak_percent": "REAL",
    "local_cycle_cpu_utilization_peak_percent": "REAL",
    "local_cycle_gpu_utilization_peak_percent": "REAL",
    "local_cycle_step_time_seconds_mean": "REAL",
    "local_cycle_step_count": "INTEGER",
    "local_cycle_resource_sample_count": "INTEGER",
}


def _ensure_resource_columns(conn: sqlite3.Connection, table: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, sql_type in RESOURCE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    ensure_dir(path.parent)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    journal_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
    if journal_mode != "delete":
        conn.close()
        raise RuntimeError(f"SQLite journal_mode is {journal_mode!r}, expected 'delete'")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(_schema_text())
    _ensure_resource_columns(conn, "updates")
    _ensure_resource_columns(conn, "fragment_updates")
    conn.commit()
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def integrity_check(self) -> None:
        rows = [str(row[0]) for row in self.conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise RuntimeError(f"SQLite integrity_check failed: {rows}")

    def pragma_settings(self) -> dict[str, Any]:
        return {
            "journal_mode": str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": int(self.conn.execute("PRAGMA synchronous").fetchone()[0]),
        }

    def committed_global_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM global_versions WHERE status = ?",
            (GLOBAL_STATUS_COMMITTED,),
        ).fetchone()
        return int(row[0])

    def latest_global_version(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM global_versions
            WHERE status = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (GLOBAL_STATUS_COMMITTED,),
        ).fetchone()
        return row_to_dict(row)

    def set_run_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO run_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, sort_keys=True), time.time()),
        )
        self.conn.commit()

    def get_run_state(self, key: str) -> Any | None:
        row = self.conn.execute("SELECT value FROM run_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def upsert_global_version(
        self,
        version: int,
        weight_path: str,
        optim_path: str,
        *,
        num_updates: int = 0,
        total_update_tokens: int = 0,
        total_seen_tokens: int = 0,
        outer_optimizer: str,
        status: str = GLOBAL_STATUS_COMMITTED,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO global_versions(
                version, weight_path, optim_path, created_at, num_updates, total_update_tokens,
                total_seen_tokens, outer_optimizer, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                weight_path=excluded.weight_path,
                optim_path=excluded.optim_path,
                num_updates=excluded.num_updates,
                total_update_tokens=excluded.total_update_tokens,
                total_seen_tokens=excluded.total_seen_tokens,
                outer_optimizer=excluded.outer_optimizer,
                status=excluded.status,
                notes=excluded.notes
            """,
            (
                version,
                weight_path,
                optim_path,
                time.time(),
                num_updates,
                total_update_tokens,
                total_seen_tokens,
                outer_optimizer,
                status,
                notes,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _set_run_state_in_transaction(
        conn: sqlite3.Connection,
        key: str,
        value: Any,
        *,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO run_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, sort_keys=True), now),
        )

    def initialize_full_run(
        self,
        *,
        weight_path: str,
        optim_path: str,
        outer_optimizer: str,
        identity: dict[str, Any],
        config_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Create v0 and its run identity in one transaction."""
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT COUNT(*) FROM global_versions WHERE status = ?",
                (GLOBAL_STATUS_COMMITTED,),
            ).fetchone()[0]
            if int(existing) != 0:
                raise RuntimeError("non-resume initialization found an existing committed version")
            self.conn.execute(
                """
                INSERT INTO global_versions(
                    version, weight_path, optim_path, created_at, num_updates,
                    total_update_tokens, total_seen_tokens, outer_optimizer, status, notes
                ) VALUES (0, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                (
                    weight_path,
                    optim_path,
                    now,
                    outer_optimizer,
                    GLOBAL_STATUS_COMMITTED,
                    "initialized",
                ),
            )
            self._set_run_state_in_transaction(
                self.conn, "identity", identity, now=now
            )
            self._set_run_state_in_transaction(
                self.conn, "config", config_snapshot, now=now
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        row = self.get_global_version(0)
        assert row is not None
        return row

    def commit_full_merge(
        self,
        *,
        predecessor_version: int,
        target_version: int,
        weight_path: str,
        optim_path: str,
        selected_updates: list[dict[str, Any]],
        effective_weights: dict[str, float],
        total_update_tokens: int,
        total_seen_tokens: int,
        outer_optimizer: str,
        max_staleness_versions: int,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Atomically commit a full merge and every resulting update transition."""
        if target_version != predecessor_version + 1:
            raise ValueError(
                f"target version {target_version} does not follow {predecessor_version}"
            )
        if not selected_updates:
            raise ValueError("cannot commit a merge without selected updates")
        selected_ids = [str(row["update_id"]) for row in selected_updates]
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("selected update IDs are not unique")
        learners = [str(row["learner_id"]) for row in selected_updates]
        if len(learners) != len(set(learners)):
            raise ValueError("selected updates contain a duplicate learner")
        if set(effective_weights) != set(selected_ids):
            raise ValueError("effective weights do not exactly match selected updates")

        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            max_row = self.conn.execute(
                """
                SELECT MAX(version) AS version
                FROM global_versions
                WHERE status = ?
                """,
                (GLOBAL_STATUS_COMMITTED,),
            ).fetchone()
            actual_predecessor = max_row["version"]
            if actual_predecessor is None or int(actual_predecessor) != predecessor_version:
                raise RuntimeError(
                    "committed predecessor mismatch: "
                    f"expected {predecessor_version}, found {actual_predecessor}"
                )

            placeholders = ",".join("?" for _ in selected_ids)
            db_rows = self.conn.execute(
                f"SELECT * FROM updates WHERE update_id IN ({placeholders})",
                selected_ids,
            ).fetchall()
            by_id = {str(row["update_id"]): row for row in db_rows}
            if set(by_id) != set(selected_ids):
                raise RuntimeError("one or more selected updates are absent from SQLite")
            for update_id in selected_ids:
                row = by_id[update_id]
                if row["status"] != UPDATE_STATUS_SELECTED:
                    raise RuntimeError(f"update {update_id} is not selected")
                base = int(row["base_global_version"])
                if base > predecessor_version:
                    raise RuntimeError(f"update {update_id} has a future base version {base}")
                stale = predecessor_version - base
                if stale > max_staleness_versions:
                    raise RuntimeError(
                        f"update {update_id} staleness {stale} exceeds {max_staleness_versions}"
                    )

            self.conn.execute(
                """
                INSERT INTO global_versions(
                    version, weight_path, optim_path, created_at, num_updates,
                    total_update_tokens, total_seen_tokens, outer_optimizer, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    target_version,
                    weight_path,
                    optim_path,
                    now,
                    len(selected_ids),
                    int(total_update_tokens),
                    int(total_seen_tokens),
                    outer_optimizer,
                    GLOBAL_STATUS_COMMITTED,
                ),
            )
            for update_id in selected_ids:
                row = by_id[update_id]
                self.conn.execute(
                    """
                    UPDATE updates
                    SET status = ?, applied_at = ?, applied_version = ?,
                        staleness_versions = ?, effective_weight = ?
                    WHERE update_id = ? AND status = ?
                    """,
                    (
                        UPDATE_STATUS_APPLIED,
                        now,
                        target_version,
                        predecessor_version - int(row["base_global_version"]),
                        float(effective_weights[update_id]),
                        update_id,
                        UPDATE_STATUS_SELECTED,
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE updates
                    SET status = ?, drop_reason = ?
                    WHERE status = ? AND learner_id = ? AND update_id != ?
                      AND (
                        local_step_end < ?
                        OR (local_step_end = ? AND committed_at <= ?)
                      )
                    """,
                    (
                        UPDATE_STATUS_DROPPED,
                        "superseded",
                        UPDATE_STATUS_PENDING,
                        row["learner_id"],
                        update_id,
                        int(row["local_step_end"]),
                        int(row["local_step_end"]),
                        float(row["committed_at"]),
                    ),
                )
            self.conn.execute(
                """
                UPDATE updates
                SET status = ?, drop_reason = ?
                WHERE status = ? AND base_global_version > ?
                """,
                (
                    UPDATE_STATUS_DROPPED,
                    "future_base",
                    UPDATE_STATUS_PENDING,
                    target_version,
                ),
            )
            self.conn.execute(
                """
                UPDATE updates
                SET status = ?, drop_reason = ?
                WHERE status = ? AND (? - base_global_version) > ?
                """,
                (
                    UPDATE_STATUS_DROPPED,
                    "too_stale",
                    UPDATE_STATUS_PENDING,
                    target_version,
                    max_staleness_versions,
                ),
            )
            if before_commit is not None:
                before_commit()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        row = self.get_global_version(target_version)
        assert row is not None
        return row

    def upsert_learner(
        self,
        learner_id: str,
        *,
        hostname: str | None = None,
        pid: int | None = None,
        last_seen: float | None = None,
        last_loaded_global_version: int | None = None,
        last_local_step: int | None = None,
        last_update_id: str | None = None,
        tokens_per_sec: float | None = None,
        last_heartbeat_path: str | None = None,
        status: str = LEARNER_STATUS_UNKNOWN,
        status_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO learners(
                learner_id, hostname, pid, last_seen, last_loaded_global_version,
                last_local_step, last_update_id, tokens_per_sec, last_heartbeat_path, status,
                status_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(learner_id) DO UPDATE SET
                hostname=COALESCE(excluded.hostname, learners.hostname),
                pid=COALESCE(excluded.pid, learners.pid),
                last_seen=COALESCE(excluded.last_seen, learners.last_seen),
                last_loaded_global_version=COALESCE(
                    excluded.last_loaded_global_version, learners.last_loaded_global_version
                ),
                last_local_step=COALESCE(excluded.last_local_step, learners.last_local_step),
                last_update_id=COALESCE(excluded.last_update_id, learners.last_update_id),
                tokens_per_sec=COALESCE(excluded.tokens_per_sec, learners.tokens_per_sec),
                last_heartbeat_path=COALESCE(excluded.last_heartbeat_path, learners.last_heartbeat_path),
                status=excluded.status,
                status_reason=excluded.status_reason
            """,
            (
                learner_id,
                hostname,
                pid,
                last_seen,
                last_loaded_global_version,
                last_local_step,
                last_update_id,
                tokens_per_sec,
                last_heartbeat_path,
                status,
                status_reason,
            ),
        )
        self.conn.commit()

    def update_learner_status(
        self, learner_id: str, status: str, reason: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE learners SET status = ?, status_reason = ? WHERE learner_id = ?",
            (status, reason, learner_id),
        )
        self.conn.commit()

    def list_learners(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM learners ORDER BY learner_id").fetchall()
        return [dict(row) for row in rows]

    def learner_resource_peaks(self, *, fragment_mode: bool) -> list[dict[str, Any]]:
        table = "fragment_updates" if fragment_mode else "updates"
        rows = self.conn.execute(
            f"""
            SELECT
                learner_id,
                MAX(training_cpu_utilization_peak_percent)
                    AS training_cpu_utilization_peak_percent,
                MAX(training_gpu_utilization_peak_percent)
                    AS training_gpu_utilization_peak_percent
            FROM {table}
            GROUP BY learner_id
            ORDER BY learner_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_update_metadata(
        self,
        metadata: dict[str, Any],
        *,
        pointer_path: str | Path | None = None,
        ingested_at: float | None = None,
    ) -> bool:
        ingested_at = time.time() if ingested_at is None else ingested_at
        params = {
            "update_id": metadata["update_id"],
            "learner_id": metadata["learner_id"],
            "hostname": metadata.get("hostname"),
            "base_global_version": metadata["base_global_version"],
            "local_step_start": metadata["local_step_start"],
            "local_step_end": metadata["local_step_end"],
            "inner_steps": metadata["inner_steps"],
            "tokens_this_update": metadata["tokens_this_update"],
            "tokens_since_global_load": metadata["tokens_since_global_load"],
            "num_examples_this_update": metadata.get("num_examples_this_update"),
            "train_loss": metadata.get("train_loss"),
            "grad_norm": metadata.get("grad_norm"),
            "param_norm": metadata.get("param_norm"),
            "delta_norm": metadata.get("delta_norm"),
            **{name: metadata.get(name) for name in RESOURCE_COLUMNS},
            "file_path": metadata["file_path"],
            "file_size_bytes": metadata.get("file_size_bytes"),
            "sha256": metadata.get("sha256"),
            "created_at": metadata["created_at"],
            "committed_at": metadata["committed_at"],
            "ingested_at": ingested_at,
            "status": UPDATE_STATUS_PENDING,
        }
        pointer = str(pointer_path or "")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            frontier = self.conn.execute(
                "SELECT last_observed_update_id FROM proposal_frontiers WHERE learner_id = ?",
                (metadata["learner_id"],),
            ).fetchone()
            if frontier is not None and frontier["last_observed_update_id"] == metadata["update_id"]:
                self.conn.rollback()
                return False
            self.conn.execute(
                """
                UPDATE updates
                SET status = ?, drop_reason = ?
                WHERE learner_id = ? AND status = ? AND update_id != ?
                """,
                (
                    UPDATE_STATUS_DROPPED,
                    "superseded",
                    metadata["learner_id"],
                    UPDATE_STATUS_PENDING,
                    metadata["update_id"],
                ),
            )
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO updates(
                    update_id, learner_id, hostname, base_global_version, local_step_start,
                    local_step_end, inner_steps, tokens_this_update, tokens_since_global_load,
                    num_examples_this_update, train_loss, grad_norm, param_norm, delta_norm,
                    training_cpu_utilization_peak_percent, training_gpu_utilization_peak_percent,
                    local_cycle_cpu_utilization_peak_percent,
                    local_cycle_gpu_utilization_peak_percent,
                    local_cycle_step_time_seconds_mean, local_cycle_step_count,
                    local_cycle_resource_sample_count, file_path, file_size_bytes, sha256,
                    created_at, committed_at, ingested_at, status
                )
                VALUES (
                    :update_id, :learner_id, :hostname, :base_global_version,
                    :local_step_start, :local_step_end, :inner_steps, :tokens_this_update,
                    :tokens_since_global_load, :num_examples_this_update, :train_loss,
                    :grad_norm, :param_norm, :delta_norm,
                    :training_cpu_utilization_peak_percent,
                    :training_gpu_utilization_peak_percent,
                    :local_cycle_cpu_utilization_peak_percent,
                    :local_cycle_gpu_utilization_peak_percent,
                    :local_cycle_step_time_seconds_mean, :local_cycle_step_count,
                    :local_cycle_resource_sample_count, :file_path, :file_size_bytes,
                    :sha256, :created_at, :committed_at, :ingested_at, :status
                )
                """,
                params,
            )
            self.conn.execute(
                """
                INSERT INTO proposal_frontiers(
                    learner_id, last_observed_update_id, last_pointer_path, observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(learner_id) DO UPDATE SET
                    last_observed_update_id=excluded.last_observed_update_id,
                    last_pointer_path=excluded.last_pointer_path,
                    observed_at=excluded.observed_at
                """,
                (metadata["learner_id"], metadata["update_id"], pointer, ingested_at),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception:
            self.conn.rollback()
            raise

    def pending_updates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM updates WHERE status = ? ORDER BY committed_at ASC",
            (UPDATE_STATUS_PENDING,),
        ).fetchall()
        return [dict(row) for row in rows]

    def eligible_updates(
        self, current_version: int, max_staleness_versions: int
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM updates
            WHERE status = ?
              AND base_global_version <= ?
              AND (? - base_global_version) <= ?
            ORDER BY committed_at ASC
            """,
            (
                UPDATE_STATUS_PENDING,
                current_version,
                current_version,
                max_staleness_versions,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_updates_selected(self, update_ids: list[str], selected_by_run: str) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            """
            UPDATE updates
            SET status = ?, selected_at = ?, selected_by_run = ?
            WHERE update_id = ? AND status = ?
            """,
            [
                (
                    UPDATE_STATUS_SELECTED,
                    time.time(),
                    selected_by_run,
                    update_id,
                    UPDATE_STATUS_PENDING,
                )
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def mark_updates_applied(
        self,
        updates: list[dict[str, Any]],
        *,
        applied_version: int,
        effective_weights: dict[str, float],
    ) -> None:
        now = time.time()
        self.conn.executemany(
            """
            UPDATE updates
            SET status = ?, applied_at = ?, applied_version = ?, staleness_versions = ?,
                effective_weight = ?
            WHERE update_id = ?
            """,
            [
                (
                    UPDATE_STATUS_APPLIED,
                    now,
                    applied_version,
                    applied_version - 1 - int(update["base_global_version"]),
                    effective_weights.get(update["update_id"]),
                    update["update_id"],
                )
                for update in updates
            ],
        )
        self.conn.commit()

    def reset_selected_to_pending(self, update_ids: list[str]) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            "UPDATE updates SET status = ?, selected_at = NULL WHERE update_id = ? AND status = ?",
            [
                (UPDATE_STATUS_PENDING, update_id, UPDATE_STATUS_SELECTED)
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def reset_all_selected_to_pending(self) -> int:
        cur = self.conn.execute(
            """
            UPDATE updates
            SET status = ?, selected_at = NULL, selected_by_run = NULL
            WHERE status = ?
            """,
            (UPDATE_STATUS_PENDING, UPDATE_STATUS_SELECTED),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def drop_updates(self, update_ids: list[str], reason: str) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            """
            UPDATE updates
            SET status = ?, drop_reason = ?
            WHERE update_id = ? AND status IN (?, ?)
            """,
            [
                (
                    UPDATE_STATUS_DROPPED,
                    reason,
                    update_id,
                    UPDATE_STATUS_PENDING,
                    UPDATE_STATUS_SELECTED,
                )
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def drop_obsolete_updates(self, current_version: int, max_staleness_versions: int) -> int:
        cur = self.conn.execute(
            """
            UPDATE updates
            SET status = ?, drop_reason = ?
            WHERE status = ? AND (? - base_global_version) > ?
            """,
            (
                UPDATE_STATUS_DROPPED,
                "too_stale",
                UPDATE_STATUS_PENDING,
                current_version,
                max_staleness_versions,
            ),
        )
        self.conn.commit()
        return cur.rowcount

    def drop_ineligible_updates(
        self, current_version: int, max_staleness_versions: int
    ) -> int:
        future = self.conn.execute(
            """
            UPDATE updates
            SET status = ?, drop_reason = ?
            WHERE status = ? AND base_global_version > ?
            """,
            (
                UPDATE_STATUS_DROPPED,
                "future_base",
                UPDATE_STATUS_PENDING,
                current_version,
            ),
        ).rowcount
        stale = self.conn.execute(
            """
            UPDATE updates
            SET status = ?, drop_reason = ?
            WHERE status = ? AND (? - base_global_version) > ?
            """,
            (
                UPDATE_STATUS_DROPPED,
                "too_stale",
                UPDATE_STATUS_PENDING,
                current_version,
                max_staleness_versions,
            ),
        ).rowcount
        self.conn.commit()
        return int(future) + int(stale)

    def finalize_unconsumed_updates(self, *, fragment_mode: bool, reason: str) -> int:
        table = "fragment_updates" if fragment_mode else "updates"
        cur = self.conn.execute(
            f"""
            UPDATE {table}
            SET status = ?, drop_reason = ?
            WHERE status IN (?, ?)
            """,
            (
                UPDATE_STATUS_DROPPED,
                reason,
                UPDATE_STATUS_PENDING,
                UPDATE_STATUS_SELECTED,
            ),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def drop_superseded_updates(
        self, selected_updates: list[dict[str, Any]], reason: str = "superseded"
    ) -> int:
        """Drop older pending updates from learners already represented in an outer step."""
        if not selected_updates:
            return 0
        total = 0
        for update in selected_updates:
            cur = self.conn.execute(
                """
                UPDATE updates
                SET status = ?, drop_reason = ?
                WHERE status = ?
                  AND learner_id = ?
                  AND update_id != ?
                  AND (
                    local_step_end <= ?
                    OR (local_step_end = ? AND committed_at <= ?)
                  )
                """,
                (
                    UPDATE_STATUS_DROPPED,
                    reason,
                    UPDATE_STATUS_PENDING,
                    update["learner_id"],
                    update["update_id"],
                    update["local_step_end"],
                    update["local_step_end"],
                    update["committed_at"],
                ),
            )
            total += cur.rowcount
        self.conn.commit()
        return total

    def get_update(self, update_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row_to_dict(row)

    def get_global_version(self, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM global_versions WHERE version = ?", (version,)
        ).fetchone()
        return row_to_dict(row)

    def active_payload_paths(self) -> set[Path]:
        paths: set[Path] = set()
        for table in ("updates", "fragment_updates"):
            rows = self.conn.execute(
                f"SELECT file_path FROM {table} WHERE status IN (?, ?)",
                (UPDATE_STATUS_PENDING, UPDATE_STATUS_SELECTED),
            ).fetchall()
            paths.update(Path(str(row["file_path"])) for row in rows)
        return paths

    def proposal_frontiers(self) -> dict[str, str]:
        return {
            str(row["learner_id"]): str(row["last_observed_update_id"])
            for row in self.conn.execute(
                "SELECT learner_id, last_observed_update_id FROM proposal_frontiers"
            ).fetchall()
        }

    def terminal_update_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table, kind in (("updates", "full"), ("fragment_updates", "fragment")):
            for row in self.conn.execute(
                f"SELECT * FROM {table} WHERE status IN (?, ?) ORDER BY committed_at, update_id",
                (UPDATE_STATUS_APPLIED, UPDATE_STATUS_DROPPED),
            ).fetchall():
                payload = dict(row)
                payload["update_kind"] = kind
                rows.append(payload)
        return rows

    def historical_version_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        current = self.latest_global_version()
        current_version = int(current["version"]) if current is not None else None
        query = "SELECT * FROM global_versions"
        params: tuple[Any, ...] = ()
        if current_version is not None:
            query += " WHERE version != ?"
            params = (current_version,)
        query += " ORDER BY version"
        for row in self.conn.execute(query, params).fetchall():
            payload = dict(row)
            payload["version_kind"] = "full"
            rows.append(payload)
        current_fragments = {
            int(row["fragment_id"]): int(row["version"])
            for row in self.conn.execute(
                """
                SELECT fragment_id, MAX(version) AS version
                FROM fragment_versions
                GROUP BY fragment_id
                """
            ).fetchall()
        }
        for row in self.conn.execute(
            "SELECT * FROM fragment_versions ORDER BY fragment_id, version"
        ).fetchall():
            if int(row["version"]) == current_fragments[int(row["fragment_id"])]:
                continue
            payload = dict(row)
            payload["version_kind"] = "fragment"
            rows.append(payload)
        return rows

    def delete_archived_rows(
        self,
        *,
        update_rows: list[dict[str, Any]],
        version_rows: list[dict[str, Any]],
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for row in update_rows:
                table = "fragment_updates" if row["update_kind"] == "fragment" else "updates"
                self.conn.execute(
                    f"DELETE FROM {table} WHERE update_id = ? AND status IN (?, ?)",
                    (
                        row["update_id"],
                        UPDATE_STATUS_APPLIED,
                        UPDATE_STATUS_DROPPED,
                    ),
                )
            for row in version_rows:
                if row["version_kind"] == "fragment":
                    self.conn.execute(
                        "DELETE FROM fragment_versions WHERE fragment_id = ? AND version = ?",
                        (row["fragment_id"], row["version"]),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM global_versions WHERE version = ?",
                        (row["version"],),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def upsert_fragment_definition(self, fragment: dict[str, Any], *, strategy: str) -> None:
        self.conn.execute(
            """
            INSERT INTO fragments(fragment_id, strategy, numel, size_bytes, slices_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fragment_id) DO UPDATE SET
                strategy=excluded.strategy,
                numel=excluded.numel,
                size_bytes=excluded.size_bytes,
                slices_json=excluded.slices_json
            """,
            (
                int(fragment["fragment_id"]),
                strategy,
                int(fragment["numel"]),
                int(fragment.get("size_bytes_float32") or int(fragment["numel"]) * 4),
                json.dumps(fragment.get("slices") or [], sort_keys=True),
                time.time(),
            ),
        )
        self.conn.commit()

    def upsert_fragment_version(
        self,
        *,
        fragment_id: int,
        version: int,
        global_merge_event: int,
        weight_path: str,
        optim_path: str,
        num_updates: int = 0,
        total_update_tokens: int = 0,
        total_seen_tokens: int = 0,
        outer_optimizer: str,
        status: str = GLOBAL_STATUS_COMMITTED,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fragment_versions(
                fragment_id, version, global_merge_event, weight_path, optim_path,
                created_at, num_updates, total_update_tokens, total_seen_tokens,
                outer_optimizer, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fragment_id, version) DO UPDATE SET
                global_merge_event=excluded.global_merge_event,
                weight_path=excluded.weight_path,
                optim_path=excluded.optim_path,
                num_updates=excluded.num_updates,
                total_update_tokens=excluded.total_update_tokens,
                total_seen_tokens=excluded.total_seen_tokens,
                outer_optimizer=excluded.outer_optimizer,
                status=excluded.status,
                notes=excluded.notes
            """,
            (
                int(fragment_id),
                int(version),
                int(global_merge_event),
                weight_path,
                optim_path,
                time.time(),
                int(num_updates),
                int(total_update_tokens),
                int(total_seen_tokens),
                outer_optimizer,
                status,
                notes,
            ),
        )
        self.conn.commit()

    def insert_fragment_update_metadata(
        self, metadata: dict[str, Any], *, ingested_at: float | None = None
    ) -> bool:
        ingested_at = time.time() if ingested_at is None else ingested_at
        params = {
            "update_id": metadata["update_id"],
            "learner_id": metadata["learner_id"],
            "hostname": metadata.get("hostname"),
            "fragment_id": metadata["fragment_id"],
            "base_fragment_version": metadata["base_fragment_version"],
            "base_global_merge_event": metadata["base_global_merge_event"],
            "local_step_start": metadata["local_step_start"],
            "local_step_end": metadata["local_step_end"],
            "inner_steps": metadata["inner_steps"],
            "tokens_this_update": metadata["tokens_this_update"],
            "tokens_since_fragment_load": metadata["tokens_since_fragment_load"],
            "num_examples_this_update": metadata.get("num_examples_this_update"),
            "train_loss": metadata.get("train_loss"),
            "grad_norm": metadata.get("grad_norm"),
            "param_norm": metadata.get("param_norm"),
            "fragment_norm": metadata.get("fragment_norm"),
            **{name: metadata.get(name) for name in RESOURCE_COLUMNS},
            "file_path": metadata["file_path"],
            "file_size_bytes": metadata.get("file_size_bytes"),
            "sha256": metadata.get("sha256"),
            "created_at": metadata["created_at"],
            "committed_at": metadata["committed_at"],
            "ingested_at": ingested_at,
            "status": UPDATE_STATUS_PENDING,
        }
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO fragment_updates(
                update_id, learner_id, hostname, fragment_id, base_fragment_version,
                base_global_merge_event, local_step_start, local_step_end, inner_steps,
                tokens_this_update, tokens_since_fragment_load, num_examples_this_update,
                train_loss, grad_norm, param_norm, fragment_norm,
                training_cpu_utilization_peak_percent, training_gpu_utilization_peak_percent,
                local_cycle_cpu_utilization_peak_percent, local_cycle_gpu_utilization_peak_percent,
                local_cycle_step_time_seconds_mean, local_cycle_step_count,
                local_cycle_resource_sample_count, file_path,
                file_size_bytes, sha256, created_at, committed_at, ingested_at, status
            )
            VALUES (
                :update_id, :learner_id, :hostname, :fragment_id, :base_fragment_version,
                :base_global_merge_event, :local_step_start, :local_step_end, :inner_steps,
                :tokens_this_update, :tokens_since_fragment_load, :num_examples_this_update,
                :train_loss, :grad_norm, :param_norm, :fragment_norm,
                :training_cpu_utilization_peak_percent, :training_gpu_utilization_peak_percent,
                :local_cycle_cpu_utilization_peak_percent, :local_cycle_gpu_utilization_peak_percent,
                :local_cycle_step_time_seconds_mean, :local_cycle_step_count,
                :local_cycle_resource_sample_count, :file_path,
                :file_size_bytes, :sha256, :created_at, :committed_at, :ingested_at,
                :status
            )
            """,
            params,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_fragment_updates(self, *, fragment_id: int | None = None) -> list[dict[str, Any]]:
        if fragment_id is None:
            rows = self.conn.execute(
                "SELECT * FROM fragment_updates WHERE status = ? ORDER BY committed_at ASC",
                (UPDATE_STATUS_PENDING,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM fragment_updates
                WHERE status = ? AND fragment_id = ?
                ORDER BY committed_at ASC
                """,
                (UPDATE_STATUS_PENDING, int(fragment_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def eligible_fragment_updates(
        self,
        *,
        fragment_id: int,
        current_fragment_version: int,
        max_staleness_versions: int,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM fragment_updates
            WHERE status = ?
              AND fragment_id = ?
              AND base_fragment_version <= ?
              AND (? - base_fragment_version) <= ?
            ORDER BY committed_at ASC
            """,
            (
                UPDATE_STATUS_PENDING,
                int(fragment_id),
                int(current_fragment_version),
                int(current_fragment_version),
                int(max_staleness_versions),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_fragment_updates_selected(self, update_ids: list[str], selected_by_run: str) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            """
            UPDATE fragment_updates
            SET status = ?, selected_at = ?, selected_by_run = ?
            WHERE update_id = ? AND status = ?
            """,
            [
                (
                    UPDATE_STATUS_SELECTED,
                    time.time(),
                    selected_by_run,
                    update_id,
                    UPDATE_STATUS_PENDING,
                )
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def mark_fragment_updates_applied(
        self,
        updates: list[dict[str, Any]],
        *,
        applied_fragment_version: int,
        applied_global_merge_event: int,
        effective_weights: dict[str, float],
    ) -> None:
        now = time.time()
        self.conn.executemany(
            """
            UPDATE fragment_updates
            SET status = ?, applied_at = ?, applied_fragment_version = ?,
                applied_global_merge_event = ?, staleness_fragment_versions = ?,
                staleness_global_events = ?, effective_weight = ?
            WHERE update_id = ?
            """,
            [
                (
                    UPDATE_STATUS_APPLIED,
                    now,
                    int(applied_fragment_version),
                    int(applied_global_merge_event),
                    int(applied_fragment_version) - 1 - int(update["base_fragment_version"]),
                    int(applied_global_merge_event) - 1 - int(update["base_global_merge_event"]),
                    effective_weights.get(update["update_id"]),
                    update["update_id"],
                )
                for update in updates
            ],
        )
        self.conn.commit()

    def reset_fragment_selected_to_pending(self, update_ids: list[str]) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            "UPDATE fragment_updates SET status = ?, selected_at = NULL WHERE update_id = ? AND status = ?",
            [
                (UPDATE_STATUS_PENDING, update_id, UPDATE_STATUS_SELECTED)
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def reset_all_fragment_selected_to_pending(self) -> int:
        cur = self.conn.execute(
            """
            UPDATE fragment_updates
            SET status = ?, selected_at = NULL, selected_by_run = NULL
            WHERE status = ?
            """,
            (UPDATE_STATUS_PENDING, UPDATE_STATUS_SELECTED),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def drop_fragment_updates(self, update_ids: list[str], reason: str) -> None:
        if not update_ids:
            return
        self.conn.executemany(
            """
            UPDATE fragment_updates
            SET status = ?, drop_reason = ?
            WHERE update_id = ? AND status IN (?, ?)
            """,
            [
                (
                    UPDATE_STATUS_DROPPED,
                    reason,
                    update_id,
                    UPDATE_STATUS_PENDING,
                    UPDATE_STATUS_SELECTED,
                )
                for update_id in update_ids
            ],
        )
        self.conn.commit()

    def drop_obsolete_fragment_updates(
        self,
        *,
        fragment_id: int,
        current_fragment_version: int,
        max_staleness_versions: int,
    ) -> int:
        cur = self.conn.execute(
            """
            UPDATE fragment_updates
            SET status = ?, drop_reason = ?
            WHERE status = ?
              AND fragment_id = ?
              AND (? - base_fragment_version) > ?
            """,
            (
                UPDATE_STATUS_DROPPED,
                "too_stale",
                UPDATE_STATUS_PENDING,
                int(fragment_id),
                int(current_fragment_version),
                int(max_staleness_versions),
            ),
        )
        self.conn.commit()
        return cur.rowcount

    def drop_superseded_fragment_updates(
        self,
        selected_updates: list[dict[str, Any]],
        reason: str = "superseded",
    ) -> int:
        if not selected_updates:
            return 0
        total = 0
        for update in selected_updates:
            cur = self.conn.execute(
                """
                UPDATE fragment_updates
                SET status = ?, drop_reason = ?
                WHERE status = ?
                  AND learner_id = ?
                  AND fragment_id = ?
                  AND update_id != ?
                  AND (
                    local_step_end <= ?
                    OR (local_step_end = ? AND committed_at <= ?)
                  )
                """,
                (
                    UPDATE_STATUS_DROPPED,
                    reason,
                    UPDATE_STATUS_PENDING,
                    update["learner_id"],
                    int(update["fragment_id"]),
                    update["update_id"],
                    update["local_step_end"],
                    update["local_step_end"],
                    update["committed_at"],
                ),
            )
            total += cur.rowcount
        self.conn.commit()
        return total

    def get_fragment_update(self, update_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM fragment_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row_to_dict(row)

    def list_fragment_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fragment_versions ORDER BY fragment_id, version",
        ).fetchall()
        return [dict(row) for row in rows]

    def current_fragment_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT fv.*
            FROM fragment_versions AS fv
            JOIN (
                SELECT fragment_id, MAX(version) AS version
                FROM fragment_versions
                GROUP BY fragment_id
            ) AS current
              ON current.fragment_id = fv.fragment_id
             AND current.version = fv.version
            ORDER BY fv.fragment_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
