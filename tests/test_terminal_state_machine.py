import json
import time
from types import SimpleNamespace

import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime import syncer
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore


def _event_types(path):
    return [
        json.loads(line)["event_type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _resume_config(tmp_path, run_id):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id=run_id,
        shared_root=str(tmp_path),
        num_learners=1,
    )
    config.init.resume = True
    config.sync.stop_after_outer_steps = 10
    config.sync.grace_window.mode = "fixed"
    config.sync.grace_window.fixed_seconds = 0.0
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    SQLiteStore(paths.sqlite_db).close()
    return config, paths


def _mock_resume(_config, _paths, _store, _logger, *, device):
    return 0, torch.zeros(1, device=device), {}, {"total_numel": 1}, 0


def test_full_terminal_grace_rechecks_reopened_input_before_stopping(tmp_path, monkeypatch):
    config, paths = _resume_config(tmp_path, "resume_reopened")
    sync_calls = 0

    def sync_then_reopen(store, _paths, _config, _logger, **_kwargs):
        nonlocal sync_calls
        sync_calls += 1
        store.upsert_learner(
            "learner_000",
            status="stopped" if sync_calls == 1 else "active",
            status_reason="old" if sync_calls == 1 else "resumed",
        )
        return {"heartbeats": 1, "metadata": 0}

    monkeypatch.setattr(syncer, "resume_run", _mock_resume)
    monkeypatch.setattr(syncer, "init_wandb_run", lambda **_kwargs: None)
    monkeypatch.setattr(syncer, "sync_liveness_and_metadata", sync_then_reopen)
    monkeypatch.setattr(syncer, "no_progress_timed_out", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(syncer, "wait_for_learner_shutdown", lambda **_kwargs: False)

    syncer.run_syncer(config)

    stop = json.loads(paths.stop_json.read_text(encoding="utf-8"))
    events = _event_types(paths.logs / "syncer.jsonl")
    assert stop["reason"] == "no_progress_timeout"
    assert events.count("terminal_input_closed") == 1
    assert events.count("terminal_input_reopened") == 1
    assert "input_exhausted" not in events
    assert events.index("terminal_input_reopened") < events.index("no_progress_timeout")


def test_full_closed_empty_runs_only_terminal_discovery(tmp_path, monkeypatch):
    config, paths = _resume_config(tmp_path, "full_closed_empty")
    eligible_calls = 0
    original_eligible = SQLiteStore.eligible_updates

    def count_eligible(self, *args, **kwargs):
        nonlocal eligible_calls
        eligible_calls += 1
        return original_eligible(self, *args, **kwargs)

    def sync_stopped(store, _paths, _config, _logger, **_kwargs):
        store.upsert_learner("learner_000", status="stopped")
        return {"heartbeats": 1, "metadata": 0}

    monkeypatch.setattr(syncer, "resume_run", _mock_resume)
    monkeypatch.setattr(syncer, "init_wandb_run", lambda **_kwargs: None)
    monkeypatch.setattr(syncer, "sync_liveness_and_metadata", sync_stopped)
    monkeypatch.setattr(SQLiteStore, "eligible_updates", count_eligible)

    syncer.run_syncer(config)

    assert json.loads(paths.stop_json.read_text(encoding="utf-8"))["reason"] == "input_exhausted"
    assert eligible_calls == 1


def test_fragment_closed_empty_runs_only_terminal_discovery(tmp_path, monkeypatch):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="fragment_closed_empty",
        shared_root=str(tmp_path),
        num_learners=1,
    )
    config.sync.stop_after_outer_steps = 10
    config.sync.grace_window.mode = "fixed"
    config.sync.grace_window.fixed_seconds = 0.0
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    store = SQLiteStore(paths.sqlite_db)
    store.upsert_learner("learner_000", status="stopped")
    logger = JsonlLogger(paths.logs / "syncer.jsonl", "syncer", mirror_stdout=False)
    eligible_calls = 0
    original_eligible = SQLiteStore.eligible_fragment_updates

    def count_eligible(self, *args, **kwargs):
        nonlocal eligible_calls
        eligible_calls += 1
        return original_eligible(self, *args, **kwargs)

    monkeypatch.setattr(
        syncer,
        "initialize_fragment_run",
        lambda *_args, **_kwargs: (
            0,
            {},
            {},
            {"total_numel": 0},
            {"num_fragments": 1, "strategy": "contiguous"},
            {0: 0},
            {0: 0},
            0,
            paths.global_weight_path(0),
        ),
    )
    monkeypatch.setattr(SQLiteStore, "eligible_fragment_updates", count_eligible)
    monkeypatch.setattr(
        syncer,
        "publish_fragment_latest",
        lambda **_kwargs: SimpleNamespace(
            materialized_weight_path=paths.global_weight_path(0)
        ),
    )

    syncer.run_fragment_syncer(
        config=config,
        paths=paths,
        store=store,
        logger=logger,
        device=torch.device("cpu"),
        wandb_run=None,
        run_started_at=time.time(),
        run_start_monotonic=time.monotonic(),
    )

    assert json.loads(paths.stop_json.read_text(encoding="utf-8"))["reason"] == "input_exhausted"
    assert eligible_calls == 1
