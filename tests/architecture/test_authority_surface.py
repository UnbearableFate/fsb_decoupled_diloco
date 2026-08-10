from __future__ import annotations

import ast
from pathlib import Path

from fs_diloco.storage.authority import LeaderAuthority, LeaderSession


ROOT = Path(__file__).resolve().parents[2]


def test_new_authority_has_no_dynamic_dispatch_or_public_sql_escape() -> None:
    for cls in (LeaderAuthority, LeaderSession):
        assert "__getattr__" not in cls.__dict__
        assert "execute" not in cls.__dict__
        assert "executemany" not in cls.__dict__
        assert "conn" not in cls.__dict__
        assert "connection" not in cls.__dict__


def test_named_write_commands_are_explicit_methods() -> None:
    expected = {
        "bind_or_replace_static_attempt",
        "mark_static_attempt_terminal",
        "initialize_v0",
        "initialize_dynamic_membership",
        "admit_dynamic_incarnation",
        "ingest_cycle_receipt",
        "ingest_proposal",
        "observe_proposal_visibility",
        "try_select_batch",
        "prepare_publication",
        "commit_merge",
        "abandon_publication",
        "reconcile_publications",
        "claim_orphan_gc",
        "retire_incarnation",
        "begin_terminal_close",
        "finalize_terminal",
    }

    assert expected <= set(LeaderSession.__dict__)


def test_authority_module_does_not_construct_a_second_schema_or_proxy_store() -> None:
    source = (ROOT / "fs_diloco/storage/authority.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "FencedSQLiteStore" not in imported
    assert "SQLiteStore" not in imported
    assert "initialize_new_run" not in imported
    assert source.count('BASE_SCHEMA_NAME = "schema.sql"') == 1
    assert source.count('DYNAMIC_SCHEMA_NAME = "schema_dynamic.sql"') == 1
