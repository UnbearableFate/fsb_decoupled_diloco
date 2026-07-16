"""Syncer-local SQLite metadata store."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .atomic_io import ensure_dir, file_size
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


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    ensure_dir(path.parent)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_schema_text())
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

    def update_learner_status(self, learner_id: str, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE learners SET status = ?, status_reason = ? WHERE learner_id = ?",
            (status, reason, learner_id),
        )
        self.conn.commit()

    def list_learners(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM learners ORDER BY learner_id").fetchall()
        return [dict(row) for row in rows]

    def insert_update_metadata(self, metadata: dict[str, Any], *, ingested_at: float | None = None) -> bool:
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
            INSERT OR IGNORE INTO updates(
                update_id, learner_id, hostname, base_global_version, local_step_start,
                local_step_end, inner_steps, tokens_this_update, tokens_since_global_load,
                num_examples_this_update, train_loss, grad_norm, param_norm, delta_norm,
                file_path, file_size_bytes, sha256, created_at, committed_at, ingested_at, status
            )
            VALUES (
                :update_id, :learner_id, :hostname, :base_global_version, :local_step_start,
                :local_step_end, :inner_steps, :tokens_this_update, :tokens_since_global_load,
                :num_examples_this_update, :train_loss, :grad_norm, :param_norm, :delta_norm,
                :file_path, :file_size_bytes, :sha256, :created_at, :committed_at, :ingested_at,
                :status
            )
            """,
            params,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_updates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM updates WHERE status = ? ORDER BY committed_at ASC",
            (UPDATE_STATUS_PENDING,),
        ).fetchall()
        return [dict(row) for row in rows]

    def eligible_updates(self, current_version: int, max_staleness_versions: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM updates
            WHERE status = ?
              AND (? - base_global_version) <= ?
            ORDER BY committed_at ASC
            """,
            (UPDATE_STATUS_PENDING, current_version, max_staleness_versions),
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
                (UPDATE_STATUS_SELECTED, time.time(), selected_by_run, update_id, UPDATE_STATUS_PENDING)
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
            [(UPDATE_STATUS_PENDING, update_id, UPDATE_STATUS_SELECTED) for update_id in update_ids],
        )
        self.conn.commit()

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
                "stale",
                UPDATE_STATUS_PENDING,
                current_version,
                max_staleness_versions,
            ),
        )
        self.conn.commit()
        return cur.rowcount

    def drop_superseded_updates(self, selected_updates: list[dict[str, Any]], reason: str = "superseded") -> int:
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

    def backup_to(self, dest_path: str | Path, *, global_version: int) -> Path:
        dest_path = Path(dest_path)
        ensure_dir(dest_path.parent)
        with sqlite3.connect(dest_path) as dest:
            self.conn.backup(dest)
        size = file_size(dest_path)
        self.conn.execute(
            """
            INSERT INTO db_dumps(created_at, global_version, path, size_bytes)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), global_version, str(dest_path), size),
        )
        self.conn.commit()
        return dest_path

    def restore_from_dump(self, dump_path: str | Path) -> None:
        self.close()
        ensure_dir(self.path.parent)
        shutil.copy2(dump_path, self.path)
        self.conn = connect(self.path)

    def get_update(self, update_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM updates WHERE update_id = ?", (update_id,)).fetchone()
        return row_to_dict(row)

    def get_global_version(self, version: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM global_versions WHERE version = ?", (version,)).fetchone()
        return row_to_dict(row)

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

    def insert_fragment_update_metadata(self, metadata: dict[str, Any], *, ingested_at: float | None = None) -> bool:
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
                train_loss, grad_norm, param_norm, fragment_norm, file_path,
                file_size_bytes, sha256, created_at, committed_at, ingested_at, status
            )
            VALUES (
                :update_id, :learner_id, :hostname, :fragment_id, :base_fragment_version,
                :base_global_merge_event, :local_step_start, :local_step_end, :inner_steps,
                :tokens_this_update, :tokens_since_fragment_load, :num_examples_this_update,
                :train_loss, :grad_norm, :param_norm, :fragment_norm, :file_path,
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
              AND (? - base_fragment_version) <= ?
            ORDER BY committed_at ASC
            """,
            (
                UPDATE_STATUS_PENDING,
                int(fragment_id),
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
                (UPDATE_STATUS_SELECTED, time.time(), selected_by_run, update_id, UPDATE_STATUS_PENDING)
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
            [(UPDATE_STATUS_PENDING, update_id, UPDATE_STATUS_SELECTED) for update_id in update_ids],
        )
        self.conn.commit()

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
                "stale",
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
        row = self.conn.execute("SELECT * FROM fragment_updates WHERE update_id = ?", (update_id,)).fetchone()
        return row_to_dict(row)

    def list_fragment_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fragment_versions ORDER BY fragment_id, version",
        ).fetchall()
        return [dict(row) for row in rows]
