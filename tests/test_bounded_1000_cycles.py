import json
import time

from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.maintenance import run_maintenance
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _metadata(learner_index, cycle):
    learner_id = f"learner_{learner_index:03d}"
    update_id = f"{learner_id}-cycle-{cycle:04d}"
    now = time.time()
    return {
        "format_version": 1,
        "run_id": "bounded-1000",
        "update_id": update_id,
        "learner_id": learner_id,
        "base_global_version": cycle,
        "local_step_start": cycle,
        "local_step_end": cycle + 1,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_global_load": 1,
        "file_path": f"/nonexistent/{update_id}.params.safetensors",
        "created_at": now,
        "committed_at": now,
    }


def _jsonl_ids(path, key):
    return [
        json.loads(line)[key]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_thousand_cycles_keep_db_and_discovery_surface_bounded(tmp_path):
    learners = 4
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, learners)
    store = SQLiteStore(paths.sqlite_db)
    store.initialize_full_run(
        weight_path="/nonexistent/global_v000000.safetensors",
        optim_path="/nonexistent/outer_v000000.safetensors",
        outer_optimizer="nesterov",
        identity={"run_id": "bounded-1000", "protocol_version": 2},
        config_snapshot={},
    )
    for learner in range(learners):
        metadata = _metadata(learner, 0)
        pointer = paths.update_pointer_path(metadata["learner_id"])
        atomic_write_json(pointer, metadata)
        store.insert_update_metadata(metadata, pointer_path=pointer)

    warm_used_pages = None
    for cycle in range(1000):
        selected_ids = [f"learner_{learner:03d}-cycle-{cycle:04d}" for learner in range(learners)]
        store.mark_updates_selected(selected_ids, f"selection-{cycle + 1}")
        selected = [store.get_update(update_id) for update_id in selected_ids]
        assert all(row is not None and row["status"] == "selected" for row in selected)

        for learner in range(learners):
            metadata = _metadata(learner, cycle + 1)
            pointer = paths.update_pointer_path(metadata["learner_id"])
            atomic_write_json(pointer, metadata)
            store.insert_update_metadata(metadata, pointer_path=pointer)
        assert store.conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0] <= 2 * learners

        target = cycle + 1
        store.commit_full_merge(
            predecessor_version=cycle,
            target_version=target,
            weight_path=f"/nonexistent/global_v{target:06d}.safetensors",
            optim_path=f"/nonexistent/outer_v{target:06d}.safetensors",
            selected_updates=selected,
            effective_weights={update_id: 1.0 / learners for update_id in selected_ids},
            total_update_tokens=learners,
            total_seen_tokens=target * learners,
            outer_optimizer="nesterov",
            max_staleness_versions=0,
        )
        run_maintenance(
            store,
            paths,
            heartbeat_interval_seconds=1.0,
            scan_interval_seconds=0.1,
        )
        if target == 100:
            page_count = store.conn.execute("PRAGMA page_count").fetchone()[0]
            freelist = store.conn.execute("PRAGMA freelist_count").fetchone()[0]
            warm_used_pages = int(page_count) - int(freelist)

    page_count = store.conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = store.conn.execute("PRAGMA freelist_count").fetchone()[0]
    final_used_pages = int(page_count) - int(freelist)
    assert warm_used_pages is not None
    assert final_used_pages <= warm_used_pages + 16
    assert store.conn.execute("SELECT COUNT(*) FROM global_versions").fetchone()[0] == 1
    assert store.latest_global_version()["version"] == 1000
    assert store.conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == learners
    assert store.conn.execute(
        "SELECT COUNT(*) FROM updates WHERE status = 'selected'"
    ).fetchone()[0] == 0
    assert len(list(paths.updates_latest.glob("learner_*.json"))) == learners
    assert not list(paths.updates_payloads.glob("**/*.safetensors"))

    update_ids = _jsonl_ids(paths.update_history_jsonl, "update_id")
    version_ids = _jsonl_ids(paths.global_version_history_jsonl, "version")
    assert len(update_ids) == len(set(update_ids)) == 4000
    assert len(version_ids) == len(set(version_ids)) == 1000
    store.integrity_check()
    store.close()
