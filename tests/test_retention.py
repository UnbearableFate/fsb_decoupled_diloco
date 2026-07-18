import json
import sqlite3
import time
from pathlib import Path

import pytest

from fs_diloco.storage.maintenance import (
    archive_and_prune,
    collect_runtime_artifacts,
    run_maintenance,
)
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _metadata(update_id, payload):
    now = time.time()
    return {
        "update_id": update_id,
        "learner_id": "learner_000",
        "hostname": "host",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 1,
        "inner_steps": 1,
        "tokens_this_update": 10,
        "tokens_since_global_load": 10,
        "file_path": str(payload),
        "created_at": now,
        "committed_at": now,
    }


def _initialized_state(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    v0_weight = paths.global_weight_path(0)
    v0_outer = paths.outer_optim_path(0)
    v0_weight.write_text("w0", encoding="utf-8")
    v0_outer.write_text("o0", encoding="utf-8")
    store = SQLiteStore(paths.sqlite_db)
    store.initialize_full_run(
        weight_path=str(v0_weight),
        optim_path=str(v0_outer),
        outer_optimizer="nesterov",
        identity={"run_id": "bounded", "protocol_version": 2},
        config_snapshot={},
    )
    return paths, store


def test_archive_prune_and_reference_driven_current_only_gc(tmp_path):
    paths, store = _initialized_state(tmp_path)
    payload = paths.update_payload_dir("learner_000") / "u0.params.safetensors"
    payload.write_text("proposal", encoding="utf-8")
    metadata = _metadata("u0", payload)
    paths.update_pointer_path("learner_000").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    store.insert_update_metadata(
        metadata, pointer_path=paths.update_pointer_path("learner_000")
    )
    selected = [store.get_update("u0")]
    store.mark_updates_selected(["u0"], "selection")

    v1_weight = paths.global_weight_path(1)
    v1_outer = paths.outer_optim_path(1)
    v1_weight.write_text("w1", encoding="utf-8")
    v1_outer.write_text("o1", encoding="utf-8")
    store.commit_full_merge(
        predecessor_version=0,
        target_version=1,
        weight_path=str(v1_weight),
        optim_path=str(v1_outer),
        selected_updates=selected,
        effective_weights={"u0": 1.0},
        total_update_tokens=10,
        total_seen_tokens=10,
        outer_optimizer="nesterov",
        max_staleness_versions=0,
    )
    high_orphan = paths.global_weight_path(2)
    high_orphan.write_text("orphan", encoding="utf-8")

    result = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=1.0,
        scan_interval_seconds=0.1,
    )

    assert {key: result[key] for key in (
        "archived_updates",
        "archived_versions",
        "deleted_artifacts",
    )} == {
        "archived_updates": 1,
        "archived_versions": 1,
        "deleted_artifacts": 4,
    }
    assert result["gc_pending_rows"] == 0
    assert result["maintenance_scanned_rows"] == 1
    assert result["maintenance_scan_seconds"] >= 0.0
    assert sorted(path.name for path in paths.weights.glob("*.safetensors")) == [
        "global_v000001.safetensors"
    ]
    assert sorted(path.name for path in paths.optim.glob("*.safetensors")) == [
        "outer_v000001.safetensors"
    ]
    assert not payload.exists()
    assert store.conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM global_versions").fetchone()[0] == 1
    assert paths.update_history_jsonl.exists()
    assert paths.global_version_history_jsonl.exists()
    store.close()


def test_unpublished_payload_grace_and_temp_cleanup(tmp_path):
    paths, store = _initialized_state(tmp_path)
    orphan = paths.update_payload_dir("learner_000") / "orphan.params.safetensors"
    orphan.write_text("partial publication", encoding="utf-8")
    tmp = paths.update_payload_dir("learner_000") / ".orphan.tmp"
    tmp.write_text("temp", encoding="utf-8")

    assert collect_runtime_artifacts(
        store,
        paths,
        orphan_grace_seconds=60.0,
        now=time.time(),
    ) == 0
    assert orphan.exists() and tmp.exists()

    result = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=30.0,
        scan_interval_seconds=1.0,
        input_closed=True,
    )
    assert {key: result[key] for key in (
        "archived_updates",
        "archived_versions",
        "deleted_artifacts",
    )} == {
        "archived_updates": 0,
        "archived_versions": 0,
        "deleted_artifacts": 2,
    }
    assert result["gc_pending_rows"] == 0
    assert result["maintenance_scanned_rows"] == 0
    assert not orphan.exists() and not tmp.exists()
    store.close()


def test_temp_cleanup_tolerates_atomic_writer_renaming_after_glob(tmp_path, monkeypatch):
    paths, store = _initialized_state(tmp_path)
    tmp = paths.update_payload_dir("learner_000") / ".concurrent.tmp"
    tmp.write_text("in flight", encoding="utf-8")
    original_stat = Path.stat

    def stat_after_concurrent_rename(path, *args, **kwargs):
        if path == tmp:
            path.unlink()
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_after_concurrent_rename)

    assert collect_runtime_artifacts(
        store,
        paths,
        orphan_grace_seconds=60.0,
        now=time.time(),
    ) == 0
    store.close()


def test_gc_pending_insert_rolls_back_with_archived_row_delete(tmp_path):
    paths, store = _initialized_state(tmp_path)
    payload = paths.update_payload_dir("learner_000") / "rollback.params.safetensors"
    payload.write_text("proposal", encoding="utf-8")
    metadata = _metadata("rollback", payload)
    store.insert_update_metadata(metadata)
    store.drop_updates(["rollback"], "test_terminal")
    update_rows = store.terminal_update_rows()
    store.conn.execute(
        """
        CREATE TRIGGER abort_archived_update_delete
        BEFORE DELETE ON updates
        BEGIN
          SELECT RAISE(ABORT, 'injected delete failure');
        END
        """
    )
    store.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected delete failure"):
        store.delete_archived_rows(update_rows=update_rows, version_rows=[])

    assert store.get_update("rollback") is not None
    assert store.gc_pending_count() == 0
    store.close()


def test_gc_pending_is_idempotent_persistent_and_recovers_archive_unlink_crash(tmp_path):
    paths, store = _initialized_state(tmp_path)
    payload = paths.update_payload_dir("learner_000") / "recover.params.safetensors"
    payload.write_text("proposal", encoding="utf-8")
    metadata = _metadata("recover", payload)
    store.insert_update_metadata(metadata)
    store.drop_updates(["recover"], "test_terminal")
    update_rows = store.terminal_update_rows()

    archived = archive_and_prune(store, paths)
    assert archived["updates"] == 1
    assert payload.exists()
    assert store.gc_pending_count() == 1
    store.delete_archived_rows(update_rows=update_rows, version_rows=[])
    assert store.gc_pending_count() == 1
    store.close()

    reopened = SQLiteStore(paths.sqlite_db)
    assert reopened.gc_pending_paths() == {payload.resolve(strict=False)}
    assert collect_runtime_artifacts(
        reopened,
        paths,
        orphan_grace_seconds=3600.0,
    ) == 1
    assert not payload.exists()
    assert reopened.gc_pending_count() == 0
    reopened.close()
