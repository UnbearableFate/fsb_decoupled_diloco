import json
import os

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.syncer import maybe_capture_terminal_predecessor_for_eval
from fs_diloco.storage.atomic_io import atomic_write_json, sha256_file
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs


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
