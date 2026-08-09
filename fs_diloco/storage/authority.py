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
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.versions import AUTHORITY_SCHEMA_VERSION, PROTOCOL_VERSION
from ..protocol._validation import identity as validate_identity
from ..protocol.authority import (
    ContributorProgress,
    DynamicAdmission,
    MergeFenceConflict,
    ProposalDisposition,
    PublicationIntent,
    ReadResult,
    ReadStatus,
    SelectionBatch,
    SelectionAttempt,
    SelectionCandidate,
    StaticBinding,
    TerminalState,
    TokenLedgerSummary,
    VisibilityDecision,
)
from ..protocol.contributor import (
    DynamicContributorFence,
    DynamicMembershipScope,
    StaticContributorFence,
    StaticMembershipScope,
    decode_contributor_fence,
)
from ..protocol.cycle_receipt import CycleReceiptV1
from ..protocol.data_cursor import ContributorResumeState
from ..protocol.proposal import FullUpdateProposalV2
from ..protocol.scheduler import (
    SchedulerOperatorAction,
    SchedulerOperatorRequest,
    scheduler_state_sha256,
)
from .atomic_io import fsync_directory, publish_immutable_bytes, read_json, sha256_file
from .audit_archive import (
    publish_command_receipt,
    read_command_receipt,
    validate_audit_batch,
    validate_audit_partition,
    validate_audit_partition_manifest,
)
from .leader_lease import (
    CommittedLeaderLease,
    LeaderToken,
    LeaseUnavailableError,
    StaleLeaderTokenError,
)
from .paths import RunPaths


PLAN03_REQUIREMENTS = frozenset(
    {
        "AUDIT-02",
        "AUDIT-04",
        "AUTH-02",
        "AUTH-03",
        "AUTH-05",
        "AUTH-09",
        "AUTH-10",
        "AUTH-11",
        "DATA-02",
        "DATA-03",
        "DMB-05",
        "DMB-09",
        "DMB-10",
        "SCHED-01",
        "SCHED-02",
        "SCHED-03",
        "SCHED-04",
        "SCHED-05",
        "SEL-03",
        "SEL-04",
        "TERM-01",
        "TERM-02",
        "TERM-03",
        "TOK-05",
        "TOK-08",
    }
)


AUTHORITY_APPLICATION_ID = 0x46534434  # "FSD4"
BASE_SCHEMA_NAME = "schema_v4.sql"
DYNAMIC_SCHEMA_NAME = "schema_v4_dynamic.sql"
V4_BOOTSTRAP_MARKER_NAME = "authority_v4_bootstrap_complete.json"


def _publication_commit_boundary(_name: str) -> None:
    """No-op seam used to inject deterministic transaction faults in gate tests."""


class AuthoritySchemaError(RuntimeError):
    """The on-disk schema or immutable identity is not the requested v4 authority."""


class CommandConflictError(RuntimeError):
    """A command ID was replayed with a different immutable request."""


class MembershipFenceError(RuntimeError):
    """A contributor fence is not current."""


class ProposalPayloadError(RuntimeError):
    """A proposal payload is not currently safe to ingest."""

    def __init__(self, result: ReadResult[Any]) -> None:
        self.result = result
        super().__init__(f"{result.status.value}: {result.diagnostic}")


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
    theta_sha256: str
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


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _same_regular_inode(left: Path, right: Path) -> bool:
    try:
        left_stat = left.lstat()
        right_stat = right.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(left_stat.st_mode)
        and stat.S_ISREG(right_stat.st_mode)
        and (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)
    )


def _lexical_protocol_path(root: Path, relative_path: str) -> Path:
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\0" in relative_path
    ):
        raise ValueError("protocol path must be a canonical relative POSIX path")
    relative = PurePosixPath(relative_path)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("protocol path must be a canonical relative POSIX path")
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("protocol path parent must be a non-symlink directory")
    return current / relative.name


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
    if _path_entry_exists(path) or _path_entry_exists(marker):
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
    database_linked = False
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
        database_linked = True
        fsync_directory(path.parent)
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
        marker_bytes = (
            json.dumps(marker_payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        publish_immutable_bytes(marker, marker_bytes)
    except BaseException:
        if database_linked and _same_regular_inode(temporary_path, path):
            path.unlink()
            fsync_directory(path.parent)
        raise
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

    def authority_created_at(self) -> float:
        row = self._authority._fetchone("SELECT created_at FROM schema_meta WHERE singleton = 1")
        if row is None:
            raise AuthoritySchemaError("authority schema metadata is missing")
        return float(row["created_at"])

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

    def static_binding_history(
        self, learner_id: str, binding_generation: int
    ) -> dict[str, Any] | None:
        validate_identity(learner_id, name="learner_id")
        if (
            isinstance(binding_generation, bool)
            or not isinstance(binding_generation, int)
            or binding_generation < 1
        ):
            raise ValueError("binding_generation must be a positive integer")
        row = self._authority._fetchone(
            """
            SELECT * FROM static_binding_history
            WHERE learner_id=? AND binding_generation=?
            """,
            (learner_id, binding_generation),
        )
        return None if row is None else dict(row)

    def contributor_progress(self, stable_contributor_key: str) -> ContributorProgress | None:
        row = self._authority._fetchone(
            "SELECT * FROM contributor_progress WHERE stable_contributor_key = ?",
            (stable_contributor_key,),
        )
        return None if row is None else _decode_progress(row)

    def update_status(self, update_id: str) -> str | None:
        validate_identity(update_id, name="update_id")
        row = self._authority._fetchone(
            "SELECT status FROM updates WHERE update_id=?", (update_id,)
        )
        return None if row is None else str(row["status"])

    def controller_status(self) -> dict[str, Any]:
        row = self._authority._fetchone("SELECT * FROM controller_state WHERE singleton = 1")
        if row is None:
            raise AuthoritySchemaError("controller state is missing")
        return dict(row)

    def terminal_record(self) -> dict[str, Any] | None:
        row = self._authority._fetchone("SELECT * FROM terminal_state WHERE singleton = 1")
        return None if row is None else dict(row)

    def terminal_contributor_fences(self) -> tuple[dict[str, Any], ...]:
        controller = self._authority._fetchone(
            "SELECT generation FROM controller_state WHERE singleton = 1"
        )
        if controller is None:
            raise AuthoritySchemaError("controller state is missing")
        rows = self._authority._fetchall(
            """
            SELECT * FROM terminal_contributor_fences
            WHERE generation=? ORDER BY stable_contributor_key
            """,
            (int(controller["generation"]),),
        )
        return tuple(dict(row) for row in rows)

    def current_contributor_fences(
        self,
    ) -> tuple[StaticContributorFence | DynamicContributorFence, ...]:
        if isinstance(self._authority._scope, StaticMembershipScope):
            rows = self._authority._fetchall(
                """
                SELECT * FROM static_contributor_bindings
                WHERE status='active' ORDER BY learner_id
                """
            )
            return tuple(
                StaticContributorFence(
                    kind="static",
                    learner_id=str(row["learner_id"]),
                    logical_launch_id=str(row["logical_launch_id"]),
                    attempt_id=str(row["attempt_id"]),
                    binding_generation=int(row["binding_generation"]),
                )
                for row in rows
            )
        rows = self._authority._fetchall(
            """
            SELECT * FROM learner_instances
            WHERE status IN ('admitted', 'draining') ORDER BY stream_id
            """
        )
        return tuple(LeaderSession._dynamic_fence_from_instance(row) for row in rows)

    def token_ledger_summary(self) -> TokenLedgerSummary:
        row = self._authority._fetchone("SELECT * FROM token_rollups WHERE singleton=1")
        gap = self._authority._fetchone(
            """
            SELECT COALESCE(SUM(hard_crash_gap_tokens_upper_bound), 0) AS gap
            FROM terminal_contributor_fences WHERE state='hard_crash'
            """
        )
        gap_tokens = int(gap["gap"] if gap is not None else 0)
        if row is None:
            return TokenLedgerSummary(
                adjudicated_processed=0,
                local_discarded=0,
                direct_applied=0,
                direct_dropped=0,
                direct_quarantined_or_conflicted=0,
                direct_reported_unpublished=0,
                direct_outstanding=0,
                carried_ancestry=0,
                hard_crash_gap_tokens_upper_bound=gap_tokens,
            )
        return TokenLedgerSummary(
            adjudicated_processed=int(row["adjudicated_processed"]),
            local_discarded=int(row["local_discarded"]),
            direct_applied=int(row["direct_applied"]),
            direct_dropped=int(row["direct_dropped"]),
            direct_quarantined_or_conflicted=int(row["direct_quarantined_or_conflicted"]),
            direct_reported_unpublished=int(row["direct_reported_unpublished"]),
            direct_outstanding=int(row["direct_outstanding"]),
            carried_ancestry=int(row["carried_ancestry"]),
            hard_crash_gap_tokens_upper_bound=gap_tokens,
        )

    def integrity_check(self) -> tuple[str, ...]:
        return tuple(str(row[0]) for row in self._authority._fetchall("PRAGMA integrity_check"))

    def table_names(self) -> tuple[str, ...]:
        rows = self._authority._fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return tuple(str(row[0]) for row in rows)

    def syncer_epochs(self) -> tuple[dict[str, Any], ...]:
        rows = self._authority._fetchall("SELECT * FROM syncer_epochs ORDER BY epoch")
        return tuple(dict(row) for row in rows)

    def dynamic_streams(self) -> tuple[dict[str, Any], ...]:
        if not isinstance(self._authority._scope, DynamicMembershipScope):
            return ()
        return tuple(
            dict(row)
            for row in self._authority._fetchall("SELECT * FROM streams ORDER BY stream_id")
        )

    def dynamic_instances(self) -> tuple[dict[str, Any], ...]:
        if not isinstance(self._authority._scope, DynamicMembershipScope):
            return ()
        return tuple(
            dict(row)
            for row in self._authority._fetchall(
                "SELECT * FROM learner_instances ORDER BY registered_at, instance_id"
            )
        )

    def dynamic_launch_requests(self) -> tuple[dict[str, Any], ...]:
        if not isinstance(self._authority._scope, DynamicMembershipScope):
            return ()
        return tuple(
            dict(row)
            for row in self._authority._fetchall(
                "SELECT * FROM launch_requests ORDER BY created_at, request_id"
            )
        )

    def capacity_observations(self) -> tuple[dict[str, Any], ...]:
        if not isinstance(self._authority._scope, DynamicMembershipScope):
            return ()
        return tuple(
            dict(row)
            for row in self._authority._fetchall(
                "SELECT * FROM capacity_observations ORDER BY observation_seq"
            )
        )

    def scheduler_operator_file_disposition(
        self, relative_path: str, content_sha256: str
    ) -> dict[str, Any] | None:
        if not isinstance(self._authority._scope, DynamicMembershipScope):
            return None
        row = self._authority._fetchone(
            """
            SELECT * FROM scheduler_operator_file_dispositions
            WHERE relative_path=? AND content_sha256=?
            """,
            (relative_path, content_sha256),
        )
        return None if row is None else dict(row)

    def v0_committed_at(self) -> float | None:
        row = self._authority._fetchone("SELECT committed_at FROM global_versions WHERE version=0")
        return None if row is None else float(row["committed_at"])

    def audit_history_records(self, *, cutoff_version: int) -> tuple[dict[str, Any], ...]:
        if isinstance(cutoff_version, bool) or not isinstance(cutoff_version, int):
            raise ValueError("cutoff_version must be an integer")
        if cutoff_version < 0:
            return ()
        return tuple(_audit_history_records(self._authority._connection, cutoff_version))

    def audit_archive_summary(self) -> dict[str, int]:
        row = self._authority._fetchone(
            """
            SELECT
                (SELECT COUNT(*) FROM archive_batches) AS hot_batches,
                (SELECT COUNT(*) FROM archive_partitions WHERE state='committed') AS partitions,
                (SELECT COALESCE(SUM(source_batch_count), 0) FROM archive_partitions
                    WHERE state='committed') AS folded_batches,
                (SELECT COUNT(*) FROM audit_partition_batches) AS folded_batch_index_rows,
                (SELECT COUNT(*) FROM audit_gc_candidates WHERE state='pending') AS pending_gc,
                (SELECT COUNT(*) FROM audit_gc_candidates WHERE state='claimed') AS claimed_gc
            """
        )
        assert row is not None
        return {key: int(row[key]) for key in row.keys()}

    def audit_hot_batches(self) -> tuple[dict[str, Any], ...]:
        rows = self._authority._fetchall(
            "SELECT * FROM archive_batches WHERE state='committed' "
            "ORDER BY cutoff_version, archive_batch_id"
        )
        return tuple(dict(row) for row in rows)

    def artifact_gc_ready(self, *, claimant_epoch: int) -> bool:
        now = float(self._authority._wall_clock())
        return (
            self._authority._fetchone(
                "SELECT 1 FROM gc_candidates "
                "WHERE not_before<=? AND (state='pending' "
                "OR (state='claimed' AND claimed_by_epoch<>?)) LIMIT 1",
                (now, claimant_epoch),
            )
            is not None
        )

    def audit_gc_ready(self, *, claimant_epoch: int) -> bool:
        return (
            self._authority._fetchone(
                "SELECT 1 FROM audit_gc_candidates "
                "WHERE state='pending' OR (state='claimed' AND claimed_by_epoch<>?) LIMIT 1",
                (claimant_epoch,),
            )
            is not None
        )


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
        run_root: str | Path | None = None,
        orphan_grace_seconds: float | None = None,
        max_quarantine_records_per_contributor: int = 64,
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
        self._run_root = (
            Path(run_root)
            if run_root is not None
            else (path.parent.parent if path.parent.name == "control" else path.parent)
        ).resolve()
        self._identity = identity
        self._scope = membership_scope
        self._connection = connection
        self._wall_clock = wall_clock
        self._lease_duration_seconds = float(lease_duration_seconds)
        self._max_clock_skew_seconds = float(max_clock_skew_seconds)
        minimum_orphan_grace = self._lease_duration_seconds + 2.0 * self._max_clock_skew_seconds
        self._orphan_grace_seconds = (
            minimum_orphan_grace if orphan_grace_seconds is None else float(orphan_grace_seconds)
        )
        if self._orphan_grace_seconds < minimum_orphan_grace:
            connection.close()
            raise ValueError("orphan grace must cover lease duration plus twice clock skew")
        if (
            isinstance(max_quarantine_records_per_contributor, bool)
            or not isinstance(max_quarantine_records_per_contributor, int)
            or max_quarantine_records_per_contributor < 1
        ):
            connection.close()
            raise ValueError("max quarantine records per contributor must be positive")
        self._max_quarantine_records_per_contributor = max_quarantine_records_per_contributor
        self._lease_safety_check = lease_safety_check
        self.metadata = metadata
        self.read = AuthorityReadModel(self)

    def close(self) -> None:
        self._connection.close()

    def assert_outside_transaction(self) -> None:
        """Fail closed if test instrumentation reached a SQLite transaction boundary."""

        if self._connection.in_transaction:
            raise RuntimeError("authority connection has an active SQLite transaction")

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
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            now = float(self._wall_clock())
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

    def committed_leader_lease(self, token: LeaderToken) -> CommittedLeaderLease:
        """Return the exact active lease row after verifying this token is still safe."""

        row = self._verify_token(token)
        return _decode_committed_leader_lease(token, row)

    def renew_leader(self, token: LeaderToken) -> CommittedLeaderLease:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            now = float(self._wall_clock())
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
            row = self._connection.execute(
                """
                SELECT * FROM syncer_leader
                WHERE singleton=1 AND epoch=? AND owner_id=? AND state='active'
                """,
                (token.epoch, token.owner_id),
            ).fetchone()
            if row is None:
                raise StaleLeaderTokenError("leader changed during renewal")
            committed = _decode_committed_leader_lease(token, row)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return committed

    def release_leader(self, token: LeaderToken) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            now = float(self._wall_clock())
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

    def fail_leader(self, token: LeaderToken) -> None:
        """Fence a candidate that exited through its error boundary."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            now = float(self._wall_clock())
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
                UPDATE syncer_epochs SET final_state='error', final_at=?, last_renewed_at=?
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

    def replay_committed_static_binding(
        self,
        *,
        command_id: str,
        learner_id: str,
        logical_launch_id: str,
        attempt_id: str,
        expected_generation: int | None = None,
        allow_logical_replacement: bool = False,
        replacement_reason: str | None = None,
        registration_created_at: float | None = None,
    ) -> StaticBinding | None:
        """Replay only an exact committed static-binding command request."""

        request = _static_binding_command_request(
            learner_id=learner_id,
            logical_launch_id=logical_launch_id,
            attempt_id=attempt_id,
            expected_generation=expected_generation,
            allow_logical_replacement=allow_logical_replacement,
            replacement_reason=replacement_reason,
            registration_created_at=registration_created_at,
        )
        try:
            payload = self._command_replay(
                command_id,
                "bind_or_replace_static_attempt",
                request,
            )
            if payload is None:
                return None
            if not isinstance(payload, Mapping):
                raise TypeError("committed static binding result is not an object")
            return _decode_static_binding(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AuthoritySchemaError("committed static binding result is invalid") from exc

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
        registration_created_at: float | None = None,
    ) -> StaticBinding:
        request = _static_binding_command_request(
            learner_id=learner_id,
            logical_launch_id=logical_launch_id,
            attempt_id=attempt_id,
            expected_generation=expected_generation,
            allow_logical_replacement=allow_logical_replacement,
            replacement_reason=replacement_reason,
            registration_created_at=registration_created_at,
        )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, StaticMembershipScope):
                raise RuntimeError("static binding command requires static authority mode")
            if learner_id not in self._authority._scope.learner_ids:
                raise MembershipFenceError(f"unknown static learner: {learner_id}")
            controller = connection.execute(
                "SELECT state, requested_at FROM controller_state WHERE singleton=1"
            ).fetchone()
            if controller is None or controller["state"] not in {"open", "preclosing"}:
                raise MembershipFenceError("static admission is closed")
            if controller["state"] == "preclosing" and (
                registration_created_at is None
                or controller["requested_at"] is None
                or float(registration_created_at) > float(controller["requested_at"])
            ):
                raise MembershipFenceError("static registration is after the preclose cutoff")
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
            self._require_current_fence(
                connection,
                receipt.contributor_fence,
                update_id=receipt.planned_update_id,
            )
            self._require_terminal_input_allowed(
                connection,
                fence=receipt.contributor_fence,
                cycle_seq=receipt.cycle_seq,
                update_id=receipt.planned_update_id,
            )
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
            self._record_token_receipt(connection, receipt=receipt, now=now)
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
        replay = self._command_replay(command_id, "ingest_proposal", request)
        if replay is not None:
            return ProposalDisposition(replay["disposition"])
        from .object_store import verify_proposal_payload

        verification = verify_proposal_payload(self._authority._run_root, proposal)
        if verification.status is not ReadStatus.OK or verification.value is None:
            raise ProposalPayloadError(verification)
        verified_payload = verification.value

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if proposal.run_id != self._authority._identity.run_id:
                raise MembershipFenceError("proposal belongs to another run")
            self._require_current_fence(
                connection, proposal.contributor_fence, update_id=proposal.update_id
            )
            self._require_terminal_input_allowed(
                connection,
                fence=proposal.contributor_fence,
                cycle_seq=proposal.cycle_seq,
                update_id=proposal.update_id,
            )
            receipt = self._require_proposal_receipt(
                connection,
                proposal,
            )
            proposal_digest = proposal.immutable_sha256()
            existing = connection.execute(
                "SELECT * FROM updates WHERE update_id=?", (proposal.update_id,)
            ).fetchone()
            if existing is not None:
                disposition = (
                    ProposalDisposition.EXACT_REPLAY
                    if existing["proposal_sha256"] == proposal_digest
                    else ProposalDisposition.IDENTITY_COLLISION
                )
                observation_id = self._record_observation(connection, proposal, disposition)
                if disposition is ProposalDisposition.IDENTITY_COLLISION:
                    connection.execute(
                        """
                        INSERT INTO proposal_conflicts(
                            observation_id, conflict_kind, existing_update_id,
                            incoming_update_id, bounded_diagnostic, fingerprint
                        ) VALUES (?, 'identity_collision', ?, ?, ?, ?)
                        """,
                        (
                            observation_id,
                            existing["update_id"],
                            proposal.update_id,
                            "update identity already has different immutable bytes",
                            proposal_digest,
                        ),
                    )
                    self._record_quarantine(
                        connection,
                        proposal=proposal,
                        disposition="identity_collision",
                        fingerprint=proposal_digest,
                        diagnostic="update identity already has different immutable bytes",
                        observation_id=observation_id,
                    )
                self._advance_proposal_frontier(connection, proposal, observation_id)
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
                self._record_quarantine(
                    connection,
                    proposal=proposal,
                    disposition="conflict",
                    fingerprint=proposal_digest,
                    diagnostic="logical proposal key already has a different update ID",
                    observation_id=observation_id,
                )
                self._advance_proposal_frontier(connection, proposal, observation_id)
                return {"disposition": ProposalDisposition.CONFLICT.value}
            if (
                receipt["planned_update_id"] != proposal.update_id
                or receipt["planned_payload_sha256"] != proposal.payload_sha256
            ):
                raise ValueError("proposal does not match its cycle receipt plan")
            if (
                verified_payload.relative_path != proposal.payload_relative_path
                or verified_payload.size_bytes != proposal.payload_size
                or verified_payload.sha256 != proposal.payload_sha256
            ):
                raise ValueError("verified payload identity does not match proposal metadata")
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
                self._transition_token_fate(
                    connection,
                    receipt_id=str(row["cycle_receipt_id"]),
                    fate="dropped",
                    reason="superseded_by_newer_cycle",
                    now=now,
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
                self._transition_token_fate(
                    connection,
                    receipt_id=proposal.cycle_receipt_id,
                    fate="dropped",
                    reason="superseded_before_arrival",
                    now=now,
                )
            observation_id = self._record_observation(
                connection, proposal, ProposalDisposition.ACCEPTED
            )
            self._advance_proposal_frontier(connection, proposal, observation_id)
            return {"disposition": ProposalDisposition.ACCEPTED.value}

        result = self._command(command_id, "ingest_proposal", request, operation)
        return ProposalDisposition(result["disposition"])

    def observe_proposal_visibility(
        self,
        *,
        command_id: str,
        stable_contributor_key: str,
        cycle_seq: int,
        update_id: str,
        object_identity: str,
        pointer_signature: str,
        pointer_sequence: int,
        source_relative_path: str,
        result: ReadResult[Any],
        grace_seconds: float,
        operator_deadline_seconds: float,
        max_archived_signatures: int = 32,
    ) -> VisibilityDecision:
        """Persist one typed read result and apply stable terminal thresholds."""

        for name, value in (
            ("stable_contributor_key", stable_contributor_key),
            ("update_id", update_id),
            ("object_identity", object_identity),
            ("pointer_signature", pointer_signature),
        ):
            validate_identity(value, name=name)
        if isinstance(cycle_seq, bool) or not isinstance(cycle_seq, int) or cycle_seq < 1:
            raise ValueError("cycle_seq must be a positive integer")
        if (
            isinstance(pointer_sequence, bool)
            or not isinstance(pointer_sequence, int)
            or pointer_sequence < 0
        ):
            raise ValueError("pointer_sequence must be a non-negative integer")
        if grace_seconds < 0.0 or operator_deadline_seconds < grace_seconds:
            raise ValueError("visibility deadlines must satisfy 0 <= grace <= operator deadline")
        if max_archived_signatures < 1:
            raise ValueError("max_archived_signatures must be positive")
        if (
            not source_relative_path
            or source_relative_path.startswith("/")
            or ".." in Path(source_relative_path).parts
        ):
            raise ValueError("source_relative_path must be normalized and run-root-relative")
        diagnostic = (result.diagnostic or "")[:512]
        raw_fingerprint = result.fingerprint or diagnostic or result.status.value
        fingerprint = (
            raw_fingerprint
            if len(raw_fingerprint) == 64
            and all(item in "0123456789abcdef" for item in raw_fingerprint)
            else hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()
        )
        request = {
            "stable_contributor_key": stable_contributor_key,
            "cycle_seq": cycle_seq,
            "update_id": update_id,
            "object_identity": object_identity,
            "pointer_signature": pointer_signature,
            "pointer_sequence": pointer_sequence,
            "source_relative_path": source_relative_path,
            "status": result.status.value,
            "diagnostic": diagnostic,
            "fingerprint": fingerprint,
            "grace_seconds": float(grace_seconds),
            "operator_deadline_seconds": float(operator_deadline_seconds),
            "max_archived_signatures": max_archived_signatures,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._require_visibility_receipt(
                connection,
                stable_contributor_key=stable_contributor_key,
                cycle_seq=cycle_seq,
                update_id=update_id,
            )
            now = float(self._authority._wall_clock())
            newest = connection.execute(
                """
                SELECT * FROM proposal_visibility
                WHERE stable_contributor_key=? AND object_identity=?
                ORDER BY pointer_sequence DESC, visibility_id DESC LIMIT 1
                """,
                (stable_contributor_key, object_identity),
            ).fetchone()
            if newest is not None and pointer_sequence < int(newest["pointer_sequence"]):
                return {
                    "status": result.status.value,
                    "stable_failure_count": 0,
                    "terminal_disposition": None,
                    "observation_id": None,
                }
            if (
                newest is not None
                and pointer_sequence == int(newest["pointer_sequence"])
                and str(newest["pointer_signature"]) != pointer_signature
            ):
                collision_fingerprint = hashlib.sha256(
                    _canonical_json(
                        {
                            "object_identity": object_identity,
                            "pointer_sequence": pointer_sequence,
                            "existing_signature": str(newest["pointer_signature"]),
                            "incoming_signature": pointer_signature,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                collision_diagnostic = "pointer sequence was reused with a different signature"
                observation_id = self._record_visibility_terminal(
                    connection,
                    stable_contributor_key=stable_contributor_key,
                    cycle_seq=cycle_seq,
                    update_id=update_id,
                    pointer_sequence=pointer_sequence,
                    disposition=ProposalDisposition.IDENTITY_MISMATCH,
                    diagnostic=collision_diagnostic,
                    source_relative_path=source_relative_path,
                    fingerprint=collision_fingerprint,
                )
                self._advance_frontier_values(
                    connection,
                    stable_contributor_key=stable_contributor_key,
                    cycle_seq=cycle_seq,
                    observation_id=observation_id,
                )
                return {
                    "status": ReadStatus.IDENTITY_MISMATCH.value,
                    "stable_failure_count": 1,
                    "terminal_disposition": "identity_mismatch",
                    "observation_id": observation_id,
                }
            if newest is not None and str(newest["pointer_signature"]) != pointer_signature:
                connection.execute(
                    """
                    INSERT INTO proposal_visibility_archive(
                        stable_contributor_key, object_identity, pointer_signature,
                        last_read_status, stable_failure_count, last_fingerprint,
                        terminal_disposition, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stable_contributor_key, object_identity, pointer_signature)
                    DO UPDATE SET last_read_status=excluded.last_read_status,
                        stable_failure_count=excluded.stable_failure_count,
                        last_fingerprint=excluded.last_fingerprint,
                        terminal_disposition=excluded.terminal_disposition,
                        archived_at=excluded.archived_at
                    """,
                    (
                        stable_contributor_key,
                        object_identity,
                        newest["pointer_signature"],
                        newest["last_read_status"],
                        newest["stable_failure_count"],
                        newest["last_fingerprint"],
                        newest["terminal_disposition"],
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM proposal_visibility WHERE visibility_id=?",
                    (newest["visibility_id"],),
                )
                newest = None
            previous = connection.execute(
                """
                SELECT * FROM proposal_visibility
                WHERE stable_contributor_key=? AND object_identity=? AND pointer_signature=?
                """,
                (stable_contributor_key, object_identity, pointer_signature),
            ).fetchone()
            if previous is not None and previous["terminal_disposition"] is not None:
                return {
                    "status": result.status.value,
                    "stable_failure_count": int(previous["stable_failure_count"]),
                    "terminal_disposition": str(previous["terminal_disposition"]),
                    "observation_id": int(previous["terminal_observation_id"]),
                }
            first_observed = now if previous is None else float(previous["first_observed_at"])
            first_failure: float | None = None
            count = 0
            if result.status is ReadStatus.NOT_FOUND:
                same = previous is not None and previous["last_read_status"] == "not_found"
                count = int(previous["stable_failure_count"]) + 1 if same else 1
                first_failure = (
                    float(previous["first_stable_failure_at"])
                    if same and previous["first_stable_failure_at"] is not None
                    else now
                )
            elif result.status is ReadStatus.MALFORMED:
                same = (
                    previous is not None
                    and previous["last_read_status"] == "malformed"
                    and previous["last_fingerprint"] == fingerprint
                )
                count = int(previous["stable_failure_count"]) + 1 if same else 1
                first_failure = (
                    float(previous["first_stable_failure_at"])
                    if same and previous["first_stable_failure_at"] is not None
                    else now
                )
            elif result.status is ReadStatus.TRANSIENT_IO:
                same = previous is not None and previous["last_read_status"] == "transient_io"
                first_failure = (
                    float(previous["first_stable_failure_at"])
                    if same and previous["first_stable_failure_at"] is not None
                    else now
                )
            terminal: str | None = None
            if result.status is ReadStatus.IDENTITY_MISMATCH:
                terminal = "identity_mismatch"
            elif (
                result.status is ReadStatus.NOT_FOUND
                and count >= 3
                and first_failure is not None
                and now - first_failure >= grace_seconds
            ):
                terminal = "missing"
            elif (
                result.status is ReadStatus.MALFORMED
                and count >= 2
                and first_failure is not None
                and now - first_failure >= grace_seconds
            ):
                terminal = "malformed"
            elif (
                result.status is ReadStatus.TRANSIENT_IO
                and first_failure is not None
                and now - first_failure >= operator_deadline_seconds
            ):
                terminal = "manual_review"
            connection.execute(
                """
                INSERT INTO proposal_visibility(
                    stable_contributor_key, cycle_seq, update_id, object_identity,
                    pointer_signature, pointer_sequence, first_observed_at,
                    first_stable_failure_at, last_observed_at, stable_failure_count,
                    last_read_status, last_fingerprint, bounded_diagnostic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_contributor_key, object_identity, pointer_signature)
                DO UPDATE SET cycle_seq=excluded.cycle_seq, update_id=excluded.update_id,
                    pointer_sequence=excluded.pointer_sequence,
                    first_stable_failure_at=excluded.first_stable_failure_at,
                    last_observed_at=excluded.last_observed_at,
                    stable_failure_count=excluded.stable_failure_count,
                    last_read_status=excluded.last_read_status,
                    last_fingerprint=excluded.last_fingerprint,
                    bounded_diagnostic=excluded.bounded_diagnostic
                """,
                (
                    stable_contributor_key,
                    cycle_seq,
                    update_id,
                    object_identity,
                    pointer_signature,
                    pointer_sequence,
                    first_observed,
                    first_failure,
                    now,
                    count,
                    result.status.value,
                    fingerprint,
                    diagnostic,
                ),
            )
            observation_id: int | None = None
            if terminal is not None:
                disposition = {
                    "missing": ProposalDisposition.MISSING,
                    "malformed": ProposalDisposition.MALFORMED,
                    "identity_mismatch": ProposalDisposition.IDENTITY_MISMATCH,
                    "manual_review": ProposalDisposition.MANUAL_REVIEW,
                }[terminal]
                observation_id = self._record_visibility_terminal(
                    connection,
                    stable_contributor_key=stable_contributor_key,
                    cycle_seq=cycle_seq,
                    update_id=update_id,
                    pointer_sequence=pointer_sequence,
                    disposition=disposition,
                    diagnostic=diagnostic,
                    source_relative_path=source_relative_path,
                    fingerprint=fingerprint,
                )
                connection.execute(
                    """
                    UPDATE proposal_visibility SET terminal_disposition=?,
                        terminal_observation_id=?
                    WHERE stable_contributor_key=? AND object_identity=?
                        AND pointer_signature=?
                    """,
                    (
                        terminal,
                        observation_id,
                        stable_contributor_key,
                        object_identity,
                        pointer_signature,
                    ),
                )
                self._advance_frontier_values(
                    connection,
                    stable_contributor_key=stable_contributor_key,
                    cycle_seq=cycle_seq,
                    observation_id=observation_id,
                )
            connection.execute(
                """
                DELETE FROM proposal_visibility_archive
                WHERE archive_id IN (
                    SELECT archive_id FROM proposal_visibility_archive
                    WHERE stable_contributor_key=? ORDER BY archive_id DESC LIMIT -1 OFFSET ?
                )
                """,
                (stable_contributor_key, max_archived_signatures),
            )
            return {
                "status": result.status.value,
                "stable_failure_count": count,
                "terminal_disposition": terminal,
                "observation_id": observation_id,
            }

        payload = self._command(command_id, "observe_proposal_visibility", request, operation)
        return VisibilityDecision(
            status=ReadStatus(payload["status"]),
            stable_failure_count=int(payload["stable_failure_count"]),
            terminal_disposition=payload["terminal_disposition"],
            observation_id=(
                None if payload["observation_id"] is None else int(payload["observation_id"])
            ),
        )

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
        weight_theta_sha256: str,
        optim_theta_sha256: str,
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
            weight_theta_sha256=weight_theta_sha256,
            optim_theta_sha256=optim_theta_sha256,
        )
        committed = self.commit_merge(
            command_id=f"{command_id}-commit", publication_id=publication_id
        )
        if isinstance(committed, MergeFenceConflict):
            raise RuntimeError("v0 unexpectedly encountered a membership fence conflict")
        return committed

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

    def record_capacity_observation(
        self,
        *,
        command_id: str,
        observation_key: str,
        global_version: int,
        eligible_contributors: int,
        selected_contributors: int,
        productive_instances: int,
        reserved_launch_capacity: int,
        desired_contributors: int,
        action: str,
        retention_count: int,
    ) -> dict[str, Any]:
        """Persist one leader-fenced capacity sample and bound its recovery hot set."""

        validate_identity(observation_key, name="observation_key")
        numeric = {
            "global_version": global_version,
            "eligible_contributors": eligible_contributors,
            "selected_contributors": selected_contributors,
            "productive_instances": productive_instances,
            "reserved_launch_capacity": reserved_launch_capacity,
            "desired_contributors": desired_contributors,
        }
        for name, value in numeric.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if isinstance(retention_count, bool) or not isinstance(retention_count, int):
            raise ValueError("retention_count must be an integer")
        if retention_count < 1:
            raise ValueError("retention_count must be positive")
        if not action:
            raise ValueError("capacity action must not be empty")
        request = {
            "observation_key": observation_key,
            **numeric,
            "action": action,
            "retention_count": retention_count,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("capacity observations require dynamic membership")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(observation_seq), 0) + 1 FROM capacity_observations"
                ).fetchone()[0]
            )
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO capacity_observations(
                    observation_key, observation_seq, kind, global_version, observed_at,
                    eligible_contributors, selected_contributors, productive_instances,
                    reserved_launch_capacity, desired_contributors, action, command_epoch
                ) VALUES (?, ?, 'scheduler_window', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_key,
                    sequence,
                    global_version,
                    now,
                    eligible_contributors,
                    selected_contributors,
                    productive_instances,
                    reserved_launch_capacity,
                    desired_contributors,
                    action,
                    self.token.epoch,
                ),
            )
            connection.execute(
                """
                DELETE FROM capacity_observations
                WHERE observation_seq <= (
                    SELECT COALESCE(MAX(observation_seq), 0) - ? FROM capacity_observations
                )
                """,
                (retention_count,),
            )
            row = connection.execute(
                "SELECT * FROM capacity_observations WHERE observation_key=?",
                (observation_key,),
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._command(command_id, "record_capacity_observation", request, operation)

    def plan_dynamic_launch_request(
        self,
        *,
        command_id: str,
        request_id: str,
        observation_key: str,
        stream_id: int,
        replace_instance_id: str | None,
        reason: str,
        expires_at: float,
        max_pending_requests: int,
        max_total_requests: int,
        expected_scheduler_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve one stream and, for proven loss, fence its old incarnation."""

        validate_identity(request_id, name="request_id")
        validate_identity(observation_key, name="observation_key")
        if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
            raise ValueError("stream_id must be a non-negative integer")
        if replace_instance_id is not None:
            validate_identity(replace_instance_id, name="replace_instance_id")
        if expected_scheduler_job_id is not None:
            validate_identity(expected_scheduler_job_id, name="expected_scheduler_job_id")
        if not reason:
            raise ValueError("launch reason must not be empty")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            raise ValueError("expires_at must be a finite timestamp")
        if not (float("-inf") < float(expires_at) < float("inf")):
            raise ValueError("expires_at must be a finite timestamp")
        for name, value in (
            ("max_pending_requests", max_pending_requests),
            ("max_total_requests", max_total_requests),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        request = {
            "request_id": request_id,
            "observation_key": observation_key,
            "stream_id": stream_id,
            "replace_instance_id": replace_instance_id,
            "reason": reason,
            "expires_at": float(expires_at),
            "max_pending_requests": max_pending_requests,
            "max_total_requests": max_total_requests,
            "expected_scheduler_job_id": expected_scheduler_job_id,
        }
        request_sha256 = hashlib.sha256(_canonical_json(request).encode("utf-8")).hexdigest()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("dynamic launch requests require dynamic membership")
            controller = connection.execute(
                "SELECT state FROM controller_state WHERE singleton=1"
            ).fetchone()
            if controller is None or controller["state"] != "open":
                raise RuntimeError("dynamic launch planning requires an open controller")
            if (
                connection.execute(
                    "SELECT 1 FROM capacity_observations WHERE observation_key=?",
                    (observation_key,),
                ).fetchone()
                is None
            ):
                raise RuntimeError("capacity observation is missing")
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM launch_requests WHERE role<>'bootstrap'"
                ).fetchone()[0]
            )
            pending = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM launch_requests
                    WHERE role<>'bootstrap' AND reservation_released_at IS NULL
                    """
                ).fetchone()[0]
            )
            if total >= max_total_requests or pending >= max_pending_requests:
                raise RuntimeError("dynamic launch request budget is exhausted")
            if (
                connection.execute(
                    """
                SELECT 1 FROM launch_requests
                WHERE role<>'bootstrap' AND stream_id=?
                    AND reservation_released_at IS NULL
                """,
                    (stream_id,),
                ).fetchone()
                is not None
            ):
                raise MembershipFenceError("dynamic stream already has a launch reservation")
            stream = connection.execute(
                "SELECT * FROM streams WHERE stream_id=?", (stream_id,)
            ).fetchone()
            if stream is None:
                raise RuntimeError("dynamic launch stream is missing")
            now = float(self._authority._wall_clock())
            if float(expires_at) <= now:
                raise RuntimeError("dynamic launch request is already expired")
            role = "scale_out"
            if replace_instance_id is None:
                if stream["state"] != "available" or stream["current_instance_id"] is not None:
                    raise MembershipFenceError("scale-out stream is not available")
            else:
                role = "replacement"
                if stream["current_instance_id"] != replace_instance_id:
                    raise MembershipFenceError("replacement does not name the current stream owner")
                instance = connection.execute(
                    "SELECT * FROM learner_instances WHERE instance_id=?",
                    (replace_instance_id,),
                ).fetchone()
                if instance is None or instance["status"] != "admitted":
                    raise MembershipFenceError("replacement source is not an admitted instance")
                if expected_scheduler_job_id is None or (
                    instance["pbs_job_id"] != expected_scheduler_job_id
                ):
                    raise MembershipFenceError(
                        "replacement requires exact terminal scheduler job evidence"
                    )
                self._retire_dynamic_in_transaction(
                    connection,
                    fence=self._dynamic_fence_from_instance(instance),
                    reason=reason,
                    final_status="expired",
                )
            connection.execute(
                """
                INSERT INTO launch_requests(
                    request_id, observation_key, bootstrap_slot, role, reason, stream_id,
                    replace_instance_id, requested_by_epoch, state, request_sha256,
                    created_at, updated_at, not_before, submission_attempts, expires_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, 0, ?)
                """,
                (
                    request_id,
                    observation_key,
                    role,
                    reason,
                    stream_id,
                    replace_instance_id,
                    self.token.epoch,
                    request_sha256,
                    now,
                    now,
                    now,
                    float(expires_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._command(command_id, "plan_dynamic_launch_request", request, operation)

    def transition_dynamic_launch_request(
        self,
        *,
        command_id: str,
        request_id: str,
        expected_state: str,
        state: str,
        pbs_job_id: str | None,
        scheduler_state: str | None,
        evidence_source: str,
        uncertainty_timeout_seconds: float | None = None,
        terminal_evidence: bool = False,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        """CAS one scheduler transition while preserving uncertainty reservations."""

        validate_identity(request_id, name="request_id")
        transitions = {
            "planned": {"submitting", "expired"},
            "submitting": {"submission_unknown", "submitted", "failed"},
            "submission_unknown": {"submitted", "terminal_uncertain", "failed"},
            "submitted": {"started", "terminal_uncertain", "failed"},
            "started": {"terminal_uncertain", "failed"},
            "terminal_uncertain": {
                "submitted",
                "started",
                "failed",
                "expired",
                "manual_review",
            },
            "manual_review": set(),
            "admitted": set(),
            "failed": set(),
            "expired": set(),
        }
        if expected_state not in transitions or state not in transitions[expected_state]:
            raise ValueError(f"invalid scheduler transition: {expected_state} -> {state}")
        if pbs_job_id is not None:
            validate_identity(pbs_job_id, name="pbs_job_id")
        if not evidence_source:
            raise ValueError("scheduler evidence_source must not be empty")
        uncertain = state in {"submission_unknown", "terminal_uncertain"}
        if uncertain and (
            uncertainty_timeout_seconds is None or uncertainty_timeout_seconds <= 0.0
        ):
            raise ValueError("scheduler uncertainty requires a positive persistent timeout")
        if terminal_evidence and state != "failed":
            raise ValueError("terminal scheduler evidence may only prove a failed launch")
        request = {
            "request_id": request_id,
            "expected_state": expected_state,
            "state": state,
            "pbs_job_id": pbs_job_id,
            "scheduler_state": scheduler_state,
            "evidence_source": evidence_source,
            "uncertainty_timeout_seconds": uncertainty_timeout_seconds,
            "terminal_evidence": terminal_evidence,
            "last_error": last_error,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("scheduler transitions require dynamic membership")
            row = connection.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or row["state"] != expected_state:
                raise RuntimeError("dynamic launch request state changed")
            now = float(self._authority._wall_clock())
            if state in {"failed", "expired", "manual_review"} and expected_state in {
                "submission_unknown",
                "terminal_uncertain",
            }:
                if not terminal_evidence and (
                    row["uncertainty_deadline"] is None or now < float(row["uncertainty_deadline"])
                ):
                    raise RuntimeError("scheduler uncertainty deadline has not elapsed")
            if state == "expired" and row["expires_at"] is not None:
                if now < float(row["expires_at"]):
                    raise RuntimeError("launch request TTL has not elapsed")
            positive = state in {"submitted", "started"}
            deadline = (
                now + float(uncertainty_timeout_seconds)
                if uncertain and uncertainty_timeout_seconds is not None
                else None
            )
            releases_reservation = state in {"failed", "expired"}
            connection.execute(
                """
                UPDATE launch_requests SET state=?,
                    submission_attempts=submission_attempts + ?,
                    pbs_job_id=COALESCE(?, pbs_job_id), scheduler_state=?,
                    scheduler_observed_at=?,
                    first_uncertain_at=CASE WHEN ? THEN NULL
                        WHEN ? THEN COALESCE(first_uncertain_at, ?)
                        ELSE first_uncertain_at END,
                    last_positive_evidence_at=CASE WHEN ? THEN ?
                        ELSE last_positive_evidence_at END,
                    uncertainty_deadline=CASE WHEN ? THEN NULL
                        WHEN ? THEN COALESCE(uncertainty_deadline, ?)
                        ELSE uncertainty_deadline END,
                    reservation_released_at=CASE WHEN ?
                        THEN COALESCE(reservation_released_at, ?) ELSE reservation_released_at END,
                    evidence_source=?, last_error=?, requested_by_epoch=?, updated_at=?
                WHERE request_id=? AND state=?
                """,
                (
                    state,
                    int(expected_state == "planned" and state == "submitting"),
                    pbs_job_id,
                    scheduler_state,
                    now,
                    int(positive),
                    int(uncertain),
                    now,
                    int(positive),
                    now,
                    int(positive),
                    int(uncertain),
                    deadline,
                    int(releases_reservation),
                    now,
                    evidence_source,
                    last_error,
                    self.token.epoch,
                    now,
                    request_id,
                    expected_state,
                ),
            )
            result = connection.execute(
                "SELECT * FROM launch_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert result is not None
            return dict(result)

        return self._command(command_id, "transition_dynamic_launch_request", request, operation)

    def apply_scheduler_operator_request(
        self,
        *,
        command_id: str,
        operator_request: SchedulerOperatorRequest,
    ) -> dict[str, Any]:
        request = operator_request.as_dict()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("scheduler launch requests require dynamic membership")
            table = "launch_requests"
            row = connection.execute(
                "SELECT * FROM launch_requests WHERE request_id=?",
                (operator_request.launch_request_id,),
            ).fetchone()
            job_column = "pbs_job_id"
            if row is None:
                raise RuntimeError("operator request names an unknown launch request")
            state_row = dict(row)
            state_row["pbs_job_id"] = row[job_column]
            actual_state_sha = scheduler_state_sha256(state_row)
            stale = actual_state_sha != operator_request.expected_state_sha256
            now = float(self._authority._wall_clock())
            if stale:
                next_state = str(row["state"])
                request_state = "stale_rejected"
            else:
                # This is an explicit operator override, not an automatic scheduler
                # transition.  The immutable expected-state hash is its CAS fence and
                # scheduler_operator_requests is its durable audit trail.
                if str(row["state"]) not in {
                    "submission_unknown",
                    "terminal_uncertain",
                    "manual_review",
                }:
                    raise RuntimeError("operator request can only resolve scheduler uncertainty")
                action = operator_request.action
                if action is SchedulerOperatorAction.CONFIRM_JOB_ID:
                    next_state = "submitted"
                    connection.execute(
                        f"""
                        UPDATE {table} SET state='submitted', {job_column}=?,
                            first_uncertain_at=NULL, uncertainty_deadline=NULL,
                            last_positive_evidence_at=?, evidence_source=?, updated_at=?
                        WHERE request_id=?
                        """,
                        (
                            operator_request.scheduler_job_id,
                            now,
                            operator_request.evidence_source or "operator_confirmed_job_id",
                            now,
                            operator_request.launch_request_id,
                        ),
                    )
                elif action is SchedulerOperatorAction.MARK_FAILED:
                    next_state = "failed"
                    connection.execute(
                        f"""
                        UPDATE {table} SET state=?, manual_reason=?, updated_at=?,
                            reservation_released_at=COALESCE(reservation_released_at, ?)
                        WHERE request_id=?
                        """,
                        (
                            next_state,
                            operator_request.reason,
                            now,
                            now,
                            operator_request.launch_request_id,
                        ),
                    )
                elif action is SchedulerOperatorAction.MARK_EXPIRED:
                    next_state = "expired"
                    connection.execute(
                        f"""
                        UPDATE {table} SET state=?, manual_reason=?, updated_at=?,
                            reservation_released_at=COALESCE(reservation_released_at, ?)
                        WHERE request_id=?
                        """,
                        (
                            next_state,
                            operator_request.reason,
                            now,
                            now,
                            operator_request.launch_request_id,
                        ),
                    )
                else:
                    next_state = "manual_review"
                    connection.execute(
                        f"""
                        UPDATE {table} SET state='manual_review', manual_reason=?,
                            evidence_source=?, updated_at=? WHERE request_id=?
                        """,
                        (
                            operator_request.reason,
                            operator_request.evidence_source or "external_cancel_evidence",
                            now,
                            operator_request.launch_request_id,
                        ),
                    )
                request_state = "applied"
            connection.execute(
                """
                INSERT INTO scheduler_operator_requests(
                    request_id, launch_request_id, action, expected_state_sha256,
                    reason, scheduler_job_id, evidence_source, request_sha256,
                    state, result_state, processed_by_epoch, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operator_request.request_id,
                    operator_request.launch_request_id,
                    operator_request.action.value,
                    operator_request.expected_state_sha256,
                    operator_request.reason,
                    operator_request.scheduler_job_id,
                    operator_request.evidence_source,
                    operator_request.immutable_sha256(),
                    request_state,
                    next_state,
                    self.token.epoch,
                    now,
                ),
            )
            return {
                "request_state": request_state,
                "launch_state": next_state,
                "actual_state_sha256": actual_state_sha,
            }

        return self._command(command_id, "apply_scheduler_operator_request", request, operation)

    def record_scheduler_operator_file_disposition(
        self,
        *,
        command_id: str,
        relative_path: str,
        content_sha256: str,
        disposition: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record one immutable operator-file observation so scans stay bounded."""

        path = PurePosixPath(relative_path)
        if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
            raise ValueError("scheduler operator relative path must be one file name")
        if len(content_sha256) != 64 or any(
            item not in "0123456789abcdef" for item in content_sha256
        ):
            raise ValueError("scheduler operator content digest must be lowercase SHA-256")
        if disposition not in {"applied", "rejected"}:
            raise ValueError("scheduler operator disposition must be applied or rejected")
        if not reason:
            raise ValueError("scheduler operator disposition reason must not be empty")
        request = {
            "relative_path": relative_path,
            "content_sha256": content_sha256,
            "disposition": disposition,
            "reason": reason,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("scheduler operator dispositions require dynamic membership")
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO scheduler_operator_file_dispositions(
                    relative_path, content_sha256, disposition, reason,
                    processed_by_epoch, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    relative_path,
                    content_sha256,
                    disposition,
                    reason,
                    self.token.epoch,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM scheduler_operator_file_dispositions
                WHERE relative_path=? AND content_sha256=?
                """,
                (relative_path, content_sha256),
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._command(
            command_id,
            "record_scheduler_operator_file_disposition",
            request,
            operation,
        )

    def admit_dynamic_incarnation(
        self,
        *,
        command_id: str,
        instance_id: str,
        placement_id: str,
        stream_id: int,
        admission_token_sha256: str,
        hostname: str,
        pid: int,
        pbs_job_id: str | None = None,
        bootstrap_slot: int | None = None,
        launch_request_id: str | None = None,
        replace_instance_id: str | None = None,
        replacement_reason: str | None = None,
        registration_created_at: float | None = None,
    ) -> DynamicAdmission:
        """Admit one current dynamic incarnation, optionally replacing one exact owner."""

        validate_identity(instance_id, name="instance_id")
        validate_identity(placement_id, name="placement_id")
        validate_identity(hostname, name="hostname")
        if launch_request_id is not None:
            validate_identity(launch_request_id, name="launch_request_id")
        if pbs_job_id is not None:
            validate_identity(pbs_job_id, name="pbs_job_id")
        if replace_instance_id is not None:
            validate_identity(replace_instance_id, name="replace_instance_id")
        if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
            raise ValueError("stream_id must be a non-negative integer")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise ValueError("pid must be a non-negative integer")
        if registration_created_at is not None and (
            isinstance(registration_created_at, bool)
            or not isinstance(registration_created_at, (int, float))
            or not float("-inf") < float(registration_created_at) < float("inf")
        ):
            raise ValueError("registration_created_at must be a finite timestamp")
        if bootstrap_slot is not None and (
            isinstance(bootstrap_slot, bool)
            or not isinstance(bootstrap_slot, int)
            or bootstrap_slot < 0
        ):
            raise ValueError("bootstrap_slot must be a non-negative integer")
        if len(admission_token_sha256) != 64 or any(
            item not in "0123456789abcdef" for item in admission_token_sha256
        ):
            raise ValueError("admission_token_sha256 must be a lowercase SHA-256 digest")
        if replacement_reason is not None and not replacement_reason:
            raise ValueError("replacement_reason must not be empty")
        if replace_instance_id is not None and launch_request_id is None:
            raise ValueError("dynamic replacement requires an explicit launch request ID")
        if launch_request_id is not None and bootstrap_slot is not None:
            raise ValueError("dynamic admission cannot be both bootstrap and launch-authorized")
        effective_bootstrap_slot = (
            stream_id if launch_request_id is None and bootstrap_slot is None else bootstrap_slot
        )
        request = {
            "instance_id": instance_id,
            "placement_id": placement_id,
            "stream_id": stream_id,
            "admission_token_sha256": admission_token_sha256,
            "hostname": hostname,
            "pid": pid,
            "pbs_job_id": pbs_job_id,
            "bootstrap_slot": effective_bootstrap_slot,
            "launch_request_id": launch_request_id,
            "replace_instance_id": replace_instance_id,
            "replacement_reason": replacement_reason,
            "registration_created_at": registration_created_at,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if not isinstance(self._authority._scope, DynamicMembershipScope):
                raise RuntimeError("dynamic admission requires dynamic authority mode")
            controller = connection.execute(
                "SELECT state, requested_at FROM controller_state WHERE singleton=1"
            ).fetchone()
            if controller is None or controller["state"] not in {"open", "preclosing"}:
                raise MembershipFenceError("dynamic admission is closed")
            if controller["state"] == "preclosing" and (
                registration_created_at is None
                or controller["requested_at"] is None
                or float(registration_created_at) > float(controller["requested_at"])
            ):
                raise MembershipFenceError("dynamic registration is after the preclose cutoff")
            if stream_id >= self._authority._scope.stream_pool_size:
                raise MembershipFenceError("stream_id is outside the configured pool")
            stream = connection.execute(
                "SELECT * FROM streams WHERE stream_id=?", (stream_id,)
            ).fetchone()
            if stream is None:
                raise AuthoritySchemaError("dynamic stream pool is not initialized")
            existing_instance = connection.execute(
                "SELECT * FROM learner_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if existing_instance is not None:
                fence = self._dynamic_fence_from_instance(existing_instance)
                self._require_current_fence(connection, fence, allow_draining=True)
                if (
                    fence.placement_id != placement_id
                    or fence.stream_id != stream_id
                    or fence.admission_token_sha256 != admission_token_sha256
                    or existing_instance["launch_request_id"] != launch_request_id
                    or existing_instance["hostname"] != hostname
                    or int(existing_instance["pid"]) != pid
                    or ((existing_instance["pbs_job_id"] is None) != (pbs_job_id is None))
                    or (
                        existing_instance["pbs_job_id"] is not None
                        and str(existing_instance["pbs_job_id"]).split(".", 1)[0]
                        != str(pbs_job_id).split(".", 1)[0]
                    )
                ):
                    raise MembershipFenceError("instance ID was replayed with different admission")
                return self._dynamic_admission_result(connection, fence=fence)
            launch_row = None
            if launch_request_id is not None:
                launch_row = connection.execute(
                    "SELECT * FROM launch_requests WHERE request_id=?",
                    (launch_request_id,),
                ).fetchone()
                if launch_row is None:
                    raise MembershipFenceError("dynamic launch authorization is missing")
                if (
                    int(launch_row["stream_id"]) != stream_id
                    or launch_row["replace_instance_id"] != replace_instance_id
                    or launch_row["state"]
                    not in {
                        "submitting",
                        "submission_unknown",
                        "submitted",
                        "started",
                        "terminal_uncertain",
                    }
                ):
                    raise MembershipFenceError("dynamic launch authorization does not match")
                if launch_row["pbs_job_id"] is None:
                    raise RuntimeError("dynamic launch scheduler job evidence is pending")
                if pbs_job_id is None or (
                    str(launch_row["pbs_job_id"]).split(".", 1)[0] != pbs_job_id.split(".", 1)[0]
                ):
                    raise MembershipFenceError("dynamic launch scheduler job does not match")
            else:
                if effective_bootstrap_slot != stream_id:
                    raise MembershipFenceError("bootstrap slot must equal its stream ID")
                prior_bootstrap = connection.execute(
                    "SELECT * FROM launch_requests WHERE bootstrap_slot=?",
                    (effective_bootstrap_slot,),
                ).fetchone()
                if prior_bootstrap is not None:
                    raise MembershipFenceError("bootstrap slot was already consumed")
            placement = connection.execute(
                "SELECT * FROM placements WHERE placement_id=?", (placement_id,)
            ).fetchone()
            occupied = {
                str(value)
                for value in (
                    None if placement is None else placement["current_instance_id"],
                    stream["current_instance_id"],
                )
                if value is not None
            }
            if len(occupied) > 1:
                raise AuthoritySchemaError("placement and stream have different current owners")
            if occupied:
                current_instance_id = next(iter(occupied))
                if replace_instance_id != current_instance_id or replacement_reason is None:
                    raise MembershipFenceError(
                        "occupied dynamic admission requires exact replacement authorization"
                    )
                current = connection.execute(
                    "SELECT * FROM learner_instances WHERE instance_id=?",
                    (current_instance_id,),
                ).fetchone()
                if current is None:
                    raise AuthoritySchemaError("current dynamic instance row is missing")
                self._retire_dynamic_in_transaction(
                    connection,
                    fence=self._dynamic_fence_from_instance(current),
                    reason=replacement_reason,
                    final_status="revoked",
                )
                placement = connection.execute(
                    "SELECT * FROM placements WHERE placement_id=?", (placement_id,)
                ).fetchone()
                stream = connection.execute(
                    "SELECT * FROM streams WHERE stream_id=?", (stream_id,)
                ).fetchone()
                assert stream is not None
            now = float(self._authority._wall_clock())
            if placement is None:
                placement_epoch = 1
                connection.execute(
                    """
                    INSERT INTO placements(
                        placement_id, current_placement_epoch, current_instance_id, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (placement_id, placement_epoch, instance_id, now),
                )
            else:
                placement_epoch = int(placement["current_placement_epoch"]) + 1
                connection.execute(
                    """
                    UPDATE placements SET current_placement_epoch=?, current_instance_id=?,
                        reusable_stream_id=?, updated_at=? WHERE placement_id=?
                    """,
                    (placement_epoch, instance_id, stream_id, now, placement_id),
                )
            prior_stream_uses = int(
                connection.execute(
                    "SELECT COUNT(*) FROM learner_instances WHERE stream_id=?", (stream_id,)
                ).fetchone()[0]
            )
            stream_epoch = int(stream["current_stream_epoch"]) + (1 if prior_stream_uses else 0)
            generation = (
                int(
                    connection.execute(
                        """
                    SELECT COALESCE(MAX(admission_generation), 0) FROM learner_instances
                    WHERE placement_id=?
                    """,
                        (placement_id,),
                    ).fetchone()[0]
                )
                + 1
            )
            connection.execute(
                """
                INSERT INTO learner_instances(
                    instance_id, placement_id, placement_epoch, stream_id, stream_epoch,
                    admission_generation, admission_token_sha256, launch_request_id,
                    pbs_job_id, hostname, pid, status, registered_at, admitted_at, last_seen,
                    admitted_by_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    placement_id,
                    placement_epoch,
                    stream_id,
                    stream_epoch,
                    generation,
                    admission_token_sha256,
                    launch_request_id,
                    pbs_job_id,
                    hostname,
                    pid,
                    now,
                    now,
                    now,
                    self.token.epoch,
                ),
            )
            connection.execute(
                """
                UPDATE streams SET current_stream_epoch=?, current_instance_id=?,
                    state='active', updated_at=? WHERE stream_id=?
                """,
                (stream_epoch, instance_id, now, stream_id),
            )
            connection.execute(
                """
                INSERT INTO admission_history(
                    instance_id, stream_id, stream_epoch, placement_id, placement_epoch,
                    admission_generation, event, reason, command_epoch, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'admitted', NULL, ?, ?)
                """,
                (
                    instance_id,
                    stream_id,
                    stream_epoch,
                    placement_id,
                    placement_epoch,
                    generation,
                    self.token.epoch,
                    now,
                ),
            )
            if launch_row is None:
                bootstrap_request_id = f"bootstrap-{effective_bootstrap_slot}"
                bootstrap_request = {
                    "bootstrap_slot": effective_bootstrap_slot,
                    "stream_id": stream_id,
                    "instance_id": instance_id,
                    "pbs_job_id": pbs_job_id,
                }
                connection.execute(
                    """
                    INSERT INTO launch_requests(
                        request_id, observation_key, bootstrap_slot, role, reason, stream_id,
                        replace_instance_id, requested_by_epoch, state, request_sha256,
                        created_at, updated_at, not_before, submission_attempts, pbs_job_id,
                        scheduler_state, scheduler_observed_at, last_positive_evidence_at,
                        reservation_released_at, evidence_source, admitted_instance_id, expires_at
                    ) VALUES (?, NULL, ?, 'bootstrap', 'initial_bootstrap', ?, NULL, ?,
                        'admitted', ?, ?, ?, ?, 0, ?, 'admitted', ?, ?, ?,
                        'registration', ?, NULL)
                    """,
                    (
                        bootstrap_request_id,
                        effective_bootstrap_slot,
                        stream_id,
                        self.token.epoch,
                        hashlib.sha256(
                            _canonical_json(bootstrap_request).encode("utf-8")
                        ).hexdigest(),
                        now,
                        now,
                        now,
                        pbs_job_id,
                        now,
                        now,
                        now,
                        instance_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE launch_requests SET state='admitted', admitted_instance_id=?,
                        scheduler_state='admitted', scheduler_observed_at=?,
                        first_uncertain_at=NULL, uncertainty_deadline=NULL,
                        last_positive_evidence_at=?, reservation_released_at=?,
                        evidence_source='registration', updated_at=?
                    WHERE request_id=? AND state IN (
                        'submitting', 'submission_unknown', 'submitted', 'started',
                        'terminal_uncertain'
                    )
                    """,
                    (instance_id, now, now, now, now, launch_request_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise MembershipFenceError("dynamic launch admission lost its authorization")
            fence = DynamicContributorFence(
                kind="dynamic",
                instance_id=instance_id,
                placement_id=placement_id,
                placement_epoch=placement_epoch,
                stream_id=stream_id,
                stream_epoch=stream_epoch,
                admission_generation=generation,
                admission_token_sha256=admission_token_sha256,
            )
            return self._dynamic_admission_result(connection, fence=fence)

        result = self._command(command_id, "admit_dynamic_incarnation", request, operation)
        return DynamicAdmission(
            fence=DynamicContributorFence.from_dict(result["fence"]),
            resume=ContributorResumeState(
                cursor=int(result["resume_cursor"]),
                last_receipt_id=result["last_receipt_id"],
                last_receipt_sha256=result["last_receipt_sha256"],
                next_cycle_seq=int(result["next_cycle_seq"]),
                stream_epoch=int(result["fence"]["stream_epoch"]),
            ),
        )

    def retire_incarnation(
        self,
        *,
        command_id: str,
        fence: DynamicContributorFence,
        reason: str,
        final_status: str = "revoked",
        final_update_id: str | None = None,
    ) -> tuple[str, ...]:
        """Retire one current dynamic incarnation and terminalize its active proposals."""

        if final_status not in {"draining", "stopped", "revoked", "expired"}:
            raise ValueError("final_status must be draining, stopped, revoked, or expired")
        if (final_status == "draining") != (final_update_id is not None):
            raise ValueError("draining requires exactly one final_update_id")
        if final_update_id is not None:
            validate_identity(final_update_id, name="final_update_id")
        if not reason:
            raise ValueError("retirement reason must not be empty")
        request = {
            "fence": fence.as_dict(),
            "reason": reason,
            "final_status": final_status,
            "final_update_id": final_update_id,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            update_ids = self._retire_dynamic_in_transaction(
                connection,
                fence=fence,
                reason=reason,
                final_status=final_status,
                final_update_id=final_update_id,
            )
            return {"terminalized_update_ids": list(update_ids)}

        result = self._command(command_id, "retire_incarnation", request, operation)
        return tuple(str(item) for item in result["terminalized_update_ids"])

    def try_select_batch(
        self, *, command_id: str, quorum_min: int, quorum_max: int
    ) -> SelectionAttempt:
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
                WITH per_contributor AS (
                    SELECT u.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY u.stable_contributor_key
                            ORDER BY u.cycle_seq DESC, u.update_id ASC
                        ) AS proposal_rank
                    FROM updates AS u
                    WHERE u.status='pending' AND u.base_global_version <= ?
                )
                SELECT u.*, COALESCE(s.committed_credit, 0) AS selection_credit,
                    COALESCE(s.last_committed_version, -1) AS service_version
                FROM per_contributor AS u
                LEFT JOIN selection_state AS s
                    ON s.stable_contributor_key = u.stable_contributor_key
                WHERE u.proposal_rank=1
                ORDER BY selection_credit ASC, service_version ASC,
                    u.stable_contributor_key ASC
                """,
                (int(latest),),
            ).fetchall()
            valid_rows: list[sqlite3.Row] = []
            invalid_ids: list[str] = []
            seen_contributors: set[str] = set()
            for row in rows:
                update_id = str(row["update_id"])
                if not self._fence_is_current_json(
                    connection, str(row["fence_json"]), update_id=update_id
                ):
                    self._drop_active_update(
                        connection,
                        row,
                        reason="stale_fence_at_selection",
                    )
                    invalid_ids.append(update_id)
                    continue
                stable_key = str(row["stable_contributor_key"])
                if stable_key in seen_contributors:
                    continue
                seen_contributors.add(stable_key)
                valid_rows.append(row)
            eligible_count = len(valid_rows)
            selected_rows = valid_rows[:quorum_max]
            if len(selected_rows) < quorum_min:
                return {
                    "selected": False,
                    "invalid_update_ids": invalid_ids,
                    "eligible_contributors": eligible_count,
                }
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
            for reduction_order, row in enumerate(
                sorted(selected_rows, key=lambda item: str(item["stable_contributor_key"]))
            ):
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
            return {
                "selected": True,
                "batch_id": batch_id,
                "invalid_update_ids": invalid_ids,
                "eligible_contributors": eligible_count,
            }

        result = self._command(command_id, "try_select_batch", request, operation)
        batch = self._load_selection_batch(str(result["batch_id"])) if result["selected"] else None
        return SelectionAttempt(
            batch=batch,
            invalid_update_ids=tuple(str(item) for item in result["invalid_update_ids"]),
            eligible_contributors=int(result["eligible_contributors"]),
        )

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
        weight_theta_sha256: str,
        optim_theta_sha256: str,
    ) -> PublicationIntent:
        if weight_theta_sha256 != optim_theta_sha256:
            raise ValueError("weight and outer-state theta identities do not match")
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
            theta_sha256=weight_theta_sha256,
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
                    optim_sha256, theta_sha256, state, prepared_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
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
                    weight_theta_sha256,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO artifact_publications(
                    publication_id, artifact_kind, relative_path, size_bytes, sha256,
                    owning_epoch, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)
                """,
                (
                    (
                        publication_id,
                        "weight",
                        weight_relative_path,
                        weight_size,
                        weight_sha256,
                        self.token.epoch,
                        now,
                    ),
                    (
                        publication_id,
                        "outer_state",
                        optim_relative_path,
                        optim_size,
                        optim_sha256,
                        self.token.epoch,
                        now,
                    ),
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

    def commit_merge(
        self,
        *,
        command_id: str,
        publication_id: str,
        terminal_generation: int | None = None,
        terminal_merge_limit: int | None = None,
    ) -> CommittedVersion | MergeFenceConflict:
        if (terminal_generation is None) != (terminal_merge_limit is None):
            raise ValueError("terminal commit requires generation and merge limit together")
        if terminal_generation is not None and (
            isinstance(terminal_generation, bool)
            or not isinstance(terminal_generation, int)
            or terminal_generation < 1
        ):
            raise ValueError("terminal_generation must be a positive integer")
        if terminal_merge_limit is not None and (
            isinstance(terminal_merge_limit, bool)
            or not isinstance(terminal_merge_limit, int)
            or terminal_merge_limit < 0
        ):
            raise ValueError("terminal_merge_limit must be a non-negative integer")
        request = {
            "publication_id": publication_id,
            "terminal_generation": terminal_generation,
            "terminal_merge_limit": terminal_merge_limit,
        }
        self._authority._verify_token(self.token)
        self._verify_prepared_publication_artifacts(publication_id)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            if intent is None:
                raise ValueError("publication intent does not exist")
            if intent["state"] == "abandoned" and intent["selection_batch_id"] is not None:
                rows = connection.execute(
                    """
                    SELECT u.update_id, u.status FROM selection_batch_updates AS b
                    JOIN updates AS u ON u.update_id=b.update_id
                    WHERE b.batch_id=? ORDER BY b.reduction_order
                    """,
                    (intent["selection_batch_id"],),
                ).fetchall()
                return {
                    "outcome": "fence_conflict",
                    "publication_id": publication_id,
                    "invalid_update_ids": [
                        str(row["update_id"]) for row in rows if row["status"] == "dropped"
                    ],
                    "reset_pending_update_ids": [
                        str(row["update_id"]) for row in rows if row["status"] == "pending"
                    ],
                }
            if intent["state"] != "prepared":
                raise ValueError("publication intent is not prepared")
            target = int(intent["target_version"])
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            expected = 0 if latest is None else int(latest) + 1
            if target != expected:
                raise ValueError(f"commit target must be the next version {expected}")
            controller = connection.execute(
                "SELECT * FROM controller_state WHERE singleton=1"
            ).fetchone()
            if controller is None:
                raise AuthoritySchemaError("controller state is missing")
            terminal_commit = terminal_generation is not None
            if terminal_commit:
                assert terminal_merge_limit is not None
                if (
                    controller["state"] not in {"closing", "draining"}
                    or int(controller["generation"]) != terminal_generation
                ):
                    raise MembershipFenceError("terminal merge generation is not current")
                if int(controller["terminal_merge_count"]) >= terminal_merge_limit:
                    raise RuntimeError("terminal merge budget is exhausted")
            elif target > 0 and controller["state"] != "open":
                raise MembershipFenceError("normal merge is closed by terminal intent")
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
                invalid_ids = [
                    str(row["update_id"])
                    for row in selected_rows
                    if row["status"] != "selected"
                    or not self._fence_is_current_json(
                        connection,
                        str(row["fence_json"]),
                        update_id=str(row["update_id"]),
                    )
                ]
                if invalid_ids:
                    reset_ids = self._reconcile_invalid_batch(
                        connection,
                        batch_id=str(batch_id),
                        invalid_update_ids=tuple(invalid_ids),
                        reason="stale_fence_at_commit",
                    )
                    return {
                        "outcome": "fence_conflict",
                        "publication_id": publication_id,
                        "invalid_update_ids": invalid_ids,
                        "reset_pending_update_ids": list(reset_ids),
                    }
            direct_tokens = sum(int(row["effective_tokens_this_update"]) for row in selected_rows)
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO global_versions(
                    version, predecessor_version, publication_id, weight_relative_path,
                    weight_size, weight_sha256, optim_relative_path, optim_size,
                    optim_sha256, committed_by_epoch, committed_by_owner_id, committed_at,
                    theta_sha256, direct_weight_tokens_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    intent["theta_sha256"],
                    direct_tokens,
                ),
            )
            _publication_commit_boundary("version_insert")
            connection.execute(
                """
                UPDATE publication_intents SET state='committed', committed_at=?
                WHERE publication_id=? AND state='prepared'
                """,
                (now, publication_id),
            )
            connection.execute(
                """
                UPDATE artifact_publications SET state='committed'
                WHERE publication_id=? AND state='prepared'
                """,
                (publication_id,),
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
                    self._transition_token_fate(
                        connection,
                        receipt_id=str(row["cycle_receipt_id"]),
                        fate="applied",
                        reason="selected_commit",
                        now=now,
                        applied_version=target,
                    )
                _publication_commit_boundary("proposal_transition")
            if terminal_commit:
                connection.execute(
                    """
                    UPDATE controller_state
                    SET terminal_merge_count=terminal_merge_count+1,
                        updated_by_epoch=?, updated_by_owner_id=?
                    WHERE singleton=1 AND generation=?
                    """,
                    (self.token.epoch, self.token.owner_id, terminal_generation),
                )
            result = connection.execute(
                "SELECT * FROM global_versions WHERE version=?", (target,)
            ).fetchone()
            assert result is not None
            _publication_commit_boundary("db_commit")
            return {"outcome": "committed", "version": dict(result)}

        result = self._command(command_id, "commit_merge", request, operation)
        if result["outcome"] == "fence_conflict":
            return MergeFenceConflict(
                publication_id=str(result["publication_id"]),
                invalid_update_ids=tuple(str(item) for item in result["invalid_update_ids"]),
                reset_pending_update_ids=tuple(
                    str(item) for item in result["reset_pending_update_ids"]
                ),
            )
        return _decode_committed_version(result["version"])

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
            self._abandon_publication_artifacts(
                connection, publication_id=publication_id, reason=reason, now=now
            )
            if row["selection_batch_id"] is not None:
                self._reconcile_invalid_batch(
                    connection,
                    batch_id=str(row["selection_batch_id"]),
                    invalid_update_ids=(),
                    reason=reason,
                )
            result = connection.execute(
                "SELECT * FROM publication_intents WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            assert result is not None
            return dict(result)

        result = self._command(command_id, "abandon_publication", request, operation)
        return _decode_publication_intent(result)

    def reconcile_publications(self, *, command_id: str) -> tuple[str, ...]:
        """Abandon prepared intents owned by an expired predecessor epoch."""

        request: dict[str, Any] = {}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            rows = connection.execute(
                """
                SELECT * FROM publication_intents
                WHERE state='prepared' AND owner_epoch<>? ORDER BY publication_id
                """,
                (self.token.epoch,),
            ).fetchall()
            reconciled: list[str] = []
            now = float(self._authority._wall_clock())
            for row in rows:
                publication_id = str(row["publication_id"])
                if row["selection_batch_id"] is None:
                    self._abandon_publication_artifacts(
                        connection,
                        publication_id=publication_id,
                        reason="predecessor_epoch_expired",
                        now=now,
                    )
                else:
                    self._reconcile_invalid_batch(
                        connection,
                        batch_id=str(row["selection_batch_id"]),
                        invalid_update_ids=(),
                        reason="predecessor_epoch_expired",
                    )
                reconciled.append(publication_id)
            return {"publication_ids": reconciled}

        result = self._command(command_id, "reconcile_publications", request, operation)
        return tuple(str(item) for item in result["publication_ids"])

    def claim_orphan_gc(self, *, command_id: str, limit: int = 64) -> tuple[dict[str, Any], ...]:
        """Claim only lease-safe artifact paths with immutable deletion identity."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("orphan GC limit must be a positive integer")
        request = {"limit": limit}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            now = float(self._authority._wall_clock())
            rows = connection.execute(
                """
                SELECT relative_path, size_bytes, sha256 FROM gc_candidates
                WHERE not_before<=?
                  AND (state='pending'
                       OR (state='claimed' AND claimed_by_epoch<>?))
                ORDER BY not_before, relative_path LIMIT ?
                """,
                (now, self.token.epoch, limit),
            ).fetchall()
            candidates = [
                {
                    "relative_path": str(row["relative_path"]),
                    "size_bytes": int(row["size_bytes"]),
                    "sha256": str(row["sha256"]),
                }
                for row in rows
            ]
            for candidate in candidates:
                connection.execute(
                    """
                    UPDATE gc_candidates
                    SET state='claimed', claimed_by_epoch=?, claimed_at=?
                    WHERE relative_path=?
                      AND (state='pending'
                           OR (state='claimed' AND claimed_by_epoch<>?))
                    """,
                    (
                        self.token.epoch,
                        now,
                        candidate["relative_path"],
                        self.token.epoch,
                    ),
                )
            return {"candidates": candidates}

        result = self._command(command_id, "claim_orphan_gc", request, operation)
        return tuple(dict(item) for item in result["candidates"])

    def complete_artifact_gc(
        self, *, command_id: str, relative_paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        paths = tuple(sorted(relative_paths))
        if not paths or len(set(paths)) != len(paths):
            raise ValueError("artifact GC completion requires unique paths")
        for relative_path in paths:
            if relative_path.startswith("/") or ".." in Path(relative_path).parts:
                raise ValueError("artifact GC completion path is invalid")
            try:
                (self._authority._run_root / relative_path).lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("artifact GC object still exists")
        request = {"relative_paths": list(paths)}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            now = float(self._authority._wall_clock())
            for relative_path in paths:
                cursor = connection.execute(
                    """
                    UPDATE gc_candidates SET state='deleted', deleted_at=?
                    WHERE relative_path=? AND state='claimed' AND claimed_by_epoch=?
                    """,
                    (now, relative_path, self.token.epoch),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("artifact GC candidate is not claimed")
                connection.execute(
                    "DELETE FROM gc_candidates WHERE relative_path=?", (relative_path,)
                )
            return {"relative_paths": list(paths)}

        result = self._command(command_id, "complete_artifact_gc", request, operation)
        return tuple(str(item) for item in result["relative_paths"])

    def archive_audit_batch(
        self,
        *,
        command_id: str,
        batch_id: str,
        cutoff_version: int,
        relative_path: str,
        sha256: str,
    ) -> dict[str, Any]:
        """Register a verified immutable history batch, then prune its exact rows."""

        validate_identity(batch_id, name="batch_id")
        if isinstance(cutoff_version, bool) or not isinstance(cutoff_version, int):
            raise ValueError("cutoff_version must be an integer")
        if cutoff_version < 0:
            raise ValueError("cutoff_version must be non-negative")
        expected_relative_path = f"audit/batches/authority_history/{batch_id}.json"
        if relative_path != expected_relative_path:
            raise ValueError("audit batch path must be canonical and authority-history scoped")
        if len(sha256) != 64:
            raise ValueError("audit batch sha256 must be a SHA-256 digest")
        request = {
            "batch_id": batch_id,
            "cutoff_version": cutoff_version,
            "relative_path": relative_path,
            "sha256": sha256,
        }
        replay = self._command_replay(command_id, "archive_audit_batch", request)
        if replay is not None:
            return replay
        compacted = self._authority._fetchone(
            "SELECT * FROM audit_partition_batches WHERE archive_batch_id=?", (batch_id,)
        )
        if compacted is not None:
            if (
                compacted["sha256"] != sha256
                or compacted["record_kind"] != "authority_history"
                or int(compacted["cutoff_version"]) != cutoff_version
                or compacted["relative_path"] != relative_path
            ):
                raise RuntimeError("compacted audit batch ID has different immutable content")
            compacted_result: dict[str, Any] | None = {
                "archive_batch_id": batch_id,
                "record_kind": str(compacted["record_kind"]),
                "cutoff_version": int(compacted["cutoff_version"]),
                "row_count": int(compacted["row_count"]),
                "relative_path": str(compacted["relative_path"]),
                "sha256": str(compacted["sha256"]),
                "state": "compacted",
                "partition_id": str(compacted["partition_id"]),
            }
        else:
            compacted_result = None
        hot_batch = self._authority._fetchone(
            "SELECT * FROM archive_batches WHERE archive_batch_id=?", (batch_id,)
        )
        if hot_batch is not None and (
            hot_batch["sha256"] != sha256
            or hot_batch["record_kind"] != "authority_history"
            or int(hot_batch["cutoff_version"]) != cutoff_version
            or hot_batch["relative_path"] != relative_path
        ):
            raise RuntimeError("audit batch ID was replayed with different content")
        batch_path = _lexical_protocol_path(self._authority._run_root, relative_path)
        payload: dict[str, Any] | None = None
        if compacted_result is None:
            try:
                metadata = batch_path.lstat()
            except FileNotFoundError:
                archived_source = self._find_compacted_audit_source(batch_id)
                if archived_source is None:
                    raise
                if (
                    archived_source["file_sha256"] != sha256
                    or int(archived_source["cutoff_version"]) != cutoff_version
                ):
                    raise RuntimeError("compacted audit batch ID has different immutable content")
                compacted_result = {
                    "archive_batch_id": batch_id,
                    "record_kind": "authority_history",
                    "cutoff_version": cutoff_version,
                    "row_count": int(archived_source["row_count"]),
                    "relative_path": relative_path,
                    "sha256": sha256,
                    "state": "compacted",
                    "partition_id": str(archived_source["partition_id"]),
                }
            else:
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
                    raise ValueError("audit batch must be a non-writable regular file")
                if sha256_file(batch_path) != sha256:
                    raise ValueError("audit batch file hash mismatch")
                payload = read_json(batch_path)
                validate_audit_batch(payload)
                if (
                    payload["batch_id"] != batch_id
                    or payload["record_kind"] != "authority_history"
                    or int(payload["cutoff_version"]) != cutoff_version
                ):
                    raise ValueError("audit batch immutable identity mismatch")
        if compacted_result is None and hot_batch is None:
            assert payload is not None
            expected_before_transaction = _audit_history_records(
                self._authority._connection, cutoff_version
            )
            if payload["records"] != expected_before_transaction:
                raise RuntimeError(
                    "audit batch does not exactly cover the archivable authority rows"
                )
            for record in expected_before_transaction:
                if record["table"] == "command_records":
                    publish_command_receipt(
                        RunPaths(self._authority._run_root), dict(record["row"])
                    )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute(
                "SELECT * FROM archive_batches WHERE archive_batch_id=?", (batch_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["sha256"] != sha256
                    or existing["record_kind"] != "authority_history"
                    or int(existing["cutoff_version"]) != cutoff_version
                    or existing["relative_path"] != relative_path
                ):
                    raise RuntimeError("audit batch ID was replayed with different content")
                return dict(existing)
            if compacted_result is not None:
                return compacted_result
            assert payload is not None
            expected = _audit_history_records(connection, cutoff_version)
            if payload["records"] != expected:
                raise RuntimeError(
                    "audit batch does not exactly cover the archivable authority rows"
                )
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO archive_batches(
                    archive_batch_id, owner_epoch, record_kind, cutoff_version,
                    row_count, relative_path, sha256, state, created_at
                ) VALUES (?, ?, 'authority_history', ?, ?, ?, ?, 'committed', ?)
                """,
                (
                    batch_id,
                    self.token.epoch,
                    cutoff_version,
                    len(expected),
                    relative_path,
                    sha256,
                    now,
                ),
            )
            keys: dict[str, list[Any]] = {}
            for record in expected:
                keys.setdefault(str(record["table"]), []).append(record["primary_key"])
                row = record["row"]
                if record["table"] == "artifact_publications":
                    connection.execute(
                        """
                        INSERT INTO gc_candidates(
                            relative_path, artifact_kind, owning_epoch, publication_id,
                            size_bytes, sha256, state, not_before, recorded_by_epoch, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                        ON CONFLICT(relative_path) DO NOTHING
                        """,
                        (
                            row["relative_path"],
                            row["artifact_kind"],
                            row["owning_epoch"],
                            row["publication_id"],
                            row["size_bytes"],
                            row["sha256"],
                            now + self._authority._orphan_grace_seconds,
                            self.token.epoch,
                            now,
                        ),
                    )
                elif record["table"] == "updates":
                    connection.execute(
                        """
                        INSERT INTO gc_candidates(
                            relative_path, artifact_kind, owning_epoch, publication_id,
                            size_bytes, sha256, state, not_before, recorded_by_epoch, recorded_at
                        ) VALUES (?, 'update_payload', ?, ?, ?, ?, 'pending', ?, ?, ?)
                        ON CONFLICT(relative_path) DO NOTHING
                        """,
                        (
                            row["payload_relative_path"],
                            row["applied_by_epoch"] or row["dropped_by_epoch"] or self.token.epoch,
                            row["update_id"],
                            row["payload_size"],
                            row["payload_sha256"],
                            now + self._authority._orphan_grace_seconds,
                            self.token.epoch,
                            now,
                        ),
                    )
            delete_order = (
                "command_records",
                "proposal_conflicts",
                "proposal_observations",
                "selection_batch_updates",
                "token_fates",
                "updates",
                "cycle_receipts",
                "artifact_publications",
                "global_versions",
                "publication_intents",
                "selection_batches",
            )
            primary_columns = {
                "command_records": "command_id",
                "proposal_conflicts": "CAST(observation_id AS TEXT)",
                "proposal_observations": "CAST(observation_id AS TEXT)",
                "selection_batch_updates": "batch_id || ':' || update_id",
                "token_fates": "receipt_id",
                "updates": "update_id",
                "cycle_receipts": "receipt_id",
                "artifact_publications": "publication_id || ':' || artifact_kind",
                "global_versions": "CAST(version AS TEXT)",
                "publication_intents": "publication_id",
                "selection_batches": "batch_id",
            }
            for table in delete_order:
                values = keys.get(table, [])
                if not values:
                    continue
                placeholders = ",".join("?" for _ in values)
                connection.execute(
                    f"DELETE FROM {table} WHERE {primary_columns[table]} IN ({placeholders})",
                    tuple(str(value) for value in values),
                )
            row = connection.execute(
                "SELECT * FROM archive_batches WHERE archive_batch_id=?", (batch_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._command(command_id, "archive_audit_batch", request, operation)

    def _immutable_audit_object(
        self, relative_path: str, expected_sha256: str, *, prefix: str
    ) -> Path:
        if (
            not relative_path.startswith(prefix)
            or relative_path.startswith("/")
            or ".." in Path(relative_path).parts
        ):
            raise ValueError("audit object path is not canonical or correctly scoped")
        target = _lexical_protocol_path(self._authority._run_root, relative_path)
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
            raise ValueError("audit object must be a non-writable regular file")
        if sha256_file(target) != expected_sha256:
            raise ValueError("audit object file hash mismatch")
        return target

    def _find_compacted_audit_source(self, batch_id: str) -> dict[str, Any] | None:
        """Rare retry path: consult immutable manifests, never the startup hot scan."""

        rows = self._authority._fetchall(
            "SELECT * FROM archive_partitions WHERE state='committed' ORDER BY partition_id"
        )
        paths = RunPaths(self._authority._run_root)
        for row in rows:
            partition_path = self._immutable_audit_object(
                str(row["relative_path"]),
                str(row["sha256"]),
                prefix="audit/partitions/",
            )
            manifest_path = self._immutable_audit_object(
                str(row["manifest_relative_path"]),
                str(row["manifest_sha256"]),
                prefix="audit/partitions/",
            )
            partition = read_json(partition_path)
            manifest = read_json(manifest_path)
            validate_audit_partition_manifest(
                paths=paths,
                partition=partition,
                manifest=manifest,
            )
            for source in manifest["source_batches"]:
                if source["batch_id"] == batch_id:
                    return {"partition_id": str(row["partition_id"]), **dict(source)}
        return None

    def compact_audit_batches(
        self,
        *,
        command_id: str,
        partition_id: str,
        record_kind: str,
        batch_ids: tuple[str, ...],
        partition_relative_path: str,
        partition_sha256: str,
        manifest_relative_path: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        """Fold committed batch rows into one verified immutable partition cursor."""

        validate_identity(partition_id, name="partition_id")
        validate_identity(record_kind, name="record_kind")
        normalized_batch_ids = tuple(sorted(batch_ids))
        if not normalized_batch_ids or len(set(normalized_batch_ids)) != len(normalized_batch_ids):
            raise ValueError("audit compaction requires unique source batch IDs")
        for batch_id in normalized_batch_ids:
            validate_identity(batch_id, name="batch_id")
        expected_partition_path = f"audit/partitions/{record_kind}/{partition_id}.json"
        expected_manifest_path = f"audit/partitions/{record_kind}/{partition_id}.manifest.json"
        if (
            partition_relative_path != expected_partition_path
            or manifest_relative_path != expected_manifest_path
        ):
            raise ValueError("audit partition paths are not canonical")
        for digest in (partition_sha256, manifest_sha256):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("audit partition digests must be lowercase SHA-256")
        request = {
            "partition_id": partition_id,
            "record_kind": record_kind,
            "batch_ids": list(normalized_batch_ids),
            "partition_relative_path": partition_relative_path,
            "partition_sha256": partition_sha256,
            "manifest_relative_path": manifest_relative_path,
            "manifest_sha256": manifest_sha256,
        }
        replay = self._command_replay(command_id, "compact_audit_batches", request)
        if replay is not None:
            return replay
        hot_partition = self._authority._fetchone(
            "SELECT * FROM archive_partitions WHERE partition_id=?", (partition_id,)
        )
        if hot_partition is not None and (
            hot_partition["record_kind"] != record_kind
            or hot_partition["relative_path"] != partition_relative_path
            or hot_partition["sha256"] != partition_sha256
            or hot_partition["manifest_relative_path"] != manifest_relative_path
            or hot_partition["manifest_sha256"] != manifest_sha256
        ):
            raise RuntimeError("audit partition ID was replayed with different content")
        partition_path = self._immutable_audit_object(
            partition_relative_path, partition_sha256, prefix="audit/partitions/"
        )
        manifest_path = self._immutable_audit_object(
            manifest_relative_path, manifest_sha256, prefix="audit/partitions/"
        )
        partition = read_json(partition_path)
        manifest = read_json(manifest_path)
        validate_audit_partition(partition)
        validate_audit_partition_manifest(
            paths=RunPaths(self._authority._run_root),
            partition=partition,
            manifest=manifest,
        )
        if (
            partition["partition_id"] != partition_id
            or partition["record_kind"] != record_kind
            or tuple(sorted(item["batch_id"] for item in partition["source_batches"]))
            != normalized_batch_ids
        ):
            raise ValueError("audit partition immutable identity mismatch")
        placeholders = ",".join("?" for _ in normalized_batch_ids)
        if hot_partition is None:
            source_rows = self._authority._fetchall(
                f"SELECT * FROM archive_batches WHERE archive_batch_id IN ({placeholders})",
                normalized_batch_ids,
            )
            if len(source_rows) != len(normalized_batch_ids):
                raise RuntimeError("audit compaction source batches are not all hot and committed")
            for row in source_rows:
                self._immutable_audit_object(
                    str(row["relative_path"]), str(row["sha256"]), prefix="audit/batches/"
                )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute(
                "SELECT * FROM archive_partitions WHERE partition_id=?", (partition_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["record_kind"] != record_kind
                    or existing["relative_path"] != partition_relative_path
                    or existing["sha256"] != partition_sha256
                    or existing["manifest_relative_path"] != manifest_relative_path
                    or existing["manifest_sha256"] != manifest_sha256
                ):
                    raise RuntimeError("audit partition ID was replayed with different content")
                return dict(existing)
            rows = connection.execute(
                f"SELECT * FROM archive_batches WHERE archive_batch_id IN ({placeholders})",
                normalized_batch_ids,
            ).fetchall()
            if len(rows) != len(normalized_batch_ids) or any(
                row["state"] != "committed" or row["record_kind"] != record_kind for row in rows
            ):
                raise RuntimeError("audit compaction source state changed")
            persisted = {str(row["archive_batch_id"]): row for row in rows}
            source_projection = {
                str(item["batch_id"]): item for item in partition["source_batches"]
            }
            for batch_id in normalized_batch_ids:
                row = persisted[batch_id]
                source = source_projection[batch_id]
                if (
                    source["file_sha256"] != row["sha256"]
                    or int(source["cutoff_version"]) != int(row["cutoff_version"])
                    or int(source["row_count"]) != int(row["row_count"])
                ):
                    raise RuntimeError("audit partition source identity differs from authority")
            now = float(self._authority._wall_clock())
            connection.execute(
                """
                INSERT INTO archive_partitions(
                    partition_id, owner_epoch, record_kind, source_batch_count,
                    row_count, relative_path, sha256, manifest_relative_path,
                    manifest_sha256, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)
                """,
                (
                    partition_id,
                    self.token.epoch,
                    record_kind,
                    len(rows),
                    len(partition["records"]),
                    partition_relative_path,
                    partition_sha256,
                    manifest_relative_path,
                    manifest_sha256,
                    now,
                ),
            )
            for batch_id in normalized_batch_ids:
                row = persisted[batch_id]
                connection.execute(
                    """
                    INSERT INTO audit_partition_batches(
                        partition_id, archive_batch_id, record_kind, cutoff_version,
                        row_count, relative_path, sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        partition_id,
                        batch_id,
                        record_kind,
                        row["cutoff_version"],
                        row["row_count"],
                        row["relative_path"],
                        row["sha256"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_gc_candidates(
                        relative_path, partition_id, archive_batch_id, sha256,
                        state, recorded_by_epoch, recorded_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        row["relative_path"],
                        partition_id,
                        batch_id,
                        row["sha256"],
                        self.token.epoch,
                        now,
                    ),
                )
            connection.execute(
                f"DELETE FROM archive_batches WHERE archive_batch_id IN ({placeholders})",
                normalized_batch_ids,
            )
            result = connection.execute(
                "SELECT * FROM archive_partitions WHERE partition_id=?", (partition_id,)
            ).fetchone()
            assert result is not None
            return dict(result)

        return self._command(command_id, "compact_audit_batches", request, operation)

    def claim_audit_gc(self, *, command_id: str, limit: int = 64) -> tuple[dict[str, str], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("audit GC claim limit must be a positive integer")
        request = {"limit": limit}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            rows = connection.execute(
                """
                SELECT relative_path, sha256 FROM audit_gc_candidates
                WHERE state='pending'
                   OR (state='claimed' AND claimed_by_epoch != ?)
                ORDER BY relative_path LIMIT ?
                """,
                (self.token.epoch, limit),
            ).fetchall()
            result = [
                {"relative_path": str(row["relative_path"]), "sha256": str(row["sha256"])}
                for row in rows
            ]
            now = float(self._authority._wall_clock())
            for item in result:
                connection.execute(
                    """
                    UPDATE audit_gc_candidates
                    SET state='claimed', claimed_by_epoch=?, claimed_at=?
                    WHERE relative_path=?
                    """,
                    (self.token.epoch, now, item["relative_path"]),
                )
            return {"candidates": result}

        result = self._command(command_id, "claim_audit_gc", request, operation)
        return tuple(dict(item) for item in result["candidates"])

    def complete_audit_gc(
        self, *, command_id: str, relative_paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        paths = tuple(sorted(relative_paths))
        if not paths or len(set(paths)) != len(paths):
            raise ValueError("audit GC completion requires unique paths")
        for relative_path in paths:
            if not relative_path.startswith("audit/batches/") or ".." in Path(relative_path).parts:
                raise ValueError("audit GC completion path is invalid")
            try:
                (self._authority._run_root / relative_path).lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("audit GC object still exists")
        request = {"relative_paths": list(paths)}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            now = float(self._authority._wall_clock())
            for relative_path in paths:
                cursor = connection.execute(
                    """
                    UPDATE audit_gc_candidates SET state='deleted', deleted_at=?
                    WHERE relative_path=? AND state='claimed' AND claimed_by_epoch=?
                    """,
                    (now, relative_path, self.token.epoch),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("audit GC candidate is not claimed")
                connection.execute(
                    "DELETE FROM audit_partition_batches WHERE relative_path=?",
                    (relative_path,),
                )
                connection.execute(
                    "DELETE FROM audit_gc_candidates WHERE relative_path=?",
                    (relative_path,),
                )
            return {"relative_paths": list(paths)}

        result = self._command(command_id, "complete_audit_gc", request, operation)
        return tuple(str(item) for item in result["relative_paths"])

    def begin_terminal_preclose(
        self,
        *,
        command_id: str,
        reason: str,
        registration_visibility_grace_seconds: float,
        hard_crash_cycle_token_budget: int = 0,
    ) -> TerminalState:
        """Durably freeze the admission cutoff before scanning filesystem visibility."""

        if not reason:
            raise ValueError("terminal close reason must not be empty")
        if (
            isinstance(registration_visibility_grace_seconds, bool)
            or not isinstance(registration_visibility_grace_seconds, (int, float))
            or registration_visibility_grace_seconds < 0.0
            or not float("-inf") < float(registration_visibility_grace_seconds) < float("inf")
        ):
            raise ValueError("registration visibility grace must be a finite non-negative value")
        if (
            isinstance(hard_crash_cycle_token_budget, bool)
            or not isinstance(hard_crash_cycle_token_budget, int)
            or hard_crash_cycle_token_budget < 0
        ):
            raise ValueError("hard crash cycle token budget must be a non-negative integer")
        request = {
            "reason": reason,
            "registration_visibility_grace_seconds": float(registration_visibility_grace_seconds),
            "hard_crash_cycle_token_budget": hard_crash_cycle_token_budget,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            assert row is not None
            if row["state"] != "open":
                raise RuntimeError("terminal preclose requires an open controller")
            now = float(self._authority._wall_clock())
            generation = int(row["generation"]) + 1
            connection.execute(
                """
                UPDATE controller_state SET state='preclosing', generation=?, reason=?,
                    requested_at=?, registration_visibility_deadline=?,
                    drain_ack_deadline=NULL, proposal_visibility_deadline=NULL,
                    terminal_merge_count=0, hard_crash_cycle_token_budget=?,
                    updated_by_epoch=?, updated_by_owner_id=?
                WHERE singleton=1 AND state='open'
                """,
                (
                    generation,
                    reason,
                    now,
                    now + float(registration_visibility_grace_seconds),
                    hard_crash_cycle_token_budget,
                    self.token.epoch,
                    self.token.owner_id,
                ),
            )
            return {"state": "preclosing"}

        result = self._command(command_id, "begin_terminal_preclose", request, operation)
        return TerminalState(result["state"])

    def begin_terminal_close(
        self,
        *,
        command_id: str,
        reason: str,
        hard_crash_cycle_token_budget: int = 0,
        drain_ack_timeout_seconds: float = 0.0,
    ) -> TerminalState:
        if not reason:
            raise ValueError("terminal close reason must not be empty")
        if (
            isinstance(hard_crash_cycle_token_budget, bool)
            or not isinstance(hard_crash_cycle_token_budget, int)
            or hard_crash_cycle_token_budget < 0
        ):
            raise ValueError("hard crash cycle token budget must be a non-negative integer")
        if (
            isinstance(drain_ack_timeout_seconds, bool)
            or not isinstance(drain_ack_timeout_seconds, (int, float))
            or drain_ack_timeout_seconds < 0.0
            or not float("-inf") < float(drain_ack_timeout_seconds) < float("inf")
        ):
            raise ValueError("drain ack timeout must be a finite non-negative value")
        request = {
            "reason": reason,
            "hard_crash_cycle_token_budget": hard_crash_cycle_token_budget,
            "drain_ack_timeout_seconds": float(drain_ack_timeout_seconds),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            assert row is not None
            if row["state"] not in {"open", "preclosing"}:
                raise RuntimeError("terminal close is already active or authority is terminal")
            now = float(self._authority._wall_clock())
            if row["state"] == "preclosing":
                if (
                    row["reason"] != reason
                    or int(row["hard_crash_cycle_token_budget"]) != hard_crash_cycle_token_budget
                ):
                    raise RuntimeError("terminal preclose intent changed before contributor freeze")
                generation = int(row["generation"])
            else:
                generation = int(row["generation"]) + 1
            if isinstance(self._authority._scope, StaticMembershipScope):
                contributors = connection.execute(
                    """
                    SELECT b.learner_id AS stable_contributor_key, 'static' AS fence_kind,
                        json_object(
                            'kind', 'static', 'learner_id', b.learner_id,
                            'logical_launch_id', b.logical_launch_id,
                            'attempt_id', b.attempt_id,
                            'binding_generation', b.binding_generation
                        ) AS fence_json,
                        COALESCE(p.last_cycle_seq, 0) AS last_cycle_seq,
                        COALESCE(p.data_cursor, 0) AS data_cursor
                    FROM static_contributor_bindings AS b
                    LEFT JOIN contributor_progress AS p
                        ON p.stable_contributor_key=b.learner_id
                    WHERE b.status='active'
                    ORDER BY b.learner_id
                    """
                ).fetchall()
            else:
                contributors = connection.execute(
                    """
                    SELECT CAST(i.stream_id AS TEXT) AS stable_contributor_key,
                        'dynamic' AS fence_kind,
                        json_object(
                            'kind', 'dynamic', 'instance_id', i.instance_id,
                            'placement_id', i.placement_id,
                            'placement_epoch', i.placement_epoch,
                            'stream_id', i.stream_id, 'stream_epoch', i.stream_epoch,
                            'admission_generation', i.admission_generation,
                            'admission_token_sha256', i.admission_token_sha256
                        ) AS fence_json,
                        COALESCE(p.last_cycle_seq, 0) AS last_cycle_seq,
                        COALESCE(p.data_cursor, 0) AS data_cursor
                    FROM learner_instances AS i
                    LEFT JOIN contributor_progress AS p
                        ON p.stable_contributor_key=CAST(i.stream_id AS TEXT)
                    WHERE i.status IN ('admitted', 'draining')
                    ORDER BY i.stream_id
                    """
                ).fetchall()
            for contributor in contributors:
                fence_payload = json.loads(str(contributor["fence_json"]))
                connection.execute(
                    """
                    INSERT INTO terminal_contributor_fences(
                        generation, stable_contributor_key, fence_kind, fence_json,
                        close_last_cycle_seq, close_data_cursor, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_ack')
                    """,
                    (
                        generation,
                        contributor["stable_contributor_key"],
                        contributor["fence_kind"],
                        _canonical_json(fence_payload),
                        contributor["last_cycle_seq"],
                        contributor["data_cursor"],
                    ),
                )
            connection.execute(
                """
                UPDATE controller_state
                SET state='closing', generation=?, reason=?, requested_at=COALESCE(requested_at, ?),
                    registration_visibility_deadline=COALESCE(
                        registration_visibility_deadline, ?
                    ),
                    drain_ack_deadline=?, proposal_visibility_deadline=NULL,
                    terminal_merge_count=0, hard_crash_cycle_token_budget=?,
                    updated_by_epoch=?, updated_by_owner_id=?
                WHERE singleton=1
                """,
                (
                    generation,
                    reason,
                    now,
                    now,
                    now + float(drain_ack_timeout_seconds),
                    hard_crash_cycle_token_budget,
                    self.token.epoch,
                    self.token.owner_id,
                ),
            )
            return {"state": "closing"}

        result = self._command(command_id, "begin_terminal_close", request, operation)
        return TerminalState(result["state"])

    def begin_terminal_proposal_visibility(
        self,
        *,
        command_id: str,
        generation: int,
        proposal_visibility_grace_seconds: float,
    ) -> float:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("terminal generation must be a positive integer")
        if (
            isinstance(proposal_visibility_grace_seconds, bool)
            or not isinstance(proposal_visibility_grace_seconds, (int, float))
            or proposal_visibility_grace_seconds < 0.0
            or not float("-inf") < float(proposal_visibility_grace_seconds) < float("inf")
        ):
            raise ValueError("proposal visibility grace must be a finite non-negative value")
        request = {
            "generation": generation,
            "proposal_visibility_grace_seconds": float(proposal_visibility_grace_seconds),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
            if (
                row is None
                or row["state"] not in {"closing", "draining"}
                or int(row["generation"]) != generation
            ):
                raise RuntimeError("proposal visibility requires the current terminal generation")
            now = float(self._authority._wall_clock())
            deadline = row["proposal_visibility_deadline"]
            if deadline is None:
                deadline = now + float(proposal_visibility_grace_seconds)
                connection.execute(
                    """
                    UPDATE controller_state SET proposal_visibility_deadline=?,
                        updated_by_epoch=?, updated_by_owner_id=?
                    WHERE singleton=1 AND generation=?
                    """,
                    (deadline, self.token.epoch, self.token.owner_id, generation),
                )
            return {"deadline": float(deadline)}

        result = self._command(
            command_id,
            "begin_terminal_proposal_visibility",
            request,
            operation,
        )
        return float(result["deadline"])

    def acknowledge_terminal_contributor(
        self,
        *,
        command_id: str,
        fence: StaticContributorFence | DynamicContributorFence,
        final_cycle_seq: int | None,
        final_update_id: str | None = None,
        hard_crash_gap_tokens_upper_bound: int = 0,
    ) -> str:
        if final_cycle_seq is not None and (
            isinstance(final_cycle_seq, bool)
            or not isinstance(final_cycle_seq, int)
            or final_cycle_seq < 0
        ):
            raise ValueError("final_cycle_seq must be a non-negative integer")
        if final_update_id is not None:
            validate_identity(final_update_id, name="final_update_id")
        if (
            isinstance(hard_crash_gap_tokens_upper_bound, bool)
            or not isinstance(hard_crash_gap_tokens_upper_bound, int)
            or hard_crash_gap_tokens_upper_bound < 0
        ):
            raise ValueError("hard crash gap bound must be a non-negative integer")
        hard_crash = final_cycle_seq is None
        if hard_crash:
            if final_update_id is not None:
                raise ValueError("hard-crash acknowledgement cannot name a final update")
        elif hard_crash_gap_tokens_upper_bound:
            raise ValueError("an acknowledged final cycle cannot carry a hard-crash gap")
        request = {
            "fence": fence.as_dict(),
            "final_cycle_seq": final_cycle_seq,
            "final_update_id": final_update_id,
            "hard_crash_gap_tokens_upper_bound": hard_crash_gap_tokens_upper_bound,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            controller = connection.execute(
                "SELECT * FROM controller_state WHERE singleton=1"
            ).fetchone()
            if controller is None or controller["state"] not in {"closing", "draining"}:
                raise RuntimeError("terminal contributor acknowledgement requires an active close")
            generation = int(controller["generation"])
            frozen = connection.execute(
                """
                SELECT * FROM terminal_contributor_fences
                WHERE generation=? AND stable_contributor_key=?
                """,
                (generation, fence.stable_contributor_key),
            ).fetchone()
            if frozen is None or frozen["fence_json"] != _canonical_json(fence.as_dict()):
                raise MembershipFenceError("terminal acknowledgement fence was not frozen")
            if frozen["state"] != "awaiting_ack":
                raise RuntimeError("terminal contributor was already acknowledged")
            now = float(self._authority._wall_clock())
            if hard_crash:
                if hard_crash_gap_tokens_upper_bound > int(
                    controller["hard_crash_cycle_token_budget"]
                ):
                    raise ValueError("hard-crash gap exceeds the frozen one-cycle token budget")
                state = "hard_crash"
                if isinstance(fence, DynamicContributorFence):
                    self._retire_dynamic_in_transaction(
                        connection,
                        fence=fence,
                        reason="terminal_hard_crash",
                        final_status="expired",
                    )
                else:
                    self._terminalize_fenced_updates(
                        connection,
                        fence_json=_canonical_json(fence.as_dict()),
                        reason="terminal_hard_crash",
                    )
                    connection.execute(
                        """
                        UPDATE static_contributor_bindings
                        SET status='terminal', terminal_at=?
                        WHERE learner_id=? AND logical_launch_id=? AND attempt_id=?
                            AND binding_generation=? AND status='active'
                        """,
                        (
                            now,
                            fence.learner_id,
                            fence.logical_launch_id,
                            fence.attempt_id,
                            fence.binding_generation,
                        ),
                    )
            else:
                assert final_cycle_seq is not None
                close_sequence = int(frozen["close_last_cycle_seq"])
                if final_cycle_seq not in {close_sequence, close_sequence + 1}:
                    raise MembershipFenceError("final cycle exceeds the frozen current-cycle bound")
                progress = connection.execute(
                    "SELECT * FROM contributor_progress WHERE stable_contributor_key=?",
                    (fence.stable_contributor_key,),
                ).fetchone()
                observed_sequence = 0 if progress is None else int(progress["last_cycle_seq"])
                if observed_sequence != final_cycle_seq:
                    raise MembershipFenceError("final cycle receipt is not contiguous and ingested")
                if final_cycle_seq == 0:
                    if progress is not None or final_update_id is not None:
                        raise MembershipFenceError(
                            "zero-cycle acknowledgement requires no receipt progress or update"
                        )
                else:
                    receipt = connection.execute(
                        """
                        SELECT proposal_expected, planned_update_id FROM cycle_receipts
                        WHERE stable_contributor_key=? AND cycle_seq=?
                        """,
                        (fence.stable_contributor_key, final_cycle_seq),
                    ).fetchone()
                    if receipt is None:
                        raise MembershipFenceError("final cycle receipt is missing")
                    proposal_expected = bool(receipt["proposal_expected"])
                    planned_update_id = receipt["planned_update_id"]
                    if proposal_expected and final_update_id is None:
                        raise MembershipFenceError(
                            "final receipt promised a proposal; acknowledge it or use hard-crash "
                            "handling"
                        )
                    if proposal_expected and final_update_id != planned_update_id:
                        raise MembershipFenceError(
                            "final update does not match the update planned by the final receipt"
                        )
                    if not proposal_expected and final_update_id is not None:
                        raise MembershipFenceError("final receipt did not declare a proposal")
                if final_update_id is not None:
                    update = connection.execute(
                        "SELECT * FROM updates WHERE update_id=?", (final_update_id,)
                    ).fetchone()
                    if update is not None and (
                        int(update["cycle_seq"]) != final_cycle_seq
                        or update["fence_json"] != _canonical_json(fence.as_dict())
                    ):
                        raise MembershipFenceError("final update does not match the frozen cycle")
                if isinstance(fence, DynamicContributorFence):
                    if final_update_id is not None:
                        self._retire_dynamic_in_transaction(
                            connection,
                            fence=fence,
                            reason="terminal_graceful_ack",
                            final_status="draining",
                            final_update_id=final_update_id,
                        )
                    else:
                        self._terminalize_fenced_updates(
                            connection,
                            fence_json=_canonical_json(fence.as_dict()),
                            reason="terminal_graceful_ack",
                        )
                else:
                    self._terminalize_fenced_updates(
                        connection,
                        fence_json=_canonical_json(fence.as_dict()),
                        reason="terminal_graceful_ack",
                        preserve_update_id=final_update_id,
                    )
                state = "acked"
            connection.execute(
                """
                UPDATE terminal_contributor_fences
                SET state=?, final_cycle_seq=?, final_update_id=?,
                    hard_crash_gap_tokens_upper_bound=?, acknowledged_at=?,
                    acknowledged_by_epoch=?
                WHERE generation=? AND stable_contributor_key=? AND state='awaiting_ack'
                """,
                (
                    state,
                    final_cycle_seq,
                    final_update_id,
                    hard_crash_gap_tokens_upper_bound,
                    now,
                    self.token.epoch,
                    generation,
                    fence.stable_contributor_key,
                ),
            )
            connection.execute(
                """
                UPDATE controller_state SET state='draining', updated_by_epoch=?,
                    updated_by_owner_id=? WHERE singleton=1
                """,
                (self.token.epoch, self.token.owner_id),
            )
            return {"state": state}

        result = self._command(command_id, "acknowledge_terminal_contributor", request, operation)
        return str(result["state"])

    def finalize_terminal(
        self,
        *,
        command_id: str,
        reason: str,
        error: bool = False,
    ) -> TerminalState:
        request = {"reason": reason, "error": error}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            controller = connection.execute(
                "SELECT * FROM controller_state WHERE singleton=1"
            ).fetchone()
            assert controller is not None
            if controller["state"] not in {"closing", "draining"}:
                raise RuntimeError("terminal close must be started before finalization")
            frozen = connection.execute(
                """
                SELECT * FROM terminal_contributor_fences
                WHERE generation=? AND state='acked' ORDER BY stable_contributor_key
                """,
                (int(controller["generation"]),),
            ).fetchall()
            now = float(self._authority._wall_clock())
            for row in frozen:
                fence = decode_contributor_fence(json.loads(str(row["fence_json"])))
                self._terminalize_fenced_updates(
                    connection,
                    fence_json=str(row["fence_json"]),
                    reason="terminal_final_update_not_selected",
                )
                if isinstance(fence, DynamicContributorFence):
                    self._retire_dynamic_in_transaction(
                        connection,
                        fence=fence,
                        reason="terminal_graceful_ack",
                        final_status="stopped",
                        final_update_id=row["final_update_id"],
                    )
                else:
                    connection.execute(
                        """
                        UPDATE static_contributor_bindings
                        SET status='terminal', terminal_at=?
                        WHERE learner_id=? AND logical_launch_id=? AND attempt_id=?
                            AND binding_generation=? AND status='active'
                        """,
                        (
                            now,
                            fence.learner_id,
                            fence.logical_launch_id,
                            fence.attempt_id,
                            fence.binding_generation,
                        ),
                    )
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
            awaiting = connection.execute(
                """
                SELECT COUNT(*) FROM terminal_contributor_fences
                WHERE generation=? AND state='awaiting_ack'
                """,
                (int(controller["generation"]),),
            ).fetchone()[0]
            token_outstanding = connection.execute(
                "SELECT COALESCE(direct_outstanding, 0) FROM token_rollups WHERE singleton=1"
            ).fetchone()
            if awaiting or (token_outstanding is not None and int(token_outstanding[0]) != 0):
                raise RuntimeError("terminal finalization requires drained contributor/token state")
            latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            if latest is None:
                raise RuntimeError("cannot finalize before v0 is committed")
            direct = connection.execute(
                "SELECT COALESCE(SUM(direct_weight_tokens_applied), 0) FROM global_versions"
            ).fetchone()[0]
            state = "error" if error else "finalized"
            generation = max(1, int(controller["generation"]))
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

    def _require_proposal_receipt(
        self,
        connection: sqlite3.Connection,
        proposal: FullUpdateProposalV2,
    ) -> sqlite3.Row:
        receipt = connection.execute(
            "SELECT * FROM cycle_receipts WHERE receipt_id=?",
            (proposal.cycle_receipt_id,),
        ).fetchone()
        if receipt is None or receipt["receipt_sha256"] != proposal.cycle_receipt_sha256:
            raise ValueError("proposal receipt reference is missing or mismatched")
        receipt_fields = {
            "run_id": proposal.run_id,
            "stable_contributor_key": proposal.stable_contributor_key,
            "cycle_seq": proposal.cycle_seq,
            "cycle_id": proposal.cycle_id,
            "processed_tokens_this_cycle": proposal.processed_tokens_this_cycle,
            "effective_tokens_this_cycle": proposal.effective_tokens_this_update,
            "local_discarded_tokens_this_cycle": proposal.local_discarded_tokens_this_cycle,
            "retained_tokens_since_base": proposal.retained_tokens_since_base,
            "data_cursor_start": proposal.data_cursor_start,
            "data_cursor_end": proposal.data_cursor_end,
            "fence_kind": proposal.contributor_fence.kind,
            "fence_json": _canonical_json(proposal.contributor_fence.as_dict()),
            "proposal_expected": 1,
        }
        if any(receipt[name] != value for name, value in receipt_fields.items()):
            raise ValueError("proposal immutable fields do not match its cycle receipt")
        return receipt

    def _require_visibility_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        stable_contributor_key: str,
        cycle_seq: int,
        update_id: str,
    ) -> sqlite3.Row:
        receipt = connection.execute(
            """
            SELECT * FROM cycle_receipts
            WHERE run_id=? AND stable_contributor_key=? AND cycle_seq=?
                AND proposal_expected=1 AND planned_update_id=?
            """,
            (
                self._authority._identity.run_id,
                stable_contributor_key,
                cycle_seq,
                update_id,
            ),
        ).fetchone()
        if receipt is None:
            raise ValueError("visibility observation requires a matching contiguous receipt")
        return receipt

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

    def _record_visibility_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        stable_contributor_key: str,
        cycle_seq: int,
        update_id: str,
        pointer_sequence: int,
        disposition: ProposalDisposition,
        diagnostic: str,
        source_relative_path: str,
        fingerprint: str,
    ) -> int:
        now = float(self._authority._wall_clock())
        existing = connection.execute(
            """
            SELECT observation_id FROM proposal_quarantine
            WHERE stable_contributor_key=? AND cycle_seq=? AND disposition=?
                AND fingerprint=?
            """,
            (
                stable_contributor_key,
                cycle_seq,
                disposition.value,
                fingerprint,
            ),
        ).fetchone()
        if existing is not None:
            return int(existing["observation_id"])
        cursor = connection.execute(
            """
            INSERT INTO proposal_observations(
                stable_contributor_key, cycle_seq, update_id, pointer_sequence,
                disposition, diagnostic, source_relative_path, object_sha256,
                observed_by_epoch, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_contributor_key,
                cycle_seq,
                update_id,
                pointer_sequence,
                disposition.value,
                diagnostic[:512],
                source_relative_path,
                fingerprint,
                self.token.epoch,
                now,
            ),
        )
        observation_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO proposal_quarantine(
                stable_contributor_key, cycle_seq, update_id, disposition, fingerprint,
                bounded_diagnostic, source_relative_path, observation_id, quarantined_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_contributor_key,
                cycle_seq,
                update_id,
                disposition.value,
                fingerprint,
                diagnostic[:512],
                source_relative_path,
                observation_id,
                now,
            ),
        )
        self._prune_quarantine(connection, stable_contributor_key)
        return observation_id

    def _record_quarantine(
        self,
        connection: sqlite3.Connection,
        *,
        proposal: FullUpdateProposalV2,
        disposition: str,
        fingerprint: str,
        diagnostic: str,
        observation_id: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO proposal_quarantine(
                stable_contributor_key, cycle_seq, update_id, disposition, fingerprint,
                bounded_diagnostic, source_relative_path, observation_id, quarantined_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stable_contributor_key, cycle_seq, disposition, fingerprint)
            DO NOTHING
            """,
            (
                proposal.stable_contributor_key,
                proposal.cycle_seq,
                proposal.update_id,
                disposition,
                fingerprint,
                diagnostic[:512],
                proposal.payload_relative_path,
                observation_id,
                float(self._authority._wall_clock()),
            ),
        )
        self._prune_quarantine(connection, proposal.stable_contributor_key)

    def _prune_quarantine(
        self,
        connection: sqlite3.Connection,
        stable_contributor_key: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM proposal_quarantine
            WHERE quarantine_id IN (
                SELECT quarantine_id FROM proposal_quarantine
                WHERE stable_contributor_key=?
                ORDER BY quarantine_id DESC LIMIT -1 OFFSET ?
            )
            """,
            (
                stable_contributor_key,
                self._authority._max_quarantine_records_per_contributor,
            ),
        )

    def _advance_proposal_frontier(
        self,
        connection: sqlite3.Connection,
        proposal: FullUpdateProposalV2,
        observation_id: int,
    ) -> None:
        self._advance_frontier_values(
            connection,
            stable_contributor_key=proposal.stable_contributor_key,
            cycle_seq=proposal.cycle_seq,
            observation_id=observation_id,
        )

    def _advance_frontier_values(
        self,
        connection: sqlite3.Connection,
        *,
        stable_contributor_key: str,
        cycle_seq: int,
        observation_id: int,
    ) -> None:
        observation = connection.execute(
            "SELECT 1 FROM proposal_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if observation is None:
            raise AuthoritySchemaError("proposal frontier requires a terminal observation")
        now = float(self._authority._wall_clock())
        connection.execute(
            """
            INSERT INTO proposal_frontiers(
                run_id, stable_contributor_key, last_terminal_cycle_seq,
                terminal_observation_id, updated_by_epoch, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, stable_contributor_key) DO UPDATE SET
                last_terminal_cycle_seq=excluded.last_terminal_cycle_seq,
                terminal_observation_id=excluded.terminal_observation_id,
                updated_by_epoch=excluded.updated_by_epoch,
                updated_at=excluded.updated_at
            WHERE excluded.last_terminal_cycle_seq >= proposal_frontiers.last_terminal_cycle_seq
            """,
            (
                self._authority._identity.run_id,
                stable_contributor_key,
                cycle_seq,
                observation_id,
                self.token.epoch,
                now,
            ),
        )

    def _terminalize_fenced_updates(
        self,
        connection: sqlite3.Connection,
        *,
        fence_json: str,
        reason: str,
        preserve_update_id: str | None = None,
    ) -> tuple[str, ...]:
        """Drop one stale fence's work and reconcile every affected durable batch."""

        affected = connection.execute(
            """
            SELECT update_id, cycle_receipt_id, selected_batch_id
            FROM updates
            WHERE fence_json=? AND status IN ('pending', 'selected')
                AND (? IS NULL OR update_id<>?)
            """,
            (fence_json, preserve_update_id, preserve_update_id),
        ).fetchall()
        batch_ids = sorted(
            {str(row["selected_batch_id"]) for row in affected if row["selected_batch_id"]}
        )
        for batch_id in batch_ids:
            invalid = tuple(
                str(row["update_id"])
                for row in affected
                if str(row["selected_batch_id"] or "") == batch_id
            )
            self._reconcile_invalid_batch(
                connection,
                batch_id=batch_id,
                invalid_update_ids=invalid,
                reason=reason,
            )
        update_ids = tuple(str(row["update_id"]) for row in affected)
        for row in affected:
            current = connection.execute(
                "SELECT * FROM updates WHERE update_id=?", (row["update_id"],)
            ).fetchone()
            if current is not None and current["status"] in {"pending", "selected"}:
                self._drop_active_update(connection, current, reason=reason)
        orphan_receipts = connection.execute(
            """
            SELECT r.receipt_id FROM cycle_receipts AS r
            JOIN token_fates AS f ON f.receipt_id=r.receipt_id
            WHERE r.fence_json=? AND f.direct_fate='outstanding'
              AND (? IS NULL OR r.planned_update_id<>?)
              AND NOT EXISTS (
                SELECT 1 FROM updates AS u
                WHERE u.cycle_receipt_id=r.receipt_id
                  AND u.status IN ('pending', 'selected')
              )
            """,
            (fence_json, preserve_update_id, preserve_update_id),
        ).fetchall()
        now = float(self._authority._wall_clock())
        for receipt in orphan_receipts:
            self._transition_token_fate(
                connection,
                receipt_id=str(receipt["receipt_id"]),
                fate="dropped",
                reason=reason,
                now=now,
            )
        return update_ids

    def _drop_active_update(
        self, connection: sqlite3.Connection, row: Mapping[str, Any], *, reason: str
    ) -> bool:
        now = float(self._authority._wall_clock())
        cursor = connection.execute(
            """
            UPDATE updates SET status='dropped', selected_batch_id=NULL,
                selected_by_epoch=NULL, dropped_by_epoch=?, drop_reason=?
            WHERE update_id=? AND status IN ('pending', 'selected')
            """,
            (self.token.epoch, reason, row["update_id"]),
        )
        if not cursor.rowcount:
            return False
        self._transition_token_fate(
            connection,
            receipt_id=str(row["cycle_receipt_id"]),
            fate="dropped",
            reason=reason,
            now=now,
        )
        return True

    @staticmethod
    def _token_rollup_column(fate: str) -> str:
        columns = {
            "applied": "direct_applied",
            "dropped": "direct_dropped",
            "quarantined": "direct_quarantined_or_conflicted",
            "conflicted": "direct_quarantined_or_conflicted",
            "unpublished": "direct_reported_unpublished",
            "outstanding": "direct_outstanding",
        }
        try:
            return columns[fate]
        except KeyError as exc:
            raise AuthoritySchemaError(f"unknown token fate: {fate}") from exc

    def _ensure_token_rollup(self, connection: sqlite3.Connection, *, now: float) -> None:
        connection.execute(
            """
            INSERT INTO token_rollups(
                singleton, adjudicated_processed, local_discarded, direct_applied,
                direct_dropped, direct_quarantined_or_conflicted,
                direct_reported_unpublished, direct_outstanding, carried_ancestry,
                updated_by_epoch, updated_at
            ) VALUES (1, 0, 0, 0, 0, 0, 0, 0, 0, ?, ?)
            ON CONFLICT(singleton) DO NOTHING
            """,
            (self.token.epoch, now),
        )

    def _record_token_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        receipt: CycleReceiptV1,
        now: float,
    ) -> None:
        self._ensure_token_rollup(connection, now=now)
        fate_column = self._token_rollup_column(
            "outstanding" if receipt.proposal_expected else "unpublished"
        )
        connection.execute(
            f"""
            UPDATE token_rollups SET
                adjudicated_processed=adjudicated_processed+?,
                local_discarded=local_discarded+?,
                {fate_column}={fate_column}+?,
                carried_ancestry=carried_ancestry+?,
                updated_by_epoch=?, updated_at=?
            WHERE singleton=1
            """,
            (
                receipt.processed_tokens_this_cycle,
                receipt.local_discarded_tokens_this_cycle,
                receipt.effective_tokens_this_cycle,
                receipt.retained_tokens_since_base - receipt.effective_tokens_this_cycle,
                self.token.epoch,
                now,
            ),
        )

    def _transition_token_fate(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        fate: str,
        reason: str,
        now: float,
        applied_version: int | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT direct_weight_tokens, direct_fate FROM token_fates WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise AuthoritySchemaError(f"token fate is missing for receipt {receipt_id}")
        old_fate = str(row["direct_fate"])
        if old_fate == fate:
            return
        tokens = int(row["direct_weight_tokens"])
        old_column = self._token_rollup_column(old_fate)
        new_column = self._token_rollup_column(fate)
        self._ensure_token_rollup(connection, now=now)
        if old_column == new_column:
            rollup_assignment = "updated_by_epoch=?, updated_at=?"
            rollup_parameters: tuple[Any, ...] = (self.token.epoch, now)
        else:
            rollup_assignment = (
                f"{old_column}={old_column}-?, {new_column}={new_column}+?, "
                "updated_by_epoch=?, updated_at=?"
            )
            rollup_parameters = (tokens, tokens, self.token.epoch, now)
        connection.execute(
            f"UPDATE token_rollups SET {rollup_assignment} WHERE singleton=1",
            rollup_parameters,
        )
        connection.execute(
            """
            UPDATE token_fates SET direct_fate=?, fate_reason=?,
                applied_version=COALESCE(?, applied_version),
                updated_by_epoch=?, updated_at=? WHERE receipt_id=?
            """,
            (fate, reason, applied_version, self.token.epoch, now, receipt_id),
        )

    def _reconcile_invalid_batch(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        invalid_update_ids: tuple[str, ...],
        reason: str,
    ) -> tuple[str, ...]:
        """Abandon one batch, drop invalid rows, and reset only still-current peers."""

        invalid = set(invalid_update_ids)
        now = float(self._authority._wall_clock())
        intents = connection.execute(
            """
            SELECT publication_id FROM publication_intents
            WHERE selection_batch_id=? AND state='prepared'
            """,
            (batch_id,),
        ).fetchall()
        for intent in intents:
            self._abandon_publication_artifacts(
                connection,
                publication_id=str(intent["publication_id"]),
                reason=reason,
                now=now,
            )
        connection.execute(
            """
            UPDATE selection_batches SET state='abandoned', abandoned_at=?, abandon_reason=?
            WHERE batch_id=? AND state IN ('selected', 'prepared')
            """,
            (now, reason, batch_id),
        )
        rows = connection.execute(
            """
            SELECT u.* FROM selection_batch_updates AS b
            JOIN updates AS u ON u.update_id=b.update_id
            WHERE b.batch_id=? ORDER BY b.reduction_order
            """,
            (batch_id,),
        ).fetchall()
        reset: list[str] = []
        for row in rows:
            update_id = str(row["update_id"])
            is_current = self._fence_is_current_json(
                connection, str(row["fence_json"]), update_id=update_id
            )
            if update_id not in invalid and row["status"] == "selected" and is_current:
                pending = connection.execute(
                    """
                    SELECT 1 FROM updates WHERE stable_contributor_key=?
                        AND status='pending' AND update_id<>? LIMIT 1
                    """,
                    (row["stable_contributor_key"], update_id),
                ).fetchone()
                if pending is None:
                    connection.execute(
                        """
                        UPDATE updates SET status='pending', selected_batch_id=NULL,
                            selected_by_epoch=NULL WHERE update_id=? AND status='selected'
                        """,
                        (update_id,),
                    )
                    reset.append(update_id)
                    continue
            if row["status"] in {"pending", "selected"}:
                drop_reason = reason if update_id in invalid else "stale_or_superseded_peer"
                self._drop_active_update(connection, row, reason=drop_reason)
        return tuple(reset)

    def _abandon_publication_artifacts(
        self,
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        reason: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            UPDATE publication_intents SET state='abandoned', abandoned_at=?, abandon_reason=?
            WHERE publication_id=? AND state='prepared'
            """,
            (now, reason, publication_id),
        )
        artifacts = connection.execute(
            """
            SELECT * FROM artifact_publications
            WHERE publication_id=? AND state='prepared'
            """,
            (publication_id,),
        ).fetchall()
        connection.execute(
            """
            UPDATE artifact_publications SET state='orphan'
            WHERE publication_id=? AND state='prepared'
            """,
            (publication_id,),
        )
        for artifact in artifacts:
            connection.execute(
                """
                INSERT INTO gc_candidates(
                    relative_path, artifact_kind, owning_epoch, publication_id,
                    size_bytes, sha256, state, not_before, recorded_by_epoch, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(relative_path) DO NOTHING
                """,
                (
                    artifact["relative_path"],
                    artifact["artifact_kind"],
                    artifact["owning_epoch"],
                    publication_id,
                    artifact["size_bytes"],
                    artifact["sha256"],
                    now + self._authority._orphan_grace_seconds,
                    self.token.epoch,
                    now,
                ),
            )

    def _retire_dynamic_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        fence: DynamicContributorFence,
        reason: str,
        final_status: str,
        final_update_id: str | None = None,
    ) -> tuple[str, ...]:
        self._require_current_fence(connection, fence, allow_draining=True)
        now = float(self._authority._wall_clock())
        update_ids = self._terminalize_fenced_updates(
            connection,
            fence_json=_canonical_json(fence.as_dict()),
            reason=reason,
            preserve_update_id=final_update_id,
        )
        stopped_at = None if final_status == "draining" else now
        persisted_final_update_id = final_update_id if final_status == "draining" else None
        connection.execute(
            """
            UPDATE learner_instances SET status=?, stopped_at=?, status_reason=?,
                final_update_id=? WHERE instance_id=?
            """,
            (
                final_status,
                stopped_at,
                reason,
                persisted_final_update_id,
                fence.instance_id,
            ),
        )
        if final_status == "draining":
            connection.execute(
                """
                UPDATE streams SET state='draining', updated_at=?
                WHERE stream_id=? AND current_instance_id=? AND current_stream_epoch=?
                """,
                (now, fence.stream_id, fence.instance_id, fence.stream_epoch),
            )
        else:
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
        return update_ids

    @staticmethod
    def _dynamic_fence_from_instance(row: Mapping[str, Any]) -> DynamicContributorFence:
        return DynamicContributorFence(
            kind="dynamic",
            instance_id=str(row["instance_id"]),
            placement_id=str(row["placement_id"]),
            placement_epoch=int(row["placement_epoch"]),
            stream_id=int(row["stream_id"]),
            stream_epoch=int(row["stream_epoch"]),
            admission_generation=int(row["admission_generation"]),
            admission_token_sha256=str(row["admission_token_sha256"]),
        )

    @staticmethod
    def _dynamic_admission_result(
        connection: sqlite3.Connection,
        *,
        fence: DynamicContributorFence,
    ) -> dict[str, Any]:
        progress = connection.execute(
            "SELECT * FROM contributor_progress WHERE stable_contributor_key=?",
            (fence.stable_contributor_key,),
        ).fetchone()
        return {
            "fence": fence.as_dict(),
            "resume_cursor": 0 if progress is None else int(progress["data_cursor"]),
            "last_receipt_id": None if progress is None else progress["last_receipt_id"],
            "last_receipt_sha256": (None if progress is None else progress["last_receipt_sha256"]),
            "next_cycle_seq": 1 if progress is None else int(progress["last_cycle_seq"]) + 1,
        }

    def _require_current_fence(
        self,
        connection: sqlite3.Connection,
        fence: StaticContributorFence | DynamicContributorFence,
        *,
        update_id: str | None = None,
        allow_draining: bool = False,
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
                    AND i.admission_token_sha256=?
                    AND (i.status='admitted'
                        OR (? AND i.status='draining')
                        OR (i.status='draining' AND i.final_update_id=?))
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
                    int(allow_draining),
                    update_id,
                ),
            ).fetchone()
        if row is None:
            raise MembershipFenceError("contributor fence is stale or not admitted")

    def _require_terminal_input_allowed(
        self,
        connection: sqlite3.Connection,
        *,
        fence: StaticContributorFence | DynamicContributorFence,
        cycle_seq: int,
        update_id: str | None,
    ) -> None:
        controller = connection.execute(
            "SELECT state, generation FROM controller_state WHERE singleton=1"
        ).fetchone()
        if controller is None:
            raise AuthoritySchemaError("controller state is missing")
        if controller["state"] in {"open", "preclosing"}:
            return
        if controller["state"] not in {"closing", "draining"}:
            raise MembershipFenceError("terminal authority no longer accepts contributor input")
        frozen = connection.execute(
            """
            SELECT * FROM terminal_contributor_fences
            WHERE generation=? AND stable_contributor_key=?
            """,
            (int(controller["generation"]), fence.stable_contributor_key),
        ).fetchone()
        if frozen is None or frozen["fence_json"] != _canonical_json(fence.as_dict()):
            raise MembershipFenceError("input does not match an awaiting pre-close fence")
        declared_update = frozen["final_update_id"]
        state_allows_input = frozen["state"] == "awaiting_ack" or (
            frozen["state"] == "acked" and update_id is not None and declared_update == update_id
        )
        if not state_allows_input:
            raise MembershipFenceError("input does not match an awaiting pre-close fence")
        close_sequence = int(frozen["close_last_cycle_seq"])
        if cycle_seq not in {close_sequence, close_sequence + 1}:
            raise MembershipFenceError("terminal input exceeds the frozen current-cycle bound")
        if declared_update is not None and update_id != declared_update:
            raise MembershipFenceError("terminal proposal does not match the declared final update")

    def _require_current_fence_json(
        self,
        connection: sqlite3.Connection,
        fence_json: str,
        *,
        update_id: str | None = None,
        allow_draining: bool = False,
    ) -> None:
        payload = json.loads(fence_json)
        fence = (
            StaticContributorFence.from_dict(payload)
            if payload.get("kind") == "static"
            else DynamicContributorFence.from_dict(payload)
        )
        self._require_current_fence(
            connection, fence, update_id=update_id, allow_draining=allow_draining
        )

    def _fence_is_current_json(
        self,
        connection: sqlite3.Connection,
        fence_json: str,
        *,
        update_id: str | None = None,
    ) -> bool:
        try:
            self._require_current_fence_json(connection, fence_json, update_id=update_id)
        except (MembershipFenceError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return True

    def _verify_prepared_publication_artifacts(self, publication_id: str) -> None:
        from .object_store import verify_publication_artifact

        intent = self._authority._fetchone(
            "SELECT * FROM publication_intents WHERE publication_id=?", (publication_id,)
        )
        if intent is None or intent["state"] != "prepared":
            return
        for prefix in ("weight", "optim"):
            result = verify_publication_artifact(
                self._authority._run_root,
                str(intent[f"{prefix}_relative_path"]),
                expected_size=int(intent[f"{prefix}_size"]),
                expected_sha256=str(intent[f"{prefix}_sha256"]),
                expected_theta_sha256=str(intent["theta_sha256"]),
                theta_tensor_key=(None if prefix == "weight" else "theta"),
            )
            if result.status is not ReadStatus.OK:
                raise ValueError(
                    f"{prefix} publication artifact failed verification: "
                    f"{result.status.value}: {result.diagnostic}"
                )

    def _command_replay(
        self,
        command_id: str,
        kind: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return an already committed result before repeatable external I/O."""

        validate_identity(command_id, name="command_id")
        request_json = _canonical_json(dict(request))
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        self._authority._verify_token(self.token)
        existing = self._authority._fetchone(
            "SELECT * FROM command_records WHERE command_id=?",
            (command_id,),
        )
        self._authority._verify_token(self.token)
        if existing is None:
            existing = read_command_receipt(RunPaths(self._authority._run_root), command_id)
            self._authority._verify_token(self.token)
            if existing is None:
                return None
        if existing["command_kind"] != kind or existing["request_sha256"] != request_sha:
            raise CommandConflictError("command ID was replayed with a different kind or request")
        return json.loads(str(existing["result_json"]))

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
        archived = self._command_replay(command_id, kind, request)
        if archived is not None:
            return archived
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


def _audit_history_records(
    connection: sqlite3.Connection, cutoff_version: int
) -> list[dict[str, Any]]:
    """Return the exact dependency-closed rows safe to remove through a cutoff."""

    latest_row = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()
    latest_version = None if latest_row is None else latest_row[0]
    safe_cutoff = (
        -1 if latest_version is None else min(int(cutoff_version), int(latest_version) - 1)
    )
    controller = connection.execute(
        "SELECT state FROM controller_state WHERE singleton=1"
    ).fetchone()
    protect_current_receipts = int(
        controller is None or str(controller["state"]) not in {"finalized", "error"}
    )

    batch_rows = connection.execute(
        """
        SELECT b.* FROM selection_batches AS b
        WHERE b.target_version<=? AND b.state IN ('committed', 'abandoned')
          AND (
            ?=0 OR NOT EXISTS (
              SELECT 1 FROM selection_batch_updates AS bu
              JOIN updates AS u ON u.update_id=bu.update_id
              JOIN contributor_progress AS p ON p.last_receipt_id=u.cycle_receipt_id
              WHERE bu.batch_id=b.batch_id
            )
          )
        ORDER BY b.batch_id
        """,
        (safe_cutoff, protect_current_receipts),
    ).fetchall()
    batch_ids = {str(row["batch_id"]) for row in batch_rows}
    publication_rows = connection.execute(
        """
        SELECT * FROM publication_intents
        WHERE target_version<=? AND state IN ('committed', 'abandoned')
        ORDER BY publication_id
        """,
        (safe_cutoff,),
    ).fetchall()
    publication_rows = [
        row
        for row in publication_rows
        if row["selection_batch_id"] is None or str(row["selection_batch_id"]) in batch_ids
    ]
    publication_ids = {str(row["publication_id"]) for row in publication_rows}
    version_rows = [
        row
        for row in connection.execute(
            "SELECT * FROM global_versions WHERE version<=? ORDER BY version",
            (safe_cutoff,),
        ).fetchall()
        if str(row["publication_id"]) in publication_ids
    ]
    update_rows = connection.execute(
        """
        SELECT u.* FROM updates AS u
        LEFT JOIN contributor_progress AS p
          ON ?=1 AND p.last_receipt_id=u.cycle_receipt_id
        WHERE p.stable_contributor_key IS NULL
          AND u.status IN ('applied', 'dropped')
        ORDER BY u.update_id
        """,
        (protect_current_receipts,),
    ).fetchall()
    update_rows = [
        row
        for row in update_rows
        if (row["selected_batch_id"] is not None and str(row["selected_batch_id"]) in batch_ids)
        or (
            row["selected_batch_id"] is None
            and (
                int(row["base_global_version"]) <= safe_cutoff
                and (row["applied_version"] is None or int(row["applied_version"]) <= safe_cutoff)
            )
        )
    ]
    receipt_ids = {str(row["cycle_receipt_id"]) for row in update_rows}
    receipt_only = connection.execute(
        """
        SELECT r.* FROM cycle_receipts AS r
        JOIN token_fates AS f ON f.receipt_id=r.receipt_id
        JOIN contributor_progress AS p
            ON p.stable_contributor_key=r.stable_contributor_key
        WHERE r.cycle_seq < p.last_cycle_seq
          AND f.direct_fate='unpublished'
          AND NOT EXISTS (SELECT 1 FROM updates AS u WHERE u.cycle_receipt_id=r.receipt_id)
        ORDER BY r.receipt_id
        """
    ).fetchall()
    receipt_ids.update(str(row["receipt_id"]) for row in receipt_only)
    if receipt_ids:
        placeholders = ",".join("?" for _ in receipt_ids)
        receipt_rows = connection.execute(
            f"SELECT * FROM cycle_receipts WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id",
            tuple(sorted(receipt_ids)),
        ).fetchall()
        fate_rows = connection.execute(
            f"SELECT * FROM token_fates WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id",
            tuple(sorted(receipt_ids)),
        ).fetchall()
        observation_rows = connection.execute(
            f"""
            SELECT o.* FROM proposal_observations AS o
            JOIN cycle_receipts AS r
              ON r.stable_contributor_key=o.stable_contributor_key
             AND r.cycle_seq=o.cycle_seq
            WHERE r.receipt_id IN ({placeholders})
              AND NOT EXISTS (
                SELECT 1 FROM proposal_frontiers AS f
                WHERE f.terminal_observation_id=o.observation_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM proposal_visibility AS v
                WHERE v.terminal_observation_id=o.observation_id
              )
            ORDER BY o.observation_id
            """,
            tuple(sorted(receipt_ids)),
        ).fetchall()
    else:
        receipt_rows = []
        fate_rows = []
        observation_rows = []
    observation_ids = {int(row["observation_id"]) for row in observation_rows}
    if observation_ids:
        placeholders = ",".join("?" for _ in observation_ids)
        conflict_rows = connection.execute(
            f"""
            SELECT * FROM proposal_conflicts
            WHERE observation_id IN ({placeholders}) ORDER BY observation_id
            """,
            tuple(sorted(observation_ids)),
        ).fetchall()
    else:
        conflict_rows = []
    batch_update_rows = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_update_rows = connection.execute(
            f"""
            SELECT * FROM selection_batch_updates
            WHERE batch_id IN ({placeholders}) ORDER BY batch_id, update_id
            """,
            tuple(sorted(batch_ids)),
        ).fetchall()
    artifact_rows = []
    if publication_ids:
        placeholders = ",".join("?" for _ in publication_ids)
        artifact_rows = connection.execute(
            f"""
            SELECT * FROM artifact_publications
            WHERE publication_id IN ({placeholders}) ORDER BY publication_id, artifact_kind
            """,
            tuple(sorted(publication_ids)),
        ).fetchall()
    groups: tuple[tuple[str, str, list[sqlite3.Row]], ...] = (
        (
            "command_records",
            "command_id",
            list(
                connection.execute("SELECT * FROM command_records ORDER BY command_id").fetchall()
            ),
        ),
        ("proposal_conflicts", "observation_id", list(conflict_rows)),
        ("proposal_observations", "observation_id", list(observation_rows)),
        ("selection_batch_updates", "batch_update", list(batch_update_rows)),
        ("token_fates", "receipt_id", list(fate_rows)),
        ("updates", "update_id", list(update_rows)),
        ("cycle_receipts", "receipt_id", list(receipt_rows)),
        ("artifact_publications", "artifact", list(artifact_rows)),
        ("global_versions", "version", list(version_rows)),
        ("publication_intents", "publication_id", list(publication_rows)),
        ("selection_batches", "batch_id", list(batch_rows)),
    )
    records: list[dict[str, Any]] = []
    for table, key_kind, rows in groups:
        for row in rows:
            if key_kind == "batch_update":
                primary_key = f"{row['batch_id']}:{row['update_id']}"
            elif key_kind == "artifact":
                primary_key = f"{row['publication_id']}:{row['artifact_kind']}"
            else:
                primary_key = str(row[key_kind])
            records.append({"table": table, "primary_key": primary_key, "row": dict(row)})
    return sorted(records, key=lambda item: (item["table"], item["primary_key"]))


def _static_binding_command_request(
    *,
    learner_id: str,
    logical_launch_id: str,
    attempt_id: str,
    expected_generation: int | None,
    allow_logical_replacement: bool,
    replacement_reason: str | None,
    registration_created_at: float | None,
) -> dict[str, Any]:
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
    if registration_created_at is not None and (
        isinstance(registration_created_at, bool)
        or not isinstance(registration_created_at, (int, float))
        or not float("-inf") < float(registration_created_at) < float("inf")
    ):
        raise ValueError("registration_created_at must be a finite timestamp")
    return {
        "learner_id": learner_id,
        "logical_launch_id": logical_launch_id,
        "attempt_id": attempt_id,
        "expected_generation": expected_generation,
        "allow_logical_replacement": allow_logical_replacement,
        "replacement_reason": replacement_reason,
        "registration_created_at": registration_created_at,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode_committed_leader_lease(
    token: LeaderToken, row: Mapping[str, Any]
) -> CommittedLeaderLease:
    return CommittedLeaderLease(
        token=token,
        renewed_at=float(row["renewed_at"]),
        lease_expires_at=float(row["lease_expires_at"]),
        heartbeat_seq=int(row["heartbeat_seq"]),
    )


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
        theta_sha256=str(row["theta_sha256"]),
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
        theta_sha256=str(row["theta_sha256"]),
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
