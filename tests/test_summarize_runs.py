"""Verify completed baseline run aggregation and primary-key deduplication."""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "summarize_runs.py"


def _module() -> ModuleType:
    """Load the standalone tool without requiring tools to be a Python package."""

    specification = importlib.util.spec_from_file_location("summarize_runs", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _write_completed_run(
    runs_root: Path,
    run_id: str,
    *,
    status: str = "completed",
) -> Path:
    """Create a small current-layout baseline run with six complete report steps."""

    run = runs_root / "torch_ddp_baselines" / run_id
    (run / "logs").mkdir(parents=True)
    (run / "metrics").mkdir()
    summary = {
        "status": status,
        "exit_status": 0,
        "run_id": run_id,
        "mode": "ddp",
        "backend": "nccl",
        "world_size": 2,
        "max_steps": 60,
        "final_step": 60,
        "gradient_sync_count": 2,
        "parameter_average_count": 0,
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
        "distributed": {"periodic_average_interval": 100},
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


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Read generated CSV rows for assertions without changing their text values."""

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_parse_completed_run_extracts_hyperparameters_losses_and_timing(
    tmp_path: Path,
) -> None:
    """A row must preserve config identity and aggregate the final five full reports."""

    module = _module()
    run = _write_completed_run(tmp_path / "runs", "completed-run")

    row = module.parse_completed_run(run)

    assert row["run_dir_name"] == "completed-run"
    assert row["model_name_or_path"] == "gpt2"
    assert row["global_batch_size"] == 32
    assert row["global_tokens_per_step"] == 32768
    assert row["final_5_report_steps"] == "20;30;40;50;60"
    assert row["final_5_report_mean_loss"] == pytest.approx(4.5)
    assert row["coordinator_training_time_seconds"] == pytest.approx(30.0)
    assert row["sync_metrics_training_span_seconds"] == pytest.approx(20.0)
    assert row["synchronization_time_seconds"] == pytest.approx(3.0)
    assert row["synchronization_time_fraction"] == pytest.approx(0.1)


def test_csv_update_discovers_recursively_and_deduplicates_by_run_name(tmp_path: Path) -> None:
    """Repeated aggregation must not add a second row for an existing run directory name."""

    module = _module()
    runs_root = tmp_path / "runs"
    _write_completed_run(runs_root, "completed-run")
    _write_completed_run(runs_root, "running-run", status="running")
    output = tmp_path / "results" / "runs.csv"

    assert module.update_summary_csv(runs_root, output) == (1, 0, 1)
    assert module.update_summary_csv(runs_root, output) == (0, 1, 1)

    rows = _read_rows(output)
    assert [row["run_dir_name"] for row in rows] == ["completed-run"]


def test_invalid_completed_run_leaves_existing_csv_unchanged(tmp_path: Path) -> None:
    """A malformed completed artifact must abort before replacing valid accumulated rows."""

    module = _module()
    runs_root = tmp_path / "runs"
    run = _write_completed_run(runs_root, "completed-run")
    output = tmp_path / "runs.csv"
    assert module.update_summary_csv(runs_root, output) == (1, 0, 1)
    original = output.read_bytes()
    manifest_path = run / "training_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "different-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(module.RunParseError, match="IDs differ"):
        module.update_summary_csv(runs_root, output)

    assert output.read_bytes() == original
