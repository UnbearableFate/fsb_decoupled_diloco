import json
import os
import time

import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime import syncer
from fs_diloco.runtime.syncer import (
    maybe_capture_terminal_predecessor_for_eval,
    select_terminal_drain_updates,
)
from fs_diloco.storage.atomic_io import atomic_write_json, sha256_file
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _runtime(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="terminal_capture",
        shared_root=str(tmp_path),
    )
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, config.sync.num_learners)
    source = paths.global_weight_path(7)
    source.write_bytes(b"authoritative-v7")
    atomic_write_json(
        paths.latest_json,
        {"global_version": 7, "weight_path": str(source)},
    )
    paths.sqlite_db.write_bytes(b"db-sentinel")
    selected = [
        {"update_id": "u0", "learner_id": "learner_000"},
        {"update_id": "u1", "learner_id": "learner_001"},
    ]
    return config, paths, source, selected


def test_terminal_predecessor_capture_is_default_off(tmp_path):
    config, paths, _source, selected = _runtime(tmp_path)

    result = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )

    assert result is None
    assert not paths.eval_checkpoints.exists()


def test_capture_only_runs_for_input_closed_partial_terminal_merge(tmp_path):
    config, paths, _source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True

    assert maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=False,
        terminal_drain=True,
        version=7,
        selected=selected,
    ) is None
    assert maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=False,
        version=7,
        selected=selected,
    ) is None
    assert not paths.eval_checkpoints.exists()


def test_terminal_partial_selection_captures_without_mutating_runtime_authority(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="terminal_partial_integration",
        shared_root=str(tmp_path),
        num_learners=2,
    )
    config.sync.quorum_min = 2
    config.sync.capture_terminal_predecessor_for_eval = True
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 2)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    for learner_id in ("learner_000", "learner_001"):
        store.upsert_learner(learner_id, status="stopped")
    source = paths.global_weight_path(0)
    source.write_bytes(b"authoritative-v0")
    atomic_write_json(paths.latest_json, {"version": 0, "weight_path": str(source)})
    payload_path = paths.update_payload_dir("learner_000") / "fresh.params.safetensors"
    payload_path.write_bytes(b"proposal")
    now = time.time()
    store.insert_update_metadata(
        {
            "update_id": "fresh",
            "learner_id": "learner_000",
            "base_global_version": 0,
            "local_step_start": 0,
            "local_step_end": 1,
            "inner_steps": 1,
            "tokens_this_update": 1,
            "tokens_since_global_load": 1,
            "file_path": str(payload_path),
            "created_at": now,
            "committed_at": now,
        },
        pointer_path=paths.update_pointer_path("learner_000"),
    )

    decision = select_terminal_drain_updates(
        store,
        paths,
        config,
        logger,
        current_version=0,
    )
    assert decision.state == "closed_selected"
    assert len(decision.selected) == 1 < config.sync.quorum_min
    latest_before = paths.latest_json.read_bytes()
    db_before = paths.sqlite_db.read_bytes()

    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=0,
        selected=decision.selected,
    )

    assert manifest is not None
    assert manifest["selected_update_ids"] == ["fresh"]
    assert (paths.shared_root / manifest["checkpoint_path"]).read_bytes() == source.read_bytes()
    assert paths.latest_json.read_bytes() == latest_before
    assert paths.sqlite_db.read_bytes() == db_before
    assert not list(paths.eval_checkpoints.glob(".*.tmp"))
    store.close()


def test_capture_hardlinks_weight_and_does_not_mutate_authorities(tmp_path):
    config, paths, source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    latest_before = paths.latest_json.read_bytes()
    db_before = paths.sqlite_db.read_bytes()

    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )

    assert manifest is not None
    checkpoint = paths.shared_root / manifest["checkpoint_path"]
    manifest_path = paths.shared_root / manifest["manifest_path"]
    assert checkpoint.read_bytes() == source.read_bytes()
    assert os.stat(checkpoint).st_ino == os.stat(source).st_ino
    assert manifest["capture_method"] == "hardlink"
    assert manifest["source_global_version"] == 7
    assert manifest["selected_count"] == 2
    assert manifest["quorum_min"] == config.sync.quorum_min
    assert manifest["quorum_max"] == config.sync.quorum_max
    assert manifest["selected_update_ids"] == ["u0", "u1"]
    assert manifest["selected_learner_ids"] == ["learner_000", "learner_001"]
    assert manifest["checkpoint_sha256"] == f"sha256:{sha256_file(checkpoint)}"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert paths.latest_json.read_bytes() == latest_before
    assert paths.sqlite_db.read_bytes() == db_before


def test_capture_falls_back_to_atomic_copy_and_supports_multiple_versions(
    tmp_path, monkeypatch
):
    config, paths, _source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    monkeypatch.setattr(os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))

    first = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )
    paths.global_weight_path(8).write_bytes(b"authoritative-v8")
    second = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=8,
        selected=selected[:1],
    )

    assert first is not None and second is not None
    assert first["capture_method"] == second["capture_method"] == "copy"
    assert first["checkpoint_path"] != second["checkpoint_path"]
    assert len(list(paths.eval_checkpoints.glob("*.safetensors"))) == 2
    assert len(list(paths.eval_checkpoints.glob("*.manifest.json"))) == 2


def test_capture_reuses_uncommitted_equal_copy_and_commits_manifest(tmp_path):
    config, paths, source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    checkpoint = paths.eval_checkpoints / "terminal_predecessor_v000007.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(source.read_bytes())

    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )

    assert manifest is not None
    assert manifest["capture_method"] == "copy"
    assert checkpoint.read_bytes() == source.read_bytes()
    assert (paths.shared_root / manifest["manifest_path"]).is_file()


def test_capture_atomically_repairs_wrong_uncommitted_checkpoint(tmp_path):
    config, paths, source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    checkpoint = paths.eval_checkpoints / "terminal_predecessor_v000007.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"wrong-version")

    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )

    assert manifest is not None
    assert manifest["capture_method"] == "copy"
    assert checkpoint.read_bytes() == source.read_bytes()
    assert manifest["checkpoint_sha256"] == f"sha256:{sha256_file(source)}"
    assert not list(paths.eval_checkpoints.glob(".*.tmp"))


def test_capture_recovers_after_checkpoint_before_manifest_crash(tmp_path, monkeypatch):
    config, paths, source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    original_atomic_write_json = syncer.atomic_write_json

    def fail_manifest(path, payload, *args, **kwargs):
        if str(path).endswith(".manifest.json"):
            raise OSError("injected manifest publication crash")
        return original_atomic_write_json(path, payload, *args, **kwargs)

    monkeypatch.setattr(syncer, "atomic_write_json", fail_manifest)
    with pytest.raises(OSError, match="injected manifest publication crash"):
        maybe_capture_terminal_predecessor_for_eval(
            config,
            paths,
            input_closed=True,
            terminal_drain=True,
            version=7,
            selected=selected,
        )

    checkpoint = paths.eval_checkpoints / "terminal_predecessor_v000007.safetensors"
    manifest_path = paths.eval_checkpoints / "terminal_predecessor_v000007.manifest.json"
    assert checkpoint.read_bytes() == source.read_bytes()
    assert not manifest_path.exists()

    monkeypatch.setattr(syncer, "atomic_write_json", original_atomic_write_json)
    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )
    assert manifest is not None
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_committed_capture_is_byte_idempotent(tmp_path):
    config, paths, _source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    first = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )
    checkpoint = paths.shared_root / first["checkpoint_path"]
    manifest_path = paths.shared_root / first["manifest_path"]
    checkpoint_before = checkpoint.read_bytes()
    manifest_before = manifest_path.read_bytes()

    second = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )

    assert second == first
    assert checkpoint.read_bytes() == checkpoint_before
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize("damage", ["missing_checkpoint", "corrupt_manifest", "selection"])
def test_committed_capture_conflicts_fail_closed(tmp_path, damage):
    config, paths, _source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )
    checkpoint = paths.shared_root / manifest["checkpoint_path"]
    manifest_path = paths.shared_root / manifest["manifest_path"]
    if damage == "missing_checkpoint":
        checkpoint.unlink()
    elif damage == "corrupt_manifest":
        manifest_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="terminal predecessor"):
        maybe_capture_terminal_predecessor_for_eval(
            config,
            paths,
            input_closed=True,
            terminal_drain=True,
            version=7,
            selected=selected[:1] if damage == "selection" else selected,
        )


def test_committed_capture_rejects_source_mutation(tmp_path):
    config, paths, source, selected = _runtime(tmp_path)
    config.sync.capture_terminal_predecessor_for_eval = True
    manifest = maybe_capture_terminal_predecessor_for_eval(
        config,
        paths,
        input_closed=True,
        terminal_drain=True,
        version=7,
        selected=selected,
    )
    manifest_before = (paths.shared_root / manifest["manifest_path"]).read_bytes()
    source.write_bytes(b"illegally-mutated-authority")

    with pytest.raises(RuntimeError, match="conflicts with source"):
        maybe_capture_terminal_predecessor_for_eval(
            config,
            paths,
            input_closed=True,
            terminal_drain=True,
            version=7,
            selected=selected,
        )

    assert (paths.shared_root / manifest["manifest_path"]).read_bytes() == manifest_before
