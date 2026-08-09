"""Strict query-only inspection for completed v1-v3 and Fragment V0 runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from ..storage.atomic_io import atomic_write_json


LEGACY_SEMANTIC_VERSION = "legacy-v1-v3-query-only"
LEGACY_TOTAL_SEEN_TOKENS_SEMANTICS = (
    "classic committed selected-proposal tokens; no v4 token-ledger conversion"
)
FRAGMENT_V0_TABLES = (
    "fragment_proposal_frontiers",
    "fragments",
    "fragment_versions",
    "fragment_updates",
)


def open_query_only_database(database_path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite file without create, migration, or write authority."""

    path = Path(database_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"legacy authority is not a regular file: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("SQLite query_only could not be enabled")
    except BaseException:
        connection.close()
        raise
    return connection


class LegacyRunReader:
    """Read-only view of a historical run; it never bootstraps or repairs state."""

    def __init__(
        self,
        run_root: str | Path,
        *,
        database_path: str | Path | None = None,
    ) -> None:
        self.run_root = Path(run_root).expanduser().resolve(strict=True)
        candidate = (
            Path(database_path)
            if database_path is not None
            else self.run_root / "control" / "syncer_metadata.sqlite3"
        )
        self.database_path = candidate.expanduser().resolve(strict=True)
        if not self.database_path.is_relative_to(self.run_root):
            raise ValueError("legacy database must be inside the legacy run root")
        self.connection = open_query_only_database(self.database_path)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def table_names(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def summary(self) -> dict[str, Any]:
        tables = self.table_names()
        table_set = set(tables)
        latest_version: int | None = None
        legacy_total_seen_tokens: int | None = None
        if "global_versions" in table_set:
            row = self.connection.execute(
                """
                SELECT version, total_seen_tokens
                FROM global_versions
                WHERE status='committed'
                ORDER BY version DESC LIMIT 1
                """
            ).fetchone()
            if row is not None:
                latest_version = int(row["version"])
                legacy_total_seen_tokens = int(row["total_seen_tokens"])
        fragment_tables = [table for table in FRAGMENT_V0_TABLES if table in table_set]
        fragment_versions: list[dict[str, Any]] = []
        if "fragment_versions" in table_set:
            fragment_versions = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT fragment_id, version, global_merge_event, status, num_updates,
                           total_update_tokens, total_seen_tokens
                    FROM fragment_versions
                    ORDER BY fragment_id, version
                    """
                )
            ]
        return {
            "semantic_version": LEGACY_SEMANTIC_VERSION,
            "run_root": str(self.run_root),
            "database_path": str(self.database_path),
            "tables": list(tables),
            "fragment_v0_tables": fragment_tables,
            "latest_global_version": latest_version,
            "legacy_total_seen_tokens": legacy_total_seen_tokens,
            "legacy_total_seen_tokens_semantics": LEGACY_TOTAL_SEEN_TOKENS_SEMANTICS,
            "fragment_versions": fragment_versions,
        }


def export_legacy_summary(
    run_root: str | Path,
    output_path: str | Path,
    *,
    database_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export derived metadata outside the immutable historical run root."""

    root = Path(run_root).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser().resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError("legacy exports must be written outside the legacy run root")
    with LegacyRunReader(root, database_path=database_path) as reader:
        payload = reader.summary()
    json.dumps(payload, allow_nan=False)
    atomic_write_json(output, payload)
    return payload
