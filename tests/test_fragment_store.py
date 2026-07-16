import time

from fs_diloco.storage.sqlite_store import SQLiteStore


def _fragment():
    return {
        "fragment_id": 0,
        "numel": 3,
        "size_bytes_float32": 12,
        "slices": [{"param_name": "a", "flat_start": 0, "flat_end": 3}],
    }


def _metadata(update_id="u1", learner_id="learner_000", fragment_id=0, local_step_end=1, base_version=0):
    now = time.time()
    return {
        "update_id": update_id,
        "learner_id": learner_id,
        "hostname": "host",
        "fragment_id": fragment_id,
        "base_fragment_version": base_version,
        "base_global_merge_event": 0,
        "local_step_start": 0,
        "local_step_end": local_step_end,
        "inner_steps": 1,
        "tokens_this_update": 10,
        "tokens_since_fragment_load": 10,
        "num_examples_this_update": 1,
        "train_loss": 1.0,
        "grad_norm": None,
        "param_norm": 1.0,
        "fragment_norm": 1.0,
        "file_path": "/tmp/update.safetensors",
        "file_size_bytes": 1,
        "sha256": None,
        "created_at": now,
        "committed_at": now,
    }


def test_fragment_version_and_update_lifecycle(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.upsert_fragment_definition(_fragment(), strategy="balanced_tensor")
    store.upsert_fragment_version(
        fragment_id=0,
        version=0,
        global_merge_event=0,
        weight_path="/tmp/f0.safetensors",
        optim_path="/tmp/o0.safetensors",
        outer_optimizer="nesterov",
    )
    assert store.insert_fragment_update_metadata(_metadata()) is True
    assert store.insert_fragment_update_metadata(_metadata()) is False
    pending = store.eligible_fragment_updates(fragment_id=0, current_fragment_version=0, max_staleness_versions=2)
    assert len(pending) == 1
    store.mark_fragment_updates_selected(["u1"], "selection")
    store.mark_fragment_updates_applied(
        pending,
        applied_fragment_version=1,
        applied_global_merge_event=1,
        effective_weights={"u1": 1.0},
    )
    row = store.get_fragment_update("u1")
    assert row["status"] == "applied"
    assert row["applied_fragment_version"] == 1
    assert row["staleness_fragment_versions"] == 0
    store.close()


def test_fragment_supersession_is_scoped_to_same_fragment(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.insert_fragment_update_metadata(_metadata("old_f0", fragment_id=0, local_step_end=1))
    store.insert_fragment_update_metadata(_metadata("new_f0", fragment_id=0, local_step_end=2))
    store.insert_fragment_update_metadata(_metadata("old_f1", fragment_id=1, local_step_end=1))
    selected = [store.get_fragment_update("new_f0")]
    assert store.drop_superseded_fragment_updates(selected) == 1
    assert store.get_fragment_update("old_f0")["status"] == "dropped"
    assert store.get_fragment_update("old_f1")["status"] == "pending"
    store.close()


def test_fragment_staleness_uses_fragment_version(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.insert_fragment_update_metadata(_metadata("fresh", base_version=3))
    store.insert_fragment_update_metadata(_metadata("stale", base_version=0, local_step_end=2))
    rows = store.eligible_fragment_updates(fragment_id=0, current_fragment_version=3, max_staleness_versions=2)
    assert [row["update_id"] for row in rows] == ["fresh"]
    assert store.drop_obsolete_fragment_updates(fragment_id=0, current_fragment_version=3, max_staleness_versions=2) == 1
    assert store.get_fragment_update("stale")["status"] == "dropped"
    store.close()
