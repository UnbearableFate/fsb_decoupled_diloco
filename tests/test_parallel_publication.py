import threading
import time

import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime import syncer
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs


class RecordingStore:
    def __init__(self, paths):
        self.paths = paths
        self.commit_called = False

    def commit_full_merge(self, **_kwargs):
        assert self.paths.global_weight_path(1).is_file()
        assert self.paths.outer_optim_path(1).is_file()
        self.commit_called = True
        return {"created_at": time.time(), "version": 1}


def _publication_inputs(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="parallel-publication",
        shared_root=str(tmp_path),
    )
    config.syncer.publish_dtype = "bfloat16"
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    theta = torch.tensor([1.001, -2.003, 0.125], dtype=torch.float32)
    return config, paths, theta


def test_publish_global_writes_weight_and_outer_concurrently_before_db_commit(
    tmp_path, monkeypatch
):
    config, paths, theta = _publication_inputs(tmp_path)
    barrier = threading.Barrier(2)
    completion_order = []

    def save_weight(path, *_args, **_kwargs):
        barrier.wait(timeout=1.0)
        time.sleep(0.02)
        path.write_bytes(b"weight")
        completion_order.append("weight")

    def save_outer(path, *_args, **_kwargs):
        barrier.wait(timeout=1.0)
        path.write_bytes(b"outer-state")
        completion_order.append("outer")

    monkeypatch.setattr(syncer, "save_global_weights", save_weight)
    monkeypatch.setattr(syncer, "save_outer_state", save_outer)
    store = RecordingStore(paths)

    publication = syncer.publish_global(
        config=config,
        paths=paths,
        store=store,
        version=1,
        theta=theta,
        outer_state={"momentum": theta.clone()},
        param_index={"total_numel": theta.numel()},
        num_updates=1,
        total_update_tokens=3,
        total_seen_tokens=3,
        selected_updates=[{"update_id": "u1"}],
        effective_weights={"u1": 1.0},
        predecessor_version=0,
    )

    assert completion_order == ["outer", "weight"]
    assert store.commit_called is True
    assert publication["publish_dtype"] == "bfloat16"
    assert publication["publish_weight_bytes"] == len(b"weight")
    assert publication["publish_outer_bytes"] == len(b"outer-state")
    assert publication["publish_weight_seconds"] >= 0.02
    assert publication["publish_outer_seconds"] >= 0.0
    assert publication["publish_checkpoint_seconds"] >= max(
        publication["publish_weight_seconds"], publication["publish_outer_seconds"]
    )
    assert publication["publish_roundtrip_l2_error"] > 0.0
    assert publication["publish_roundtrip_linf_error"] > 0.0
    assert publication["publish_roundtrip_relative_l2_error"] > 0.0


def test_serial_experiment_mode_preserves_weight_then_outer_commit_order(
    tmp_path, monkeypatch
):
    config, paths, theta = _publication_inputs(tmp_path)
    config.syncer.parallel_checkpoint_writes = False
    completion_order = []

    def save_weight(path, *_args, **_kwargs):
        path.write_bytes(b"weight")
        completion_order.append("weight")

    def save_outer(path, *_args, **_kwargs):
        path.write_bytes(b"outer-state")
        completion_order.append("outer")

    monkeypatch.setattr(syncer, "save_global_weights", save_weight)
    monkeypatch.setattr(syncer, "save_outer_state", save_outer)
    store = RecordingStore(paths)

    publication = syncer.publish_global(
        config=config,
        paths=paths,
        store=store,
        version=1,
        theta=theta,
        outer_state={"momentum": theta.clone()},
        param_index={"total_numel": theta.numel()},
        num_updates=1,
        total_update_tokens=3,
        total_seen_tokens=3,
        selected_updates=[{"update_id": "u1"}],
        effective_weights={"u1": 1.0},
        predecessor_version=0,
    )

    assert completion_order == ["weight", "outer"]
    assert store.commit_called is True
    assert publication["publish_checkpoint_seconds"] >= (
        publication["publish_weight_seconds"] + publication["publish_outer_seconds"]
    )


@pytest.mark.parametrize("failing_side", ["weight", "outer"])
def test_single_checkpoint_failure_never_commits_db_or_latest(
    tmp_path, monkeypatch, failing_side
):
    config, paths, theta = _publication_inputs(tmp_path)
    barrier = threading.Barrier(2)

    def saver(side, path):
        barrier.wait(timeout=1.0)
        if side == failing_side:
            raise OSError(f"injected {side} failure")
        path.write_bytes(side.encode("ascii"))

    monkeypatch.setattr(
        syncer,
        "save_global_weights",
        lambda path, *_args, **_kwargs: saver("weight", path),
    )
    monkeypatch.setattr(
        syncer,
        "save_outer_state",
        lambda path, *_args, **_kwargs: saver("outer", path),
    )
    store = RecordingStore(paths)

    with pytest.raises(OSError, match=f"injected {failing_side} failure"):
        syncer.publish_global(
            config=config,
            paths=paths,
            store=store,
            version=1,
            theta=theta,
            outer_state={"momentum": theta.clone()},
            param_index={"total_numel": theta.numel()},
            num_updates=1,
            total_update_tokens=3,
            total_seen_tokens=3,
            selected_updates=[{"update_id": "u1"}],
            effective_weights={"u1": 1.0},
            predecessor_version=0,
        )

    assert store.commit_called is False
    assert not paths.latest_json.exists()


def test_main_thread_can_ingest_while_checkpoint_workers_are_pending(
    tmp_path, monkeypatch
):
    config, paths, theta = _publication_inputs(tmp_path)
    both_workers_started = threading.Barrier(2)
    release_workers = threading.Event()
    main_thread_id = threading.get_ident()
    callback_thread_ids = []

    def saver(path, content):
        both_workers_started.wait(timeout=1.0)
        assert release_workers.wait(timeout=1.0)
        path.write_bytes(content)

    monkeypatch.setattr(
        syncer,
        "save_global_weights",
        lambda path, *_args, **_kwargs: saver(path, b"weight"),
    )
    monkeypatch.setattr(
        syncer,
        "save_outer_state",
        lambda path, *_args, **_kwargs: saver(path, b"outer"),
    )
    store = RecordingStore(paths)

    def during_checkpoint_wait():
        callback_thread_ids.append(threading.get_ident())
        release_workers.set()
        return {"metadata": 2, "heartbeats": 1}

    publication = syncer.publish_global(
        config=config,
        paths=paths,
        store=store,
        version=1,
        theta=theta,
        outer_state={"momentum": theta.clone()},
        param_index={"total_numel": theta.numel()},
        num_updates=1,
        total_update_tokens=3,
        total_seen_tokens=3,
        selected_updates=[{"update_id": "u1"}],
        effective_weights={"u1": 1.0},
        predecessor_version=0,
        during_checkpoint_wait=during_checkpoint_wait,
        checkpoint_poll_seconds=0.01,
    )

    assert callback_thread_ids == [main_thread_id]
    assert publication["publish_ingest_passes"] == 1
    assert publication["publish_ingested_updates"] == 2
    assert publication["publish_ingested_heartbeats"] == 1
    assert store.commit_called is True
