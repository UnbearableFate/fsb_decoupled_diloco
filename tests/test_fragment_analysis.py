from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import fs_diloco.tools.analysis as analysis_runtime
from fs_diloco.legacy.reader import open_query_only_database


def _legacy_fragment_run(root: Path) -> Path:
    for relative in ("control", "fragments", "weights", "metrics", "heartbeats", "logs"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    index = {
        "format_version": 1,
        "strategy": "balanced_tensor",
        "num_fragments": 2,
        "total_numel": 4,
        "fragments": [
            {
                "fragment_id": 0,
                "numel": 2,
                "slices": [{"param_name": "a", "flat_start": 0, "flat_end": 2}],
            },
            {
                "fragment_id": 1,
                "numel": 2,
                "slices": [{"param_name": "b", "flat_start": 2, "flat_end": 4}],
            },
        ],
    }
    index_path = root / "fragments" / "fragment_index.json"
    index_path.write_text(json.dumps(index) + "\n", encoding="utf-8")
    materialized = root / "weights" / "global_v000002.safetensors"
    materialized.write_bytes(b"fixture")
    (root / "control" / "latest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "latest_kind": "fragment",
                "version": 2,
                "global_merge_event": 2,
                "fragment_index_path": str(index_path),
                "materialized_weight_path": str(materialized),
                "fragments": {"0": {"version": 1}, "1": {"version": 1}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    database = root / "control" / "syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE global_versions(version INTEGER PRIMARY KEY);
            INSERT INTO global_versions VALUES (0), (1), (2);
            CREATE TABLE updates(
                update_id TEXT PRIMARY KEY, learner_id TEXT, status TEXT,
                applied_version INTEGER, effective_weight REAL,
                mid_cycle_adoption_count INTEGER, base_switched_at_step INTEGER
            );
            CREATE TABLE fragment_proposal_frontiers(
                learner_id TEXT, fragment_id INTEGER,
                PRIMARY KEY(learner_id, fragment_id)
            );
            CREATE TABLE fragments(fragment_id INTEGER PRIMARY KEY);
            CREATE TABLE fragment_versions(
                fragment_id INTEGER, version INTEGER, global_merge_event INTEGER,
                status TEXT, num_updates INTEGER,
                PRIMARY KEY(fragment_id, version)
            );
            INSERT INTO fragment_versions VALUES (0, 1, 1, 'committed', 1);
            INSERT INTO fragment_versions VALUES (1, 1, 2, 'committed', 1);
            CREATE TABLE fragment_updates(
                update_id TEXT PRIMARY KEY, learner_id TEXT, status TEXT,
                applied_global_merge_event INTEGER, staleness_fragment_versions INTEGER
            );
            INSERT INTO fragment_updates VALUES ('f0', 'learner_000', 'applied', 1, 0);
            INSERT INTO fragment_updates VALUES ('f1', 'learner_001', 'applied', 2, 0);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return database


def test_fragment_v0_analysis_uses_legacy_pure_decoders(tmp_path: Path) -> None:
    root = tmp_path / "legacy-fragment"
    database = _legacy_fragment_run(root)

    summary = analysis_runtime.summarize_run(root, database)

    assert summary["latest_kind"] == "fragment"
    assert summary["fragment_versions"] == {"0": 1, "1": 1}
    assert summary["fragment_sizes"] == {
        "min": 2,
        "max": 2,
        "mean": 2.0,
        "imbalance_ratio": 0.0,
    }
    assert summary["materialized_weight_exists"] is True
    assert summary["db"]["fragment_applied_updates"] == 2


def test_analysis_opens_authority_database_query_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "legacy-fragment"
    database = _legacy_fragment_run(root)
    observed: list[int] = []

    def checked(path: Path) -> sqlite3.Connection:
        connection = open_query_only_database(path)
        observed.append(int(connection.execute("PRAGMA query_only").fetchone()[0]))
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value INTEGER)")
        return connection

    monkeypatch.setattr(analysis_runtime, "open_readonly", checked)

    assert analysis_runtime.summarize_run(root, database)["db"]["integrity_ok"] is True
    assert observed == [1]
