from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.legacy.reader import LegacyRunReader, export_legacy_summary


FRAGMENT_TABLES = (
    "fragment_proposal_frontiers",
    "fragments",
    "fragment_versions",
    "fragment_updates",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_fixture(root: Path, *, fragment: bool) -> Path:
    (root / "control").mkdir(parents=True)
    database = root / "control" / "syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE run_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);
            CREATE TABLE global_versions(
                version INTEGER PRIMARY KEY, weight_path TEXT, optim_path TEXT,
                created_at REAL, num_updates INTEGER, total_update_tokens INTEGER,
                total_seen_tokens INTEGER, outer_optimizer TEXT, status TEXT, notes TEXT
            );
            CREATE TABLE updates(
                update_id TEXT PRIMARY KEY, learner_id TEXT, status TEXT,
                tokens_this_update INTEGER, applied_version INTEGER, drop_reason TEXT
            );
            INSERT INTO run_state VALUES ('mode', 'full', 1.0);
            INSERT INTO global_versions VALUES
                (0, 'weights/global_v000000.safetensors', 'optim/global_v000000.pt',
                 1.0, 0, 0, 0, 'nesterov', 'committed', NULL),
                (1, 'weights/global_v000001.safetensors', 'optim/global_v000001.pt',
                 2.0, 1, 32, 32, 'nesterov', 'committed', NULL);
            INSERT INTO updates VALUES ('u1', 'learner_000', 'applied', 32, 1, NULL);
            """
        )
        if fragment:
            connection.executescript(
                """
                CREATE TABLE fragment_proposal_frontiers(
                    learner_id TEXT, fragment_id INTEGER, last_observed_update_id TEXT,
                    last_pointer_path TEXT, observed_at REAL,
                    PRIMARY KEY(learner_id, fragment_id)
                );
                CREATE TABLE fragments(
                    fragment_id INTEGER PRIMARY KEY, strategy TEXT, numel INTEGER,
                    size_bytes INTEGER, slices_json TEXT, created_at REAL
                );
                CREATE TABLE fragment_versions(
                    fragment_id INTEGER, version INTEGER, global_merge_event INTEGER,
                    weight_path TEXT, optim_path TEXT, created_at REAL, num_updates INTEGER,
                    total_update_tokens INTEGER, total_seen_tokens INTEGER,
                    outer_optimizer TEXT, status TEXT, notes TEXT,
                    PRIMARY KEY(fragment_id, version)
                );
                CREATE TABLE fragment_updates(
                    update_id TEXT PRIMARY KEY, learner_id TEXT, fragment_id INTEGER,
                    base_fragment_version INTEGER, tokens_this_update INTEGER,
                    status TEXT, applied_fragment_version INTEGER,
                    applied_global_merge_event INTEGER, drop_reason TEXT
                );
                INSERT INTO fragments VALUES (0, 'balanced_tensor', 4, 16, '[]', 1.0);
                INSERT INTO fragment_versions VALUES
                    (0, 1, 1, 'fragments/weights/f0_v1.safetensors',
                     'fragments/optim/f0_v1.pt', 2.0, 1, 32, 32,
                     'nesterov', 'committed', NULL);
                INSERT INTO fragment_updates VALUES
                    ('fu1', 'learner_000', 0, 0, 32, 'applied', 1, 1, NULL);
                """
            )
        connection.commit()
    finally:
        connection.close()
    (root / "control" / "latest.json").write_text(
        json.dumps({"format_version": 1, "latest_kind": "fragment" if fragment else "full"}) + "\n",
        encoding="utf-8",
    )
    return database


@pytest.mark.parametrize("fragment", [False, True], ids=["full-v1-v3", "fragment-v0"])
def test_legacy_reader_is_query_only_and_preserves_fixture_bytes(
    tmp_path: Path, fragment: bool
) -> None:
    root = tmp_path / "legacy"
    database = _legacy_fixture(root, fragment=fragment)
    before = {
        path: (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }

    with LegacyRunReader(root, database_path=database) as reader:
        assert reader.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute("CREATE TABLE forbidden(value INTEGER)")
        summary = reader.summary()

    assert summary["semantic_version"] == "legacy-v1-v3-query-only"
    assert summary["legacy_total_seen_tokens"] == 32
    assert summary["legacy_total_seen_tokens_semantics"] == (
        "classic committed selected-proposal tokens; no v4 token-ledger conversion"
    )
    assert summary["fragment_v0_tables"] == (list(FRAGMENT_TABLES) if fragment else [])
    after = {
        path: (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }
    assert after == before


def test_legacy_export_must_be_outside_source_root_and_keeps_legacy_labels(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    database = _legacy_fixture(root, fragment=True)
    with pytest.raises(ValueError, match="outside the legacy run root"):
        export_legacy_summary(root, root / "export.json", database_path=database)

    output = tmp_path / "exports" / "summary.json"
    payload = export_legacy_summary(root, output, database_path=database)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "total_seen_tokens" not in payload
    assert payload["legacy_total_seen_tokens"] == 32
