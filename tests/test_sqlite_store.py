import time

from fs_diloco.storage.sqlite_store import SQLiteStore


def _metadata(update_id="u1", local_step_end=1):
    now = time.time()
    return {
        "update_id": update_id,
        "learner_id": "learner_000",
        "hostname": "host",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": local_step_end,
        "inner_steps": 1,
        "tokens_this_update": 10,
        "tokens_since_global_load": 10,
        "num_examples_this_update": 1,
        "train_loss": 1.0,
        "grad_norm": None,
        "param_norm": 1.0,
        "delta_norm": None,
        "file_path": "/tmp/update.safetensors",
        "file_size_bytes": 1,
        "sha256": None,
        "created_at": now,
        "committed_at": now,
    }


def test_insert_select_apply_and_uniqueness(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    assert store.insert_update_metadata(_metadata()) is True
    assert store.insert_update_metadata(_metadata()) is False
    pending = store.pending_updates()
    assert len(pending) == 1
    store.mark_updates_selected(["u1"], "selection")
    store.mark_updates_applied(pending, applied_version=1, effective_weights={"u1": 1.0})
    row = store.get_update("u1")
    assert row["status"] == "applied"
    assert row["applied_version"] == 1
    store.close()


def test_backup_to_shared_dump(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.insert_update_metadata(_metadata())
    dump = store.backup_to(tmp_path / "dump.db", global_version=0)
    assert dump.exists()
    store.close()


def test_drop_superseded_updates(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.insert_update_metadata(_metadata("old", local_step_end=1))
    store.insert_update_metadata(_metadata("new", local_step_end=2))
    selected = [store.get_update("new")]
    assert store.drop_superseded_updates(selected) == 1
    assert store.get_update("old")["status"] == "dropped"
    assert store.get_update("new")["status"] == "pending"
    store.close()
