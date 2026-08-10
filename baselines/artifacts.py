"""Durable local evidence for torch.distributed baseline runs."""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..fs_diloco.core.config import Config, config_to_dict
from ..fs_diloco.storage.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    safe_read_json,
)


RANK_METRIC_FIELDS = [
    "timestamp",
    "mode",
    "rank",
    "hostname",
    "step",
    "loss",
    "learning_rate",
    "tokens",
    "cumulative_tokens",
    "tokens_per_second",
    "global_tokens_per_second",
    "step_time_seconds",
    "grad_norm",
    "gradient_sync_count",
    "parameter_average_count",
    "last_model_average_step",
]

SYNC_METRIC_FIELDS = [
    "timestamp",
    "mode",
    "step",
    "sync_kind",
    "duration_seconds",
    "flattened_numel",
    "world_size",
    "cumulative_sync_count",
]


@dataclass(frozen=True)
class BaselineRunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "training_manifest.json"

    @property
    def resolved_config(self) -> Path:
        return self.root / "resolved_config.yaml"

    @property
    def source_identity(self) -> Path:
        return self.root / "source_identity.json"

    @property
    def metrics_dir(self) -> Path:
        return self.root / "metrics"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def heartbeats_dir(self) -> Path:
        return self.root / "heartbeats"

    @property
    def sync_metrics(self) -> Path:
        return self.metrics_dir / "synchronization.csv"

    @property
    def summary(self) -> Path:
        return self.root / "summary.json"

    @property
    def final_checkpoint(self) -> Path:
        return self.root / "checkpoints" / "final"

    def rank_metrics(self, rank: int) -> Path:
        return self.metrics_dir / f"rank_{rank:03d}.csv"

    def rank_log(self, rank: int) -> Path:
        return self.logs_dir / f"rank_{rank:03d}.jsonl"

    def rank_heartbeat(self, rank: int) -> Path:
        return self.heartbeats_dir / f"rank_{rank:03d}.json"


def _exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def initialize_run_root(
    paths: BaselineRunPaths,
    *,
    config: Config,
    mode: str,
    backend: str,
    max_steps: int,
    average_interval: int,
    runtimes: list[dict[str, Any]],
) -> None:
    """Create a new run manifest without overwriting prior training evidence."""

    ensure_dir(paths.root)
    existing_metrics = list(paths.root.glob("metrics/rank_*.csv"))
    if paths.manifest.exists() or existing_metrics:
        raise FileExistsError(
            f"refusing to overwrite existing torch baseline run: {paths.root}"
        )
    ensure_dir(paths.metrics_dir)
    ensure_dir(paths.logs_dir)
    ensure_dir(paths.heartbeats_dir)
    ensure_dir(paths.final_checkpoint.parent)

    expected_source_identity = {
        "git_commit": config.run.git_commit,
        "git_dirty": config.run.git_dirty,
        "source_fingerprint": config.run.source_fingerprint,
    }
    if paths.source_identity.exists():
        existing_identity = safe_read_json(paths.source_identity)
        if existing_identity is None:
            raise ValueError(f"invalid existing source identity: {paths.source_identity}")
        mismatched = {
            key: (existing_identity.get(key), expected)
            for key, expected in expected_source_identity.items()
            if expected is not None and existing_identity.get(key) != expected
        }
        if mismatched:
            raise ValueError(f"existing source identity does not match runtime: {mismatched}")

    manifest = {
        "format_version": 1,
        "run_id": config.run.run_id,
        "shared_root": str(paths.root),
        "mode": mode,
        "backend": backend,
        "world_size": len(runtimes),
        "expected_world_size": int(config.sync.num_learners),
        "max_steps": int(max_steps),
        "average_interval": int(average_interval),
        "created_at": time.time(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "source_identity": expected_source_identity,
        "runtimes": runtimes,
    }
    _exclusive_json(paths.manifest, manifest)
    atomic_write_text(
        paths.resolved_config,
        __import__("yaml").safe_dump(config_to_dict(config), sort_keys=False),
    )
    if not paths.source_identity.exists():
        atomic_write_json(paths.source_identity, manifest["source_identity"])


def append_durable_csv(path: Path, row: dict[str, Any], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()


def write_heartbeat(
    paths: BaselineRunPaths,
    *,
    rank: int,
    status: str,
    step: int,
    mode: str,
    hostname: str,
    loss: float | None,
    last_model_average_step: int,
    error: str | None = None,
) -> None:
    atomic_write_json(
        paths.rank_heartbeat(rank),
        {
            "format_version": 1,
            "timestamp": time.time(),
            "rank": rank,
            "hostname": hostname,
            "mode": mode,
            "status": status,
            "step": step,
            "loss": loss,
            "last_model_average_step": last_model_average_step,
            "error": error,
        },
    )
