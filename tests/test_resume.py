import time

from fs_diloco.config import resolve_config
from fs_diloco.logging_utils import JsonlLogger
from fs_diloco.paths import RunPaths, prepare_run_dirs
from fs_diloco.sqlite_store import SQLiteStore
from fs_diloco.syncer import initialize_run, resume_run


def test_resume_loads_global_and_restores_db_dump(tmp_path):
    shared_root = tmp_path / "run"
    sqlite_dir = tmp_path / "db"
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="resume_test",
        shared_root=str(shared_root),
        sqlite_local_dir=str(sqlite_dir),
    )
    paths = RunPaths(shared_root)
    prepare_run_dirs(paths, config.sync.num_learners)
    store = SQLiteStore(sqlite_dir / "syncer_metadata.sqlite3")
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    version, theta, outer_state, param_index, _ = initialize_run(config, paths, store, logger)
    assert version == 0
    store.insert_update_metadata(
        {
            "update_id": "applied",
            "learner_id": "learner_000",
            "hostname": "host",
            "base_global_version": 0,
            "local_step_start": 0,
            "local_step_end": 1,
            "inner_steps": 1,
            "tokens_this_update": 10,
            "tokens_since_global_load": 10,
            "num_examples_this_update": 1,
            "train_loss": 1.0,
            "grad_norm": None,
            "param_norm": 1.0,
            "delta_norm": None,
            "file_path": str(shared_root / "fake.safetensors"),
            "file_size_bytes": 1,
            "sha256": None,
            "created_at": time.time(),
            "committed_at": time.time(),
        }
    )
    store.mark_updates_applied(
        [store.get_update("applied")],
        applied_version=1,
        effective_weights={"applied": 1.0},
    )
    dump = store.backup_to(paths.db_dump_path("test", 0), global_version=0)
    store.close()

    resume_db = tmp_path / "resume_db"
    resume_config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="resume_test",
        shared_root=str(shared_root),
        sqlite_local_dir=str(resume_db),
    )
    resume_config.init.resume = True
    resume_config.init.resume_db_dump = str(dump)
    resume_store = SQLiteStore(resume_db / "syncer_metadata.sqlite3")
    resumed_version, resumed_theta, _state, resumed_index, _tokens = resume_run(
        resume_config,
        paths,
        resume_store,
        logger,
    )
    assert resumed_version == 0
    assert resumed_theta.numel() == theta.numel()
    assert resumed_index == param_index
    assert resume_store.get_update("applied")["status"] == "applied"
    resume_store.close()
