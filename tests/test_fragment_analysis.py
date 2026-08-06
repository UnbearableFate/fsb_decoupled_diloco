import sqlite3
import time

import pytest

import fs_diloco.tools.analysis as analysis_runtime
from fs_diloco.protocol.fragment_index import build_fragment_index, save_fragment_index
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.schema_bootstrap import open_readonly as enforced_open_readonly
from fs_diloco.storage.sqlite_store import SQLiteStore
from fs_diloco.tools.analysis import assert_fragment_run, summarize_run


class Args:
    run_root = ""
    db = None
    expected_learners = 2
    expected_local_steps = 8
    expected_global_merge_events = 4
    expected_fragment_ids = "0,1,2,3"
    min_selected_count = 1


def _param_index():
    return {
        "format_version": 1,
        "model_name_or_path": "tiny",
        "trainable_only": True,
        "total_numel": 16,
        "params": [
            {"name": "a", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 0},
            {"name": "b", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 4},
            {"name": "c", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 8},
            {"name": "d", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 12},
        ],
    }


def _metadata(update_id, learner_id, fragment_id, event, path):
    now = time.time()
    return {
        "update_id": update_id,
        "learner_id": learner_id,
        "fragment_id": fragment_id,
        "base_fragment_version": 0,
        "base_global_merge_event": event - 1,
        "local_step_start": 0,
        "local_step_end": event * 2,
        "inner_steps": 2,
        "tokens_this_update": 10,
        "tokens_since_fragment_load": 10,
        "file_path": str(path),
        "file_size_bytes": path.stat().st_size,
        "sha256": None,
        "created_at": now,
        "committed_at": now,
    }


def test_fragment_summary_and_assertions(tmp_path):
    root = tmp_path / "run"
    (root / "control").mkdir(parents=True)
    (root / "fragments").mkdir()
    (root / "weights").mkdir()
    (root / "heartbeats").mkdir()
    (root / "metrics").mkdir()
    (root / "logs").mkdir()
    final_weight = root / "weights" / "global_v000004.safetensors"
    final_weight.write_bytes(b"fake")
    fragment_index = build_fragment_index(
        _param_index(), strategy="balanced_tensor", num_fragments=4
    )
    save_fragment_index(fragment_index, root / "fragments" / "fragment_index.json")
    atomic_write_json(
        root / "control" / "latest.json",
        {
            "format_version": 1,
            "latest_kind": "fragment",
            "latest_layout_version": 2,
            "run_id": "run",
            "version": 4,
            "global_merge_event": 4,
            "param_index_path": str(root / "control" / "param_index.json"),
            "fragment_index_path": str(root / "fragments" / "fragment_index.json"),
            "materialized_weight_path": str(final_weight),
            "total_seen_tokens": 80,
            "fragments": {
                str(fragment_id): {
                    "version": 1,
                    "weight_path": f"f{fragment_id}",
                    "optim_path": f"o{fragment_id}",
                    "updated_at_global_merge_event": fragment_id + 1,
                }
                for fragment_id in range(4)
            },
        },
    )
    atomic_write_json(
        root / "control" / "stop.json", {"reason": "stop_after_outer_steps", "version": 4}
    )
    atomic_write_json(
        root / "control" / "summary.json",
        {
            "complete_training_time_seconds": 123.0,
            "learner_resources": {
                "training_cpu_utilization_peak_percent_max": 42.0,
                "training_gpu_utilization_peak_percent_max": 99.0,
            },
        },
    )
    for learner in ("learner_000", "learner_001"):
        atomic_write_json(
            root / "heartbeats" / f"{learner}.json",
            {
                "format_version": 1,
                "run_id": "run",
                "learner_id": learner,
                "timestamp": time.time(),
                "status": "stopped",
                "last_local_step": 8,
                "last_loaded_fragment_versions": {"0": 1, "1": 1, "2": 1, "3": 1},
                "last_adopted_fragments": [3],
            },
        )
    (root / "metrics" / "syncer_metrics.csv").write_text(
        "timestamp,global_merge_event,selected_count,fragment_staleness_min,fragment_staleness_mean,fragment_staleness_max\n"
        "1,1,2,0,0,0\n2,2,2,0,0,0\n3,3,2,0,0,0\n4,4,2,0,0,0\n",
        encoding="utf-8",
    )
    (root / "metrics" / "learner_metrics.csv").write_text(
        "timestamp,learner_id,local_step,train_loss,fragment_adopt_count\n"
        "1,learner_000,2,3.0,1\n2,learner_001,2,3.1,1\n"
        "3,learner_000,8,2.9,2\n4,learner_001,8,3.0,2\n",
        encoding="utf-8",
    )
    (root / "logs" / "syncer.jsonl").write_text('{"event": "ok"}\n', encoding="utf-8")
    db_path = root / "metadata.db"
    store = SQLiteStore(db_path)
    for fragment in fragment_index["fragments"]:
        store.upsert_fragment_definition(fragment, strategy="balanced_tensor")
        store.upsert_fragment_version(
            fragment_id=int(fragment["fragment_id"]),
            version=0,
            global_merge_event=0,
            weight_path="w0",
            optim_path="o0",
            outer_optimizer="nesterov",
        )
        store.upsert_fragment_version(
            fragment_id=int(fragment["fragment_id"]),
            version=1,
            global_merge_event=int(fragment["fragment_id"]) + 1,
            weight_path="w1",
            optim_path="o1",
            outer_optimizer="nesterov",
            num_updates=2,
        )
    for event in range(1, 5):
        fragment_id = event - 1
        for learner in ("learner_000", "learner_001"):
            update_path = root / f"{learner}_{fragment_id}.safetensors"
            update_path.write_bytes(b"fake")
            metadata = _metadata(
                f"{learner}_f{fragment_id}", learner, fragment_id, event, update_path
            )
            store.insert_fragment_update_metadata(metadata)
            row = store.get_fragment_update(metadata["update_id"])
            store.mark_fragment_updates_applied(
                [row],
                applied_fragment_version=1,
                applied_global_merge_event=event,
                effective_weights={metadata["update_id"]: 0.5},
            )
    store.close()

    summary = summarize_run(root, db_path)
    assert summary["latest_kind"] == "fragment"
    assert summary["fragment_versions"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    assert summary["complete_training_time_seconds"] == 123.0
    assert summary["learner_resources"]["training_gpu_utilization_peak_percent_max"] == 99.0
    args = Args()
    args.run_root = str(root)
    args.db = str(db_path)
    assert_fragment_run(args, require_local_steps=True)


def test_analysis_opens_authority_database_query_only(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    db_path = root / "metadata.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES (1)")
        connection.commit()
    finally:
        connection.close()

    observed_query_only: list[int] = []

    def checked_open_readonly(path):
        readonly = enforced_open_readonly(path)
        observed_query_only.append(int(readonly.execute("PRAGMA query_only").fetchone()[0]))
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("CREATE TABLE forbidden(value INTEGER)")
        return readonly

    monkeypatch.setattr(analysis_runtime, "open_readonly", checked_open_readonly)
    summary = analysis_runtime.summarize_run(root, db_path)

    assert summary["db"]["exists"] is True
    assert observed_query_only == [1]
