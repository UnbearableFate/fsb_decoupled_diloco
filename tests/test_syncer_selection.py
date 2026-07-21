import json
import time

import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.core.constants import FORMAT_VERSION
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.protocol.merge import select_one_per_learner
from fs_diloco.runtime.syncer import (
    UpdateFirstSeenRegistry,
    all_expected_learners_stopped,
    configured_grace_seconds,
    fastest_next_upload_eta_seconds,
    ingest_update_metadata,
    learner_shutdown_timeout_seconds,
    maybe_shorten_grace_deadline,
    select_terminal_drain_fragment_updates,
    select_terminal_drain_updates,
    wait_for_learner_shutdown,
)
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def test_syncer_selection_respects_quorum_max():
    updates = [
        {"update_id": "u0", "learner_id": "learner_000", "local_step_end": 1, "committed_at": 1.0},
        {"update_id": "u1", "learner_id": "learner_001", "local_step_end": 1, "committed_at": 2.0},
        {"update_id": "u2", "learner_id": "learner_002", "local_step_end": 1, "committed_at": 3.0},
    ]
    selected = select_one_per_learner(updates, quorum_max=2)
    assert len(selected) == 2
    assert {row["learner_id"] for row in selected} == {"learner_000", "learner_001"}


def test_fastest_next_upload_eta_uses_measured_cycle_compute_time():
    first_seen = UpdateFirstSeenRegistry(capacity=64)
    first_seen.observe("u0", now_monotonic=90.0, now_wall=1000.0)
    first_seen.observe("u1", now_monotonic=99.0, now_wall=1001.0)
    updates = [
        {
            "update_id": "u0",
            "committed_at": -60.0,
            "local_cycle_step_time_seconds_mean": 0.3,
        },
        {
            "update_id": "u1",
            "committed_at": 10_000.0,
            "local_cycle_step_time_seconds_mean": 0.05,
        },
        {"update_id": "missing", "local_cycle_step_time_seconds_mean": None},
    ]
    assert fastest_next_upload_eta_seconds(
        updates,
        first_seen=first_seen,
        inner_steps=100,
        now_monotonic=100.0,
    ) == 4.0

    for update in updates:
        update["committed_at"] = float(update.get("committed_at", 0.0)) + 120.0
    assert fastest_next_upload_eta_seconds(
        updates,
        first_seen=first_seen,
        inner_steps=100,
        now_monotonic=100.0,
    ) == 4.0


def test_update_first_seen_registry_is_stable_bounded_and_discardable():
    first_seen = UpdateFirstSeenRegistry(capacity=64)
    assert first_seen.observe("u0", now_monotonic=1.0, now_wall=100.0)
    assert not first_seen.observe("u0", now_monotonic=2.0, now_wall=200.0)
    assert first_seen.get("u0").monotonic == 1.0
    assert first_seen.get("u0").wall == 100.0

    for index in range(1, 66):
        first_seen.observe(f"u{index}", now_monotonic=float(index), now_wall=float(index))
    assert len(first_seen) == 64
    assert first_seen.get("u0") is None
    first_seen.discard_many(["u64", "u65"])
    assert len(first_seen) == 62


def test_adaptive_grace_starts_at_ten_seconds_and_only_shortens():
    config = resolve_config("configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml")
    first_seen = UpdateFirstSeenRegistry(capacity=64)
    first_seen.observe("u0", now_monotonic=98.0, now_wall=1000.0)
    assert configured_grace_seconds(config) == 10.0
    deadline, eta = maybe_shorten_grace_deadline(
        deadline=110.0,
        selected=[
            {
                "update_id": "u0",
                "committed_at": 998.0,
                "local_cycle_step_time_seconds_mean": 0.05,
            }
        ],
        config=config,
        first_seen=first_seen,
        now_monotonic=100.0,
    )
    assert eta == 3.0
    assert deadline == 103.0

    later_deadline, later_eta = maybe_shorten_grace_deadline(
        deadline=deadline,
        selected=[
            {
                "update_id": "u0",
                "committed_at": 1000.0,
                "local_cycle_step_time_seconds_mean": 0.2,
            }
        ],
        config=config,
        first_seen=first_seen,
        now_monotonic=101.0,
    )
    assert later_eta == 17.0
    assert later_deadline == deadline


def _pointer(config, paths, learner_id, update_id, *, base=0, step=1):
    payload_path = paths.update_payload_dir(learner_id) / f"{update_id}.params.safetensors"
    payload_path.write_text("payload", encoding="utf-8")
    now = time.time()
    metadata = {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "update_id": update_id,
        "learner_id": learner_id,
        "base_global_version": base,
        "local_step_start": step - 1,
        "local_step_end": step,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_global_load": 1,
        "file_path": str(payload_path),
        "created_at": now,
        "committed_at": now,
    }
    atomic_write_json(paths.update_pointer_path(learner_id), metadata)
    return metadata


def test_full_discovery_reads_only_fixed_pointer_surface(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="fixed_surface",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    first_seen = UpdateFirstSeenRegistry(capacity=64)
    _pointer(config, paths, "learner_000", "u0")
    _pointer(config, paths, "learner_001", "u1")
    ignored = paths.update_payload_dir("learner_000") / "update_history.meta.json"
    ignored.write_text("{}", encoding="utf-8")

    assert ingest_update_metadata(store, paths, config, logger, first_seen=first_seen) == 2
    u0_seen = first_seen.get("u0")
    assert u0_seen is not None
    assert ingest_update_metadata(store, paths, config, logger, first_seen=first_seen) == 0
    assert first_seen.get("u0") is u0_seen
    _pointer(config, paths, "learner_000", "u2", step=2)
    assert ingest_update_metadata(store, paths, config, logger, first_seen=first_seen) == 1
    assert store.get_update("u0")["drop_reason"] == "superseded"
    assert store.get_update("u2")["status"] == "pending"
    assert first_seen.get("u0") is None
    assert first_seen.get("u1") is not None
    assert first_seen.get("u2") is not None
    store.close()


def test_terminal_drain_requires_stopped_and_keeps_strict_eligibility(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="terminal",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    config.sync.max_staleness_versions = 0
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    for learner_id in ("learner_000", "learner_001"):
        store.upsert_learner(learner_id, status="stopped")
    assert all_expected_learners_stopped(store, config)

    fresh = _pointer(config, paths, "learner_000", "fresh", base=3)
    future = _pointer(config, paths, "learner_001", "future", base=4)
    store.insert_update_metadata(fresh, pointer_path=paths.update_pointer_path("learner_000"))
    store.insert_update_metadata(future, pointer_path=paths.update_pointer_path("learner_001"))
    selected = select_terminal_drain_updates(
        store, paths, config, logger, current_version=3
    )
    assert selected.state == "closed_selected"
    assert [row["update_id"] for row in selected.selected] == ["fresh"]
    assert store.get_update("future")["drop_reason"] == "future_base"

    store.update_learner_status("learner_001", "dead", "heartbeat timeout")
    assert not all_expected_learners_stopped(store, config)
    reopened = select_terminal_drain_updates(
        store, paths, config, logger, current_version=3
    )
    assert reopened.state == "open"
    assert reopened.selected == []
    store.close()


def test_fragment_terminal_drain_allows_partial_quorum_and_rejects_future(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fragment_terminal",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    config.sync.quorum_min = 2
    config.sync.max_staleness_versions = 0
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    for learner_id in ("learner_000", "learner_001"):
        store.upsert_learner(learner_id, status="stopped")

    now = time.time()
    for update_id, learner_id, base_version in (
        ("fresh", "learner_000", 3),
        ("future", "learner_001", 4),
    ):
        payload_path = paths.update_payload_dir(learner_id) / f"{update_id}.safetensors"
        payload_path.write_text("payload", encoding="utf-8")
        store.insert_fragment_update_metadata(
            {
                "update_id": update_id,
                "learner_id": learner_id,
                "fragment_id": 0,
                "base_fragment_version": base_version,
                "base_global_merge_event": 3,
                "local_step_start": 0,
                "local_step_end": 1,
                "inner_steps": 1,
                "tokens_this_update": 1,
                "tokens_since_fragment_load": 1,
                "file_path": str(payload_path),
                "created_at": now,
                "committed_at": now,
            }
        )

    selected = select_terminal_drain_fragment_updates(
        store,
        paths,
        config,
        logger,
        fragment_id=0,
        current_fragment_version=3,
        global_merge_event=3,
    )
    assert selected.state == "closed_selected"
    assert [row["update_id"] for row in selected.selected] == ["fresh"]
    assert store.get_fragment_update("future")["drop_reason"] == "future_base"
    store.close()


def test_terminal_drain_distinguishes_closed_empty_from_reopened(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="terminal_states",
        shared_root=str(tmp_path),
        num_learners=1,
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)

    store.upsert_learner("learner_000", status="stopped")
    empty = select_terminal_drain_updates(
        store,
        paths,
        config,
        logger,
        current_version=0,
    )
    assert empty.state == "closed_empty"
    assert empty.selected == []

    store.update_learner_status("learner_000", "active", "resumed")
    reopened = select_terminal_drain_updates(
        store,
        paths,
        config,
        logger,
        current_version=0,
    )
    assert reopened.state == "open"
    assert reopened.selected == []
    store.close()


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], False),
        ([('learner_000', 'stopped')], False),
        ([('learner_000', 'stopped'), ('learner_001', 'active')], False),
        ([('learner_000', 'stopped'), ('learner_001', 'dead')], False),
        ([('learner_000', 'stopped'), ('learner_001', 'stopped')], True),
        (
            [
                ('learner_000', 'stopped'),
                ('learner_001', 'stopped'),
                ('learner_999', 'stopped'),
            ],
            False,
        ),
    ],
)
def test_all_expected_learners_stopped_requires_exact_expected_set(tmp_path, rows, expected):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="stopped_set",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    store = SQLiteStore(tmp_path / "db.sqlite3")
    for learner_id, status in rows:
        store.upsert_learner(learner_id, status=status)

    assert all_expected_learners_stopped(store, config) is expected
    store.close()


def test_shutdown_wait_ingests_final_heartbeats_into_learner_table(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="shutdown_ingest",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    now = time.time()
    for index in range(2):
        learner_id = f"learner_{index:03d}"
        store.upsert_learner(
            learner_id,
            last_seen=now - 1.0,
            last_local_step=10,
            status="active",
        )
        atomic_write_json(
            paths.heartbeats / f"{learner_id}.json",
            {
                "format_version": FORMAT_VERSION,
                "run_id": config.run.run_id,
                "learner_id": learner_id,
                "hostname": "host",
                "pid": 100 + index,
                "timestamp": now,
                "status": "stopped",
                "phase": "process_exit",
                "last_loaded_global_version": 50,
                "last_local_step": 20 + index,
                "last_update_id": f"update_{index}",
            },
        )

    assert wait_for_learner_shutdown(
        paths=paths,
        store=store,
        config=config,
        logger=logger,
        stop_reason="stop_after_outer_steps",
    )
    learners = store.list_learners()
    assert [row["status"] for row in learners] == ["stopped", "stopped"]
    assert [row["status_reason"] for row in learners] == ["stopped", "stopped"]
    assert [row["last_local_step"] for row in learners] == [20, 21]
    assert [row["last_loaded_global_version"] for row in learners] == [50, 50]
    store.close()


def test_shutdown_timeout_formula_and_unconfirmed_learner_details(tmp_path, monkeypatch):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="shutdown_timeout",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    config.liveness.heartbeat_interval_seconds = 80.0
    assert learner_shutdown_timeout_seconds(config) == 160.0
    config.liveness.learner_shutdown_timeout_seconds = 2.0
    assert learner_shutdown_timeout_seconds(config) == 2.0

    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    last_seen = time.time()
    store.upsert_learner(
        "learner_000",
        last_seen=last_seen,
        status="active",
        status_reason="training",
    )
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    clock = [10.0]
    monkeypatch.setattr("fs_diloco.runtime.syncer.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "fs_diloco.runtime.syncer.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert not wait_for_learner_shutdown(
        paths=paths,
        store=store,
        config=config,
        logger=logger,
        stop_reason="stop_after_outer_steps",
    )
    events = [
        json.loads(line)
        for line in (paths.logs / "test.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    timeout = [event for event in events if event["event_type"] == "learner_shutdown_timeout"]
    assert len(timeout) == 1
    assert timeout[0]["timeout_seconds"] == 2.0
    assert timeout[0]["unconfirmed_learners"] == [
        {
            "learner_id": "learner_000",
            "status": "active",
            "status_reason": None,
            "last_seen": last_seen,
        },
        {
            "learner_id": "learner_001",
            "status": "unknown",
            "status_reason": None,
            "last_seen": None,
        },
    ]
    store.close()
