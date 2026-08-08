"""Fresh v4 authority bootstrap, typed read model, and explicit fenced commands.

Only this module owns the writable SQLite connection.  Application code gets a
``LeaderSession`` with named commands or an ``AuthorityReadModel``; neither
surface exposes SQL execution or the raw connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core.versions import AUTHORITY_SCHEMA_VERSION, PROTOCOL_VERSION
from ..protocol._validation import identity as validate_identity
from ..protocol.authority import (
    ContributorProgress,
    ProposalDisposition,
    PublicationIntent,
    SelectionBatch,
    SelectionCandidate,
    StaticBinding,
    TerminalState,
)
from ..protocol.contributor import (
    DynamicContributorFence,
    DynamicMembershipScope,
    StaticContributorFence,
    StaticMembershipScope,
)
from ..protocol.cycle_receipt import CycleReceiptV1
from ..protocol.proposal import FullUpdateProposalV2
from .atomic_io import atomic_write_json, read_json
from .leader_lease import (
    LeaderToken,
    LeaseUnavailableError,
    StaleLeaderTokenError,
)


AUTHORITY_APPLICATION_ID = 0x46534434  # "FSD4"
BASE_SCHEMA_NAME = "schema_v4.sql"
DYNAMIC_SCHEMA_NAME = "schema_v4_dynamic.sql"
V4_BOOTSTRAP_MARKER_NAME = "authority_v4_bootstrap_complete.json"


class AuthoritySchemaError(RuntimeError):
    """The on-disk schema or immutable identity is not the requested v4 authority."""


class CommandConflictError(RuntimeError):
    """A command ID was replayed with a different immutable request."""


class MembershipFenceError(RuntimeError):
    """A contributor fence is not current."""


@dataclass(frozen=True)
class AuthorityIdentity:
    run_id: str
    source_fingerprint: str
    config_sha256: str

    def __post_init__(self) -> None:
        if not self.run_id or not self.source_fingerprint:
            raise ValueError("run_id and source_fingerprint must not be empty")
        if len(self.config_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.config_sha256
        ):
            raise ValueError("config_sha256 must be a lowercase SHA-256 digest")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AuthorityMetadata:
    schema_version: int
    protocol_version: int
    mode: str
    features: tuple[str, ...]
    ddl_sha256: str
    schema_fingerprint: str


@dataclass(frozen=True)
class CommittedVersion:
    version: int
    predecessor_version: int | None
    publication_id: str
    weight_relative_path: str
    weight_size: int
    weight_sha256: str
    optim_relative_path: str
    optim_size: int
    optim_sha256: str
    committed_by_epoch: int
    committed_by_owner_id: str
    committed_at: float
    direct_weight_tokens_applied: int


def _schema_dir() -> Path:
    return Path(__file__).resolve().parent


def _schema_text(mode: str) -> tuple[str, ...]:
    base = (_schema_dir() / BASE_SCHEMA_NAME).read_text(encoding="utf-8")
    if mode == "static":
        return (base,)
    if mode == "dynamic":
        dynamic = (_schema_dir() / DYNAMIC_SCHEMA_NAME).read_text(encoding="utf-8")
        return (base, dynamic)
    raise ValueError("authority mode must be 'static' or 'dynamic'")


def canonical_features(mode: str) -> tuple[str, ...]:
    if mode == "static":
        return ()
    if mode == "dynamic":
        return ("dynamic_membership",)
    raise ValueError("authority mode must be 'static' or 'dynamic'")


def _features_json(features: tuple[str, ...]) -> str:
    return json.dumps(list(features), sort_keys=True, separators=(",", ":"))


def ddl_bundle_sha256(mode: str) -> str:
    digest = hashlib.sha256()
    for name, schema in zip(
        (BASE_SCHEMA_NAME, DYNAMIC_SCHEMA_NAME), _schema_text(mode), strict=False
    ):
        encoded = schema.encode("utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b"\0")
        digest.update(encoded)
    return digest.hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '') AS sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    payload = [tuple(row) for row in rows]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _marker_path(database_path: Path, marker_path: str | Path | None) -> Path:
    return (
        Path(marker_path)
        if marker_path is not None
        else database_path.with_name(V4_BOOTSTRAP_MARKER_NAME)
    )


def _configure_connection(connection: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")


def initialize_authority_v4(
    database_path: str | Path,
    identity: AuthorityIdentity,
    membership_scope: StaticMembershipScope | DynamicMembershipScope,
    *,
    marker_path: str | Path | None = None,
    busy_timeout_ms: int = 60_000,
    wall_clock: Callable[[], float] = time.time,
) -> AuthorityMetadata:
    """Create a fresh complete v4 DB without executing any legacy DDL."""

    path = Path(database_path)
    marker = _marker_path(path, marker_path)
    if path.exists() or marker.exists():
        raise FileExistsError(f"v4 authority target already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "static" if isinstance(membership_scope, StaticMembershipScope) else "dynamic"
    schemas = _schema_text(mode)
    ddl_sha = ddl_bundle_sha256(mode)
    features = canonical_features(mode)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staging", dir=path.parent
    )
    os.close(temporary_fd)
    temporary_path = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            _configure_connection(connection, busy_timeout_ms=busy_timeout_ms)
            connection.executescript("BEGIN IMMEDIATE;\n" + "\n".join(schemas) + "\nCOMMIT;\n")
            connection.execute(f"PRAGMA application_id={AUTHORITY_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={AUTHORITY_SCHEMA_VERSION}")
            fingerprint = _schema_fingerprint(connection)
            now = float(wall_clock())
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO schema_meta(
                    singleton, schema_version, protocol_version, mode, features_json,
                    ddl_sha256, schema_fingerprint, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    AUTHORITY_SCHEMA_VERSION,
                    PROTOCOL_VERSION,
                    mode,
                    _features_json(features),
                    ddl_sha,
                    fingerprint,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_identity(
                    singleton, run_id, source_fingerprint, config_sha256
                ) VALUES (1, ?, ?, ?)
                """,
                (identity.run_id, identity.source_fingerprint, identity.config_sha256),
            )
            connection.execute(
                "INSERT INTO controller_state(singleton, state, generation) VALUES (1, 'open', 0)"
            )
            connection.commit()
        finally:
            connection.close()
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        marker_payload = {
            "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "mode": mode,
            "features": list(features),
            "ddl_sha256": ddl_sha,
            "schema_fingerprint": fingerprint,
            "identity": identity.as_dict(),
            "database_name": path.name,
            "created_at": now,
        }
        atomic_write_json(marker, marker_payload)
    finally:
        temporary_path.unlink(missing_ok=True)
    return AuthorityMetadata(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        mode=mode,
        features=features,
        ddl_sha256=ddl_sha,
        schema_fingerprint=fingerprint,
    )


def _validate_open(
    connection: sqlite3.Connection,
    *,
    path: Path,
    marker: Path,
    identity: AuthorityIdentity,
    mode: str,
    busy_timeout_ms: int,
) -> AuthorityMetadata:
    marker_payload = read_json(marker)
    if not isinstance(marker_payload, Mapping):
        raise AuthoritySchemaError(f"missing or malformed v4 bootstrap marker: {marker}")
    expected_features = canonical_features(mode)
    expected_ddl = ddl_bundle_sha256(mode)
    row = connection.execute("SELECT * FROM schema_meta WHERE singleton = 1").fetchone()
    identity_row = connection.execute("SELECT * FROM run_identity WHERE singleton = 1").fetchone()
    if row is None or identity_row is None:
        raise AuthoritySchemaError("v4 authority metadata is incomplete")
    try:
        stored_features = tuple(json.loads(str(row["features_json"])))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthoritySchemaError("features_json is malformed") from exc
    actual_fingerprint = _schema_fingerprint(connection)
    expected = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "mode": mode,
        "features_json": _features_json(expected_features),
        "ddl_sha256": expected_ddl,
        "schema_fingerprint": actual_fingerprint,
    }
    for key, value in expected.items():
        if row[key] != value:
            raise AuthoritySchemaError(
                f"authority metadata mismatch for {key}: expected {value!r}, got {row[key]!r}"
            )
    if stored_features != expected_features:
        raise AuthoritySchemaError("authority features are not canonical")
    for key, value in identity.as_dict().items():
        if identity_row[key] != value:
            raise AuthoritySchemaError(f"authority identity mismatch for {key}")
    marker_expectations: dict[str, Any] = {
        "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "mode": mode,
        "features": list(expected_features),
        "ddl_sha256": expected_ddl,
        "schema_fingerprint": actual_fingerprint,
        "identity": identity.as_dict(),
        "database_name": path.name,
    }
    for key, value in marker_expectations.items():
        if marker_payload.get(key) != value:
            raise AuthoritySchemaError(f"bootstrap marker mismatch for {key}")
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != AUTHORITY_APPLICATION_ID:
        raise AuthoritySchemaError("authority application_id mismatch")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != AUTHORITY_SCHEMA_VERSION:
        raise AuthoritySchemaError("authority user_version mismatch")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise AuthoritySchemaError("foreign key enforcement is disabled")
    if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
        raise AuthoritySchemaError("journal_mode must be DELETE")
    if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
        raise AuthoritySchemaError("synchronous must be FULL")
    if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) != int(busy_timeout_ms):
        raise AuthoritySchemaError("busy_timeout does not match the configured value")
    return AuthorityMetadata(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        mode=mode,
        features=expected_features,
        ddl_sha256=expected_ddl,
        schema_fingerprint=actual_fingerprint,
    )


class AuthorityReadModel:
    """Typed, query-only view over the v4 authority."""

    def __init__(self, authority: "LeaderAuthority") -> None:
        self._authority = authority

    def metadata(self) -> AuthorityMetadata:
        return self._authority.metadata

    def latest_committed_version(self) -> CommittedVersion | None:
        row = self._authority._fetchone(
            "SELECT * FROM global_versions ORDER BY version DESC LIMIT 1"
        )
        return None if row is None else _decode_committed_version(row)

    def committed_version(self, version: int) -> CommittedVersion | None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("version must be a non-negative integer")
        row = self._authority._fetchone(
            "SELECT * FROM global_versions WHERE version = ?", (version,)
        )
        return None if row is None else _decode_committed_version(row)

    def static_binding(self, learner_id: str) -> StaticBinding | None:
        row = self._authority._fetchone(
            "SELECT * FROM static_contributor_bindings WHERE learner_id = ?", (learner_id,)
        )
        return None if row is None else _decode_static_binding(row)

    def contributor_progress(self, stable_contributor_key: str) -> ContributorProgress | None:
        row = self._authority._fetchone(
            "SELECT * FROM contributor_progress WHERE stable_contributor_key = ?",
            (stable_contributor_key,),
        )
        return None if row is None else _decode_progress(row)

    def integrity_check(self) -> tuple[str, ...]:
        return tuple(str(row[0]) for row in self._authority._fetchall("PRAGMA integrity_check"))

    def table_names(self) -> tuple[str, ...]:
        rows = self._authority._fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return tuple(str(row[0]) for row in rows)


class LeaderAuthority:
    """Owner of a validated writable v4 connection and lease lifecycle."""

    def __init__(
        self,
        database_path: str | Path,
        identity: AuthorityIdentity,
        membership_scope: StaticMembershipScope | DynamicMembershipScope,
        *,
        marker_path: str | Path | None = None,
        lease_duration_seconds: float = 90.0,
        max_clock_skew_seconds: float = 2.0,
        busy_timeout_ms: int = 60_000,
        wall_clock: Callable[[], float] = time.time,
        lease_safety_check: Callable[[LeaderToken], None] | None = None,
    ) -> None:
        if lease_duration_seconds <= max_clock_skew_seconds:
            raise ValueError("lease duration must exceed maximum clock skew")
        path = Path(database_path)
        mode = "static" if isinstance(membership_scope, StaticMembershipScope) else "dynamic"
        connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0)
        _configure_connection(connection, busy_timeout_ms=busy_timeout_ms)
        try:
            metadata = _validate_open(
                connection,
                path=path,
                marker=_marker_path(path, marker_path),
                identity=identity,
                mode=mode,
                busy_timeout_ms=busy_timeout_ms,
            )
        except Exception:
            connection.close()
            raise
        self._path = path
        self._identity = identity
        self._scope = membership_scope
        self._connection = connection
        self._wall_clock = wall_clock
        self._lease_duration_seconds = float(lease_duration_seconds)
        self._max_clock_skew_seconds = float(max_clock_skew_seconds)
        self._lease_safety_check = lease_safety_check
        self.metadata = metadata
        self.read = AuthorityReadModel(self)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "LeaderAuthority":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def acquire_leader(
        self,
        *,
        owner_id: str,
        hostname: str | None = None,
        pid: int | None = None,
        pbs_job_id: str | None = None,
    ) -> LeaderToken:
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        now = float(self._wall_clock())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._connection.execute(
                "SELECT * FROM syncer_leader WHERE singleton = 1"
            ).fetchone()
            if current is None:
                epoch = 1
            else:
                active = str(current["state"]) == "active"
                expired = now > (float(current["lease_expires_at"]) + self._max_clock_skew_seconds)
                if active and not expired:
                    raise LeaseUnavailableError(
                        f"leader lease is held by epoch={current['epoch']} owner={current['owner_id']}"
                    )
                epoch = int(current["epoch"]) + 1
                final_state = "expired" if active else "released"
                self._connection.execute(
                    """
                    UPDATE syncer_epochs
                    SET final_state=COALESCE(final_state, ?), final_at=COALESCE(final_at, ?),
                        superseded_by_epoch=?
                    WHERE epoch=?
                    """,
                    (final_state, now, epoch, int(current["epoch"])),
                )
            host = hostname or socket.gethostname()
            process_id = os.getpid() if pid is None else int(pid)
            expires = now + self._lease_duration_seconds
            self._connection.execute(
                """
                INSERT INTO syncer_leader(
                    singleton, epoch, owner_id, state, pbs_job_id, hostname, pid,
                    acquired_at, renewed_at, lease_expires_at, heartbeat_seq
                ) VALUES (1, ?, ?, 'active', ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(singleton) DO UPDATE SET
                    epoch=excluded.epoch, owner_id=excluded.owner_id, state='active',
                    pbs_job_id=excluded.pbs_job_id, hostname=excluded.hostname, pid=excluded.pid,
                    acquired_at=excluded.acquired_at, renewed_at=excluded.renewed_at,
                    lease_expires_at=excluded.lease_expires_at, heartbeat_seq=1
                """,
                (epoch, owner_id, pbs_job_id, host, process_id, now, now, expires),
            )
            self._connection.execute(
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
                    host,
                    process_id,
                    now,
                    now,
                    self._identity.source_fingerprint,
                    self._identity.config_sha256,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return LeaderToken(self._identity.run_id, epoch, owner_id)

    def renew_leader(self, token: LeaderToken) -> None:
        now = float(self._wall_clock())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._verify_token(token, require_safe_expiry=False)
            cursor = self._connection.execute(
                """
                UPDATE syncer_leader
                SET renewed_at=?, lease_expires_at=?, heartbeat_seq=heartbeat_seq+1
                WHERE singleton=1 AND epoch=? AND owner_id=? AND state='active'
                """,
                (now, now + self._lease_duration_seconds, token.epoch, token.owner_id),
            )
            if cursor.rowcount != 1:
                raise StaleLeaderTokenError("leader changed during renewal")
            self._connection.execute(
                "UPDATE syncer_epochs SET last_renewed_at=? WHERE epoch=? AND owner_id=?",
                (now, token.epoch, token.owner_id),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def release_leader(self, token: LeaderToken) -> None:
        now = float(self._wall_clock())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._verify_token(token)
            self._connection.execute(
                """
                UPDATE syncer_leader SET state='released', renewed_at=?, lease_expires_at=?
                WHERE singleton=1 AND epoch=? AND owner_id=? AND state='active'
                """,
                (now, now, token.epoch, token.owner_id),
            )
            self._connection.execute(
                """
                UPDATE syncer_epochs SET final_state='released', final_at=?, last_renewed_at=?
                WHERE epoch=? AND owner_id=?
                """,
                (now, now, token.epoch, token.owner_id),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def open_leader(self, token: LeaderToken) -> "LeaderSession":
        self._verify_token(token)
        return LeaderSession(self, token)

    def _verify_token(self, token: LeaderToken, *, require_safe_expiry: bool = True) -> sqlite3.Row:
        if token.run_id != self._identity.run_id:
            raise StaleLeaderTokenError("leader token belongs to another run")
        if self._lease_safety_check is not None:
            self._lease_safety_check(token)
        row = self._connection.execute(
            """
            SELECT * FROM syncer_leader
            WHERE singleton=1 AND epoch=? AND owner_id=? AND state='active'
            """,
            (token.epoch, token.owner_id),
        ).fetchone()
        if row is None:
            raise StaleLeaderTokenError("leader token has been superseded or released")
        expiry = float(row["lease_expires_at"])
        boundary = expiry - self._max_clock_skew_seconds if require_safe_expiry else expiry
        if float(self._wall_clock()) > boundary:
            raise StaleLeaderTokenError("leader token crossed the lease safety boundary")
        return row

    def _fetchone(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._connection.execute(sql, parameters).fetchone()

    def _fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._connection.execute(sql, parameters).fetchall())


class LeaderSession:
    """Named fenced commands bound to one immutable leader token."""

    def __init__(self, authority: LeaderAuthority, token: LeaderToken) -> None:
        self._authority = authority
        self.token = token

    def bind_or_replace_static_attempt(
        self,
        *,
        command_id: str,
        learner_id: str,
        logical_launch_id: str,
        attempt_id: str,
        expected_generation: int | None = None,
        allow_logical_replacement: bool = False,
        replacement_reason: str | None = None,
    ) -> StaticBinding:
        validate_identity(learner_id, name="learner_id")
        validate_identity(logical_launch_id, name="logical_launch_id")
        validate_identity(attempt_id, name="attempt_id")
        if expected_generation is not None and (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation must be a non-negative integer")
        if not isinstance(allow_logical_replacement, bool):
            raise ValueError("allow_logical_replacement must be a boolean")
        if replacement_reason is not None and not replacement_reason:
            raise ValueError("replacement_reason must not be empty")
        request = {
            "learner_id": learner_id,
            "logical_launch_id": logical_launch_id,
            "attempt_id": attempt_id,
            "expected_generation": expected_generation,
            "allow_logical_replacement": allow_logical_replacement,
            "replacement_reason": replacement_reason,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, StaticMembershipScope):
                raise RuntimeError("static binding command requires static authority mode")
            if learner_id not in self._authority._scope.learner_ids:
                raise MembershipFenceError(f"unknown static learner: {learner_id}")
            row = connection.execute(
                "SELECT * FROM static_contributor_bindings WHERE learner_id=?", (learner_id,)
            ).fetchone()
            now = float(self._authority._wall_clock())
            if row is None:
                if expected_generation not in (None, 0):
                    raise MembershipFenceError("static binding generation changed")
                generation = 1
                connection.execute(
                    """
                    INSERT INTO static_contributor_bindings(
                        learner_id, logical_launch_id, attempt_id, binding_generation,
                        status, bound_by_epoch, bound_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        learner_id,
                        logical_launch_id,
                        attempt_id,
                        generation,
                        self.token.epoch,
                        now,
                    ),
                )
            else:
                current_generation = int(row["binding_generation"])
                if expected_generation is not None and expected_generation != current_generation:
                    raise MembershipFenceError("static binding generation changed")
                if (
                    row["logical_launch_id"] == logical_launch_id
                    and row["attempt_id"] == attempt_id
                    and row["status"] == "active"
                ):
                    return dict(row)
                history_status = str(row["status"])
                if row["status"] == "active":
                    if expected_generation is None or replacement_reason is None:
                        raise MembershipFenceError(
                            "active static replacement requires expected_generation and reason"
                        )
                    old_fence = StaticContributorFence(
                        kind="static",
                        learner_id=str(row["learner_id"]),
                        logical_launch_id=str(row["logical_launch_id"]),
                        attempt_id=str(row["attempt_id"]),
                        binding_generation=current_generation,
                    )
                    self._terminalize_fenced_updates(
                        connection,
                        fence_json=_canonical_json(old_fence.as_dict()),
                        reason=replacement_reason,
                    )
                    history_status = "replaced"
                if row["logical_launch_id"] != logical_launch_id and not allow_logical_replacement:
                    raise MembershipFenceError(
                        "a new logical launch requires explicit replacement authorization"
                    )
                generation = current_generation + 1
                connection.execute(
                    """
                    INSERT INTO static_binding_history(
                        learner_id, binding_generation, logical_launch_id, attempt_id,
                        final_status, bound_by_epoch, bound_at, finalized_by_epoch, finalized_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        learner_id,
                        current_generation,
                        row["logical_launch_id"],
                        row["attempt_id"],
                        history_status,
                        row["bound_by_epoch"],
                        row["bound_at"],
                        self.token.epoch,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE static_contributor_bindings
                    SET logical_launch_id=?, attempt_id=?, binding_generation=?, status='active',
                        bound_by_epoch=?, bound_at=?, terminal_at=NULL
                    WHERE learner_id=?
                    """,
                    (
                        logical_launch_id,
                        attempt_id,
                        generation,
                        self.token.epoch,
                        now,
                        learner_id,
                    ),
                )
            result = connection.execute(
                "SELECT * FROM static_contributor_bindings WHERE learner_id=?", (learner_id,)
            ).fetchone()
            assert result is not None
            return dict(result)

        result = self._command(command_id, "bind_or_replace_static_attempt", request, operation)
        return _decode_static_binding(result)

    def mark_static_attempt_terminal(
        self,
        *,
        command_id: str,
        fence: StaticContributorFence,
        reason: str = "static_attempt_terminal",
    ) -> StaticBinding:
        if not reason:
            raise ValueError("terminal reason must not be empty")
        request = {"fence": fence.as_dict(), "reason": reason}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._require_current_fence(connection, fence)
            now = float(self._authority._wall_clock())
            self._terminalize_fenced_updates(
                connection,
                fence_json=_canonical_json(fence.as_dict()),
                reason=reason,
            )
            connection.execute(
                """
                UPDATE static_contributor_bindings SET status='terminal', terminal_at=?
                WHERE learner_id=?
                """,
                (now, fence.learner_id),
            )
            row = connection.execute(
                "SELECT * FROM static_contributor_bindings WHERE learner_id=?",
                (fence.learner_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

        result = self._command(command_id, "mark_static_attempt_terminal", request, operation)
        return _decode_static_binding(result)

    def ingest_cycle_receipt(
        self, *, command_id: str, receipt: CycleReceiptV1
    ) -> ContributorProgress:
        request = receipt.as_dict()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if receipt.run_id != self._authority._identity.run_id:
                raise MembershipFenceError("receipt belongs to another run")
            self._require_current_fence(connection, receipt.contributor_fence)
            progress = connection.execute(
                "SELECT * FROM contributor_progress WHERE stable_contributor_key=?",
                (receipt.stable_contributor_key,),
            ).fetchone()
            expected_sequence = 1 if progress is None else int(progress["last_cycle_seq"]) + 1
            if receipt.cycle_seq != expected_sequence:
                raise ValueError(
                    f"receipt sequence must be contiguous: expected {expected_sequence}, "
                    f"got {receipt.cycle_seq}"
                )
            if progress is not None and (
                receipt.previous_receipt_id != progress["last_receipt_id"]
                or receipt.previous_receipt_sha256 != progress["last_receipt_sha256"]
                or receipt.data_cursor_start != int(progress["data_cursor"])
            ):
                raise ValueError("receipt hash chain or data cursor does not match progress")
            digest = receipt.immutable_sha256()
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO cycle_receipts(
                    receipt_id, receipt_sha256, run_id, stable_contributor_key, cycle_seq,
                    cycle_id, previous_receipt_id, previous_receipt_sha256,
                    processed_tokens_this_cycle, effective_tokens_this_cycle,
                    local_discarded_tokens_this_cycle, retained_tokens_since_base,
                    data_cursor_start, data_cursor_end, proposal_expected, planned_update_id,
                    planned_payload_sha256, fence_kind, fence_json, created_at, ingested_at,
                    ingested_by_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    digest,
                    receipt.run_id,
                    receipt.stable_contributor_key,
                    receipt.cycle_seq,
                    receipt.cycle_id,
                    receipt.previous_receipt_id,
                    receipt.previous_receipt_sha256,
                    receipt.processed_tokens_this_cycle,
                    receipt.effective_tokens_this_cycle,
                    receipt.local_discarded_tokens_this_cycle,
                    receipt.retained_tokens_since_base,
                    receipt.data_cursor_start,
                    receipt.data_cursor_end,
                    int(receipt.proposal_expected),
                    receipt.planned_update_id,
                    receipt.planned_payload_sha256,
                    receipt.contributor_fence.kind,
                    _canonical_json(receipt.contributor_fence.as_dict()),
                    receipt.created_at,
                    now,
                    self.token.epoch,
                ),
            )
            connection.execute(
                """
                INSERT INTO contributor_progress(
                    stable_contributor_key, last_cycle_seq, last_receipt_id,
                    last_receipt_sha256, data_cursor, updated_by_epoch, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_contributor_key) DO UPDATE SET
                    last_cycle_seq=excluded.last_cycle_seq,
                    last_receipt_id=excluded.last_receipt_id,
                    last_receipt_sha256=excluded.last_receipt_sha256,
                    data_cursor=excluded.data_cursor,
                    updated_by_epoch=excluded.updated_by_epoch,
                    updated_at=excluded.updated_at
                """,
                (
                    receipt.stable_contributor_key,
                    receipt.cycle_seq,
                    receipt.receipt_id,
                    digest,
                    receipt.data_cursor_end,
                    self.token.epoch,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO token_fates(
                    receipt_id, stable_contributor_key, local_discarded_tokens,
                    direct_weight_tokens, direct_fate, updated_by_epoch, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.stable_contributor_key,
                    receipt.local_discarded_tokens_this_cycle,
                    receipt.effective_tokens_this_cycle,
                    "outstanding" if receipt.proposal_expected else "unpublished",
                    self.token.epoch,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM contributor_progress WHERE stable_contributor_key=?",
                (receipt.stable_contributor_key,),
            ).fetchone()
            assert row is not None
            return dict(row)

        result = self._command(command_id, "ingest_cycle_receipt", request, operation)
        return _decode_progress(result)

    def ingest_proposal(
        self, *, command_id: str, proposal: FullUpdateProposalV2
    ) -> ProposalDisposition:
        request = proposal.as_dict()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if proposal.run_id != self._authority._identity.run_id:
                raise MembershipFenceError("proposal belongs to another run")
            self._require_current_fence(connection, proposal.contributor_fence)
            receipt = connection.execute(
                "SELECT * FROM cycle_receipts WHERE receipt_id=?",
                (proposal.cycle_receipt_id,),
            ).fetchone()
            if receipt is None or receipt["receipt_sha256"] != proposal.cycle_receipt_sha256:
                raise ValueError("proposal receipt reference is missing or mismatched")
            if (
                receipt["planned_update_id"] != proposal.update_id
                or receipt["planned_payload_sha256"] != proposal.payload_sha256
                or int(receipt["cycle_seq"]) != proposal.cycle_seq
            ):
                raise ValueError("proposal does not match its cycle receipt")
            receipt_fields = {
                "run_id": proposal.run_id,
                "stable_contributor_key": proposal.stable_contributor_key,
                "cycle_id": proposal.cycle_id,
                "processed_tokens_this_cycle": proposal.processed_tokens_this_cycle,
                "effective_tokens_this_cycle": proposal.effective_tokens_this_update,
                "local_discarded_tokens_this_cycle": (proposal.local_discarded_tokens_this_cycle),
                "retained_tokens_since_base": proposal.retained_tokens_since_base,
                "data_cursor_start": proposal.data_cursor_start,
                "data_cursor_end": proposal.data_cursor_end,
                "fence_kind": proposal.contributor_fence.kind,
                "fence_json": _canonical_json(proposal.contributor_fence.as_dict()),
                "proposal_expected": 1,
            }
            if any(receipt[name] != value for name, value in receipt_fields.items()):
                raise ValueError("proposal immutable fields do not match its cycle receipt")
            existing = connection.execute(
                "SELECT * FROM updates WHERE update_id=?", (proposal.update_id,)
            ).fetchone()
            proposal_digest = proposal.immutable_sha256()
            if existing is not None:
                disposition = (
                    ProposalDisposition.EXACT_REPLAY
                    if existing["proposal_sha256"] == proposal_digest
                    else ProposalDisposition.IDENTITY_COLLISION
                )
                self._record_observation(connection, proposal, disposition)
                return {"disposition": disposition.value}
            conflict = connection.execute(
                """
                SELECT update_id FROM updates
                WHERE run_id=? AND stable_contributor_key=? AND cycle_seq=?
                """,
                (proposal.run_id, proposal.stable_contributor_key, proposal.cycle_seq),
            ).fetchone()
            if conflict is not None:
                observation_id = self._record_observation(
                    connection, proposal, ProposalDisposition.CONFLICT
                )
                connection.execute(
                    """
                    INSERT INTO proposal_conflicts(
                        observation_id, conflict_kind, existing_update_id, incoming_update_id,
                        bounded_diagnostic, fingerprint
                    ) VALUES (?, 'logical_key', ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        conflict["update_id"],
                        proposal.update_id,
                        "logical proposal key already has a different update ID",
                        proposal_digest,
                    ),
                )
                return {"disposition": ProposalDisposition.CONFLICT.value}
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO updates(
                    update_id, run_id, stable_contributor_key, cycle_seq, cycle_id,
                    cycle_receipt_id, cycle_receipt_sha256, proposal_sha256, base_global_version,
                    local_step_start, local_step_end, inner_steps,
                    processed_tokens_this_cycle, effective_tokens_this_update,
                    local_discarded_tokens_this_cycle, retained_tokens_since_base,
                    data_cursor_start, data_cursor_end, fence_kind, fence_json,
                    payload_relative_path, payload_size, payload_sha256,
                    tensor_schema_sha256, tensor_dtype, tensor_numel, created_at,
                    ingested_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    proposal.update_id,
                    proposal.run_id,
                    proposal.stable_contributor_key,
                    proposal.cycle_seq,
                    proposal.cycle_id,
                    proposal.cycle_receipt_id,
                    proposal.cycle_receipt_sha256,
                    proposal_digest,
                    proposal.base_global_version,
                    proposal.local_step_start,
                    proposal.local_step_end,
                    proposal.inner_steps,
                    proposal.processed_tokens_this_cycle,
                    proposal.effective_tokens_this_update,
                    proposal.local_discarded_tokens_this_cycle,
                    proposal.retained_tokens_since_base,
                    proposal.data_cursor_start,
                    proposal.data_cursor_end,
                    proposal.contributor_fence.kind,
                    _canonical_json(proposal.contributor_fence.as_dict()),
                    proposal.payload_relative_path,
                    proposal.payload_size,
                    proposal.payload_sha256,
                    proposal.tensor_schema_sha256,
                    proposal.tensor_dtype,
                    proposal.tensor_numel,
                    proposal.created_at,
                    now,
                ),
            )
            older_pending = connection.execute(
                """
                SELECT update_id, cycle_receipt_id FROM updates
                WHERE stable_contributor_key=? AND status='pending' AND update_id<>?
                    AND cycle_seq < ?
                """,
                (
                    proposal.stable_contributor_key,
                    proposal.update_id,
                    proposal.cycle_seq,
                ),
            ).fetchall()
            for row in older_pending:
                connection.execute(
                    """
                    UPDATE updates SET status='dropped', dropped_by_epoch=?,
                        drop_reason='superseded_by_newer_cycle'
                    WHERE update_id=? AND status='pending'
                    """,
                    (self.token.epoch, row["update_id"]),
                )
                connection.execute(
                    """
                    UPDATE token_fates SET direct_fate='dropped',
                        fate_reason='superseded_by_newer_cycle', updated_by_epoch=?, updated_at=?
                    WHERE receipt_id=?
                    """,
                    (self.token.epoch, now, row["cycle_receipt_id"]),
                )
            newer_pending = connection.execute(
                """
                SELECT 1 FROM updates
                WHERE stable_contributor_key=? AND status='pending' AND update_id<>?
                    AND cycle_seq > ?
                LIMIT 1
                """,
                (
                    proposal.stable_contributor_key,
                    proposal.update_id,
                    proposal.cycle_seq,
                ),
            ).fetchone()
            if newer_pending is not None:
                connection.execute(
                    """
                    UPDATE updates SET status='dropped', dropped_by_epoch=?,
                        drop_reason='superseded_before_arrival'
                    WHERE update_id=? AND status='pending'
                    """,
                    (self.token.epoch, proposal.update_id),
                )
                connection.execute(
                    """
                    UPDATE token_fates SET direct_fate='dropped',
                        fate_reason='superseded_before_arrival', updated_by_epoch=?, updated_at=?
                    WHERE receipt_id=?
                    """,
                    (self.token.epoch, now, proposal.cycle_receipt_id),
                )
            self._record_observation(connection, proposal, ProposalDisposition.ACCEPTED)
            return {"disposition": ProposalDisposition.ACCEPTED.value}

        result = self._command(command_id, "ingest_proposal", request, operation)
        return ProposalDisposition(result["disposition"])

    def record_proposal(
        self, *, command_id: str, proposal: FullUpdateProposalV2
    ) -> ProposalDisposition:
        """Compatibility spelling for early P1 callers; production uses ingest_proposal."""

        return self.ingest_proposal(command_id=command_id, proposal=proposal)

    def initialize_v0(
        self,
        *,
        command_id: str,
        publication_id: str,
        weight_relative_path: str,
        weight_size: int,
        weight_sha256: str,
        optim_relative_path: str,
        optim_size: int,
        optim_sha256: str,
    ) -> CommittedVersion:
        """Run v0 through the same prepared-intent and fenced commit chain."""

        self.prepare_publication(
            command_id=f"{command_id}-prepare",
            publication_id=publication_id,
            target_version=0,
            selection_batch_id=None,
            weight_relative_path=weight_relative_path,
            weight_size=weight_size,
            weight_sha256=weight_sha256,
            optim_relative_path=optim_relative_path,
            optim_size=optim_size,
            optim_sha256=optim_sha256,
        )
        return self.commit_merge(command_id=f"{command_id}-commit", publication_id=publication_id)

    def initialize_dynamic_membership(self, *, command_id: str) -> tuple[int, ...]:
        """Create the configured dynamic stream pool exactly once."""

        request = {
            "stream_pool_size": (
                self._authority._scope.stream_pool_size
                if isinstance(self._authority._scope, DynamicMembershipScope)
                else None
            )
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("dynamic membership initialization requires dynamic mode")
            existing = int(connection.execute("SELECT COUNT(*) FROM streams").fetchone()[0])
            if existing not in {0, self._authority._scope.stream_pool_size}:
                raise AuthoritySchemaError("dynamic stream pool is partially initialized")
            now = float(self._authority._wall_clock())
            if existing == 0:
                connection.executemany(
                    """
                    INSERT INTO streams(
                        stream_id, current_stream_epoch, state, resume_cursor, updated_at
                    ) VALUES (?, 1, 'available', 0, ?)
                    """,
                    (
                        (stream_id, now)
                        for stream_id in range(self._authority._scope.stream_pool_size)
                    ),
                )
            return {"stream_ids": list(range(self._authority._scope.stream_pool_size))}

        result = self._command(command_id, "initialize_dynamic_membership", request, operation)
        return tuple(int(item) for item in result["stream_ids"])

    def retire_incarnation(
        self,
        *,
        command_id: str,
        fence: DynamicContributorFence,
        reason: str,
        final_status: str = "revoked",
    ) -> tuple[str, ...]:
        """Retire one current dynamic incarnation and terminalize its active proposals."""

        if final_status not in {"stopped", "revoked", "expired"}:
            raise ValueError("final_status must be stopped, revoked, or expired")
        if not reason:
            raise ValueError("retirement reason must not be empty")
        request = {
            "fence": fence.as_dict(),
            "reason": reason,
            "final_status": final_status,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._require_current_fence(connection, fence)
            now = float(self._authority._wall_clock())
            update_ids = self._terminalize_fenced_updates(
                connection,
                fence_json=_canonical_json(fence.as_dict()),
                reason=reason,
            )
            connection.execute(
                """
                UPDATE learner_instances
                SET status=?, stopped_at=?, status_reason=?
                WHERE instance_id=?
                """,
                (final_status, now, reason, fence.instance_id),
            )
            connection.execute(
                """
                UPDATE placements SET current_instance_id=NULL, updated_at=?
                WHERE placement_id=? AND current_instance_id=?
                    AND current_placement_epoch=?
                """,
                (now, fence.placement_id, fence.instance_id, fence.placement_epoch),
            )
            connection.execute(
                """
                UPDATE streams SET current_instance_id=NULL, state='available', updated_at=?
                WHERE stream_id=? AND current_instance_id=? AND current_stream_epoch=?
                """,
                (now, fence.stream_id, fence.instance_id, fence.stream_epoch),
            )
            connection.execute(
                """
                INSERT INTO admission_history(
                    instance_id, stream_id, stream_epoch, placement_id, placement_epoch,
                    admission_generation, event, reason, command_epoch, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fence.instance_id,
                    fence.stream_id,
                    fence.stream_epoch,
                    fence.placement_id,
                    fence.placement_epoch,
                    fence.admission_generation,
                    final_status,
                    reason,
                    self.token.epoch,
                    now,
                ),
            )
            return {"terminalized_update_ids": list(update_ids)}

        result = self._command(command_id, "retire_incarnation", request, operation)
        return tuple(str(item) for item in result["terminalized_update_ids"])

    def try_select_batch(
        self, *, command_id: str, quorum_min: int, quorum_max: int
    ) -> SelectionBatch | None:
        if isinstance(quorum_min, bool) or not isinstance(quorum_min, int) or quorum_min < 1:
            raise ValueError("quorum_min must be a positive integer")
        if isinstance(quorum_max, bool) or not isinstance(quorum_max, int):
            raise ValueError("quorum_max must be an integer")
        if quorum_max < quorum_min:
            raise ValueError("quorum_max must be >= quorum_min")
        request = {"quorum_min": quorum_min, "quorum_max": quorum_max}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            if latest is None:
                raise RuntimeError("v0 must be committed before selecting learner proposals")
            rows = connection.execute(
                """
                SELECT u.*, COALESCE(s.committed_credit, 0) AS selection_credit
                FROM updates AS u
                LEFT JOIN selection_state AS s
                    ON s.stable_contributor_key = u.stable_contributor_key
                WHERE u.status='pending' AND u.base_global_version <= ?
                ORDER BY selection_credit ASC, u.stable_contributor_key ASC, u.update_id ASC
                LIMIT ?
                """,
                (int(latest), quorum_max),
            ).fetchall()
            if len(rows) < quorum_min:
                return {"selected": False}
            target_version = int(latest) + 1
            batch_id = "batch-" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:32]
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO selection_batches(
                    batch_id, command_id, owner_epoch, target_version, state, created_at
                ) VALUES (?, ?, ?, ?, 'selected', ?)
                """,
                (batch_id, command_id, self.token.epoch, target_version, now),
            )
            for reduction_order, row in enumerate(rows):
                self._require_current_fence_json(connection, str(row["fence_json"]))
                connection.execute(
                    """
                    INSERT INTO selection_batch_updates(
                        batch_id, update_id, stable_contributor_key, reduction_order, raw_weight
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        row["update_id"],
                        row["stable_contributor_key"],
                        reduction_order,
                        float(row["effective_tokens_this_update"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE updates
                    SET status='selected', selected_batch_id=?, selected_by_epoch=?
                    WHERE update_id=? AND status='pending'
                    """,
                    (batch_id, self.token.epoch, row["update_id"]),
                )
            return {"selected": True, "batch_id": batch_id}

        result = self._command(command_id, "try_select_batch", request, operation)
        if not result["selected"]:
            return None
        return self._load_selection_batch(str(result["batch_id"]))

    def prepare_publication(
        self,
        *,
        command_id: str,
        publication_id: str,
        target_version: int,
        selection_batch_id: str | None,
        weight_relative_path: str,
        weight_size: int,
        weight_sha256: str,
        optim_relative_path: str,
        optim_size: int,
        optim_sha256: str,
    ) -> PublicationIntent:
        predecessor = None if target_version == 0 else target_version - 1
        intent = PublicationIntent(
            publication_id=publication_id,
            command_id=command_id,
            owner_epoch=self.token.epoch,
            target_version=target_version,
            predecessor_version=predecessor,
            selection_batch_id=selection_batch_id,
            weight_relative_path=weight_relative_path,
            weight_size=weight_size,
            weight_sha256=weight_sha256,
            optim_relative_path=optim_relative_path,
            optim_size=optim_size,
            optim_sha256=optim_sha256,
        )
        request = asdict(intent)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            expected_target = 0 if latest is None else int(latest) + 1
            if target_version != expected_target:
                raise ValueError(f"publication target must be the next version {expected_target}")
            if target_version == 0:
                if selection_batch_id is not None:
                    raise ValueError("v0 publication cannot bind a selection batch")
            else:
                batch = connection.execute(
                    "SELECT * FROM selection_batches WHERE batch_id=?",
                    (selection_batch_id,),
                ).fetchone()
                if (
                    batch is None
                    or batch["state"] != "selected"
                    or int(batch["target_version"]) != target_version
                ):
                    raise ValueError("publication requires the selected batch for its target")
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO publication_intents(
                    publication_id, command_id, owner_epoch, target_version,
                    predecessor_version, selection_batch_id, weight_relative_path,
                    weight_size, weight_sha256, optim_relative_path, optim_size,
                    optim_sha256, state, prepared_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
                """,
                (
                    publication_id,
                    command_id,
                    self.token.epoch,
                    target_version,
                    predecessor,
                    selection_batch_id,
                    weight_relative_path,
                    weight_size,
                    weight_sha256,
                    optim_relative_path,
                    optim_size,
                    optim_sha256,
                    now,
                ),
            )
            if selection_batch_id is not None:
                connection.execute(
                    """
                    UPDATE selection_batches SET state='prepared', prepared_at=?
                    WHERE batch_id=? AND state='selected'
                    """,
                    (now, selection_batch_id),
                )
            row = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

        result = self._command(command_id, "prepare_publication", request, operation)
        return _decode_publication_intent(result)

    def commit_merge(self, *, command_id: str, publication_id: str) -> CommittedVersion:
        request = {"publication_id": publication_id}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            if intent is None or intent["state"] != "prepared":
                raise ValueError("publication intent is not prepared")
            target = int(intent["target_version"])
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            expected = 0 if latest is None else int(latest) + 1
            if target != expected:
                raise ValueError(f"commit target must be the next version {expected}")
            batch_id = intent["selection_batch_id"]
            selected_rows: list[sqlite3.Row] = []
            if target > 0:
                batch = connection.execute(
                    "SELECT * FROM selection_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if batch is None or batch["state"] != "prepared":
                    raise ValueError("selection batch is not prepared")
                selected_rows = connection.execute(
                    """
                    SELECT u.* FROM selection_batch_updates AS b
                    JOIN updates AS u ON u.update_id=b.update_id
                    WHERE b.batch_id=? ORDER BY b.reduction_order
                    """,
                    (batch_id,),
                ).fetchall()
                if not selected_rows:
                    raise ValueError("non-v0 publication has an empty selection")
                for row in selected_rows:
                    if row["status"] != "selected":
                        raise ValueError("selection contains a non-selected update")
                    self._require_current_fence_json(connection, str(row["fence_json"]))
            direct_tokens = sum(int(row["effective_tokens_this_update"]) for row in selected_rows)
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO global_versions(
                    version, predecessor_version, publication_id, weight_relative_path,
                    weight_size, weight_sha256, optim_relative_path, optim_size,
                    optim_sha256, committed_by_epoch, committed_by_owner_id, committed_at,
                    direct_weight_tokens_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target,
                    intent["predecessor_version"],
                    publication_id,
                    intent["weight_relative_path"],
                    intent["weight_size"],
                    intent["weight_sha256"],
                    intent["optim_relative_path"],
                    intent["optim_size"],
                    intent["optim_sha256"],
                    self.token.epoch,
                    self.token.owner_id,
                    now,
                    direct_tokens,
                ),
            )
            connection.execute(
                """
                UPDATE publication_intents SET state='committed', committed_at=?
                WHERE publication_id=? AND state='prepared'
                """,
                (now, publication_id),
            )
            if batch_id is not None:
                connection.execute(
                    """
                    UPDATE selection_batches SET state='committed', committed_at=?
                    WHERE batch_id=? AND state='prepared'
                    """,
                    (now, batch_id),
                )
                for row in selected_rows:
                    connection.execute(
                        """
                        UPDATE updates
                        SET status='applied', applied_version=?, applied_by_epoch=?
                        WHERE update_id=? AND status='selected'
                        """,
                        (target, self.token.epoch, row["update_id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO selection_state(
                            stable_contributor_key, committed_credit, last_committed_version,
                            updated_by_epoch, updated_at
                        ) VALUES (?, 1, ?, ?, ?)
                        ON CONFLICT(stable_contributor_key) DO UPDATE SET
                            committed_credit=selection_state.committed_credit + 1,
                            last_committed_version=excluded.last_committed_version,
                            updated_by_epoch=excluded.updated_by_epoch,
                            updated_at=excluded.updated_at
                        """,
                        (
                            row["stable_contributor_key"],
                            target,
                            self.token.epoch,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE token_fates SET direct_fate='applied', applied_version=?,
                            updated_by_epoch=?, updated_at=?
                        WHERE receipt_id=?
                        """,
                        (target, self.token.epoch, now, row["cycle_receipt_id"]),
                    )
            result = connection.execute(
                "SELECT * FROM global_versions WHERE version=?", (target,)
            ).fetchone()
            assert result is not None
            return dict(result)

        result = self._command(command_id, "commit_merge", request, operation)
        return _decode_committed_version(result)

    def abandon_publication(
        self, *, command_id: str, publication_id: str, reason: str
    ) -> PublicationIntent:
        request = {"publication_id": publication_id, "reason": reason}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            if row is None or row["state"] != "prepared":
                raise ValueError("only a prepared publication can be abandoned")
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                UPDATE publication_intents
                SET state='abandoned', abandoned_at=?, abandon_reason=?
                WHERE publication_id=?
                """,
                (now, reason, publication_id),
            )
            if row["selection_batch_id"] is not None:
                connection.execute(
                    """
                    UPDATE selection_batches
                    SET state='abandoned', abandoned_at=?, abandon_reason=?
                    WHERE batch_id=?
                    """,
                    (now, reason, row["selection_batch_id"]),
                )
                connection.execute(
                    """
                    UPDATE updates SET status='pending', selected_batch_id=NULL,
                        selected_by_epoch=NULL
                    WHERE selected_batch_id=? AND status='selected'
                    """,
                    (row["selection_batch_id"],),
                )
            result = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            assert result is not None
            return dict(result)

        result = self._command(command_id, "abandon_publication", request, operation)
        return _decode_publication_intent(result)

    def begin_terminal_close(self, *, command_id: str, reason: str) -> TerminalState:
        request = {"reason": reason}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            assert row is not None
            if row["state"] not in {"open", "closing", "draining"}:
                raise RuntimeError("authority is already terminal")
            generation = int(row["generation"]) + (1 if row["state"] == "open" else 0)
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                UPDATE controller_state
                SET state='closing', generation=?, reason=?, requested_at=?,
                    updated_by_epoch=?, updated_by_owner_id=?
                WHERE singleton=1
                """,
                (generation, reason, now, self.token.epoch, self.token.owner_id),
            )
            return {"state": "closing"}

        result = self._command(command_id, "begin_terminal_close", request, operation)
        return TerminalState(result["state"])

    def finalize_terminal(
        self,
        *,
        command_id: str,
        reason: str,
        error: bool = False,
    ) -> TerminalState:
        request = {"reason": reason, "error": error}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            outstanding = connection.execute(
                """
                SELECT COUNT(*) FROM updates WHERE status IN ('pending', 'selected')
                """
            ).fetchone()[0]
            prepared = connection.execute(
                "SELECT COUNT(*) FROM publication_intents WHERE state='prepared'"
            ).fetchone()[0]
            if outstanding or prepared:
                raise RuntimeError("terminal finalization requires no outstanding work")
            controller = connection.execute(
                "SELECT * FROM controller_state WHERE singleton=1"
            ).fetchone()
            assert controller is not None
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            if latest is None:
                raise RuntimeError("cannot finalize before v0 is committed")
            direct = connection.execute(
                "SELECT COALESCE(SUM(direct_weight_tokens_applied), 0) FROM global_versions"
            ).fetchone()[0]
            state = "error" if error else "finalized"
            generation = max(1, int(controller["generation"]))
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO terminal_state(
                    singleton, generation, state, stop_reason, final_version,
                    direct_weight_tokens_applied, finalized_by_epoch,
                    finalized_by_owner_id, finalized_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    state,
                    reason,
                    int(latest),
                    int(direct),
                    self.token.epoch,
                    self.token.owner_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE controller_state SET state=?, reason=?, updated_by_epoch=?,
                    updated_by_owner_id=? WHERE singleton=1
                """,
                (state, reason, self.token.epoch, self.token.owner_id),
            )
            return {"state": state}

        result = self._command(command_id, "finalize_terminal", request, operation)
        return TerminalState(result["state"])

    def _record_observation(
        self,
        connection: sqlite3.Connection,
        proposal: FullUpdateProposalV2,
        disposition: ProposalDisposition,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO proposal_observations(
                stable_contributor_key, cycle_seq, update_id, disposition,
                source_relative_path, object_sha256, observed_by_epoch, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.stable_contributor_key,
                proposal.cycle_seq,
                proposal.update_id,
                disposition.value,
                proposal.payload_relative_path,
                proposal.immutable_sha256(),
                self.token.epoch,
                float(self._authority._wall_clock()),
            ),
        )
        return int(cursor.lastrowid)

    def _terminalize_fenced_updates(
        self,
        connection: sqlite3.Connection,
        *,
        fence_json: str,
        reason: str,
    ) -> tuple[str, ...]:
        """Drop one stale fence's work and reconcile every affected durable batch."""

        affected = connection.execute(
            """
            SELECT update_id, cycle_receipt_id, selected_batch_id
            FROM updates
            WHERE fence_json=? AND status IN ('pending', 'selected')
            """,
            (fence_json,),
        ).fetchall()
        batch_ids = sorted(
            {str(row["selected_batch_id"]) for row in affected if row["selected_batch_id"]}
        )
        now = float(self._authority._wall_clock())
        for batch_id in batch_ids:
            connection.execute(
                """
                UPDATE publication_intents
                SET state='abandoned', abandoned_at=?, abandon_reason=?
                WHERE selection_batch_id=? AND state='prepared'
                """,
                (now, reason, batch_id),
            )
            connection.execute(
                """
                UPDATE selection_batches
                SET state='abandoned', abandoned_at=?, abandon_reason=?
                WHERE batch_id=? AND state IN ('selected', 'prepared')
                """,
                (now, reason, batch_id),
            )
            batch_updates = connection.execute(
                """
                SELECT u.* FROM selection_batch_updates AS b
                JOIN updates AS u ON u.update_id=b.update_id
                WHERE b.batch_id=? AND u.status='selected'
                """,
                (batch_id,),
            ).fetchall()
            for row in batch_updates:
                row_fence_json = str(row["fence_json"])
                if row_fence_json == fence_json:
                    replacement_status = "dropped"
                    drop_reason = reason
                else:
                    try:
                        self._require_current_fence_json(connection, row_fence_json)
                    except MembershipFenceError:
                        replacement_status = "dropped"
                        drop_reason = "stale_fence_during_reconciliation"
                    else:
                        pending = connection.execute(
                            """
                            SELECT 1 FROM updates
                            WHERE stable_contributor_key=? AND status='pending'
                            LIMIT 1
                            """,
                            (row["stable_contributor_key"],),
                        ).fetchone()
                        replacement_status = "pending" if pending is None else "dropped"
                        drop_reason = (
                            None if pending is None else "superseded_during_reconciliation"
                        )
                connection.execute(
                    """
                    UPDATE updates SET status=?, selected_batch_id=NULL,
                        selected_by_epoch=NULL, dropped_by_epoch=?, drop_reason=?
                    WHERE update_id=? AND status='selected'
                    """,
                    (
                        replacement_status,
                        self.token.epoch if replacement_status == "dropped" else None,
                        drop_reason,
                        row["update_id"],
                    ),
                )
                if replacement_status == "dropped":
                    connection.execute(
                        """
                        UPDATE token_fates SET direct_fate='dropped', fate_reason=?,
                            updated_by_epoch=?, updated_at=? WHERE receipt_id=?
                        """,
                        (
                            drop_reason,
                            self.token.epoch,
                            now,
                            row["cycle_receipt_id"],
                        ),
                    )
        update_ids = tuple(str(row["update_id"]) for row in affected)
        for row in affected:
            cursor = connection.execute(
                """
                UPDATE updates SET status='dropped', selected_batch_id=NULL,
                    selected_by_epoch=NULL, dropped_by_epoch=?, drop_reason=?
                WHERE update_id=? AND status IN ('pending', 'selected')
                """,
                (self.token.epoch, reason, row["update_id"]),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE token_fates SET direct_fate='dropped', fate_reason=?,
                        updated_by_epoch=?, updated_at=? WHERE receipt_id=?
                    """,
                    (reason, self.token.epoch, now, row["cycle_receipt_id"]),
                )
        return update_ids

    def _require_current_fence(
        self,
        connection: sqlite3.Connection,
        fence: StaticContributorFence | DynamicContributorFence,
    ) -> None:
        if isinstance(fence, StaticContributorFence):
            if not isinstance(self._authority._scope, StaticMembershipScope):
                raise MembershipFenceError("static fence used with dynamic authority")
            row = connection.execute(
                """
                SELECT 1 FROM static_contributor_bindings
                WHERE learner_id=? AND logical_launch_id=? AND attempt_id=?
                    AND binding_generation=? AND status='active'
                """,
                (
                    fence.learner_id,
                    fence.logical_launch_id,
                    fence.attempt_id,
                    fence.binding_generation,
                ),
            ).fetchone()
        else:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise MembershipFenceError("dynamic fence used with static authority")
            row = connection.execute(
                """
                SELECT 1 FROM learner_instances AS i
                JOIN placements AS p ON p.placement_id=i.placement_id
                JOIN streams AS s ON s.stream_id=i.stream_id
                WHERE i.instance_id=? AND i.placement_id=? AND i.placement_epoch=?
                    AND i.stream_id=? AND i.stream_epoch=? AND i.admission_generation=?
                    AND i.admission_token_sha256=? AND i.status='admitted'
                    AND p.current_instance_id=i.instance_id
                    AND p.current_placement_epoch=i.placement_epoch
                    AND s.current_instance_id=i.instance_id
                    AND s.current_stream_epoch=i.stream_epoch
                """,
                (
                    fence.instance_id,
                    fence.placement_id,
                    fence.placement_epoch,
                    fence.stream_id,
                    fence.stream_epoch,
                    fence.admission_generation,
                    fence.admission_token_sha256,
                ),
            ).fetchone()
        if row is None:
            raise MembershipFenceError("contributor fence is stale or not admitted")

    def _require_current_fence_json(self, connection: sqlite3.Connection, fence_json: str) -> None:
        payload = json.loads(fence_json)
        fence = (
            StaticContributorFence.from_dict(payload)
            if payload.get("kind") == "static"
            else DynamicContributorFence.from_dict(payload)
        )
        self._require_current_fence(connection, fence)

    def _command(
        self,
        command_id: str,
        kind: str,
        request: Mapping[str, Any],
        operation: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        validate_identity(command_id, name="command_id")
        request_json = _canonical_json(dict(request))
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        connection = self._authority._connection
        started_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            started_transaction = True
            self._authority._verify_token(self.token)
            existing = connection.execute(
                "SELECT * FROM command_records WHERE command_id=?", (command_id,)
            ).fetchone()
            if existing is not None:
                if existing["command_kind"] != kind or existing["request_sha256"] != request_sha:
                    raise CommandConflictError(
                        "command ID was replayed with a different kind or request"
                    )
                result = json.loads(str(existing["result_json"]))
                connection.commit()
                return result
            result = operation(connection)
            self._authority._verify_token(self.token)
            connection.execute(
                """
                INSERT INTO command_records(
                    command_id, command_kind, request_sha256, owner_epoch,
                    result_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    kind,
                    request_sha,
                    self.token.epoch,
                    _canonical_json(result),
                    float(self._authority._wall_clock()),
                ),
            )
            connection.commit()
            return result
        except Exception:
            if started_transaction and connection.in_transaction:
                connection.rollback()
            raise

    def _load_selection_batch(self, batch_id: str) -> SelectionBatch:
        batch = self._authority._fetchone(
            "SELECT * FROM selection_batches WHERE batch_id=?", (batch_id,)
        )
        if batch is None:
            raise AuthoritySchemaError(f"selection batch disappeared: {batch_id}")
        rows = self._authority._fetchall(
            """
            SELECT u.*, b.reduction_order, COALESCE(s.committed_credit, 0) AS selection_credit
            FROM selection_batch_updates AS b
            JOIN updates AS u ON u.update_id=b.update_id
            LEFT JOIN selection_state AS s
                ON s.stable_contributor_key=u.stable_contributor_key
            WHERE b.batch_id=? ORDER BY b.reduction_order
            """,
            (batch_id,),
        )
        candidates = tuple(
            SelectionCandidate(
                proposal=_decode_proposal(row),
                selection_credit=int(row["selection_credit"]),
            )
            for row in rows
        )
        return SelectionBatch(
            batch_id=str(batch["batch_id"]),
            command_id=str(batch["command_id"]),
            owner_epoch=int(batch["owner_epoch"]),
            target_version=int(batch["target_version"]),
            candidates=candidates,
            state=str(batch["state"]),
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode_static_binding(row: Mapping[str, Any]) -> StaticBinding:
    return StaticBinding(
        learner_id=str(row["learner_id"]),
        logical_launch_id=str(row["logical_launch_id"]),
        attempt_id=str(row["attempt_id"]),
        binding_generation=int(row["binding_generation"]),
        status=str(row["status"]),
    )


def _decode_progress(row: Mapping[str, Any]) -> ContributorProgress:
    return ContributorProgress(
        stable_contributor_key=str(row["stable_contributor_key"]),
        last_cycle_seq=int(row["last_cycle_seq"]),
        last_receipt_id=(None if row["last_receipt_id"] is None else str(row["last_receipt_id"])),
        last_receipt_sha256=(
            None if row["last_receipt_sha256"] is None else str(row["last_receipt_sha256"])
        ),
        data_cursor=int(row["data_cursor"]),
        updated_at=float(row["updated_at"]),
    )


def _decode_publication_intent(row: Mapping[str, Any]) -> PublicationIntent:
    return PublicationIntent(
        publication_id=str(row["publication_id"]),
        command_id=str(row["command_id"]),
        owner_epoch=int(row["owner_epoch"]),
        target_version=int(row["target_version"]),
        predecessor_version=(
            None if row["predecessor_version"] is None else int(row["predecessor_version"])
        ),
        selection_batch_id=(
            None if row["selection_batch_id"] is None else str(row["selection_batch_id"])
        ),
        weight_relative_path=str(row["weight_relative_path"]),
        weight_size=int(row["weight_size"]),
        weight_sha256=str(row["weight_sha256"]),
        optim_relative_path=str(row["optim_relative_path"]),
        optim_size=int(row["optim_size"]),
        optim_sha256=str(row["optim_sha256"]),
        state=str(row["state"]),
    )


def _decode_committed_version(row: Mapping[str, Any]) -> CommittedVersion:
    return CommittedVersion(
        version=int(row["version"]),
        predecessor_version=(
            None if row["predecessor_version"] is None else int(row["predecessor_version"])
        ),
        publication_id=str(row["publication_id"]),
        weight_relative_path=str(row["weight_relative_path"]),
        weight_size=int(row["weight_size"]),
        weight_sha256=str(row["weight_sha256"]),
        optim_relative_path=str(row["optim_relative_path"]),
        optim_size=int(row["optim_size"]),
        optim_sha256=str(row["optim_sha256"]),
        committed_by_epoch=int(row["committed_by_epoch"]),
        committed_by_owner_id=str(row["committed_by_owner_id"]),
        committed_at=float(row["committed_at"]),
        direct_weight_tokens_applied=int(row["direct_weight_tokens_applied"]),
    )


def _decode_proposal(row: Mapping[str, Any]) -> FullUpdateProposalV2:
    return FullUpdateProposalV2.from_dict(
        {
            "proposal_format_version": 2,
            "run_id": row["run_id"],
            "stable_contributor_key": row["stable_contributor_key"],
            "cycle_seq": row["cycle_seq"],
            "cycle_id": row["cycle_id"],
            "update_id": row["update_id"],
            "cycle_receipt_id": row["cycle_receipt_id"],
            "cycle_receipt_sha256": row["cycle_receipt_sha256"],
            "base_global_version": row["base_global_version"],
            "local_step_start": row["local_step_start"],
            "local_step_end": row["local_step_end"],
            "inner_steps": row["inner_steps"],
            "processed_tokens_this_cycle": row["processed_tokens_this_cycle"],
            "effective_tokens_this_update": row["effective_tokens_this_update"],
            "local_discarded_tokens_this_cycle": row["local_discarded_tokens_this_cycle"],
            "retained_tokens_since_base": row["retained_tokens_since_base"],
            "data_cursor_start": row["data_cursor_start"],
            "data_cursor_end": row["data_cursor_end"],
            "contributor_fence": json.loads(str(row["fence_json"])),
            "payload_relative_path": row["payload_relative_path"],
            "payload_size": row["payload_size"],
            "payload_sha256": row["payload_sha256"],
            "tensor_schema_sha256": row["tensor_schema_sha256"],
            "tensor_dtype": row["tensor_dtype"],
            "tensor_numel": row["tensor_numel"],
            "created_at": row["created_at"],
        }
    )
