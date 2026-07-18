import time

import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.core.constants import FORMAT_VERSION
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime import syncer
from fs_diloco.runtime.learner import write_fragment_update
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _metadata(config, paths, update_id, learner_id, fragment_id, step):
    payload_path = paths.update_payload_dir(learner_id) / f"{update_id}.safetensors"
    payload_path.write_bytes(b"payload")
    now = time.time()
    return {
        "format_version": FORMAT_VERSION,
        "update_kind": "fragment",
        "run_id": config.run_id,
        "update_id": update_id,
        "learner_id": learner_id,
        "fragment_id": fragment_id,
        "base_fragment_version": 0,
        "base_global_merge_event": 0,
        "local_step_start": step - 1,
        "local_step_end": step,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_fragment_load": 1,
        "file_path": str(payload_path),
        "created_at": now,
        "committed_at": now,
    }


def test_fragment_writer_uses_one_atomic_pointer_per_learner_fragment(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fragment-pointers",
        shared_root=str(tmp_path),
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    common = {
        "paths": paths,
        "config": config,
        "learner_id": "learner_000",
        "base_fragment_version": 0,
        "base_global_merge_event": 0,
        "interval_start_step": 0,
        "local_step": 1,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_fragment_load": 1,
        "num_examples": 1,
        "train_loss": 1.0,
        "grad_norm": None,
        "param_norm": 1.0,
        "fragment_norm": 1.0,
        "fragment_tensor": torch.tensor([1.0]),
        "resource_metrics": {},
    }

    _, _, pointer0, _ = write_fragment_update(fragment_id=0, **common)
    _, _, pointer1, _ = write_fragment_update(fragment_id=1, **common)

    assert pointer0 == paths.fragment_update_pointer_path("learner_000", 0)
    assert pointer1 == paths.fragment_update_pointer_path("learner_000", 1)
    assert pointer0.is_file() and pointer1.is_file()
    assert not list(paths.update_payload_dir("learner_000").glob("*.meta.json"))


def test_fragment_frontier_is_transactional_latest_wins_per_pair(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    old_f0 = _metadata(NoneConfig("r"), DummyPaths(tmp_path), "old-f0", "learner_000", 0, 1)
    new_f0 = _metadata(NoneConfig("r"), DummyPaths(tmp_path), "new-f0", "learner_000", 0, 2)
    old_f1 = _metadata(NoneConfig("r"), DummyPaths(tmp_path), "old-f1", "learner_000", 1, 1)

    assert store.insert_fragment_update_metadata(old_f0, pointer_path="f0.json")
    assert store.insert_fragment_update_metadata(new_f0, pointer_path="f0.json")
    assert store.get_fragment_update("old-f0")["drop_reason"] == "superseded"
    assert store.insert_fragment_update_metadata(old_f1, pointer_path="f1.json")
    store.mark_fragment_updates_selected(["new-f0"], "selection")
    newest_f0 = _metadata(
        NoneConfig("r"), DummyPaths(tmp_path), "newest-f0", "learner_000", 0, 3
    )
    assert store.insert_fragment_update_metadata(newest_f0, pointer_path="f0.json")
    assert store.get_fragment_update("new-f0")["status"] == "selected"
    assert store.get_fragment_update("old-f1")["status"] == "pending"
    assert store.fragment_proposal_frontiers() == {
        ("learner_000", 0): "newest-f0",
        ("learner_000", 1): "old-f1",
    }
    assert not store.insert_fragment_update_metadata(newest_f0, pointer_path="f0.json")
    store.close()


class NoneConfig:
    def __init__(self, run_id):
        self.run_id = run_id


class DummyPaths:
    def __init__(self, root):
        self.root = root

    def update_payload_dir(self, learner_id):
        path = self.root / learner_id
        path.mkdir(exist_ok=True)
        return path


def test_fragment_discovery_is_fixed_surface_and_signature_cached(tmp_path, monkeypatch):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fixed-fragment-surface",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    for learner_index in range(2):
        learner_id = f"learner_{learner_index:03d}"
        metadata = _metadata(config.run, paths, f"u{learner_index}", learner_id, 0, 1)
        atomic_write_json(paths.fragment_update_pointer_path(learner_id, 0), metadata)
    for index in range(100):
        (paths.update_payload_dir("learner_000") / f"history-{index}.meta.json").write_text(
            "{}", encoding="utf-8"
        )

    reads = []
    original_read = syncer.safe_read_json

    def counted_read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(syncer, "safe_read_json", counted_read)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    assert syncer.ingest_update_metadata(store, paths, config, logger) == 2
    assert len(reads) == 2
    assert syncer.ingest_update_metadata(store, paths, config, logger) == 0
    assert len(reads) == 2
    store.close()

    restarted = SQLiteStore(paths.sqlite_db)
    assert syncer.ingest_update_metadata(restarted, paths, config, logger) == 0
    assert len(reads) == 4
    assert len(list(paths.updates_latest.glob("learner_*_f*.json"))) == 2
    restarted.close()


def test_fragment_discovery_surface_stays_bounded_for_1000_cycles(tmp_path, monkeypatch):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fragment-bounded-1000",
        shared_root=str(tmp_path),
        num_learners=4,
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 4)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    read_count = 0
    original_read = syncer.safe_read_json

    def counted_read(path):
        nonlocal read_count
        read_count += 1
        return original_read(path)

    monkeypatch.setattr(syncer, "safe_read_json", counted_read)
    for cycle in range(1000):
        learner_index = cycle % 4
        fragment_id = (cycle // 4) % 2
        learner_id = f"learner_{learner_index:03d}"
        metadata = _metadata(
            config.run,
            paths,
            f"u{cycle:04d}",
            learner_id,
            fragment_id,
            cycle + 1,
        )
        atomic_write_json(
            paths.fragment_update_pointer_path(learner_id, fragment_id), metadata
        )
        assert syncer.ingest_update_metadata(store, paths, config, logger) == 1
        assert syncer.ingest_update_metadata(store, paths, config, logger) == 0

    assert read_count == 1000
    assert len(list(paths.updates_latest.glob("learner_*_f*.json"))) == 8
    assert len(store.fragment_proposal_frontiers()) == 8
    store.close()
