"""Verify the sole authority DDL, identity marker, and open-time checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.core.versions import AUTHORITY_SCHEMA_VERSION
from fs_diloco.protocol.contributor import MembershipScope
from fs_diloco.storage import authority as authority_module
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    AuthorityReader,
    AuthoritySchemaError,
    LeaderAuthority,
    ddl_bundle_sha256,
    initialize_authority,
)


def identity() -> AuthorityIdentity:
    """Return one stable authority identity shared by schema tests."""

    return AuthorityIdentity(
        "run-current",
        "source-fingerprint",
        hashlib.sha256(b"config").hexdigest(),
    )


def test_fresh_schema_initializes_reopens_and_is_integral(tmp_path: Path) -> None:
    """A fresh authority publishes the one complete schema and immutable marker."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(2)
    metadata = initialize_authority(database, identity(), scope)

    with LeaderAuthority(database, identity(), scope, wall_clock=lambda: 100.0) as authority:
        tables = set(authority.read.table_names())
        assert authority.read.integrity_check() == ("ok",)
        assert metadata.ddl_sha256 == ddl_bundle_sha256()
        assert metadata.schema_version == AUTHORITY_SCHEMA_VERSION == 12
        assert {
            "streams",
            "learner_instances",
            "launch_requests",
            "scheduler_operator_requests",
            "scheduler_operator_file_dispositions",
            "publication_intents",
        } <= tables
        assert {"static_contributor_bindings", "static_binding_history"}.isdisjoint(tables)
        with sqlite3.connect(database) as connection:
            launch_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(launch_requests)").fetchall()
            }
            progress_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(contributor_progress)").fetchall()
            }
            epoch_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='syncer_epochs'"
            ).fetchone()[0]
            all_columns = {
                str(row[1])
                for table in tables
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
        assert {"reservation_released_at", "stream_id", "replace_instance_id"} <= launch_columns
        assert "last_update_id" in progress_columns
        assert "fence_kind" not in all_columns
        assert "'terminal'" not in epoch_sql
        assert all(value in epoch_sql for value in ("'released'", "'expired'", "'error'"))

    marker = database.with_name("bootstrap_complete.json")
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert {"mode", "features"}.isdisjoint(marker_payload)
    assert marker.stat().st_mode & 0o222 == 0

    with AuthorityReader(database, identity(), scope) as reader:
        assert reader.read.integrity_check() == ("ok",)
        with pytest.raises(sqlite3.OperationalError, match="readonly|query only"):
            reader._connection.execute("DELETE FROM run_identity")


@pytest.mark.parametrize("collision_kind", ["database", "marker"])
def test_fresh_schema_rejects_broken_symlink_collisions_without_partial_publish(
    tmp_path: Path, collision_kind: str
) -> None:
    """Initialization fails closed when either create-once identity path is a symlink."""

    database = tmp_path / "authority.sqlite3"
    marker = database.with_name("bootstrap_complete.json")
    collision = database if collision_kind == "database" else marker
    collision.symlink_to(tmp_path / f"missing-{collision_kind}")

    with pytest.raises(FileExistsError, match="already exists"):
        initialize_authority(database, identity(), MembershipScope(1))

    assert collision.is_symlink()
    if collision_kind == "marker":
        assert not database.exists()


def test_open_rejects_membership_and_run_identity_mismatch(tmp_path: Path) -> None:
    """Reopening binds both the immutable stream-pool size and run identity."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, identity(), scope)

    with pytest.raises(AuthoritySchemaError, match="membership scope"):
        LeaderAuthority(database, identity(), MembershipScope(2))

    other = AuthorityIdentity("other-run", "source-fingerprint", identity().config_sha256)
    with pytest.raises(AuthoritySchemaError, match="identity"):
        LeaderAuthority(database, other, scope)


def test_query_reader_rejects_symlinked_authority_identity_files(tmp_path: Path) -> None:
    """Read-only opens require regular database and marker files at their exact paths."""

    database = tmp_path / "authority.sqlite3"
    marker = tmp_path / "bootstrap_complete.json"
    scope = MembershipScope(1)
    initialize_authority(database, identity(), scope)
    real_database = tmp_path / "authority-real.sqlite3"
    database.rename(real_database)
    database.symlink_to(real_database)
    with pytest.raises(AuthoritySchemaError, match="regular file"):
        AuthorityReader(database, identity(), scope)

    database.unlink()
    real_database.rename(database)
    real_marker = tmp_path / "bootstrap-real.json"
    marker.rename(real_marker)
    marker.symlink_to(real_marker)
    with pytest.raises(AuthoritySchemaError, match="immutable regular file"):
        AuthorityReader(database, identity(), scope)


def test_noncurrent_authority_schema_revision_fails_with_explicit_diagnostic(
    tmp_path: Path,
) -> None:
    """Legacy SQLite user versions are rejected instead of migrated."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, identity(), scope)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=4")
    connection.close()

    with pytest.raises(AuthoritySchemaError, match="user_version"):
        LeaderAuthority(database, identity(), scope)


def test_open_detects_out_of_band_ddl_even_when_metadata_hash_is_unchanged(
    tmp_path: Path,
) -> None:
    """The live schema fingerprint detects unauthorized tables after initialization."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, identity(), scope)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unauthorized_table(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(AuthoritySchemaError, match="schema_fingerprint"):
        LeaderAuthority(database, identity(), scope)


def test_stream_pool_is_initialized_by_explicit_fenced_command(tmp_path: Path) -> None:
    """Membership initialization is idempotent under one durable command identity."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(2)
    initialize_authority(database, identity(), scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity(), scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)

        assert leader.initialize_membership(command_id="initialize-membership") == (0, 1)
        assert leader.initialize_membership(command_id="initialize-membership") == (0, 1)


def test_open_verifies_configured_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authority detects a connection whose applied busy timeout differs from policy."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, identity(), scope)
    configure = authority_module._configure_connection

    def configure_wrong_timeout(connection: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
        """Apply a deliberately wrong timeout to exercise the open-time assertion."""

        configure(connection, busy_timeout_ms=busy_timeout_ms + 1)

    monkeypatch.setattr(authority_module, "_configure_connection", configure_wrong_timeout)

    with pytest.raises(AuthoritySchemaError, match="busy_timeout"):
        LeaderAuthority(database, identity(), scope, busy_timeout_ms=5000)
