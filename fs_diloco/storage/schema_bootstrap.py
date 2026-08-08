"""Explicit schema bootstrap and non-mutating SQLite open helpers.

HA actors never create or migrate the database implicitly.  The operator-facing
initializer is the only caller of :func:`initialize_new_run`; every later open
validates the immutable bootstrap marker and database identity first.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.constants import DYNAMIC_SCHEMA_VERSION, HA_SCHEMA_VERSION, PROTOCOL_VERSION
from .atomic_io import atomic_write_json, read_json
from .sqlite_store import _schema_text


BOOTSTRAP_MARKER_NAME = "bootstrap_complete.json"


HA_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    protocol_version INTEGER NOT NULL,
    mode TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE syncer_leader (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    renewed_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    heartbeat_seq INTEGER NOT NULL
);

CREATE TABLE syncer_epochs (
    epoch INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    last_renewed_at REAL NOT NULL,
    final_state TEXT,
    final_at REAL,
    superseded_by_epoch INTEGER,
    source_fingerprint TEXT NOT NULL,
    config_sha256 TEXT NOT NULL
);

CREATE TABLE controller_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL,
    generation INTEGER NOT NULL,
    reason TEXT,
    requested_at REAL,
    max_terminal_version INTEGER,
    updated_by_epoch INTEGER NOT NULL,
    updated_by_owner_id TEXT NOT NULL
);

CREATE TABLE terminal_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    stop_reason TEXT NOT NULL,
    final_version INTEGER NOT NULL,
    total_seen_tokens INTEGER NOT NULL,
    finalized_by_epoch INTEGER NOT NULL,
    finalized_by_owner_id TEXT NOT NULL,
    finalized_at REAL NOT NULL
);

CREATE TABLE control_publications (
    kind TEXT NOT NULL,
    logical_generation INTEGER NOT NULL,
    published_by_epoch INTEGER NOT NULL,
    published_by_owner_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(kind, logical_generation, published_by_epoch)
);

CREATE TABLE gc_candidates (
    relative_path TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL,
    owning_epoch INTEGER NOT NULL,
    publication_id TEXT NOT NULL,
    state TEXT NOT NULL,
    not_before REAL NOT NULL,
    recorded_by_epoch INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    deleted_at REAL
);

ALTER TABLE global_versions ADD COLUMN commit_epoch INTEGER;
ALTER TABLE global_versions ADD COLUMN commit_owner_id TEXT;
ALTER TABLE global_versions ADD COLUMN publication_id TEXT;
ALTER TABLE global_versions ADD COLUMN weight_size_bytes INTEGER;
ALTER TABLE global_versions ADD COLUMN optim_size_bytes INTEGER;
ALTER TABLE global_versions ADD COLUMN weight_sha256 TEXT;
ALTER TABLE global_versions ADD COLUMN optim_sha256 TEXT;

ALTER TABLE updates ADD COLUMN selected_by_epoch INTEGER;
ALTER TABLE updates ADD COLUMN applied_by_epoch INTEGER;
ALTER TABLE updates ADD COLUMN dropped_by_epoch INTEGER;

CREATE UNIQUE INDEX idx_global_versions_publication_id
    ON global_versions(publication_id)
    WHERE publication_id IS NOT NULL;
CREATE INDEX idx_syncer_epochs_owner ON syncer_epochs(owner_id);
CREATE INDEX idx_gc_candidates_state ON gc_candidates(state, not_before);

CREATE TRIGGER stamp_global_version_leader
AFTER INSERT ON global_versions
WHEN NEW.commit_epoch IS NULL OR NEW.commit_owner_id IS NULL
BEGIN
    UPDATE global_versions
    SET commit_epoch = current_leader_epoch(),
        commit_owner_id = current_leader_owner()
    WHERE version = NEW.version;
END;

CREATE TRIGGER stamp_update_selection_leader
AFTER UPDATE OF status ON updates
WHEN NEW.status = 'selected'
BEGIN
    UPDATE updates
    SET selected_by_epoch = current_leader_epoch()
    WHERE update_id = NEW.update_id;
END;

CREATE TRIGGER stamp_update_terminal_leader
AFTER UPDATE OF status ON updates
WHEN NEW.status IN ('applied', 'dropped')
BEGIN
    UPDATE updates
    SET applied_by_epoch = CASE
            WHEN NEW.status = 'applied' THEN current_leader_epoch()
            ELSE applied_by_epoch
        END,
        dropped_by_epoch = CASE
            WHEN NEW.status = 'dropped' THEN current_leader_epoch()
            ELSE dropped_by_epoch
        END
    WHERE update_id = NEW.update_id;
END;
"""


DYNAMIC_SCHEMA_SQL = """
CREATE TABLE learner_instances (
    instance_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL,
    placement_epoch INTEGER NOT NULL,
    stream_id INTEGER,
    stream_epoch INTEGER,
    admission_generation INTEGER,
    admission_token_hash TEXT,
    launch_request_id TEXT,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    gpu_identity TEXT,
    status TEXT NOT NULL,
    registered_at REAL NOT NULL,
    admitted_at REAL,
    last_seen REAL,
    last_proposal_at REAL,
    last_cycle_step_time_seconds_mean REAL,
    last_cycle_step_count INTEGER,
    last_contributed_observation_seq INTEGER,
    drained_generation INTEGER,
    final_update_id TEXT,
    stopped_at REAL,
    expired_at REAL,
    status_reason TEXT,
    admitted_by_epoch INTEGER,
    stream_restarted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(placement_id, placement_epoch)
);

CREATE TABLE placements (
    placement_id TEXT PRIMARY KEY,
    current_placement_epoch INTEGER NOT NULL,
    current_instance_id TEXT,
    reusable_stream_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE streams (
    stream_id INTEGER PRIMARY KEY,
    current_stream_epoch INTEGER NOT NULL,
    current_instance_id TEXT,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE registration_requests (
    instance_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    launch_request_id TEXT,
    placement_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    processed_by_epoch INTEGER,
    rejection_reason TEXT,
    result_json TEXT
);

CREATE TABLE launch_requests (
    request_id TEXT PRIMARY KEY,
    observation_key TEXT,
    bootstrap_slot INTEGER,
    role TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    not_before REAL,
    submission_attempts INTEGER NOT NULL,
    pbs_job_id TEXT,
    scheduler_state TEXT,
    scheduler_observed_at REAL,
    first_uncertain_at REAL,
    last_positive_evidence_at REAL,
    uncertainty_deadline REAL,
    evidence_source TEXT,
    manual_reason TEXT,
    admitted_instance_id TEXT,
    expires_at REAL,
    reservation_released_at REAL,
    last_error TEXT,
    authorized_placement_id TEXT,
    authorized_placement_epoch INTEGER,
    UNIQUE(bootstrap_slot)
);

CREATE TABLE capacity_observations (
    observation_key TEXT PRIMARY KEY,
    observation_seq INTEGER UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    global_version INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    eligible_contributors INTEGER NOT NULL,
    selected_contributors INTEGER NOT NULL,
    productive_instances INTEGER NOT NULL,
    reserved_launch_capacity INTEGER NOT NULL,
    low_capacity INTEGER NOT NULL,
    close_generation INTEGER NOT NULL,
    recorded_by_epoch INTEGER NOT NULL
);

ALTER TABLE updates ADD COLUMN learner_instance_id TEXT;
ALTER TABLE updates ADD COLUMN placement_id TEXT;
ALTER TABLE updates ADD COLUMN placement_epoch INTEGER;
ALTER TABLE updates ADD COLUMN stream_id INTEGER;
ALTER TABLE updates ADD COLUMN stream_epoch INTEGER;
ALTER TABLE updates ADD COLUMN admission_generation INTEGER;
ALTER TABLE updates ADD COLUMN admission_token_hash TEXT;
ALTER TABLE updates ADD COLUMN selected_membership_generation INTEGER;

CREATE INDEX idx_learner_instances_status ON learner_instances(status, last_seen);
CREATE INDEX idx_registration_requests_state ON registration_requests(state, expires_at);
CREATE INDEX idx_launch_requests_state ON launch_requests(state, updated_at);
CREATE INDEX idx_capacity_observation_seq ON capacity_observations(observation_seq);
CREATE UNIQUE INDEX idx_current_instance_stream
    ON learner_instances(stream_id)
    WHERE status IN ('admitted', 'draining', 'drained');
CREATE UNIQUE INDEX idx_current_instance_placement
    ON learner_instances(placement_id)
    WHERE status IN ('admitted', 'draining', 'drained');
"""


@dataclass(frozen=True)
class BootstrapIdentity:
    run_id: str
    source_fingerprint: str
    config_sha256: str
    mode: str = "full"

    def as_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "source_fingerprint": self.source_fingerprint,
            "config_sha256": self.config_sha256,
            "mode": self.mode,
        }


def _identity_dict(identity: BootstrapIdentity | Mapping[str, Any]) -> dict[str, str]:
    payload = identity.as_dict() if isinstance(identity, BootstrapIdentity) else dict(identity)
    required = ("run_id", "source_fingerprint", "config_sha256", "mode")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"bootstrap identity is missing: {', '.join(missing)}")
    return {key: str(payload[key]) for key in required}


def schema_version_for_mode(mode: str) -> int:
    if mode == "full":
        return HA_SCHEMA_VERSION
    if mode == "full_dynamic":
        return DYNAMIC_SCHEMA_VERSION
    raise ValueError(f"unsupported HA bootstrap mode: {mode}")


def _statements(script: str) -> Iterator[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise ValueError("incomplete SQLite schema statement")


def _configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    conn.row_factory = sqlite3.Row
    mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
    if mode != "delete":
        raise RuntimeError(f"SQLite journal_mode is {mode!r}, expected 'delete'")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")


def _marker_path(database_path: Path, marker_path: str | Path | None) -> Path:
    return (
        Path(marker_path)
        if marker_path is not None
        else database_path.parent / BOOTSTRAP_MARKER_NAME
    )


def initialize_new_run(
    database_path: str | Path,
    identity: BootstrapIdentity | Mapping[str, Any],
    *,
    marker_path: str | Path | None = None,
    bootstrap_generation: int = 1,
    busy_timeout_ms: int = 60_000,
) -> dict[str, Any]:
    """Atomically create and publish a new HA schema, then publish its marker."""
    database_path = Path(database_path)
    marker = _marker_path(database_path, marker_path)
    expected = _identity_dict(identity)
    schema_version = schema_version_for_mode(expected["mode"])
    if bootstrap_generation < 1:
        raise ValueError("bootstrap_generation must be >= 1")
    if database_path.exists() or marker.exists():
        raise FileExistsError(
            f"refusing to overwrite existing HA bootstrap: {database_path} / {marker}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.", suffix=".bootstrap", dir=database_path.parent
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    conn: sqlite3.Connection | None = None
    created_at = time.time()
    try:
        conn = sqlite3.connect(temporary_path, timeout=busy_timeout_ms / 1000.0)
        _configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
        conn.execute("BEGIN IMMEDIATE")
        for statement in _statements(_schema_text()):
            conn.execute(statement)
        for statement in _statements(HA_SCHEMA_SQL):
            conn.execute(statement)
        if schema_version == DYNAMIC_SCHEMA_VERSION:
            for statement in _statements(DYNAMIC_SCHEMA_SQL):
                conn.execute(statement)
        conn.execute(
            """
            INSERT INTO schema_meta(
                singleton, schema_version, protocol_version, mode, created_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (schema_version, PROTOCOL_VERSION, expected["mode"], created_at),
        )
        for key, value in (
            ("schema_version", schema_version),
            ("bootstrap_identity", expected),
        ):
            conn.execute(
                "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, sort_keys=True), created_at),
            )
        conn.execute(f"PRAGMA user_version={schema_version}")
        conn.commit()
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        conn.close()
        conn = None
        os.replace(temporary_path, database_path)
        directory_fd = os.open(database_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        payload: dict[str, Any] = {
            **expected,
            "schema_version": schema_version,
            "protocol_version": PROTOCOL_VERSION,
            "bootstrap_generation": int(bootstrap_generation),
            "created_at": created_at,
        }
        atomic_write_json(marker, payload)
        return payload
    except Exception:
        if conn is not None:
            conn.close()
        temporary_path.unlink(missing_ok=True)
        raise


def _load_and_validate_marker(
    database_path: Path,
    marker: Path,
    expected_identity: BootstrapIdentity | Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    expected = _identity_dict(expected_identity)
    expected_schema_version = schema_version_for_mode(expected["mode"])
    payload = read_json(marker)
    mismatches = {
        key: (payload.get(key), value)
        for key, value in expected.items()
        if payload.get(key) != value
    }
    for key, value in (
        ("schema_version", expected_schema_version),
        ("protocol_version", PROTOCOL_VERSION),
    ):
        if payload.get(key) != value:
            mismatches[key] = (payload.get(key), value)
    if mismatches:
        raise RuntimeError(f"bootstrap marker identity mismatch: {mismatches}")
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    return payload, expected


def open_existing(
    database_path: str | Path,
    expected_identity: BootstrapIdentity | Mapping[str, Any],
    *,
    marker_path: str | Path | None = None,
    busy_timeout_ms: int = 60_000,
) -> sqlite3.Connection:
    """Open a completed HA database without issuing DDL or ALTER statements."""
    database_path = Path(database_path)
    marker = _marker_path(database_path, marker_path)
    _payload, expected = _load_and_validate_marker(database_path, marker, expected_identity)
    expected_schema_version = schema_version_for_mode(expected["mode"])
    conn = sqlite3.connect(database_path, timeout=busy_timeout_ms / 1000.0)
    try:
        _configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
        schema = conn.execute("SELECT * FROM schema_meta WHERE singleton = 1").fetchone()
        if schema is None:
            raise RuntimeError("HA schema_meta row is missing")
        schema_values = (
            int(schema["schema_version"]),
            int(schema["protocol_version"]),
            str(schema["mode"]),
        )
        if schema_values != (expected_schema_version, PROTOCOL_VERSION, expected["mode"]):
            raise RuntimeError(f"HA schema identity mismatch: {schema_values}")
        state_row = conn.execute(
            "SELECT value FROM run_state WHERE key = 'schema_version'"
        ).fetchone()
        identity_row = conn.execute(
            "SELECT value FROM run_state WHERE key = 'bootstrap_identity'"
        ).fetchone()
        if state_row is None or json.loads(state_row[0]) != expected_schema_version:
            raise RuntimeError("run_state schema_version does not match HA schema")
        if identity_row is None or json.loads(identity_row[0]) != expected:
            raise RuntimeError("run_state bootstrap identity mismatch")
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != expected_schema_version:
            raise RuntimeError("SQLite user_version does not match HA schema")
        return conn
    except Exception:
        conn.close()
        raise


def open_readonly(database_path: str | Path) -> sqlite3.Connection:
    """Open SQLite in enforced read-only/query-only mode without side effects."""
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    conn = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro&immutable=0",
        uri=True,
        timeout=60.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
