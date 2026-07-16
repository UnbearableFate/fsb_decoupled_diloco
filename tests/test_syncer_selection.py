import time

from fs_diloco.core.config import resolve_config
from fs_diloco.core.constants import FORMAT_VERSION
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.protocol.merge import select_one_per_learner
from fs_diloco.runtime.syncer import (
    all_expected_learners_stopped,
    configured_grace_seconds,
    fastest_next_upload_eta_seconds,
    ingest_update_metadata,
    maybe_shorten_grace_deadline,
    select_terminal_drain_updates,
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
    updates = [
        {
            "committed_at": 90.0,
            "local_cycle_step_time_seconds_mean": 0.3,
        },
        {
            "committed_at": 99.0,
            "local_cycle_step_time_seconds_mean": 0.05,
        },
        {"committed_at": 100.0, "local_cycle_step_time_seconds_mean": None},
    ]
    assert fastest_next_upload_eta_seconds(updates, inner_steps=100, now=100.0) == 4.0


def test_adaptive_grace_starts_at_ten_seconds_and_only_shortens():
    config = resolve_config("configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml")
    assert configured_grace_seconds(config) == 10.0
    deadline, eta = maybe_shorten_grace_deadline(
        deadline=110.0,
        selected=[
            {
                "committed_at": 998.0,
                "local_cycle_step_time_seconds_mean": 0.05,
            }
        ],
        config=config,
        now_monotonic=100.0,
        now_wall=1000.0,
    )
    assert eta == 3.0
    assert deadline == 103.0

    later_deadline, later_eta = maybe_shorten_grace_deadline(
        deadline=deadline,
        selected=[
            {
                "committed_at": 1000.0,
                "local_cycle_step_time_seconds_mean": 0.2,
            }
        ],
        config=config,
        now_monotonic=101.0,
        now_wall=1001.0,
    )
    assert later_eta == 19.0
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
    _pointer(config, paths, "learner_000", "u0")
    _pointer(config, paths, "learner_001", "u1")
    ignored = paths.update_payload_dir("learner_000") / "update_history.meta.json"
    ignored.write_text("{}", encoding="utf-8")

    assert ingest_update_metadata(store, paths, config, logger) == 2
    assert ingest_update_metadata(store, paths, config, logger) == 0
    _pointer(config, paths, "learner_000", "u2", step=2)
    assert ingest_update_metadata(store, paths, config, logger) == 1
    assert store.get_update("u0")["drop_reason"] == "superseded"
    assert store.get_update("u2")["status"] == "pending"
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
    assert [row["update_id"] for row in selected] == ["fresh"]
    assert store.get_update("future")["drop_reason"] == "future_base"

    store.update_learner_status("learner_001", "dead", "heartbeat timeout")
    assert not all_expected_learners_stopped(store, config)
    assert select_terminal_drain_updates(
        store, paths, config, logger, current_version=3
    ) == []
    store.close()
