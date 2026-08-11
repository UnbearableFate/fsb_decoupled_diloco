"""Verify unified baseline and Dynamic Full run aggregation."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
from types import ModuleType

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "summarize_runs.py"


def _module() -> ModuleType:
    """Load the standalone tool without requiring tools to be a package."""

    specification = importlib.util.spec_from_file_location("summarize_runs", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _shared_config() -> dict[str, object]:
    """Return the pinned model, data, and optimizer identity shared by fixtures."""

    return {
        "model": {
            "name_or_path": "gpt2",
            "revision": "model-revision",
            "tokenizer_revision": "tokenizer-revision",
            "dtype": "bfloat16",
        },
        "data": {
            "dataset_name": "Salesforce/wikitext",
            "dataset_config_name": "wikitext-2-raw-v1",
            "revision": "dataset-revision",
            "train_split": "train",
            "block_size": 1024,
            "shuffle_blocks": True,
        },
    }


def _write_baseline_run(
    runs_root: Path,
    run_id: str,
    *,
    mode: str = "ddp",
    status: str = "completed",
) -> Path:
    """Create one small current baseline run with six complete report steps."""

    run = runs_root / "torch_ddp_baselines" / run_id
    (run / "logs").mkdir(parents=True)
    (run / "metrics").mkdir()
    summary = {
        "status": status,
        "exit_status": 0,
        "run_id": run_id,
        "mode": mode,
        "backend": "nccl",
        "world_size": 2,
        "max_steps": 60,
        "final_step": 60,
        "gradient_sync_count": 2 if mode == "ddp" else 0,
        "parameter_average_count": 0 if mode == "ddp" else 2,
        "completed_at": 130.0,
    }
    manifest = {
        "run_id": run_id,
        "world_size": 2,
        "created_at": 100.0,
        "pbs_job_id": "123.opbs",
        "source_identity": {"git_commit": "1" * 40},
    }
    config = {
        **_shared_config(),
        "training": {
            "max_steps": 60,
            "micro_batch_size": 2,
            "gradient_accumulation_steps": 8,
            "seed": 1337,
            "grad_clip": 1.0,
            "log_every_steps": 10,
        },
        "optimizer": {
            "lr": 5.0e-5,
            "betas": [0.9, 0.95],
            "eps": 1.0e-8,
            "weight_decay": 0.1,
            "warmup_steps": 10,
            "min_lr_ratio": 0.1,
        },
        "distributed": {"periodic_average_interval": 30},
    }
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run / "training_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "resolved_config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for rank in range(2):
        events = [
            {
                "event_type": "optimizer_step",
                "rank": rank,
                "step": step,
                "loss": rank + step / 10,
            }
            for step in range(10, 61, 10)
        ]
        (run / "logs" / f"rank_{rank:03d}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
    with (run / "metrics" / "synchronization.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "duration_seconds"])
        writer.writeheader()
        writer.writerows(
            [
                {"timestamp": 105.0, "duration_seconds": 1.0},
                {"timestamp": 125.0, "duration_seconds": 2.0},
            ]
        )
    return run


def _write_full_protocol_run(runs_root: Path, run_id: str) -> Path:
    """Create a finalized eight-stream Dynamic Full authority and telemetry fixture."""

    run = runs_root / "full_protocol" / run_id
    control = run / "control"
    control.mkdir(parents=True)
    summary = {
        "authority": "full_protocol",
        "all_learners_stopped": True,
        "run_id": run_id,
        "final_version": 10,
        "finalized_at": 160.0,
    }
    descriptor = {
        "run_id": run_id,
        "mode": "dynamic",
        "created_at": 100.0,
    }
    source = {
        "git_commit": "1" * 40,
        "source_fingerprint": "sha256:" + "2" * 64,
    }
    config = {
        **_shared_config(),
        "sync": {
            "num_learners": 8,
            "quorum_min": 4,
            "quorum_max": 4,
            "stop_after_outer_steps": 10,
        },
        "training": {
            "inner_steps": 200,
            "micro_batch_size": 2,
            "gradient_accumulation_steps": 8,
        },
        "inner_optimizer": {
            "lr": 5.0e-5,
            "betas": [0.9, 0.95],
            "eps": 1.0e-8,
            "weight_decay": 0.1,
            "warmup_steps": 100,
            "min_lr_ratio": 0.1,
        },
    }
    (control / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (control / "run_descriptor.json").write_text(json.dumps(descriptor), encoding="utf-8")
    (control / "run_source_manifest.json").write_text(json.dumps(source), encoding="utf-8")
    (control / "run_config.resolved.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    database = control / "syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE controller_state(singleton INTEGER, generation INTEGER, state TEXT);
        CREATE TABLE terminal_contributor_fences(
            generation INTEGER,
            stable_contributor_key TEXT,
            fence_json TEXT,
            state TEXT
        );
        CREATE TABLE updates(
            update_id TEXT,
            fence_json TEXT,
            local_step_end INTEGER,
            inner_steps INTEGER,
            status TEXT,
            applied_version INTEGER
        );
        CREATE TABLE learner_instances(pbs_job_id TEXT);
        CREATE TABLE syncer_epochs(pbs_job_id TEXT);
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO syncer_epochs VALUES('syncer.opbs');
        """
    )
    fences: list[str] = []
    for stream in range(8):
        instance_id = f"instance-{stream}"
        fence = json.dumps(
            {"kind": "dynamic", "instance_id": instance_id, "stream_id": stream},
            sort_keys=True,
            separators=(",", ":"),
        )
        fences.append(fence)
        connection.execute(
            "INSERT INTO terminal_contributor_fences VALUES(1, ?, ?, 'acked')",
            (str(stream), fence),
        )
        connection.execute("INSERT INTO learner_instances VALUES(?)", (f"{stream}.opbs",))
        metrics = run / "metrics" / "learner" / instance_id
        metrics.mkdir(parents=True)
        (metrics / f"attempt-{stream}.jsonl").write_text(
            json.dumps(
                {
                    "event_type": "proposal_published",
                    "timestamp": 120.0 + stream,
                    "mean_loss": 2.0 + stream / 10,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    local_steps = [0] * 8
    for version in range(1, 11):
        for offset in range(4):
            stream = (version * 4 + offset) % 8
            local_steps[stream] += 200
            connection.execute(
                "INSERT INTO updates VALUES(?, ?, ?, 200, 'applied', ?)",
                (f"update-{version}-{offset}", fences[stream], local_steps[stream], version),
            )
    connection.commit()
    connection.close()
    return run


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Read generated CSV rows without changing their textual values."""

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_parse_baseline_extracts_unified_loss_workload_and_timing(tmp_path: Path) -> None:
    """Baseline rows must preserve their workload and final complete rank reports."""

    module = _module()
    run = _write_baseline_run(tmp_path / "runs", "ddp-run")

    row = module.parse_completed_run(run)

    assert row["run_kind"] == "torch_ddp_baseline"
    assert row["tokens_per_optimizer_step_per_contributor"] == 16384
    assert row["final_report_coordinate"] == "20;30;40;50;60"
    assert row["final_mean_loss"] == pytest.approx(4.5)
    assert row["training_time_seconds"] == pytest.approx(30.0)
    assert row["synchronization_time_seconds"] == pytest.approx(3.0)
    assert row["synchronization_time_fraction"] == pytest.approx(0.1)


def test_parse_dynamic_full_uses_terminal_fences_and_exact_merge_counts(
    tmp_path: Path,
) -> None:
    """Dynamic rows must use final fence telemetry and ten exact four-update merges."""

    module = _module()
    run = _write_full_protocol_run(tmp_path / "runs", "dynamic-run")

    row = module.parse_completed_run(run)

    assert row["run_kind"] == "fs_diloco_dynamic_full"
    assert row["terminal_contributors"] == 8
    assert row["global_steps"] == 10
    assert row["merge_contributors"] == 4
    assert row["optimizer_steps_min"] == 1000
    assert row["optimizer_steps_max"] == 1000
    assert row["final_report_count"] == 8
    assert row["final_mean_loss"] == pytest.approx(2.35)


def test_csv_update_discovers_both_layouts_and_deduplicates(tmp_path: Path) -> None:
    """Repeated recursive aggregation must keep one row per current run identity."""

    module = _module()
    runs_root = tmp_path / "runs"
    _write_baseline_run(runs_root, "ddp-run")
    _write_full_protocol_run(runs_root, "dynamic-run")
    _write_baseline_run(runs_root, "running-run", status="running")
    output = tmp_path / "results" / "runs.csv"

    assert module.update_summary_csv([runs_root], output) == (2, 0, 2)
    assert module.update_summary_csv([runs_root], output) == (0, 2, 2)
    assert [row["run_id"] for row in _read_rows(output)] == ["dynamic-run", "ddp-run"]


def test_comparison_flags_twenty_percent_metric_difference(tmp_path: Path) -> None:
    """Comparison output must flag either metric whose absolute delta exceeds 20%."""

    module = _module()
    runs_root = tmp_path / "runs"
    ddp = _write_baseline_run(runs_root, "ddp-run", mode="ddp")
    periodic = _write_baseline_run(runs_root, "periodic-run", mode="periodic_average")
    dynamic = _write_full_protocol_run(runs_root, "dynamic-run")
    output = tmp_path / "runs.csv"
    comparison = tmp_path / "comparison.json"
    module.update_summary_csv([ddp, periodic, dynamic], output)

    module.write_comparisons(output, comparison)

    payload = json.loads(comparison.read_text(encoding="utf-8"))
    assert payload["comparison_count"] == 2
    assert all(item["comparable_identity"] for item in payload["comparisons"])
    assert all(item["investigation_required"] for item in payload["comparisons"])


def test_invalid_completed_run_leaves_existing_csv_unchanged(tmp_path: Path) -> None:
    """A malformed completed artifact must not replace an existing valid table."""

    module = _module()
    runs_root = tmp_path / "runs"
    run = _write_baseline_run(runs_root, "ddp-run")
    output = tmp_path / "runs.csv"
    assert module.update_summary_csv([runs_root], output) == (1, 0, 1)
    original = output.read_bytes()
    manifest_path = run / "training_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "different-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(module.RunParseError, match="IDs differ"):
        module.update_summary_csv([runs_root], output)

    assert output.read_bytes() == original
