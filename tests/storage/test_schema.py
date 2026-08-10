from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import DynamicMembershipScope, StaticMembershipScope
from fs_diloco.storage import authority as authority_module
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    AuthorityReader,
    AuthoritySchemaError,
    LeaderAuthority,
    ddl_bundle_sha256,
    initialize_authority,
)
from fs_diloco.core.versions import AUTHORITY_SCHEMA_VERSION


def identity() -> AuthorityIdentity:
    return AuthorityIdentity(
        "run-current",
        "source-fingerprint",
        hashlib.sha256(b"config").hexdigest(),
    )


@pytest.mark.parametrize(
    ("scope", "dynamic_expected"),
    [(StaticMembershipScope(("learner-0",)), False), (DynamicMembershipScope(2), True)],
)
def test_fresh_schema_initializes_reopens_and_is_integral(
    tmp_path: Path,
    scope: StaticMembershipScope | DynamicMembershipScope,
    dynamic_expected: bool,
) -> None:
    database = tmp_path / "authority.sqlite3"
    metadata = initialize_authority(database, identity(), scope)

    with LeaderAuthority(database, identity(), scope, wall_clock=lambda: 100.0) as authority:
        tables = set(authority.read.table_names())
        assert authority.read.integrity_check() == ("ok",)
        assert metadata.ddl_sha256 == ddl_bundle_sha256(metadata.mode)
        assert AUTHORITY_SCHEMA_VERSION == 9
        assert metadata.schema_version == 9
        assert ("learner_instances" in tables) is dynamic_expected
        assert "static_contributor_bindings" in tables
        assert "publication_intents" in tables
        assert ("launch_requests" in tables) is dynamic_expected
        assert ("scheduler_operator_requests" in tables) is dynamic_expected
        assert ("scheduler_operator_file_dispositions" in tables) is dynamic_expected
        if dynamic_expected:
            with sqlite3.connect(database) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(launch_requests)").fetchall()
                }
            assert {"reservation_released_at", "stream_id", "replace_instance_id"} <= columns
    marker = database.with_name("bootstrap_complete.json")
    assert marker.stat().st_mode & 0o222 == 0

    with AuthorityReader(database, identity(), scope) as reader:
        assert reader.read.integrity_check() == ("ok",)
        with pytest.raises(sqlite3.OperationalError, match="readonly|query only"):
            reader._connection.execute("DELETE FROM run_identity")


@pytest.mark.parametrize("collision_kind", ["database", "marker"])
def test_fresh_schema_rejects_broken_symlink_collisions_without_partial_publish(
    tmp_path: Path, collision_kind: str
) -> None:
    database = tmp_path / "authority.sqlite3"
    marker = database.with_name("bootstrap_complete.json")
    collision = database if collision_kind == "database" else marker
    collision.symlink_to(tmp_path / f"missing-{collision_kind}")

    with pytest.raises(FileExistsError, match="already exists"):
        initialize_authority(
            database,
            identity(),
            StaticMembershipScope(("learner-0",)),
        )

    assert collision.is_symlink()
    if collision_kind == "marker":
        assert not database.exists()


def test_open_rejects_mode_identity_and_schema_feature_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    static = StaticMembershipScope(("learner-0",))
    initialize_authority(database, identity(), static)

    with pytest.raises(AuthoritySchemaError, match="mode"):
        LeaderAuthority(database, identity(), DynamicMembershipScope(1))

    other = AuthorityIdentity("other-run", "source-fingerprint", identity().config_sha256)
    with pytest.raises(AuthoritySchemaError, match="identity"):
        LeaderAuthority(database, other, static)

    connection = sqlite3.connect(database)
    connection.execute("UPDATE schema_meta SET features_json='[\"unexpected\"]'")
    connection.commit()
    connection.close()
    with pytest.raises(AuthoritySchemaError, match="features"):
        LeaderAuthority(database, identity(), static)


def test_query_reader_rejects_symlinked_authority_identity_files(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    marker = tmp_path / "bootstrap_complete.json"
    scope = StaticMembershipScope(("learner-0",))
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
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority(database, identity(), scope)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=4")
    connection.close()

    with pytest.raises(AuthoritySchemaError, match="user_version"):
        LeaderAuthority(database, identity(), scope)


def test_open_detects_out_of_band_ddl_even_when_metadata_hash_is_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority(database, identity(), scope)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unauthorized_table(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(AuthoritySchemaError, match="schema_fingerprint"):
        LeaderAuthority(database, identity(), scope)


def test_dynamic_stream_pool_is_initialized_by_explicit_fenced_command(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(2)
    initialize_authority(database, identity(), scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity(), scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)

        assert leader.initialize_dynamic_membership(command_id="initialize-membership") == (
            0,
            1,
        )
        assert leader.initialize_dynamic_membership(command_id="initialize-membership") == (
            0,
            1,
        )


def test_open_verifies_configured_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority(database, identity(), scope)
    configure = authority_module._configure_connection

    def configure_wrong_timeout(connection: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
        configure(connection, busy_timeout_ms=busy_timeout_ms + 1)

    monkeypatch.setattr(authority_module, "_configure_connection", configure_wrong_timeout)

    with pytest.raises(AuthoritySchemaError, match="busy_timeout"):
        LeaderAuthority(database, identity(), scope, busy_timeout_ms=5000)
