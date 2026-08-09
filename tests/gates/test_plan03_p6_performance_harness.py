from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HARNESS = PROJECT_ROOT / "scripts/miyabi/plan03_p6_performance.py"


def _load_harness():
    specification = importlib.util.spec_from_file_location("plan03_p6_performance", HARNESS)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_dynamic_comparison_adds_omitted_static_terminal_defaults(tmp_path: Path) -> None:
    module = _load_harness()
    source = PROJECT_ROOT / "configs/fs_diloco_tiny_ha_static.yaml"
    original = source.read_bytes()

    configs = module._prepare_configs(
        current_root=PROJECT_ROOT,
        classic_root=None,
        scratch=tmp_path,
        comparison="dynamic",
    )

    baseline = yaml.safe_load(configs["baseline"].read_text(encoding="utf-8"))
    candidate = yaml.safe_load(configs["candidate"].read_text(encoding="utf-8"))
    assert baseline["terminal"]["max_terminal_merges"] == 0
    assert baseline["learner"]["post_publish_latest_wait_seconds"] == module.ARM_TIMEOUT_SECONDS
    assert candidate["learner"]["post_publish_latest_wait_seconds"] == module.ARM_TIMEOUT_SECONDS
    assert source.read_bytes() == original


def test_frozen_classic_ref_records_tag_object_and_peeled_commit() -> None:
    module = _load_harness()

    identity = module._classic_ref_identity(PROJECT_ROOT)

    assert identity["ref"] == "archive/classic-full-v1-final"
    assert identity["object_type"] == "tag"
    assert identity["object_id"] != identity["commit"]
    object_type = subprocess.run(
        ["git", "cat-file", "-t", identity["commit"]],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert object_type == "commit"


def test_classic_summary_joins_archived_history_with_hot_authority(tmp_path: Path) -> None:
    module = _load_harness()
    control = tmp_path / "control"
    metrics = tmp_path / "metrics"
    control.mkdir()
    metrics.mkdir()
    connection = sqlite3.connect(control / "syncer_metadata.sqlite3")
    connection.executescript(
        """
        CREATE TABLE global_versions (
            version INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            num_updates INTEGER NOT NULL,
            total_update_tokens INTEGER NOT NULL,
            total_seen_tokens INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE updates (
            update_id TEXT PRIMARY KEY,
            learner_id TEXT NOT NULL,
            local_step_end INTEGER NOT NULL,
            tokens_this_update INTEGER NOT NULL,
            applied_version INTEGER,
            status TEXT NOT NULL
        );
        INSERT INTO global_versions VALUES (2, 3.0, 2, 128, 256, 'committed');
        """
    )
    connection.commit()
    connection.close()
    archived_versions = [
        {
            "version": 0,
            "created_at": 1.0,
            "num_updates": 0,
            "total_update_tokens": 0,
            "total_seen_tokens": 0,
            "status": "committed",
            "archived_at": 4.0,
        },
        {
            "version": 1,
            "created_at": 2.0,
            "num_updates": 2,
            "total_update_tokens": 128,
            "total_seen_tokens": 128,
            "status": "committed",
            "archived_at": 4.0,
        },
    ]
    archived_updates = [
        {
            "update_id": f"u{version}-{learner}",
            "learner_id": f"learner_{learner:03d}",
            "local_step_end": version * 2,
            "tokens_this_update": 64,
            "applied_version": version,
            "status": "applied",
            "archived_at": 4.0,
        }
        for version in (1, 2)
        for learner in range(2)
    ]
    (metrics / "global_version_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in archived_versions), encoding="utf-8"
    )
    (metrics / "update_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in archived_updates), encoding="utf-8"
    )

    summary = module._classic_summary(tmp_path)

    assert summary == {
        "final_version": 2,
        "processed_tokens": 256,
        "direct_weight_tokens": 256,
        "selected_count": 4,
        "cursor_identity": [4, 4],
        "active_protocol_seconds": 2.0,
    }


def test_classic_and_current_configs_share_post_publish_barrier(tmp_path: Path) -> None:
    module = _load_harness()
    classic_root = tmp_path / "classic"
    classic_configs = classic_root / "configs"
    classic_configs.mkdir(parents=True)
    frozen_config = subprocess.run(
        [
            "git",
            "show",
            f"{module.CLASSIC_REF}:configs/fs_diloco_tiny_ha_static.yaml",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (classic_configs / "fs_diloco_tiny_ha_static.yaml").write_text(frozen_config, encoding="utf-8")

    configs = module._prepare_configs(
        current_root=PROJECT_ROOT,
        classic_root=classic_root,
        scratch=tmp_path,
        comparison="classic",
    )

    baseline = yaml.safe_load(configs["baseline"].read_text(encoding="utf-8"))
    candidate = yaml.safe_load(configs["candidate"].read_text(encoding="utf-8"))
    for config in (baseline, candidate):
        assert config["training"]["completion_mode"] == "global_only"
        assert config["learner"]["post_publish_latest_wait_seconds"] == module.ARM_TIMEOUT_SECONDS
        assert config["learner"]["post_publish_latest_poll_seconds"] == 0.2
