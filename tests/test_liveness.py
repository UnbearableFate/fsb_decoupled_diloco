import time

from fs_diloco.core.constants import FORMAT_VERSION
from fs_diloco.protocol.liveness import (
    capture_heartbeat_fences,
    ingest_heartbeats,
    no_progress_timed_out,
    update_liveness_statuses,
)
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.sqlite_store import SQLiteStore


def test_heartbeat_ingest_and_status_transitions(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    heartbeat_dir = tmp_path / "heartbeats"
    now = time.time()
    atomic_write_json(
        heartbeat_dir / "learner_000.json",
        {
            "format_version": FORMAT_VERSION,
            "run_id": "run",
            "learner_id": "learner_000",
            "hostname": "host",
            "pid": 123,
            "timestamp": now,
            "status": "active",
            "phase": "inner_steps",
            "last_loaded_global_version": 0,
            "last_local_step": 2,
            "last_update_id": None,
            "tokens_per_sec": 10.0,
        },
    )
    assert ingest_heartbeats(store, heartbeat_dir, run_id="run", num_learners=1) == 1
    counts = update_liveness_statuses(
        store,
        stale_after_seconds=1.0,
        dead_after_seconds=2.0,
        now=now + 1.5,
    )
    assert counts["stale"] == 1
    counts = update_liveness_statuses(
        store,
        stale_after_seconds=1.0,
        dead_after_seconds=2.0,
        now=now + 3.0,
    )
    assert counts["dead"] == 1
    store.close()


def test_stopped_preserved_and_no_progress_timeout(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.upsert_learner("learner_000", last_seen=time.time() - 1000, status="stopped")
    counts = update_liveness_statuses(store, stale_after_seconds=1.0, dead_after_seconds=2.0)
    assert counts["stopped"] == 1
    assert no_progress_timed_out(0.0, 1.0, now=2.0)
    store.close()


def test_resume_heartbeat_fence_ignores_old_pointer_until_atomic_replacement(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    heartbeat_dir = tmp_path / "heartbeats"
    path = heartbeat_dir / "learner_000.json"
    old_timestamp = time.time()
    atomic_write_json(
        path,
        {
            "format_version": FORMAT_VERSION,
            "run_id": "run",
            "learner_id": "learner_000",
            "hostname": "old-host",
            "pid": 10,
            "timestamp": old_timestamp,
            "status": "stopped",
            "phase": "process_exit",
        },
    )
    fences = capture_heartbeat_fences(
        heartbeat_dir,
        run_id="run",
        num_learners=1,
    )
    assert set(fences) == {"learner_000"}

    store.upsert_learner("learner_000", status="unknown", status_reason="resumed")
    assert ingest_heartbeats(
        store,
        heartbeat_dir,
        run_id="run",
        num_learners=1,
        heartbeat_fences=fences,
    ) == 0
    assert store.list_learners()[0]["status"] == "unknown"

    atomic_write_json(
        path,
        {
            "format_version": FORMAT_VERSION,
            "run_id": "run",
            "learner_id": "learner_000",
            "hostname": "new-host",
            "pid": 20,
            "timestamp": old_timestamp + 1.0,
            "status": "active",
            "phase": "inner_steps",
        },
    )
    assert ingest_heartbeats(
        store,
        heartbeat_dir,
        run_id="run",
        num_learners=1,
        heartbeat_fences=fences,
    ) == 1
    learner = store.list_learners()[0]
    assert learner["status"] == "active"
    assert learner["hostname"] == "new-host"
    assert learner["pid"] == 20
    store.close()


def test_resume_heartbeat_fence_excludes_invalid_or_foreign_pointers(tmp_path):
    heartbeat_dir = tmp_path / "heartbeats"
    atomic_write_json(
        heartbeat_dir / "learner_000.json",
        {
            "format_version": FORMAT_VERSION,
            "run_id": "other-run",
            "learner_id": "learner_000",
            "timestamp": time.time(),
            "status": "stopped",
        },
    )
    (heartbeat_dir / "learner_001.json").write_text("{broken", encoding="utf-8")
    assert capture_heartbeat_fences(
        heartbeat_dir,
        run_id="run",
        num_learners=2,
    ) == {}
