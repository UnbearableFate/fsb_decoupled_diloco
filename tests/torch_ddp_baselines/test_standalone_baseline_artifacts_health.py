"""Tests for standalone baseline artifacts and terminal health evidence."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from torch_ddp_baselines.artifacts import (
    RANK_METRIC_FIELDS,
    SYNC_METRIC_FIELDS,
    BaselineRunPaths,
    initialize_run,
)
from torch_ddp_baselines.config import load_config
from torch_ddp_baselines.health import evaluate_health


CONFIG_PATH = Path("torch_ddp_baselines/configs/gpt2_wikitext2_8n_5000steps.yaml")
COMMIT = "1" * 40


def _runtimes() -> list[dict[str, Any]]:
    """Build the declared eight-rank CUDA topology for artifact tests."""

    return [
        {
            "rank": rank,
            "world_size": 8,
            "local_rank": 0,
            "hostname": f"mg{rank:04d}",
            "backend": "nccl",
            "device": "cuda:0",
            "device_type": "cuda",
        }
        for rank in range(8)
    ]


def _write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: list[dict[str, Any]],
) -> None:
    """Create deterministic CSV evidence for a completed synthetic run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_completed_run(root: Path, *, mode: str, omit_sync_step: int | None = None) -> None:
    """Materialize the complete configured run required by the health checker."""

    config = load_config(CONFIG_PATH)
    paths = BaselineRunPaths(root)
    initialize_run(
        paths,
        config=config,
        mode=mode,
        run_id=f"test-{mode}",
        runtimes=_runtimes(),
        source_commit=COMMIT,
    )
    for rank in range(8):
        rows = [
            {
                "timestamp": step,
                "mode": mode,
                "rank": rank,
                "hostname": f"mg{rank:04d}",
                "step": step,
                "loss": 4.0 - step * 0.001 + rank * 0.0001,
                "learning_rate": 5.0e-5,
                "tokens": 1,
                "cumulative_tokens": step,
                "tokens_per_second": 1.0,
                "global_tokens_per_second": 8.0 if rank == 0 else "",
                "step_time_seconds": 1.0,
                "grad_norm": 1.0,
                "gradient_sync_count": step if mode == "ddp" else 0,
                "parameter_average_count": step // 200 if mode != "ddp" else 0,
                "last_model_average_step": step // 200 * 200 if mode != "ddp" else 0,
            }
            for step in range(1, config.training.max_steps + 1)
        ]
        _write_csv(paths.rank_metrics(rank), RANK_METRIC_FIELDS, rows)
    interval = config.distributed.periodic_average_interval
    sync_steps = (
        range(1, config.training.max_steps + 1)
        if mode == "ddp"
        else range(interval, config.training.max_steps + 1, interval)
    )
    sync_rows = [
        {
            "timestamp": step,
            "mode": mode,
            "step": step,
            "sync_kind": "gradient_all_reduce" if mode == "ddp" else "parameter_average",
            "duration_seconds": 0.1,
            "flattened_numel": 1,
            "world_size": 8,
            "cumulative_sync_count": step if mode == "ddp" else step // interval,
        }
        for step in sync_steps
        if step != omit_sync_step
    ]
    _write_csv(paths.sync_metrics, SYNC_METRIC_FIELDS, sync_rows)
    paths.final_checkpoint.mkdir()
    (paths.final_checkpoint / "model.safetensors").write_bytes(b"model")
    paths.summary.write_text(
        json.dumps(
            {
                "status": "completed",
                "exit_status": 0,
                "final_step": config.training.max_steps,
            }
        ),
        encoding="utf-8",
    )


def test_run_initialization_refuses_to_overwrite_manifest(tmp_path: Path) -> None:
    """Experiment identity is create-once so repeated submissions cannot mix evidence."""

    config = load_config(CONFIG_PATH)
    paths = BaselineRunPaths(tmp_path / "exclusive")
    initialize_run(
        paths,
        config=config,
        mode="ddp",
        run_id="exclusive",
        runtimes=_runtimes(),
        source_commit=COMMIT,
    )

    try:
        initialize_run(
            paths,
            config=config,
            mode="ddp",
            run_id="exclusive",
            runtimes=_runtimes(),
            source_commit=COMMIT,
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing manifest was overwritten")


def test_health_accepts_complete_ddp_and_periodic_runs(tmp_path: Path) -> None:
    """Both current modes must produce sufficient evidence after all configured steps."""

    ddp_root = tmp_path / "ddp"
    periodic_root = tmp_path / "periodic"
    _build_completed_run(ddp_root, mode="ddp")
    _build_completed_run(periodic_root, mode="periodic_average")

    assert evaluate_health(ddp_root, mode="ddp")["passed"]
    periodic = evaluate_health(periodic_root, mode="periodic_average")
    assert periodic["passed"]
    assert periodic["checks"]["observed_sync_steps"] == list(range(200, 5001, 200))


def test_health_rejects_a_missing_periodic_average(tmp_path: Path) -> None:
    """A terminal run cannot pass if one configured communication boundary is absent."""

    root = tmp_path / "missing-sync"
    _build_completed_run(root, mode="periodic_average", omit_sync_step=5000)

    result = evaluate_health(root, mode="periodic_average")

    assert not result["passed"]
    assert any("5000" in failure for failure in result["failures"])
