import json
import time
from pathlib import Path

import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime.syncer import initialize_run, resume_run, run_syncer
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore
from fs_diloco.storage.tensor_codec import save_outer_state


def _setup(tmp_path):
    shared_root = tmp_path / "run"
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="resume_test",
        shared_root=str(shared_root),
    )
    paths = RunPaths(shared_root)
    prepare_run_dirs(paths, config.sync.num_learners)
    store = SQLiteStore(paths.sqlite_db)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    initialized = initialize_run(config, paths, store, logger)
    return config, paths, store, logger, initialized


def test_initialize_writes_matching_resolved_config_at_run_root_and_control(tmp_path):
    config, paths, store, _logger, _initialized = _setup(tmp_path)

    assert paths.run_root_config_yaml.is_file()
    assert paths.resolved_config_yaml.is_file()
    assert paths.run_root_config_yaml.read_bytes() == paths.resolved_config_yaml.read_bytes()
    snapshot = paths.run_root_config_yaml.read_text(encoding="utf-8")
    assert f"run_id: {config.run.run_id}" in snapshot
    assert "global_adoption_strategy: replace" in snapshot
    store.close()


@pytest.mark.parametrize("latest_state", ["missing", "invalid", "behind", "ahead", "wrong_run"])
def test_resume_repairs_latest_from_persistent_db(tmp_path, latest_state):
    config, paths, store, logger, initialized = _setup(tmp_path)
    theta = initialized[1]
    committed = store.latest_global_version()
    if latest_state == "missing":
        paths.latest_json.unlink()
    elif latest_state == "invalid":
        paths.latest_json.write_text("{not-json", encoding="utf-8")
    elif latest_state == "behind":
        paths.latest_json.write_text(json.dumps({"version": -1}), encoding="utf-8")
    elif latest_state == "wrong_run":
        paths.latest_json.write_text(
            json.dumps({"run_id": "other", "version": 0}), encoding="utf-8"
        )
    else:
        orphan_weight = paths.global_weight_path(99)
        orphan_outer = paths.outer_optim_path(99)
        orphan_weight.write_text("orphan", encoding="utf-8")
        orphan_outer.write_text("orphan", encoding="utf-8")
        paths.latest_json.write_text(
            json.dumps(
                {
                    "run_id": config.run.run_id,
                    "version": 99,
                    "weight_path": str(orphan_weight),
                    "optim_path": str(orphan_outer),
                }
            ),
            encoding="utf-8",
        )

    config.init.resume = True
    resumed_version, resumed_theta, _state, resumed_index, _tokens = resume_run(
        config, paths, store, logger
    )
    latest = json.loads(paths.latest_json.read_text(encoding="utf-8"))
    assert resumed_version == int(committed["version"]) == 0
    assert torch.equal(resumed_theta, theta)
    assert resumed_index == initialized[3]
    assert latest["run_id"] == "resume_test"
    assert latest["version"] == 0
    assert latest["weight_path"] == committed["weight_path"]
    assert not paths.global_weight_path(99).exists()
    assert not paths.outer_optim_path(99).exists()
    store.close()


def test_resume_resets_selected_and_is_idempotent(tmp_path):
    config, paths, store, logger, _initialized = _setup(tmp_path)
    payload = paths.update_payload_dir("learner_000") / "selected.params.safetensors"
    payload.write_text("selected", encoding="utf-8")
    now = time.time()
    metadata = {
        "update_id": "selected",
        "learner_id": "learner_000",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 1,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_global_load": 1,
        "file_path": str(payload),
        "created_at": now,
        "committed_at": now,
    }
    store.insert_update_metadata(metadata, pointer_path=paths.update_pointer_path("learner_000"))
    store.mark_updates_selected(["selected"], "interrupted")
    config.init.resume = True

    resume_run(config, paths, store, logger)
    first_latest = paths.latest_json.read_bytes()
    first_rows = [dict(row) for row in store.conn.execute("SELECT * FROM updates")]
    assert first_rows[0]["status"] == "pending"

    resume_run(config, paths, store, logger)
    second_rows = [dict(row) for row in store.conn.execute("SELECT * FROM updates")]
    assert paths.latest_json.read_bytes() == first_latest
    assert second_rows == first_rows
    store.close()


def test_resume_fails_closed_for_missing_or_mismatched_checkpoint(tmp_path):
    config, paths, store, logger, initialized = _setup(tmp_path)
    config.init.resume = True
    committed = store.latest_global_version()
    weight_path = Path(committed["weight_path"])
    weight_path.unlink()
    with pytest.raises(FileNotFoundError, match="checkpoint is incomplete"):
        resume_run(config, paths, store, logger)
    store.close()

    config, paths, store, logger, initialized = _setup(tmp_path / "mismatch")
    config.init.resume = True
    theta, state = initialized[1], initialized[2]
    save_outer_state(paths.outer_optim_path(0), theta + 1.0, state)
    with pytest.raises(RuntimeError, match="do not match"):
        resume_run(config, paths, store, logger)
    store.close()


def test_resume_rejects_absent_db_even_when_latest_exists(tmp_path):
    shared_root = tmp_path / "run"
    paths = RunPaths(shared_root)
    prepare_run_dirs(paths, 1)
    paths.latest_json.write_text(json.dumps({"version": 7}), encoding="utf-8")
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="missing_db",
        shared_root=str(shared_root),
        num_learners=1,
    )
    config.init.resume = True
    with pytest.raises(FileNotFoundError, match="latest.json is not authoritative"):
        run_syncer(config)
    assert not paths.sqlite_db.exists()


def test_non_resume_initialization_rejects_committed_db(tmp_path):
    config, paths, store, logger, _initialized = _setup(tmp_path)
    with pytest.raises(RuntimeError, match="cannot overwrite"):
        initialize_run(config, paths, store, logger)
    store.close()
