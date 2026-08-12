"""Summarize current baseline and Full Protocol training artifacts.

The tool accepts exact run directories or roots containing runs, projects both
current artifact layouts into one CSV schema, and optionally writes explicit
Full Protocol versus baseline comparisons. Completed artifacts are parsed
strictly before any output is replaced, so malformed evidence cannot produce a
partially updated result table.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from fs_diloco.protocol.contributor import ContributorFence
from fs_diloco.storage.audit_archive import read_logical_authority_rows
from fs_diloco.storage.paths import RunPaths


CSV_FIELDS = (
    "run_id",
    "run_dir",
    "run_kind",
    "status",
    "mode",
    "git_commit",
    "source_fingerprint",
    "pbs_job_ids",
    "model_name_or_path",
    "model_revision",
    "model_dtype",
    "dataset_name",
    "dataset_config_name",
    "dataset_revision",
    "train_split",
    "block_size",
    "expected_contributors",
    "terminal_contributors",
    "optimizer_steps_min",
    "optimizer_steps_max",
    "global_steps",
    "micro_batch_size",
    "gradient_accumulation_steps",
    "tokens_per_optimizer_step_per_contributor",
    "learning_rate",
    "optimizer_beta1",
    "optimizer_beta2",
    "optimizer_epsilon",
    "weight_decay",
    "warmup_steps",
    "min_lr_ratio",
    "merge_contributors",
    "synchronization_interval",
    "synchronization_count",
    "final_report_count",
    "final_report_coordinate",
    "final_mean_loss",
    "training_time_seconds",
    "synchronization_time_seconds",
    "synchronization_time_fraction",
)
PRIMARY_KEY = "run_id"
FINAL_BASELINE_REPORT_COUNT = 5
COMPARISON_THRESHOLD = 0.20


class RunParseError(ValueError):
    """Report artifacts that violate the sole current summary schema."""


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
    """Return one required non-empty text field from an artifact."""

    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RunParseError(f"{path}: {key} must be a non-empty string")
    return value


def _optional_text(payload: dict[str, Any], key: str, *, path: Path) -> str:
    """Return an optional text field as an empty CSV value when absent."""

    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or not value:
        raise RunParseError(f"{path}: {key} must be null or a non-empty string")
    return value


def _integer(payload: dict[str, Any], key: str, *, path: Path) -> int:
    """Return one required integer while rejecting booleans."""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunParseError(f"{path}: {key} must be an integer")
    return value


def _number(payload: dict[str, Any], key: str, *, path: Path) -> float:
    """Return one required finite number while rejecting booleans."""

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


def _optimizer_betas(optimizer: dict[str, Any], *, path: Path) -> tuple[float, float]:
    """Validate and return the two configured AdamW beta values."""

    betas = optimizer.get("betas")
    if not isinstance(betas, list) or len(betas) != 2:
        raise RunParseError(f"{path}: optimizer betas must contain two values")
    payload = {"beta1": betas[0], "beta2": betas[1]}
    return _number(payload, "beta1", path=path), _number(payload, "beta2", path=path)


def _baseline_final_loss(run_dir: Path, *, world_size: int) -> tuple[str, float]:
    """Average the last five complete optimizer reports across all ranks."""

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

    report_steps = sorted(losses)[-FINAL_BASELINE_REPORT_COUNT:]
    if len(report_steps) != FINAL_BASELINE_REPORT_COUNT:
        raise RunParseError(
            f"{run_dir}: expected at least {FINAL_BASELINE_REPORT_COUNT} optimizer reports"
        )
    expected_ranks = set(range(world_size))
    selected: list[float] = []
    for step in report_steps:
        if set(losses[step]) != expected_ranks:
            raise RunParseError(f"{run_dir}: optimizer report step {step} is incomplete")
        selected.extend(losses[step][rank] for rank in range(world_size))
    coordinate = ";".join(str(step) for step in report_steps)
    return coordinate, math.fsum(selected) / len(selected)


def _baseline_sync_metrics(
    run_dir: Path,
    *,
    expected_count: int,
    training_time_seconds: float,
) -> tuple[float, float]:
    """Return total synchronization time and its fraction of baseline runtime."""

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
    durations = [_csv_number(row, "duration_seconds", path=path) for row in rows]
    if any(duration < 0.0 for duration in durations):
        raise RunParseError(f"{path}: synchronization duration cannot be negative")
    total = math.fsum(durations)
    return total, total / training_time_seconds


def _parse_baseline_run(run_dir: Path) -> dict[str, Any]:
    """Project one successful standalone torch baseline into the unified schema."""

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
        raise RunParseError(f"{run_dir}: baseline is not successfully completed")
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
    if world_size < 1 or _integer(summary, "final_step", path=summary_path) != max_steps:
        raise RunParseError(f"{run_dir}: completed step or world size is inconsistent")
    if _integer(manifest, "world_size", path=manifest_path) != world_size:
        raise RunParseError(f"{run_dir}: summary and manifest world sizes differ")
    created_at = _number(manifest, "created_at", path=manifest_path)
    completed_at = _number(summary, "completed_at", path=summary_path)
    training_time = completed_at - created_at
    if training_time <= 0.0:
        raise RunParseError(f"{run_dir}: completion time must follow creation time")
    coordinate, final_loss = _baseline_final_loss(run_dir, world_size=world_size)
    gradient_syncs = _integer(summary, "gradient_sync_count", path=summary_path)
    parameter_averages = _integer(summary, "parameter_average_count", path=summary_path)
    synchronization_count = gradient_syncs + parameter_averages
    sync_time, sync_fraction = _baseline_sync_metrics(
        run_dir,
        expected_count=synchronization_count,
        training_time_seconds=training_time,
    )
    beta1, beta2 = _optimizer_betas(optimizer, path=config_path)
    source = _mapping(manifest, "source_identity", path=manifest_path)
    micro_batch = _integer(training, "micro_batch_size", path=config_path)
    accumulation = _integer(training, "gradient_accumulation_steps", path=config_path)
    block_size = _integer(data, "block_size", path=config_path)
    mode = _text(summary, "mode", path=summary_path)
    interval = _integer(distributed, "periodic_average_interval", path=config_path)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "run_kind": "torch_ddp_baseline",
        "status": "completed",
        "mode": mode,
        "git_commit": _text(source, "git_commit", path=manifest_path),
        "source_fingerprint": "",
        "pbs_job_ids": _text(manifest, "pbs_job_id", path=manifest_path),
        "model_name_or_path": _text(model, "name_or_path", path=config_path),
        "model_revision": _text(model, "revision", path=config_path),
        "model_dtype": _text(model, "dtype", path=config_path),
        "dataset_name": _text(data, "dataset_name", path=config_path),
        "dataset_config_name": _text(data, "dataset_config_name", path=config_path),
        "dataset_revision": _text(data, "revision", path=config_path),
        "train_split": _text(data, "train_split", path=config_path),
        "block_size": block_size,
        "expected_contributors": world_size,
        "terminal_contributors": world_size,
        "optimizer_steps_min": max_steps,
        "optimizer_steps_max": max_steps,
        "global_steps": "",
        "micro_batch_size": micro_batch,
        "gradient_accumulation_steps": accumulation,
        "tokens_per_optimizer_step_per_contributor": micro_batch * accumulation * block_size,
        "learning_rate": _number(optimizer, "lr", path=config_path),
        "optimizer_beta1": beta1,
        "optimizer_beta2": beta2,
        "optimizer_epsilon": _number(optimizer, "eps", path=config_path),
        "weight_decay": _number(optimizer, "weight_decay", path=config_path),
        "warmup_steps": _integer(optimizer, "warmup_steps", path=config_path),
        "min_lr_ratio": _number(optimizer, "min_lr_ratio", path=config_path),
        "merge_contributors": world_size,
        "synchronization_interval": 1 if mode == "ddp" else interval,
        "synchronization_count": synchronization_count,
        "final_report_count": FINAL_BASELINE_REPORT_COUNT * world_size,
        "final_report_coordinate": coordinate,
        "final_mean_loss": final_loss,
        "training_time_seconds": training_time,
        "synchronization_time_seconds": sync_time,
        "synchronization_time_fraction": sync_fraction,
    }


def _open_authority(path: Path) -> sqlite3.Connection:
    """Open one completed Full Protocol authority read-only and verify integrity."""

    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        result = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise RunParseError(f"cannot read Full Protocol authority: {path}") from exc
    if [tuple(row) for row in result] != [("ok",)]:
        connection.close()
        raise RunParseError(f"Full Protocol authority integrity check failed: {path}")
    return connection


def _terminal_fences(connection: sqlite3.Connection, *, path: Path) -> list[dict[str, Any]]:
    """Decode the final generation's exact contributor fences."""

    try:
        controller = connection.execute(
            "SELECT generation, state FROM controller_state WHERE singleton=1"
        ).fetchone()
        if controller is None or controller["state"] != "finalized":
            raise RunParseError(f"{path}: controller is not finalized")
        rows = connection.execute(
            "SELECT stable_contributor_key, fence_json, state, final_cycle_seq, "
            "hard_crash_gap_tokens_upper_bound "
            "FROM terminal_contributor_fences WHERE generation=? "
            "ORDER BY stable_contributor_key",
            (int(controller["generation"]),),
        ).fetchall()
    except sqlite3.Error as exc:
        raise RunParseError(f"cannot read terminal contributor fences: {path}") from exc
    fences: list[dict[str, Any]] = []
    for row in rows:
        state = str(row["state"])
        if state not in {"acked", "hard_crash"}:
            raise RunParseError(f"{path}: terminal contributor is not adjudicated")
        try:
            fence = json.loads(row["fence_json"])
        except json.JSONDecodeError as exc:
            raise RunParseError(f"{path}: terminal fence JSON is malformed") from exc
        try:
            typed_fence = ContributorFence.from_dict(fence)
        except (TypeError, ValueError) as exc:
            raise RunParseError(f"{path}: terminal fence has an invalid current shape") from exc
        final_cycle_seq = row["final_cycle_seq"]
        if state == "acked" and (
            isinstance(final_cycle_seq, bool)
            or not isinstance(final_cycle_seq, int)
            or final_cycle_seq < 0
        ):
            raise RunParseError(f"{path}: terminal fence has no valid final cycle sequence")
        if state == "hard_crash" and (
            final_cycle_seq is not None or int(row["hard_crash_gap_tokens_upper_bound"]) <= 0
        ):
            raise RunParseError(f"{path}: hard-crash fence has invalid bounded-gap evidence")
        decoded = typed_fence.as_dict()
        decoded["stable_contributor_key"] = str(row["stable_contributor_key"])
        decoded["canonical_json"] = str(row["fence_json"])
        decoded["final_cycle_seq"] = final_cycle_seq
        decoded["terminal_state"] = state
        decoded["hard_crash_gap_tokens_upper_bound"] = int(row["hard_crash_gap_tokens_upper_bound"])
        fences.append(decoded)
    return fences


def _last_proposal_loss(run_dir: Path, fence: dict[str, Any], *, required: bool) -> float | None:
    """Return the last proposal loss, allowing absent telemetry only for a hard crash."""

    instance_id = fence.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise RunParseError(f"{run_dir}: terminal fence has no instance ID")
    metric_paths = sorted((run_dir / "metrics" / "learner" / instance_id).glob("*.jsonl"))
    if len(metric_paths) != 1:
        if not required and not metric_paths:
            return None
        raise RunParseError(
            f"{run_dir}: expected one telemetry attempt for {instance_id}, found {len(metric_paths)}"
        )
    selected: tuple[float, float] | None = None
    path = metric_paths[0]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RunParseError(f"cannot read learner telemetry: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunParseError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(event, dict):
            raise RunParseError(f"JSONL event must be an object at {path}:{line_number}")
        if event.get("event_type") != "proposal_published":
            continue
        loss = _number(event, "mean_loss", path=path)
        timestamp = _number(event, "timestamp", path=path)
        if selected is None or timestamp > selected[0]:
            selected = (timestamp, loss)
    if selected is None:
        if not required:
            return None
        raise RunParseError(f"{run_dir}: terminal instance {instance_id} has no proposal loss")
    return selected[1]


def _parse_full_protocol_run(run_dir: Path) -> dict[str, Any]:
    """Project one completed current Full Protocol run into the unified schema."""

    summary_path = run_dir / "control" / "summary.json"
    descriptor_path = run_dir / "control" / "run_descriptor.json"
    config_path = run_dir / "control" / "run_config.resolved.yaml"
    source_path = run_dir / "control" / "run_source_manifest.json"
    authority_path = run_dir / "control" / "syncer_metadata.sqlite3"
    summary = _load_json_object(summary_path)
    descriptor = _load_json_object(descriptor_path)
    config = _load_yaml_object(config_path)
    source = _load_json_object(source_path)
    if (
        summary.get("authority") != "full_protocol"
        or summary.get("all_learners_stopped") is not True
    ):
        raise RunParseError(f"{run_dir}: Full Protocol run is not successfully completed")
    run_id = _text(summary, "run_id", path=summary_path)
    if run_id != run_dir.name or _text(descriptor, "run_id", path=descriptor_path) != run_id:
        raise RunParseError(f"{run_dir}: run directory, summary, and descriptor IDs differ")
    model = _mapping(config, "model", path=config_path)
    data = _mapping(config, "data", path=config_path)
    sync = _mapping(config, "sync", path=config_path)
    membership = _mapping(config, "membership", path=config_path)
    training = _mapping(config, "training", path=config_path)
    optimizer = _mapping(config, "inner_optimizer", path=config_path)
    final_version = _integer(summary, "final_version", path=summary_path)
    configured_version = _integer(sync, "stop_after_outer_steps", path=config_path)
    if final_version != configured_version:
        raise RunParseError(f"{run_dir}: final and configured global versions differ")
    created_at = _number(descriptor, "created_at", path=descriptor_path)
    finalized_at = _number(summary, "finalized_at", path=summary_path)
    training_time = finalized_at - created_at
    if training_time <= 0.0:
        raise RunParseError(f"{run_dir}: finalization time must follow descriptor creation")

    connection = _open_authority(authority_path)
    try:
        fences = _terminal_fences(connection, path=authority_path)
        expected = _integer(membership, "stream_pool_size", path=config_path)
        if len(fences) != expected:
            raise RunParseError(
                f"{run_dir}: expected {expected} terminal contributors, found {len(fences)}"
            )
        logical_updates = read_logical_authority_rows(
            connection,
            RunPaths(run_dir),
            table="updates",
            primary_key="update_id",
        )
        inner_steps = _integer(training, "inner_steps", path=config_path)
        progress_rows = connection.execute(
            "SELECT stable_contributor_key, last_cycle_seq FROM contributor_progress"
        ).fetchall()
        progress = {
            str(row["stable_contributor_key"]): int(row["last_cycle_seq"]) for row in progress_rows
        }
        steps = []
        for fence in fences:
            if fence["terminal_state"] == "acked":
                cycle_seq = int(fence["final_cycle_seq"])
            else:
                key = str(fence["stable_contributor_key"])
                if key not in progress:
                    if any(
                        str(row["fence_json"]) == str(fence["canonical_json"])
                        for row in logical_updates
                    ):
                        raise RunParseError(
                            f"{authority_path}: hard-crash updates have no progress row"
                        )
                    cycle_seq = 0
                else:
                    cycle_seq = progress[key]
            steps.append(cycle_seq * inner_steps)
        job_rows = connection.execute(
            "SELECT pbs_job_id FROM learner_instances WHERE pbs_job_id IS NOT NULL "
            "UNION SELECT pbs_job_id FROM syncer_epochs WHERE pbs_job_id IS NOT NULL"
        ).fetchall()
        pbs_job_ids = sorted({str(row["pbs_job_id"]) for row in job_rows})
        counts_by_version: dict[int, int] = {}
        for row in logical_updates:
            if row["status"] == "applied":
                version = int(row["applied_version"])
                counts_by_version[version] = counts_by_version.get(version, 0) + 1
    except RunParseError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise RunParseError(f"cannot summarize Full Protocol authority: {authority_path}") from exc
    finally:
        connection.close()
    merge_contributors = _integer(sync, "quorum_min", path=config_path)
    if _integer(sync, "quorum_max", path=config_path) != merge_contributors:
        raise RunParseError(f"{run_dir}: Full Protocol comparison requires one merge threshold")
    expected_counts = [(version, merge_contributors) for version in range(1, final_version + 1)]
    actual_counts = sorted(counts_by_version.items())
    if actual_counts != expected_counts:
        raise RunParseError(f"{run_dir}: applied updates do not match every exact merge threshold")

    loss_reports = [
        (
            fence,
            _last_proposal_loss(
                run_dir,
                fence,
                required=(fence["terminal_state"] == "acked" and fence["final_cycle_seq"] > 0),
            ),
        )
        for fence in fences
    ]
    available_losses = [loss for _fence, loss in loss_reports if loss is not None]
    if not available_losses:
        raise RunParseError(f"{run_dir}: Full Protocol run has no proposal loss reports")
    beta1, beta2 = _optimizer_betas(optimizer, path=config_path)
    micro_batch = _integer(training, "micro_batch_size", path=config_path)
    accumulation = _integer(training, "gradient_accumulation_steps", path=config_path)
    block_size = _integer(data, "block_size", path=config_path)
    coordinate = ";".join(
        str(fence["stable_contributor_key"]) for fence, loss in loss_reports if loss is not None
    )
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "run_kind": "fs_diloco_full_protocol",
        "status": "completed",
        "mode": "full_protocol",
        "git_commit": _text(source, "git_commit", path=source_path),
        "source_fingerprint": _text(source, "source_fingerprint", path=source_path),
        "pbs_job_ids": ";".join(pbs_job_ids),
        "model_name_or_path": _text(model, "name_or_path", path=config_path),
        "model_revision": _optional_text(model, "revision", path=config_path),
        "model_dtype": _text(model, "dtype", path=config_path),
        "dataset_name": _text(data, "dataset_name", path=config_path),
        "dataset_config_name": _optional_text(data, "dataset_config_name", path=config_path),
        "dataset_revision": _optional_text(data, "revision", path=config_path),
        "train_split": _text(data, "train_split", path=config_path),
        "block_size": block_size,
        "expected_contributors": expected,
        "terminal_contributors": len(fences),
        "optimizer_steps_min": min(steps),
        "optimizer_steps_max": max(steps),
        "global_steps": final_version,
        "micro_batch_size": micro_batch,
        "gradient_accumulation_steps": accumulation,
        "tokens_per_optimizer_step_per_contributor": micro_batch * accumulation * block_size,
        "learning_rate": _number(optimizer, "lr", path=config_path),
        "optimizer_beta1": beta1,
        "optimizer_beta2": beta2,
        "optimizer_epsilon": _number(optimizer, "eps", path=config_path),
        "weight_decay": _number(optimizer, "weight_decay", path=config_path),
        "warmup_steps": _integer(optimizer, "warmup_steps", path=config_path),
        "min_lr_ratio": _number(optimizer, "min_lr_ratio", path=config_path),
        "merge_contributors": merge_contributors,
        "synchronization_interval": inner_steps,
        "synchronization_count": final_version,
        "final_report_count": len(available_losses),
        "final_report_coordinate": coordinate,
        "final_mean_loss": math.fsum(available_losses) / len(available_losses),
        "training_time_seconds": training_time,
        "synchronization_time_seconds": "",
        "synchronization_time_fraction": "",
    }


def parse_completed_run(run_dir: str | Path) -> dict[str, Any]:
    """Detect and parse one exact current-layout completed run directory."""

    resolved = Path(run_dir).resolve()
    baseline = (resolved / "summary.json").is_file()
    full_protocol = (resolved / "control" / "summary.json").is_file()
    if baseline == full_protocol:
        raise RunParseError(f"{resolved}: expected exactly one current run artifact layout")
    return _parse_baseline_run(resolved) if baseline else _parse_full_protocol_run(resolved)


def _completed_layout(path: Path) -> bool:
    """Return whether one exact directory contains a completed current layout."""

    baseline = path / "summary.json"
    if baseline.is_file():
        return _load_json_object(baseline).get("status") == "completed"
    full = path / "control" / "summary.json"
    if full.is_file():
        payload = _load_json_object(full)
        return payload.get("all_learners_stopped") is True
    return False


def find_completed_runs(inputs: Iterable[str | Path]) -> tuple[Path, ...]:
    """Resolve exact runs and recursively discover completed current layouts."""

    discovered: set[Path] = set()
    for value in inputs:
        path = Path(value).resolve()
        if not path.is_dir():
            raise RunParseError(f"run input is not a directory: {path}")
        if _completed_layout(path):
            discovered.add(path)
            continue
        for summary_path in sorted(path.rglob("summary.json")):
            candidate = summary_path.parent
            if candidate.name == "control":
                candidate = candidate.parent
            if _completed_layout(candidate):
                discovered.add(candidate)
    return tuple(sorted(discovered))


def _read_existing_rows(output_path: Path) -> list[dict[str, str]]:
    """Read and validate an existing unified CSV before adding records."""

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


def _atomic_text(path: Path, content: str) -> None:
    """Replace one derived summary artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_rows_atomic(output_path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Serialize all unified rows before atomically replacing the CSV."""

    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_text(output_path, buffer.getvalue())


def update_summary_csv(
    run_inputs: Iterable[str | Path],
    output_path: str | Path,
) -> tuple[int, int, int]:
    """Add unseen completed runs and return added, duplicate, and total counts."""

    output = Path(output_path).resolve()
    existing_rows = _read_existing_rows(output)
    known_keys = {row[PRIMARY_KEY] for row in existing_rows}
    discovered_records = [parse_completed_run(path) for path in find_completed_runs(run_inputs)]
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


def _float_cell(row: dict[str, str], key: str) -> float:
    """Decode one required finite numeric summary cell for comparison."""

    try:
        value = float(row[key])
    except (KeyError, ValueError) as exc:
        raise RunParseError(f"summary row {row.get('run_id')!r} has no numeric {key}") from exc
    if not math.isfinite(value):
        raise RunParseError(f"summary row {row.get('run_id')!r} has non-finite {key}")
    return value


def build_comparisons(rows: Sequence[dict[str, str]]) -> dict[str, Any]:
    """Compare every Full Protocol row with both baseline modes at a 20% threshold."""

    baselines = [row for row in rows if row.get("run_kind") == "torch_ddp_baseline"]
    full_protocol_runs = [row for row in rows if row.get("run_kind") == "fs_diloco_full_protocol"]
    modes = {row.get("mode") for row in baselines}
    if modes != {"ddp", "periodic_average"}:
        raise RunParseError("comparison requires exactly the ddp and periodic_average modes")
    if not full_protocol_runs:
        raise RunParseError("comparison requires at least one Full Protocol run")
    comparisons: list[dict[str, Any]] = []
    for full_protocol in full_protocol_runs:
        for baseline in baselines:
            identity_fields = (
                "model_name_or_path",
                "model_revision",
                "dataset_name",
                "dataset_config_name",
                "dataset_revision",
                "train_split",
                "block_size",
                "micro_batch_size",
                "gradient_accumulation_steps",
            )
            mismatches = [
                key for key in identity_fields if full_protocol.get(key) != baseline.get(key)
            ]
            metrics: dict[str, Any] = {}
            exceeded = False
            for key in ("final_mean_loss", "training_time_seconds"):
                baseline_value = _float_cell(baseline, key)
                full_protocol_value = _float_cell(full_protocol, key)
                if baseline_value == 0.0:
                    raise RunParseError(f"baseline {baseline['run_id']} has zero {key}")
                relative = (full_protocol_value - baseline_value) / abs(baseline_value)
                metric_exceeded = abs(relative) > COMPARISON_THRESHOLD
                exceeded = exceeded or metric_exceeded
                metrics[key] = {
                    "baseline": baseline_value,
                    "full_protocol": full_protocol_value,
                    "relative_difference": relative,
                    "absolute_difference_exceeds_threshold": metric_exceeded,
                }
            comparisons.append(
                {
                    "full_protocol_run_id": full_protocol["run_id"],
                    "baseline_run_id": baseline["run_id"],
                    "baseline_mode": baseline["mode"],
                    "identity_mismatches": mismatches,
                    "comparable_identity": not mismatches,
                    "optimizer_step_ranges": {
                        "baseline": [
                            int(baseline["optimizer_steps_min"]),
                            int(baseline["optimizer_steps_max"]),
                        ],
                        "full_protocol": [
                            int(full_protocol["optimizer_steps_min"]),
                            int(full_protocol["optimizer_steps_max"]),
                        ],
                    },
                    "metrics": metrics,
                    "investigation_required": bool(mismatches) or exceeded,
                }
            )
    return {
        "format_version": 1,
        "threshold": COMPARISON_THRESHOLD,
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
    }


def write_comparisons(summary_csv: str | Path, output_path: str | Path) -> None:
    """Read one unified CSV and atomically publish its baseline comparisons."""

    rows = _read_existing_rows(Path(summary_csv).resolve())
    payload = build_comparisons(rows)
    _atomic_text(
        Path(output_path).resolve(),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for aggregation and comparison."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_inputs",
        nargs="+",
        type=Path,
        help="exact run directories or roots searched recursively",
    )
    parser.add_argument("--output", type=Path, required=True, help="unified output CSV")
    parser.add_argument(
        "--comparison-output",
        type=Path,
        help="optional Full Protocol versus baseline comparison JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Aggregate current runs, optionally compare them, and print row counts."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        added, duplicates, total = update_summary_csv(args.run_inputs, args.output)
        if args.comparison_output is not None:
            write_comparisons(args.output, args.comparison_output)
    except RunParseError as exc:
        parser.error(str(exc))
    print(f"added={added} duplicates={duplicates} total={total} output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
