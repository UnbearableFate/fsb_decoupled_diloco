import csv
import json

import pytest

from fs_diloco.tools.run_metrics_csv import (
    CSV_COLUMNS,
    extract_run_metrics,
    write_metrics_csv,
)


def _write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _base_run(tmp_path):
    root = tmp_path / "run"
    _write_json(
        root / "control" / "latest.json",
        {"run_id": "example", "version": 2},
    )
    _write_json(
        root / "control" / "summary.json",
        {
            "run_id": "example",
            "final_version": 2,
            "stop_reason": "stop_after_outer_steps",
            "complete_training_time_seconds": 12.5,
            "all_learners_stopped": True,
        },
    )
    _write_csv(
        root / "metrics" / "update_manifest.csv",
        [
            "update_id",
            "learner_id",
            "update_kind",
            "base_global_version",
            "tokens_this_update",
        ],
        [
            {
                "update_id": "u1",
                "learner_id": "l0",
                "base_global_version": 0,
                "tokens_this_update": 10,
            },
            {
                "update_id": "u2",
                "learner_id": "l1",
                "base_global_version": 0,
                "tokens_this_update": 20,
            },
            {
                "update_id": "u3",
                "learner_id": "l0",
                "base_global_version": 1,
                "tokens_this_update": 30,
            },
            {
                "update_id": "u4",
                "learner_id": "l1",
                "base_global_version": 1,
                "tokens_this_update": 40,
            },
        ],
    )
    _write_jsonl(
        root / "metrics" / "update_history.jsonl",
        [
            {
                "update_id": "u1",
                "status": "applied",
                "staleness_versions": 0,
                "tokens_this_update": 10,
            },
            {
                "update_id": "u2",
                "status": "applied",
                "staleness_versions": 1,
                "tokens_this_update": 20,
            },
            {
                "update_id": "u3",
                "status": "dropped",
                "drop_reason": "superseded",
                "tokens_this_update": 30,
            },
            {
                "update_id": "u4",
                "status": "dropped",
                "drop_reason": "stop_after_outer_steps",
                "tokens_this_update": 40,
            },
        ],
    )
    _write_csv(
        root / "metrics" / "learner_metrics.csv",
        ["learner_id", "local_step", "train_loss"],
        [
            {"learner_id": f"l{index % 2}", "local_step": index + 1, "train_loss": index + 1}
            for index in range(12)
        ],
    )
    _write_csv(
        root / "metrics" / "syncer_metrics.csv",
        ["version", "selected_count"],
        [
            {"version": 1, "selected_count": 1},
            {"version": 2, "selected_count": 1},
        ],
    )
    _write_json(
        root / "heartbeats" / "l0.json",
        {"learner_id": "l0", "last_local_step": 11},
    )
    _write_json(
        root / "heartbeats" / "l1.json",
        {"learner_id": "l1", "last_local_step": 12},
    )
    return root


def test_extract_report_oriented_metrics(tmp_path):
    row = extract_run_metrics(_base_run(tmp_path))

    assert row["run_id"] == "example"
    assert row["produced_updates"] == 4
    assert row["applied_updates"] == 2
    assert row["update_utilization_percent"] == 50.0
    assert row["dropped_updates"] == 2
    assert row["dropped_superseded"] == 1
    assert row["dropped_stop_finalized"] == 1
    assert row["local_steps_total"] == 23
    assert row["complete_training_time_seconds"] == 12.5
    assert row["applied_staleness_0"] == 1
    assert row["applied_staleness_1"] == 1
    assert row["applied_tokens"] == 30
    assert row["produced_tokens"] == 100
    assert row["loss_first_10_mean"] == 5.5
    assert row["loss_last_10_mean"] == 7.5
    assert row["loss_mean"] == 6.5


def test_old_run_falls_back_to_committed_selection_logs(tmp_path):
    root = tmp_path / "old"
    _write_json(root / "control" / "summary.json", {"run_id": "old", "final_version": 2})
    _write_csv(
        root / "metrics" / "update_manifest.csv",
        ["update_id", "base_global_version", "tokens_this_update"],
        [
            {"update_id": "u1", "base_global_version": 0, "tokens_this_update": 10},
            {"update_id": "u2", "base_global_version": 0, "tokens_this_update": 20},
            {"update_id": "u3", "base_global_version": 1, "tokens_this_update": 30},
        ],
    )
    _write_csv(
        root / "metrics" / "syncer_metrics.csv",
        ["version", "selected_count"],
        [{"version": 1, "selected_count": 1}, {"version": 2, "selected_count": 1}],
    )
    _write_jsonl(
        root / "logs" / "syncer.jsonl",
        [
            {"event_type": "updates_selected", "version": 0, "update_ids": ["u1"]},
            {"event_type": "updates_selected", "version": 1, "update_ids": ["u2"]},
        ],
    )

    row = extract_run_metrics(root)

    assert row["produced_updates"] == 3
    assert row["applied_updates"] == 2
    assert row["dropped_updates"] == 1
    assert row["dropped_unknown"] == 1
    assert row["applied_staleness_0"] == 1
    assert row["applied_staleness_1"] == 1
    assert row["applied_tokens"] == 30


def test_csv_overwrite_then_append_and_schema_guard(tmp_path):
    output = tmp_path / "metrics.csv"
    row = {column: "" for column in CSV_COLUMNS}
    row["run_id"] = "one"

    assert write_metrics_csv([row], output, append=False) == 1
    row["run_id"] = "two"
    assert write_metrics_csv([row], output, append=True) == 1
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [item["run_id"] for item in rows] == ["one", "two"]

    output.write_text("wrong,header\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema differs"):
        write_metrics_csv([row], output, append=True)
