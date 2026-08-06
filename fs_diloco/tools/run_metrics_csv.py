"""Discover completed runs below one or more roots and add their metrics to CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ..storage.paths import RunPaths


CSV_COLUMNS = [
    "run_id",
    "run_path",
    "mode",
    "final_version",
    "stop_reason",
    "all_learners_stopped",
    "num_learners",
    "produced_updates",
    "applied_updates",
    "update_utilization_ratio",
    "update_utilization_percent",
    "dropped_updates",
    "pending_or_unclassified_updates",
    "drop_reasons_json",
    "dropped_superseded",
    "dropped_stale",
    "dropped_stop_finalized",
    "dropped_missing_file",
    "dropped_future_base",
    "dropped_unknown",
    "local_steps_total",
    "local_steps_min",
    "local_steps_max",
    "local_steps_mean",
    "local_steps_by_learner_json",
    "complete_training_time_seconds",
    "source_fingerprint",
    "training_seed",
    "sync_scan_interval_seconds",
    "ingest_during_publish",
    "merge_count",
    "selected_per_merge_min",
    "selected_per_merge_max",
    "selected_per_merge_mean",
    "selected_count_distribution_json",
    "global_interval_seconds_mean",
    "global_interval_seconds_p50",
    "global_interval_seconds_p95",
    "quorum_detection_seconds_mean",
    "quorum_detection_seconds_p95",
    "quorum_max_trigger_count",
    "quorum_max_trigger_ratio",
    "quorum_trigger_distribution_json",
    "publish_ingest_passes_total",
    "publish_ingested_updates_total",
    "interval_residual_ratio_mean",
    "syncer_merge_compute_seconds_p95",
    "syncer_duty_cycle_percent",
    "estimated_idle_gpu_node_hours",
    "applied_staleness_0",
    "applied_staleness_1",
    "applied_staleness_2",
    "applied_staleness_gt_2",
    "applied_staleness_mean",
    "applied_staleness_distribution_json",
    "produced_tokens",
    "applied_tokens",
    "loss_count",
    "loss_first_10_mean",
    "loss_last_10_mean",
    "loss_mean",
    "loss_last_vs_first_ratio",
    "model_name_or_path",
    "update_tensor_dtype",
    "syncer_device",
    "syncer_compute_dtype",
    "syncer_publish_dtype",
    "max_staleness_versions",
    "inner_steps",
    "max_local_steps",
    "completion_mode",
    "global_adoption_strategy",
    "grace_window_mode",
    "grace_window_seconds",
    "db_integrity_ok",
]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _mean(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: list[float], quantile: float) -> float | None:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return None
    position = (len(finite) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return finite[lower]
    return finite[lower] + (finite[upper] - finite[lower]) * (position - lower)


def _json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _read_db(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any], bool | None]:
    path = root / "control" / "syncer_metadata.sqlite3"
    if not path.is_file():
        return [], {}, None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    config: dict[str, Any] = {}
    integrity_ok: bool | None = None
    try:
        integrity_ok = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
        for table in ("updates", "fragment_updates"):
            if _table_exists(conn, table):
                rows.extend(dict(row) for row in conn.execute(f"SELECT * FROM {table}"))
        if _table_exists(conn, "run_state"):
            row = conn.execute("SELECT value FROM run_state WHERE key='config'").fetchone()
            if row is not None:
                try:
                    payload = json.loads(row["value"])
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict):
                    config = payload
    finally:
        conn.close()
    return rows, config, integrity_ok


def _update_records(root: Path, live_rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(root / "metrics" / "update_history.jsonl"):
        update_id = row.get("update_id")
        if update_id:
            records[str(update_id)] = row
    for row in live_rows:
        update_id = row.get("update_id")
        if update_id:
            records[str(update_id)] = row
    return records


def _manifest_records(root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for row in _read_csv(root / "metrics" / "update_manifest.csv"):
        update_id = row.get("update_id")
        if update_id:
            records[str(update_id)] = row
    return records


def _committed_merge_versions(syncer_rows: list[dict[str, str]]) -> tuple[set[int], set[int]]:
    full: set[int] = set()
    fragment: set[int] = set()
    for row in syncer_rows:
        version = _as_int(row.get("version"))
        event = _as_int(row.get("global_merge_event"))
        if event is not None:
            fragment.add(event)
        elif version is not None:
            full.add(version)
    return full, fragment


def _selection_fallback(
    root: Path,
    syncer_rows: list[dict[str, str]],
) -> dict[str, dict[str, int | str | None]]:
    full_versions, fragment_events = _committed_merge_versions(syncer_rows)
    selected: dict[str, dict[str, int | str | None]] = {}
    for log_path in RunPaths(root).iter_syncer_logs():
        for event in _read_jsonl(log_path):
            event_type = event.get("event_type")
            update_ids = event.get("update_ids")
            if not isinstance(update_ids, list):
                continue
            if event_type == "updates_selected":
                current = _as_int(event.get("version"))
                target = None if current is None else current + 1
                if full_versions and target not in full_versions:
                    continue
                for update_id in update_ids:
                    selected[str(update_id)] = {
                        "kind": "full",
                        "applied_version": target,
                        "current_fragment_version": None,
                    }
            elif event_type == "fragment_updates_selected":
                current_event = _as_int(event.get("global_merge_event"))
                target_event = None if current_event is None else current_event + 1
                if fragment_events and target_event not in fragment_events:
                    continue
                for update_id in update_ids:
                    selected[str(update_id)] = {
                        "kind": "fragment",
                        "applied_version": target_event,
                        "current_fragment_version": _as_int(event.get("fragment_version")),
                    }
    return selected


def _loss_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    losses = [value for row in rows if (value := _as_float(row.get("train_loss"))) is not None]
    if not losses:
        return {
            "loss_count": 0,
            "loss_first_10_mean": None,
            "loss_last_10_mean": None,
            "loss_mean": None,
            "loss_last_vs_first_ratio": None,
        }
    first = _mean(losses[:10])
    last = _mean(losses[-10:])
    return {
        "loss_count": len(losses),
        "loss_first_10_mean": first,
        "loss_last_10_mean": last,
        "loss_mean": _mean(losses),
        "loss_last_vs_first_ratio": last / first if first and last is not None else None,
    }


def _local_steps(root: Path, learner_rows: list[dict[str, str]]) -> dict[str, int]:
    steps: dict[str, int] = {}
    for path in RunPaths(root).iter_learner_heartbeats():
        heartbeat = _read_json(path)
        learner_id = str(heartbeat.get("learner_id") or path.stem)
        local_step = _as_int(heartbeat.get("last_local_step"))
        if local_step is not None:
            steps[learner_id] = max(steps.get(learner_id, 0), local_step)
    for row in learner_rows:
        learner_id = row.get("learner_id")
        local_step = _as_int(row.get("local_step"))
        if learner_id and local_step is not None:
            steps[learner_id] = max(steps.get(learner_id, 0), local_step)
    return dict(sorted(steps.items()))


def _staleness_for_update(
    update_id: str,
    row: dict[str, Any],
    manifest: dict[str, Any],
    fallback: dict[str, int | str | None] | None,
) -> int | None:
    stored = _as_int(row.get("staleness_versions"))
    if stored is None:
        stored = _as_int(row.get("staleness_fragment_versions"))
    if stored is not None:
        return max(0, stored)

    is_fragment = (
        row.get("update_kind") == "fragment"
        or manifest.get("update_kind") == "fragment"
        or (fallback or {}).get("kind") == "fragment"
    )
    if is_fragment:
        base = _as_int(row.get("base_fragment_version"))
        if base is None:
            base = _as_int(manifest.get("base_fragment_version"))
        current = _as_int((fallback or {}).get("current_fragment_version"))
        if base is not None and current is not None:
            return max(0, current - base)
        applied = _as_int(row.get("applied_fragment_version"))
        if applied is not None and base is not None:
            return max(0, applied - 1 - base)
        return None

    base = _as_int(row.get("base_global_version"))
    if base is None:
        base = _as_int(manifest.get("base_global_version"))
    applied = _as_int(row.get("applied_version"))
    if applied is None:
        applied = _as_int((fallback or {}).get("applied_version"))
    if base is None or applied is None:
        return None
    return max(0, applied - 1 - base)


def _nested(config: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    value = config.get(section)
    return value.get(key, default) if isinstance(value, dict) else default


def extract_run_metrics(run_path: str | Path) -> dict[str, Any]:
    """Return one flat CSV-ready metrics row for a run root."""
    root = Path(run_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"run root does not exist: {root}")
    if not (root / "control").is_dir() and not (root / "metrics").is_dir():
        raise ValueError(f"not an fs-diloco run root: {root}")

    latest = _read_json(root / "control" / "latest.json")
    stop = _read_json(root / "control" / "stop.json")
    summary = _read_json(root / "control" / "summary.json")
    learner_rows = _read_csv(root / "metrics" / "learner_metrics.csv")
    syncer_rows = _read_csv(root / "metrics" / "syncer_metrics.csv")
    manifest = _manifest_records(root)
    live_rows, config, integrity_ok = _read_db(root)
    updates = _update_records(root, live_rows)
    selections = _selection_fallback(root, syncer_rows)

    produced_ids = set(manifest) or set(updates)
    status_by_id = {update_id: str(row.get("status") or "") for update_id, row in updates.items()}
    applied_ids = {update_id for update_id, status in status_by_id.items() if status == "applied"}
    for update_id in selections:
        if update_id not in status_by_id:
            applied_ids.add(update_id)
    applied_ids &= produced_ids

    explicit_dropped_ids = {
        update_id for update_id, status in status_by_id.items() if status == "dropped"
    } & produced_ids
    unclassified_ids = produced_ids - applied_ids - explicit_dropped_ids
    run_complete = bool(summary)
    dropped_ids = set(explicit_dropped_ids)
    pending_ids: set[str] = set()
    if run_complete:
        dropped_ids.update(unclassified_ids)
    else:
        pending_ids.update(unclassified_ids)

    drop_reasons = Counter()
    for update_id in dropped_ids:
        reason = updates.get(update_id, {}).get("drop_reason")
        drop_reasons[str(reason or "unknown")] += 1

    staleness_values: list[int] = []
    for update_id in sorted(applied_ids):
        value = _staleness_for_update(
            update_id,
            updates.get(update_id, {}),
            manifest.get(update_id, {}),
            selections.get(update_id),
        )
        if value is not None:
            staleness_values.append(value)
    staleness_counts = Counter(staleness_values)

    def tokens_for(update_id: str) -> int:
        row = updates.get(update_id, {})
        value = _as_int(row.get("tokens_this_update"))
        if value is None:
            value = _as_int(manifest.get(update_id, {}).get("tokens_this_update"), 0)
        return int(value or 0)

    produced_tokens = sum(tokens_for(update_id) for update_id in produced_ids)
    applied_tokens = sum(tokens_for(update_id) for update_id in applied_ids)

    local_steps = _local_steps(root, learner_rows)
    local_values = list(local_steps.values())
    selected_counts = [
        value for row in syncer_rows if (value := _as_int(row.get("selected_count"))) is not None
    ]
    selected_distribution = Counter(selected_counts)
    interval_values: list[float] = []
    quorum_detection_values: list[float] = []
    merge_compute_values: list[float] = []
    residual_ratios: list[float] = []
    quorum_triggers: Counter[str] = Counter()
    publish_ingest_passes = 0
    publish_ingested_updates = 0
    syncer_active_total = 0.0
    for metric in syncer_rows:
        interval = _as_float(metric.get("global_interval_seconds"))
        if interval is not None and interval >= 0.0:
            interval_values.append(interval)
        discovery = _as_float(metric.get("discovery_seconds"), 0.0)
        idle = _as_float(metric.get("idle_seconds"), 0.0)
        if discovery is not None and idle is not None:
            quorum_detection_values.append(discovery + idle)
        read = _as_float(metric.get("read_seconds"), 0.0)
        aggregation = _as_float(metric.get("aggregation_seconds"), 0.0)
        outer = _as_float(metric.get("outer_step_seconds"), 0.0)
        publish = _as_float(metric.get("publish_seconds"), 0.0)
        if None not in (read, aggregation, outer, publish):
            merge_compute = float(read) + float(aggregation) + float(outer)
            merge_compute_values.append(merge_compute)
            syncer_active_total += merge_compute + float(publish)
        residual = _as_float(metric.get("interval_residual_seconds"))
        if residual is not None and interval is not None and interval > 0.0:
            residual_ratios.append(residual / interval)
        trigger = str(metric.get("quorum_trigger") or "unknown")
        quorum_triggers[trigger] += 1
        publish_ingest_passes += int(_as_int(metric.get("publish_ingest_passes"), 0) or 0)
        publish_ingested_updates += int(
            _as_int(metric.get("publish_ingested_updates"), 0) or 0
        )

    produced = len(produced_ids)
    applied = len(applied_ids)
    utilization = applied / produced if produced else None
    mode = str(
        latest.get("latest_kind")
        or ("fragment" if _nested(config, "fragments", "enabled") else "full")
    )
    final_version = summary.get("final_version")
    if final_version is None:
        final_version = latest.get("global_merge_event", latest.get("version"))
    run_id = summary.get("run_id") or latest.get("run_id") or stop.get("run_id") or root.name
    source_identity = _read_json(root / "control" / "source_identity.json")

    grace_mode = _nested(config, "sync", "grace_window", {})
    if not isinstance(grace_mode, dict):
        grace_mode = {}
    grace_name = grace_mode.get("mode")
    grace_seconds = (
        grace_mode.get("fixed_seconds")
        if grace_name == "fixed"
        else grace_mode.get("initial_seconds")
    )
    syncer_config = config.get("syncer") if isinstance(config.get("syncer"), dict) else {}
    complete_seconds = _as_float(summary.get("complete_training_time_seconds"))
    syncer_duty_cycle = (
        min(1.0, syncer_active_total / complete_seconds)
        if complete_seconds is not None and complete_seconds > 0.0
        else None
    )

    stale_drop_names = {"stale", "too_stale", "obsolete"}
    future_drop_names = {"future_base_version", "future_fragment_version"}
    stop_drop_count = sum(
        count
        for reason, count in drop_reasons.items()
        if reason.startswith("stop_") or reason in {"input_exhausted", "completed"}
    )

    row: dict[str, Any] = {
        "run_id": run_id,
        "run_path": str(root),
        "mode": mode,
        "final_version": final_version,
        "stop_reason": summary.get("stop_reason") or stop.get("reason"),
        "all_learners_stopped": summary.get("all_learners_stopped"),
        "num_learners": _nested(config, "sync", "num_learners", len(local_steps)),
        "produced_updates": produced,
        "applied_updates": applied,
        "update_utilization_ratio": utilization,
        "update_utilization_percent": utilization * 100.0 if utilization is not None else None,
        "dropped_updates": len(dropped_ids),
        "pending_or_unclassified_updates": len(pending_ids),
        "drop_reasons_json": _json_cell(dict(sorted(drop_reasons.items()))),
        "dropped_superseded": drop_reasons.get("superseded", 0),
        "dropped_stale": sum(drop_reasons.get(name, 0) for name in stale_drop_names),
        "dropped_stop_finalized": stop_drop_count,
        "dropped_missing_file": drop_reasons.get("missing_file", 0),
        "dropped_future_base": sum(drop_reasons.get(name, 0) for name in future_drop_names),
        "dropped_unknown": drop_reasons.get("unknown", 0),
        "local_steps_total": sum(local_values),
        "local_steps_min": min(local_values) if local_values else None,
        "local_steps_max": max(local_values) if local_values else None,
        "local_steps_mean": _mean(local_values),
        "local_steps_by_learner_json": _json_cell(local_steps),
        "complete_training_time_seconds": summary.get("complete_training_time_seconds"),
        "source_fingerprint": source_identity.get("source_fingerprint")
        or _nested(config, "run", "source_fingerprint"),
        "training_seed": _nested(config, "training", "seed"),
        "sync_scan_interval_seconds": _nested(config, "sync", "scan_interval_seconds"),
        "ingest_during_publish": _nested(config, "sync", "ingest_during_publish", False),
        "merge_count": len(syncer_rows),
        "selected_per_merge_min": min(selected_counts) if selected_counts else None,
        "selected_per_merge_max": max(selected_counts) if selected_counts else None,
        "selected_per_merge_mean": _mean(selected_counts),
        "selected_count_distribution_json": _json_cell(
            {str(key): value for key, value in sorted(selected_distribution.items())}
        ),
        "global_interval_seconds_mean": _mean(interval_values),
        "global_interval_seconds_p50": _percentile(interval_values, 0.50),
        "global_interval_seconds_p95": _percentile(interval_values, 0.95),
        "quorum_detection_seconds_mean": _mean(quorum_detection_values),
        "quorum_detection_seconds_p95": _percentile(quorum_detection_values, 0.95),
        "quorum_max_trigger_count": quorum_triggers.get("quorum_max", 0),
        "quorum_max_trigger_ratio": (
            quorum_triggers.get("quorum_max", 0) / len(syncer_rows)
            if syncer_rows
            else None
        ),
        "quorum_trigger_distribution_json": _json_cell(dict(sorted(quorum_triggers.items()))),
        "publish_ingest_passes_total": publish_ingest_passes,
        "publish_ingested_updates_total": publish_ingested_updates,
        "interval_residual_ratio_mean": _mean(residual_ratios),
        "syncer_merge_compute_seconds_p95": _percentile(merge_compute_values, 0.95),
        "syncer_duty_cycle_percent": (
            syncer_duty_cycle * 100.0 if syncer_duty_cycle is not None else None
        ),
        "estimated_idle_gpu_node_hours": (
            complete_seconds / 3600.0 * (1.0 - syncer_duty_cycle)
            if complete_seconds is not None and syncer_duty_cycle is not None
            else None
        ),
        "applied_staleness_0": staleness_counts.get(0, 0),
        "applied_staleness_1": staleness_counts.get(1, 0),
        "applied_staleness_2": staleness_counts.get(2, 0),
        "applied_staleness_gt_2": sum(
            count for stale, count in staleness_counts.items() if stale > 2
        ),
        "applied_staleness_mean": _mean(staleness_values),
        "applied_staleness_distribution_json": _json_cell(
            {str(key): value for key, value in sorted(staleness_counts.items())}
        ),
        "produced_tokens": produced_tokens,
        "applied_tokens": applied_tokens,
        "model_name_or_path": _nested(config, "model", "name_or_path"),
        "update_tensor_dtype": _nested(config, "io", "tensor_dtype"),
        "syncer_device": syncer_config.get("device", "auto"),
        "syncer_compute_dtype": syncer_config.get("compute_dtype", "float32"),
        "syncer_publish_dtype": syncer_config.get("publish_dtype", "float32"),
        "max_staleness_versions": _nested(config, "sync", "max_staleness_versions"),
        "inner_steps": _nested(config, "training", "inner_steps"),
        "max_local_steps": _nested(config, "training", "max_local_steps"),
        "completion_mode": _nested(config, "training", "completion_mode", "local_or_global"),
        "global_adoption_strategy": _nested(
            config, "learner", "global_adoption_strategy", "replace"
        ),
        "grace_window_mode": grace_name,
        "grace_window_seconds": grace_seconds,
        "db_integrity_ok": integrity_ok,
        **_loss_metrics(learner_rows),
    }
    return {column: row.get(column) for column in CSV_COLUMNS}


def find_finished_run_roots(root_paths: Iterable[str | Path]) -> list[Path]:
    """Find run roots recursively, using ``control/stop.json`` as completion marker."""
    discovered: set[Path] = set()
    for root_path in root_paths:
        root = Path(root_path).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"root path does not exist: {root}")

        for current, directory_names, _file_names in os.walk(root, followlinks=False):
            directory_names.sort()
            current_path = Path(current)
            control = current_path / "control"
            if not control.is_dir():
                continue

            # A directory containing control/ is a run boundary. Do not scan its
            # potentially large checkpoint/update trees or discover nested paths
            # as separate runs.
            directory_names.clear()
            if (control / "stop.json").is_file():
                discovered.add(current_path.resolve())

    return sorted(discovered, key=lambda path: str(path))


def _row_identity_tokens(row: dict[str, Any]) -> set[tuple[str, str]]:
    tokens: set[tuple[str, str]] = set()
    run_id = str(row.get("run_id") or "").strip()
    if run_id:
        tokens.add(("run_id", run_id))
    run_path = str(row.get("run_path") or "").strip()
    if run_path:
        normalized_path = str(Path(run_path).expanduser().resolve())
        tokens.add(("run_path", normalized_path))
    if not tokens:
        raise ValueError("CSV row must contain run_id or run_path")
    return tokens


def _new_unique_records(
    records: Iterable[dict[str, Any]],
    existing_records: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    for record in existing_records:
        seen.update(_row_identity_tokens(record))

    unique: list[dict[str, Any]] = []
    for record in records:
        tokens = _row_identity_tokens(record)
        if tokens & seen:
            continue
        unique.append(record)
        seen.update(tokens)
    return unique


def write_metrics_csv(
    rows: Iterable[dict[str, Any]],
    output: str | Path,
    *,
    append: bool = True,
) -> int:
    """Write only previously unseen runs, keyed by run ID or normalized run path."""
    records = list(rows)
    if not records:
        return 0
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = append and path.is_file() and path.stat().st_size > 0
    if existing:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            existing_records = list(reader)
        if header != CSV_COLUMNS:
            raise ValueError(
                f"existing CSV schema differs at {path}; use --overwrite or a new output path"
            )
        records = _new_unique_records(records, existing_records)
        if not records:
            return 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
            writer.writerows(records)
            handle.flush()
            os.fsync(handle.fileno())
        return len(records)

    records = _new_unique_records(records)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(records)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return len(records)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root_paths",
        nargs="+",
        help=(
            "One or more roots to scan recursively for completed fs-diloco runs "
            "(marked by control/stop.json)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="reports/run_metrics.csv",
        help="Output CSV (default: reports/run_metrics.csv)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the output atomically instead of appending rows",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    run_roots = find_finished_run_roots(args.root_paths)
    rows = [extract_run_metrics(path) for path in run_roots]
    count = write_metrics_csv(rows, args.output, append=not args.overwrite)
    skipped = len(rows) - count
    print(
        f"found {len(run_roots)} finished run(s); wrote {count} new row(s); "
        f"skipped {skipped} existing/duplicate run(s); "
        f"output={Path(args.output).expanduser().resolve()}"
    )


if __name__ == "__main__":
    main()
