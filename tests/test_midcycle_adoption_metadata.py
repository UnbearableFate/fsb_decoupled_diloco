import json

import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.learner import MidCycleAdoptionTracker, write_update
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore
from fs_diloco.tools.analysis import summarize_run


def test_midcycle_adoption_tracker_records_latest_step_and_resets():
    tracker = MidCycleAdoptionTracker()

    tracker.record(completed_interval_steps=1)
    tracker.record(completed_interval_steps=3)

    assert tracker.metadata() == {
        "mid_cycle_adoption_count": 2,
        "base_switched_at_step": 3,
    }

    # A skipped upload still enters a new interval and must clear old evidence.
    tracker.reset()
    assert tracker.metadata() == {
        "mid_cycle_adoption_count": 0,
        "base_switched_at_step": None,
    }


def test_write_update_always_publishes_midcycle_adoption_fields(tmp_path):
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.run.run_id = "midcycle-metadata-unit"
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, num_learners=1)

    _, _, pointer_path, metadata = write_update(
        paths=paths,
        config=config,
        learner_id="learner_000",
        base_global_version=2,
        interval_start_step=4,
        local_step=6,
        inner_steps=2,
        tokens_this_update=64,
        tokens_since_global_load=32,
        num_examples=4,
        train_loss=1.0,
        grad_norm=None,
        param_norm=2.0,
        flat=torch.arange(4, dtype=torch.float32),
        resource_metrics={},
        mid_cycle_adoption_count=0,
        base_switched_at_step=None,
    )

    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert metadata["mid_cycle_adoption_count"] == 0
    assert metadata["base_switched_at_step"] is None
    assert pointer["mid_cycle_adoption_count"] == 0
    assert pointer["base_switched_at_step"] is None

    stored_metadata = {
        **metadata,
        "mid_cycle_adoption_count": 2,
        "base_switched_at_step": 2,
    }
    store = SQLiteStore(paths.sqlite_db)
    assert store.insert_update_metadata(stored_metadata, pointer_path=pointer_path)
    store.close()

    summary = summarize_run(paths.shared_root)
    assert summary["db"]["mid_cycle_adoption"] == {
        "proposals_with_adoption": 1,
        "adoption_count": 2,
        "base_switched_at_step_values": [2],
    }
