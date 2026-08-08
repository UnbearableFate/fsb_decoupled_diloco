from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import DynamicMembershipScope, StaticMembershipScope
from fs_diloco.storage import authority as authority_module
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    AuthoritySchemaError,
    LeaderAuthority,
    ddl_bundle_sha256,
    initialize_authority_v4,
)


def identity() -> AuthorityIdentity:
    return AuthorityIdentity("run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest())


@pytest.mark.parametrize(
    ("scope", "dynamic_expected"),
    [(StaticMembershipScope(("learner-0",)), False), (DynamicMembershipScope(2), True)],
)
def test_fresh_v4_schema_initializes_reopens_and_is_integral(
    tmp_path: Path,
    scope: StaticMembershipScope | DynamicMembershipScope,
    dynamic_expected: bool,
) -> None:
    database = tmp_path / "authority.sqlite3"
    metadata = initialize_authority_v4(database, identity(), scope)

    with LeaderAuthority(database, identity(), scope, wall_clock=lambda: 100.0) as authority:
        tables = set(authority.read.table_names())
        assert authority.read.integrity_check() == ("ok",)
        assert metadata.ddl_sha256 == ddl_bundle_sha256(metadata.mode)
        assert ("learner_instances" in tables) is dynamic_expected
        assert "static_contributor_bindings" in tables
        assert "publication_intents" in tables
        assert "candidate_launch_outbox" in tables
        assert not (tables & {"fragments", "fragment_updates", "fragment_versions"})


def test_v4_open_rejects_mode_identity_and_schema_feature_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    static = StaticMembershipScope(("learner-0",))
    initialize_authority_v4(database, identity(), static)

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


def test_v4_open_detects_out_of_band_ddl_even_when_metadata_hash_is_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority_v4(database, identity(), scope)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unauthorized_table(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(AuthoritySchemaError, match="schema_fingerprint"):
        LeaderAuthority(database, identity(), scope)


def test_dynamic_stream_pool_is_initialized_by_explicit_fenced_command(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(2)
    initialize_authority_v4(database, identity(), scope, wall_clock=lambda: 100.0)
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


def test_v4_open_verifies_configured_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority_v4(database, identity(), scope)
    configure = authority_module._configure_connection

    def configure_wrong_timeout(connection: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
        configure(connection, busy_timeout_ms=busy_timeout_ms + 1)

    monkeypatch.setattr(authority_module, "_configure_connection", configure_wrong_timeout)

    with pytest.raises(AuthoritySchemaError, match="busy_timeout"):
        LeaderAuthority(database, identity(), scope, busy_timeout_ms=5000)
