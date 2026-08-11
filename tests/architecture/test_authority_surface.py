"""Verify the explicit neutral authority API and sole DDL owner."""

from __future__ import annotations

import ast
from pathlib import Path

from fs_diloco.storage.authority import AuthorityReader, LeaderAuthority, LeaderSession


ROOT = Path(__file__).resolve().parents[2]


def test_authority_surfaces_have_no_dynamic_dispatch_or_public_sql_escape() -> None:
    """Authority callers cannot reach dynamic dispatch or public raw SQL."""

    for cls in (AuthorityReader, LeaderAuthority, LeaderSession):
        assert "__getattr__" not in cls.__dict__
        assert "execute" not in cls.__dict__
        assert "executemany" not in cls.__dict__
        assert "conn" not in cls.__dict__
        assert "connection" not in cls.__dict__


def test_named_write_commands_are_explicit_methods() -> None:
    """Every current write command is an explicit LeaderSession method."""

    expected = {
        "initialize_genesis",
        "initialize_membership",
        "admit_incarnation",
        "plan_launch_request",
        "transition_launch_request",
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


def test_authority_module_owns_the_canonical_schema_and_read_write_surfaces() -> None:
    """The authority module owns the only schema bundle and read/write surfaces."""

    source = (ROOT / "fs_diloco/storage/authority.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = {
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }

    assert {"AuthorityReader", "LeaderAuthority", "LeaderSession", "initialize_authority"} <= (
        definitions
    )
    assert source.count('BASE_SCHEMA_NAME = "schema.sql"') == 1
    assert "schema_dynamic.sql" not in source
