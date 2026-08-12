"""Durable run artifacts for standalone distributed baseline experiments."""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
import yaml

from .config import BaselineConfig, config_to_dict


RANK_METRIC_FIELDS = (
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
)

SYNC_METRIC_FIELDS = (
    "timestamp",
    "mode",
    "step",
    "sync_kind",
    "duration_seconds",
    "flattened_numel",
    "world_size",
    "cumulative_sync_count",
)


@dataclass(frozen=True)
class BaselineRunPaths:
    """Resolve every artifact path beneath one immutable run root."""

    root: Path  # Unique directory assigned to one experiment.

    @property
    def manifest(self) -> Path:
        """Return the create-once experiment manifest path."""

        return self.root / "training_manifest.json"

    @property
    def resolved_config(self) -> Path:
        """Return the fully resolved YAML snapshot path."""

        return self.root / "resolved_config.yaml"

    @property
    def source_identity(self) -> Path:
        """Return the source commit evidence path."""

        return self.root / "source_identity.json"

    @property
    def metrics_dir(self) -> Path:
        """Return the per-rank metrics directory."""

        return self.root / "metrics"

    @property
    def logs_dir(self) -> Path:
        """Return the per-rank JSONL log directory."""

        return self.root / "logs"

    @property
    def heartbeats_dir(self) -> Path:
        """Return the per-rank heartbeat directory."""

        return self.root / "heartbeats"

    @property
    def sync_metrics(self) -> Path:
        """Return the rank-zero synchronization metric path."""

        return self.metrics_dir / "synchronization.csv"

    @property
    def summary(self) -> Path:
        """Return the terminal run summary path."""

        return self.root / "summary.json"

    @property
    def final_checkpoint(self) -> Path:
        """Return the atomically published final checkpoint directory."""

        return self.root / "checkpoints" / "final"

    def rank_metrics(self, rank: int) -> Path:
        """Return one rank's optimizer-step metrics path."""

        return self.metrics_dir / f"rank_{rank:03d}.csv"

    def rank_log(self, rank: int) -> Path:
        """Return one rank's structured event log path."""

        return self.logs_dir / f"rank_{rank:03d}.jsonl"

    def rank_heartbeat(self, rank: int) -> Path:
        """Return one rank's replaceable liveness snapshot path."""

        return self.heartbeats_dir / f"rank_{rank:03d}.json"


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry update before reporting publication success."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Publish UTF-8 text with an atomic same-directory replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish deterministic JSON through the atomic text writer."""

    atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")


def safe_read_json(path: Path) -> dict[str, Any] | None:
    """Return a JSON object or ``None`` for absent, partial, or invalid content."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    """Create a JSON claim without replacing an existing experiment manifest."""

    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def initialize_run(
    paths: BaselineRunPaths,
    *,
    config: BaselineConfig,
    mode: str,
    run_id: str,
    runtimes: list[dict[str, Any]],
    source_identity: dict[str, Any],
) -> None:
    """Claim a fresh run root and record immutable config, source, and topology."""

    paths.root.mkdir(parents=True, exist_ok=True)
    if paths.manifest.exists() or list(paths.root.glob("metrics/rank_*.csv")):
        raise FileExistsError(f"refusing to overwrite baseline run: {paths.root}")
    paths.metrics_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.heartbeats_dir.mkdir(parents=True, exist_ok=True)
    paths.final_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    required_source_fields = {
        "git_commit",
        "git_dirty",
        "source_fingerprint",
        "source_scopes",
    }
    if set(source_identity) != required_source_fields:
        raise ValueError("baseline source identity has the wrong fields")
    if source_identity["git_dirty"] is not False:
        raise ValueError("formal baseline source identity must be clean")
    manifest = {
        "format_version": 1,
        "run_id": run_id,
        "run_root": str(paths.root),
        "mode": mode,
        "backend": config.distributed.backend,
        "world_size": len(runtimes),
        "expected_world_size": config.distributed.world_size,
        "require_distinct_hosts": config.distributed.require_distinct_hosts,
        "max_steps": config.training.max_steps,
        "periodic_average_interval": config.distributed.periodic_average_interval,
        "created_at": time.time(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "source_identity": dict(source_identity),
        "runtimes": runtimes,
    }
    _exclusive_json(paths.manifest, manifest)
    atomic_write_text(
        paths.resolved_config,
        yaml.safe_dump(config_to_dict(config), sort_keys=False),
    )
    atomic_write_json(paths.source_identity, dict(source_identity))


def append_csv(path: Path, row: dict[str, Any], fieldnames: tuple[str, ...]) -> None:
    """Append and fsync one CSV row owned by a single distributed rank."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not existing:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


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
    """Replace one rank's heartbeat with its latest durable training state."""

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


class JsonlLogger:
    """Append fsynced structured events to one rank-owned JSONL file."""

    def __init__(self, path: Path, *, rank: int) -> None:
        """Bind the logger to one immutable rank and output path."""

        self.path = path  # Rank-owned durable log path.
        self.rank = rank  # Immutable distributed rank embedded in every event.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event_type: str, **payload: Any) -> None:
        """Append one event and mirror it to the batch-job stdout stream."""

        row = {
            "timestamp": time.time(),
            "rank": self.rank,
            "event_type": event_type,
            **payload,
        }
        text = json.dumps(row, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(text, flush=True)

    def exception(self, event_type: str, **payload: Any) -> None:
        """Append an event carrying the currently handled exception traceback."""

        payload["traceback"] = traceback.format_exc()
        self.event(event_type, **payload)


def save_final_checkpoint(
    paths: BaselineRunPaths,
    model: torch.nn.Module,
    tokenizer: Any,
) -> None:
    """Atomically publish the final model and tokenizer from a staging directory."""

    if paths.final_checkpoint.exists():
        raise FileExistsError(f"final checkpoint already exists: {paths.final_checkpoint}")
    staging = Path(tempfile.mkdtemp(prefix=".final.", dir=paths.final_checkpoint.parent))
    unwrapped = model.module if hasattr(model, "module") else model
    try:
        if not hasattr(unwrapped, "save_pretrained"):
            raise TypeError("final model does not support save_pretrained")
        if not hasattr(tokenizer, "save_pretrained"):
            raise TypeError("final tokenizer does not support save_pretrained")
        unwrapped.save_pretrained(staging, safe_serialization=True)
        tokenizer.save_pretrained(staging)
        os.replace(staging, paths.final_checkpoint)
        _fsync_directory(paths.final_checkpoint.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
