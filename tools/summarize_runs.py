"""Collect completed baseline training runs into one deduplicated CSV table.

The run directory name is the table's primary key.  Only the current baseline
artifact layout is accepted: a terminal ``summary.json`` alongside
``training_manifest.json``, ``resolved_config.yaml``, rank JSONL logs, and the
rank-zero synchronization CSV.  A malformed completed run aborts the update so
the output never mixes complete rows with silently partial data.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml


CSV_FIELDS = (
    "run_dir_name",
    "run_dir",
    "status",
    "mode",
    "backend",
    "world_size",
    "pbs_job_id",
    "git_commit",
    "model_name_or_path",
    "model_revision",
    "tokenizer_revision",
    "model_dtype",
    "dataset_name",
    "dataset_config_name",
    "dataset_revision",
    "train_split",
    "block_size",
    "shuffle_blocks",
    "max_steps",
    "micro_batch_size",
    "gradient_accumulation_steps",
    "global_batch_size",
    "global_tokens_per_step",
    "seed",
    "grad_clip",
    "log_every_steps",
    "learning_rate",
    "optimizer_beta1",
    "optimizer_beta2",
    "optimizer_epsilon",
    "weight_decay",
    "warmup_steps",
    "min_lr_ratio",
    "periodic_average_interval",
    "final_step",
    "gradient_sync_count",
    "parameter_average_count",
    "final_5_report_steps",
    "final_5_report_mean_loss",
    "coordinator_training_time_seconds",
    "sync_metrics_training_span_seconds",
    "synchronization_time_seconds",
    "mean_synchronization_time_seconds",
    "synchronization_time_fraction",
)
PRIMARY_KEY = "run_dir_name"
FINAL_REPORT_COUNT = 5


class RunParseError(ValueError):
    """Report a completed run whose artifacts violate the current schema."""


def _load_json_object(path: Path) -> dict[str, Any]:
    """Read one JSON object and attach its path to decoding failures."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunParseError(f"cannot read JSON object: {path}") from exc
    if not isinstance(payload, dict):
        raise RunParseError(f"JSON root must be an object: {path}")
    return payload


def _load_yaml_object(path: Path) -> dict[str, Any]:
    """Read one YAML mapping and attach its path to decoding failures."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunParseError(f"cannot read YAML mapping: {path}") from exc
    if not isinstance(payload, dict):
        raise RunParseError(f"YAML root must be a mapping: {path}")
    return payload


def _mapping(payload: dict[str, Any], key: str, *, path: Path) -> dict[str, Any]:
    """Return one required mapping-valued field from an artifact."""

    value = payload.get(key)
    if not isinstance(value, dict):
        raise RunParseError(f"{path}: {key} must be a mapping")
    return value


def _text(payload: dict[str, Any], key: str, *, path: Path) -> str:
    """Return one required non-empty string field from an artifact."""

    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RunParseError(f"{path}: {key} must be a non-empty string")
    return value


def _integer(payload: dict[str, Any], key: str, *, path: Path) -> int:
    """Return one required integer field while rejecting booleans."""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunParseError(f"{path}: {key} must be an integer")
    return value


def _number(payload: dict[str, Any], key: str, *, path: Path) -> float:
    """Return one required finite numeric field while rejecting booleans."""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunParseError(f"{path}: {key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RunParseError(f"{path}: {key} must be finite")
    return result


def _csv_number(row: dict[str, str], key: str, *, path: Path) -> float:
    """Parse one required finite numeric CSV cell."""

    value = row.get(key)
    try:
        result = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise RunParseError(f"{path}: {key} must be numeric") from exc
    if not math.isfinite(result):
        raise RunParseError(f"{path}: {key} must be finite")
    return result


def _final_report_loss(run_dir: Path, *, world_size: int) -> tuple[str, float]:
    """Average the last five complete optimizer-step reports across every rank."""

    losses: dict[int, dict[int, float]] = {}
    log_paths = sorted((run_dir / "logs").glob("rank_*.jsonl"))
    if len(log_paths) != world_size:
        raise RunParseError(f"{run_dir}: expected {world_size} rank logs, found {len(log_paths)}")
    for log_path in log_paths:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RunParseError(f"cannot read rank log: {log_path}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunParseError(f"invalid JSON at {log_path}:{line_number}") from exc
            if not isinstance(event, dict):
                raise RunParseError(f"JSONL event must be an object at {log_path}:{line_number}")
            if event.get("event_type") != "optimizer_step":
                continue
            step = _integer(event, "step", path=log_path)
            rank = _integer(event, "rank", path=log_path)
            loss = _number(event, "loss", path=log_path)
            by_rank = losses.setdefault(step, {})
            if rank in by_rank:
                raise RunParseError(f"duplicate optimizer report for step {step}, rank {rank}")
            by_rank[rank] = loss

    report_steps = sorted(losses)[-FINAL_REPORT_COUNT:]
    if len(report_steps) != FINAL_REPORT_COUNT:
        raise RunParseError(
            f"{run_dir}: expected at least {FINAL_REPORT_COUNT} optimizer report steps"
        )
    expected_ranks = set(range(world_size))
    selected_losses: list[float] = []
    for step in report_steps:
        if set(losses[step]) != expected_ranks:
            raise RunParseError(f"{run_dir}: optimizer report step {step} is incomplete")
        selected_losses.extend(losses[step][rank] for rank in range(world_size))
    return ";".join(str(step) for step in report_steps), math.fsum(selected_losses) / len(
        selected_losses
    )


def _synchronization_metrics(
    run_dir: Path,
    *,
    expected_count: int,
    training_time_seconds: float,
) -> tuple[float, float, float, float]:
    """Summarize synchronization timestamps and measured synchronization windows."""

    path = run_dir / "metrics" / "synchronization.csv"
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise RunParseError(f"cannot read synchronization metrics: {path}") from exc
    if len(rows) != expected_count:
        raise RunParseError(
            f"{path}: expected {expected_count} synchronization rows, found {len(rows)}"
        )
    timestamps = [_csv_number(row, "timestamp", path=path) for row in rows]
    durations = [_csv_number(row, "duration_seconds", path=path) for row in rows]
    if any(duration < 0.0 for duration in durations):
        raise RunParseError(f"{path}: synchronization duration cannot be negative")
    span = max(timestamps) - min(timestamps) if timestamps else 0.0
    total = math.fsum(durations)
    mean = total / len(durations) if durations else 0.0
    fraction = total / training_time_seconds if training_time_seconds else 0.0
    return span, total, mean, fraction


def parse_completed_run(run_dir: str | Path) -> dict[str, Any]:
    """Project one successful current-layout baseline run into a CSV record."""

    run_dir = Path(run_dir).resolve()
    summary_path = run_dir / "summary.json"
    manifest_path = run_dir / "training_manifest.json"
    config_path = run_dir / "resolved_config.yaml"
    summary = _load_json_object(summary_path)
    manifest = _load_json_object(manifest_path)
    config = _load_yaml_object(config_path)

    if (
        summary.get("status") != "completed"
        or _integer(summary, "exit_status", path=summary_path) != 0
    ):
        raise RunParseError(f"{run_dir}: run is not successfully completed")
    run_id = _text(summary, "run_id", path=summary_path)
    if run_id != run_dir.name or _text(manifest, "run_id", path=manifest_path) != run_id:
        raise RunParseError(f"{run_dir}: run directory, summary, and manifest IDs differ")

    model = _mapping(config, "model", path=config_path)
    data = _mapping(config, "data", path=config_path)
    training = _mapping(config, "training", path=config_path)
    optimizer = _mapping(config, "optimizer", path=config_path)
    distributed = _mapping(config, "distributed", path=config_path)
    world_size = _integer(summary, "world_size", path=summary_path)
    max_steps = _integer(training, "max_steps", path=config_path)
    micro_batch_size = _integer(training, "micro_batch_size", path=config_path)
    accumulation_steps = _integer(training, "gradient_accumulation_steps", path=config_path)
    block_size = _integer(data, "block_size", path=config_path)
    final_step = _integer(summary, "final_step", path=summary_path)
    if world_size <= 0 or final_step != max_steps:
        raise RunParseError(f"{run_dir}: completed step or world size is inconsistent")
    if _integer(manifest, "world_size", path=manifest_path) != world_size:
        raise RunParseError(f"{run_dir}: summary and manifest world sizes differ")
    if _integer(summary, "max_steps", path=summary_path) != max_steps:
        raise RunParseError(f"{run_dir}: summary and config max steps differ")

    created_at = _number(manifest, "created_at", path=manifest_path)
    completed_at = _number(summary, "completed_at", path=summary_path)
    training_time_seconds = completed_at - created_at
    if training_time_seconds <= 0.0:
        raise RunParseError(f"{run_dir}: completion time must follow creation time")
    report_steps, final_report_loss = _final_report_loss(run_dir, world_size=world_size)
    gradient_sync_count = _integer(summary, "gradient_sync_count", path=summary_path)
    parameter_average_count = _integer(summary, "parameter_average_count", path=summary_path)
    sync_span, sync_total, sync_mean, sync_fraction = _synchronization_metrics(
        run_dir,
        expected_count=gradient_sync_count + parameter_average_count,
        training_time_seconds=training_time_seconds,
    )
    betas = optimizer.get("betas")
    if not isinstance(betas, list) or len(betas) != 2:
        raise RunParseError(f"{config_path}: optimizer.betas must contain two values")
    beta_payload = {"beta1": betas[0], "beta2": betas[1]}
    source_identity = _mapping(manifest, "source_identity", path=manifest_path)

    return {
        "run_dir_name": run_id,
        "run_dir": str(run_dir),
        "status": "completed",
        "mode": _text(summary, "mode", path=summary_path),
        "backend": _text(summary, "backend", path=summary_path),
        "world_size": world_size,
        "pbs_job_id": _text(manifest, "pbs_job_id", path=manifest_path),
        "git_commit": _text(source_identity, "git_commit", path=manifest_path),
        "model_name_or_path": _text(model, "name_or_path", path=config_path),
        "model_revision": _text(model, "revision", path=config_path),
        "tokenizer_revision": _text(model, "tokenizer_revision", path=config_path),
        "model_dtype": _text(model, "dtype", path=config_path),
        "dataset_name": _text(data, "dataset_name", path=config_path),
        "dataset_config_name": _text(data, "dataset_config_name", path=config_path),
        "dataset_revision": _text(data, "revision", path=config_path),
        "train_split": _text(data, "train_split", path=config_path),
        "block_size": block_size,
        "shuffle_blocks": data.get("shuffle_blocks"),
        "max_steps": max_steps,
        "micro_batch_size": micro_batch_size,
        "gradient_accumulation_steps": accumulation_steps,
        "global_batch_size": micro_batch_size * accumulation_steps * world_size,
        "global_tokens_per_step": micro_batch_size * accumulation_steps * world_size * block_size,
        "seed": _integer(training, "seed", path=config_path),
        "grad_clip": _number(training, "grad_clip", path=config_path),
        "log_every_steps": _integer(training, "log_every_steps", path=config_path),
        "learning_rate": _number(optimizer, "lr", path=config_path),
        "optimizer_beta1": _number(beta_payload, "beta1", path=config_path),
        "optimizer_beta2": _number(beta_payload, "beta2", path=config_path),
        "optimizer_epsilon": _number(optimizer, "eps", path=config_path),
        "weight_decay": _number(optimizer, "weight_decay", path=config_path),
        "warmup_steps": _integer(optimizer, "warmup_steps", path=config_path),
        "min_lr_ratio": _number(optimizer, "min_lr_ratio", path=config_path),
        "periodic_average_interval": _integer(
            distributed, "periodic_average_interval", path=config_path
        ),
        "final_step": final_step,
        "gradient_sync_count": gradient_sync_count,
        "parameter_average_count": parameter_average_count,
        "final_5_report_steps": report_steps,
        "final_5_report_mean_loss": final_report_loss,
        "coordinator_training_time_seconds": training_time_seconds,
        "sync_metrics_training_span_seconds": sync_span,
        "synchronization_time_seconds": sync_total,
        "mean_synchronization_time_seconds": sync_mean,
        "synchronization_time_fraction": sync_fraction,
    }


def find_completed_runs(runs_root: str | Path) -> tuple[Path, ...]:
    """Find current-layout run directories whose terminal status is completed."""

    root = Path(runs_root).resolve()
    if not root.is_dir():
        raise RunParseError(f"runs root is not a directory: {root}")
    completed: list[Path] = []
    for summary_path in sorted(root.rglob("summary.json")):
        summary = _load_json_object(summary_path)
        if summary.get("status") == "completed":
            completed.append(summary_path.parent)
    return tuple(completed)


def _read_existing_rows(output_path: Path) -> list[dict[str, str]]:
    """Read and validate an existing current-schema CSV before adding records."""

    if not output_path.exists():
        return []
    try:
        with output_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise RunParseError(f"CSV header does not match current schema: {output_path}")
            rows = list(reader)
    except OSError as exc:
        raise RunParseError(f"cannot read output CSV: {output_path}") from exc
    seen: set[str] = set()
    for row in rows:
        key = row.get(PRIMARY_KEY, "")
        if not key or key in seen:
            raise RunParseError(f"CSV contains a missing or duplicate primary key: {output_path}")
        seen.add(key)
    return rows


def _write_rows_atomic(output_path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Replace the CSV atomically after every row has been parsed and deduplicated."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def update_summary_csv(
    runs_root: str | Path,
    output_path: str | Path,
) -> tuple[int, int, int]:
    """Add unseen completed runs and return added, duplicate, and total row counts."""

    output = Path(output_path).resolve()
    existing_rows = _read_existing_rows(output)
    known_keys = {row[PRIMARY_KEY] for row in existing_rows}
    discovered_records = [parse_completed_run(path) for path in find_completed_runs(runs_root)]
    discovered_keys: set[str] = set()
    for record in discovered_records:
        key = str(record[PRIMARY_KEY])
        if key in discovered_keys:
            raise RunParseError(f"multiple run directories use the primary key {key!r}")
        discovered_keys.add(key)
    new_records = [
        record for record in discovered_records if str(record[PRIMARY_KEY]) not in known_keys
    ]
    duplicate_count = len(discovered_records) - len(new_records)
    combined: list[dict[str, Any]] = [*existing_rows, *new_records]
    if new_records or not output.exists():
        _write_rows_atomic(output, combined)
    return len(new_records), duplicate_count, len(combined)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for recursive run aggregation."""

    parser = argparse.ArgumentParser(
        description="Add completed baseline runs to a primary-keyed CSV summary."
    )
    parser.add_argument("runs_root", type=Path, help="root below which summary.json is searched")
    parser.add_argument(
        "--output",
        type=Path,
        help="output CSV path (default: RUNS_ROOT/runs.csv)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run aggregation and print a concise idempotency summary."""

    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.output or args.runs_root / "runs.csv"
    try:
        added, duplicates, total = update_summary_csv(args.runs_root, output)
    except RunParseError as exc:
        parser.error(str(exc))
    print(f"added={added} duplicates={duplicates} total={total} output={output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
