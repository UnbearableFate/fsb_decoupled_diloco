"""Inspect and assert filesystem DiLoCo runs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..legacy.fragment_v0 import (
    expected_fragment_versions_after_events,
    fragment_size_summary,
    load_fragment_index,
)
from ..legacy.reader import open_query_only_database as open_readonly
from ..storage.atomic_io import safe_read_json
from ..storage.paths import RunPaths


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        first_field = reader.fieldnames[0] if reader.fieldnames else None
        return [row for row in reader if first_field is None or row.get(first_field) != first_field]


def _read_csv_summary(path: Path) -> dict[str, Any]:
    rows = _read_csv_rows(path)
    return {"exists": path.exists(), "rows": len(rows), "last": rows[-1] if rows else None}


def _read_jsonl_deduplicated(path: Path, key: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = row.get(key)
            if value is not None:
                if key == "version":
                    value = (
                        row.get("version_kind", "full"),
                        row.get("fragment_id"),
                        value,
                    )
                rows[str(value)] = row
    return list(rows.values())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fragment_update_integrity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    corrupt: list[str] = []
    missing: list[str] = []
    for row in rows:
        path = Path(row["file_path"])
        if not path.exists():
            missing.append(row["update_id"])
            continue
        expected = row.get("sha256")
        if expected and _sha256_file(path) != expected:
            corrupt.append(row["update_id"])
    return {"missing": missing, "corrupt": corrupt, "ok": not missing and not corrupt}


def _db_summary(path: Path | None, root: Path) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"exists": False}
    conn = open_readonly(path)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        archived_updates = _read_jsonl_deduplicated(
            root / "metrics" / "update_history.jsonl", "update_id"
        )
        archived_versions = _read_jsonl_deduplicated(
            root / "metrics" / "global_version_history.jsonl", "version"
        )
        payload: dict[str, Any] = {
            "exists": True,
            "path": str(path),
            "integrity_check": integrity,
            "integrity_ok": integrity == ["ok"],
            "archived_updates": len(archived_updates),
            "archived_versions": len(archived_versions),
        }
        if _table_exists(conn, "updates"):
            live_full = [dict(row) for row in conn.execute("SELECT * FROM updates").fetchall()]
            archived_full = [
                row for row in archived_updates if row.get("update_kind", "full") == "full"
            ]
            full_by_id = {str(row["update_id"]): row for row in [*archived_full, *live_full]}
            applied_rows = [row for row in full_by_id.values() if row.get("status") == "applied"]
            dropped_rows = [row for row in full_by_id.values() if row.get("status") == "dropped"]
            mid_cycle_counts = [
                int(row.get("mid_cycle_adoption_count") or 0) for row in full_by_id.values()
            ]
            base_switch_steps = [
                int(row["base_switched_at_step"])
                for row in full_by_id.values()
                if row.get("base_switched_at_step") is not None
            ]
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM updates WHERE status='pending'"
            ).fetchone()["n"]
            versions = conn.execute("SELECT COUNT(*) AS n FROM global_versions").fetchone()["n"]
            contributors = [
                {
                    key: row.get(key)
                    for key in (
                        "applied_version",
                        "learner_id",
                        "update_id",
                        "effective_weight",
                    )
                }
                for row in sorted(
                    applied_rows,
                    key=lambda item: (
                        int(item.get("applied_version") or 0),
                        str(item.get("learner_id") or ""),
                    ),
                )
            ]
            payload.update(
                {
                    "applied_updates": len(applied_rows),
                    "pending_updates": pending,
                    "dropped_updates": len(dropped_rows),
                    "global_versions": versions,
                    "contributors": contributors,
                    "mid_cycle_adoption": {
                        "proposals_with_adoption": sum(count > 0 for count in mid_cycle_counts),
                        "adoption_count": sum(mid_cycle_counts),
                        "base_switched_at_step_values": base_switch_steps,
                    },
                }
            )
        if _table_exists(conn, "fragment_updates"):
            live_fragment = [
                dict(row) for row in conn.execute("SELECT * FROM fragment_updates").fetchall()
            ]
            archived_fragment = [
                row for row in archived_updates if row.get("update_kind") == "fragment"
            ]
            fragment_by_id = {
                str(row["update_id"]): row for row in [*archived_fragment, *live_fragment]
            }
            applied_rows = [
                row for row in fragment_by_id.values() if row.get("status") == "applied"
            ]
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM fragment_updates WHERE status='pending'",
            ).fetchone()["n"]
            dropped = sum(1 for row in fragment_by_id.values() if row.get("status") == "dropped")
            learners_with_updates = sorted(
                {str(row["learner_id"]) for row in fragment_by_id.values() if row.get("learner_id")}
            )
            fragment_versions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT fragment_id, version, global_merge_event, status, num_updates
                    FROM fragment_versions
                    ORDER BY fragment_id, version
                    """
                )
            ]
            selected_by_event = defaultdict(list)
            for row in applied_rows:
                selected_by_event[int(row["applied_global_merge_event"])].append(row)
            selected_counts = {
                str(event): len(rows) for event, rows in sorted(selected_by_event.items())
            }
            staleness = [
                int(row["staleness_fragment_versions"])
                for row in applied_rows
                if row.get("staleness_fragment_versions") is not None
            ]
            payload.update(
                {
                    "fragment_applied_updates": len(applied_rows),
                    "fragment_pending_updates": pending,
                    "fragment_dropped_updates": dropped,
                    "fragment_learners_with_updates": learners_with_updates,
                    "fragment_versions_rows": fragment_versions,
                    "fragment_selected_counts_by_event": selected_counts,
                    "fragment_update_integrity": {
                        "ok": True,
                        "archived_payloads_expected_absent": True,
                    },
                    "fragment_staleness_values": staleness,
                }
            )
    finally:
        conn.close()
    return payload


def _read_heartbeats(root: Path) -> dict[str, dict[str, Any]]:
    heartbeats: dict[str, dict[str, Any]] = {}
    for path in RunPaths(root).iter_learner_heartbeats():
        payload = safe_read_json(path)
        if payload and payload.get("learner_id"):
            heartbeats[str(payload["learner_id"])] = payload
    return heartbeats


def _distribution(values: list[int]) -> dict[str, int]:
    return {str(key): value for key, value in sorted(Counter(values).items())}


def _numeric_summary(values: list[float]) -> dict[str, Any]:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return {"count": 0}
    return {
        "count": len(finite),
        "min": min(finite),
        "max": max(finite),
        "mean": sum(finite) / len(finite),
    }


def staleness_observational_summary(
    syncer_rows: list[dict[str, str]],
    learner_rows: list[dict[str, str]],
) -> dict[str, Any]:
    evidence_rows: list[tuple[float, dict[str, str]]] = []
    aggregate_counts: Counter[int] = Counter()
    effective_values: list[float] = []
    fresh_values: list[float] = []
    for row in syncer_rows:
        raw_effective = row.get("effective_staleness_mean")
        raw_fresh = row.get("fresh_effective_weight")
        raw_timestamp = row.get("timestamp")
        if raw_effective in (None, "") or raw_fresh in (None, ""):
            continue
        try:
            timestamp = float(raw_timestamp or 0.0)
            effective = float(raw_effective)
            fresh = float(raw_fresh)
            counts = json.loads(row.get("staleness_counts_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not all(math.isfinite(value) for value in (timestamp, effective, fresh)):
            continue
        evidence_rows.append((timestamp, row))
        effective_values.append(effective)
        fresh_values.append(fresh)
        if isinstance(counts, dict):
            for key, value in counts.items():
                try:
                    aggregate_counts[int(key)] += int(value)
                except (TypeError, ValueError):
                    continue
    if not evidence_rows:
        return {"status": "unavailable", "reason": "staleness evidence fields missing"}

    learner_events: list[tuple[float, dict[str, str], float]] = []
    for row in learner_rows:
        try:
            timestamp = float(row.get("timestamp") or 0.0)
            loss = float(row.get("train_loss") or "nan")
        except (TypeError, ValueError):
            continue
        if math.isfinite(timestamp) and math.isfinite(loss):
            learner_events.append((timestamp, row, loss))
    learner_events.sort(key=lambda item: item[0])

    links: list[dict[str, Any]] = []
    learner_index = 0
    for merge_timestamp, row in sorted(evidence_rows, key=lambda item: item[0]):
        while (
            learner_index < len(learner_events)
            and learner_events[learner_index][0] <= merge_timestamp
        ):
            learner_index += 1
        link: dict[str, Any] = {
            "merge_version": row.get("global_merge_event") or row.get("version"),
            "merge_timestamp": merge_timestamp,
            "effective_staleness_mean": float(row["effective_staleness_mean"]),
            "fresh_effective_weight": float(row["fresh_effective_weight"]),
            "staleness_counts": json.loads(row.get("staleness_counts_json") or "{}"),
        }
        if learner_index < len(learner_events):
            next_timestamp, next_row, loss = learner_events[learner_index]
            link.update(
                next_learner_id=next_row.get("learner_id"),
                next_local_step=int(float(next_row.get("local_step") or 0)),
                next_train_loss=loss,
                next_update_timestamp=next_timestamp,
                delay_seconds=next_timestamp - merge_timestamp,
            )
        links.append(link)
    return {
        "status": "available_observational_only",
        "warning": "merge-to-next-learner loss links are temporal observations, not validation",
        "merge_count": len(evidence_rows),
        "effective_staleness_mean": _numeric_summary(effective_values),
        "fresh_effective_weight": _numeric_summary(fresh_values),
        "aggregate_staleness_counts": {
            str(key): value for key, value in sorted(aggregate_counts.items())
        },
        "links": links,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return None
    position = (len(finite) - 1) * float(quantile)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return finite[lower]
    fraction = position - lower
    return finite[lower] + (finite[upper] - finite[lower]) * fraction


def syncer_resource_cost(
    rows: list[dict[str, str]],
    complete_training_time_seconds: float | None,
) -> dict[str, Any]:
    """Account for the dedicated syncer node using non-overlapping merge metrics."""
    if not rows or complete_training_time_seconds is None:
        return {"status": "unavailable", "merge_count": len(rows)}
    duration = float(complete_training_time_seconds)
    if not math.isfinite(duration) or duration <= 0.0:
        return {"status": "unavailable", "merge_count": len(rows)}

    merge_compute_samples: list[float] = []
    publish_samples: list[float] = []
    for row in rows:
        try:
            merge_compute = sum(
                float(row.get(key) or 0.0)
                for key in ("read_seconds", "aggregation_seconds", "outer_step_seconds")
            )
            publish = float(row.get("publish_seconds") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if merge_compute < 0.0 or publish < 0.0:
            continue
        if math.isfinite(merge_compute) and math.isfinite(publish):
            merge_compute_samples.append(merge_compute)
            publish_samples.append(publish)
    if not merge_compute_samples:
        return {"status": "unavailable", "merge_count": len(rows)}

    merge_compute_total = sum(merge_compute_samples)
    publish_total = sum(publish_samples)
    active_total = merge_compute_total + publish_total
    duty_cycle = min(1.0, active_total / duration)
    reserved_node_hours = duration / 3600.0
    return {
        "status": "available",
        "merge_count": len(merge_compute_samples),
        "complete_training_time_seconds": duration,
        "merge_compute_total_seconds": merge_compute_total,
        "merge_compute_p50_seconds": _percentile(merge_compute_samples, 0.50),
        "merge_compute_p95_seconds": _percentile(merge_compute_samples, 0.95),
        "publish_total_seconds": publish_total,
        "publish_p50_seconds": _percentile(publish_samples, 0.50),
        "publish_p95_seconds": _percentile(publish_samples, 0.95),
        "active_total_seconds": active_total,
        "duty_cycle": duty_cycle,
        "duty_cycle_percent": duty_cycle * 100.0,
        "reserved_syncer_node_hours": reserved_node_hours,
        "estimated_idle_gpu_node_hours": reserved_node_hours * (1.0 - duty_cycle),
    }


def _loss_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    values: list[float] = []
    for row in rows:
        raw = row.get("train_loss")
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        values.append(value)
    summary = _numeric_summary(values)
    if values:
        first = values[:10]
        last = values[-10:]
        first_mean = sum(first) / len(first)
        last_mean = sum(last) / len(last)
        summary.update(
            {
                "first_10_mean": first_mean,
                "last_10_mean": last_mean,
                "last_vs_first_ratio": last_mean / first_mean if first_mean > 0 else None,
                "obvious_divergence": last_mean > max(first_mean * 3.0, first_mean + 1.0),
            }
        )
    return summary


def _learner_fragment_adoption(
    heartbeats: dict[str, dict[str, Any]],
    learner_metric_rows: list[dict[str, str]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in learner_metric_rows:
        learner_id = row.get("learner_id")
        if not learner_id:
            continue
        try:
            count = int(float(row.get("fragment_adopt_count") or 0))
        except ValueError:
            count = 0
        counts[learner_id] = max(counts.get(learner_id, 0), count)
    payload: dict[str, Any] = {}
    for learner_id, heartbeat in heartbeats.items():
        versions = heartbeat.get("last_loaded_fragment_versions") or {}
        adopted = heartbeat.get("last_adopted_fragments") or []
        inferred = any(int(version) > 0 for version in versions.values()) if versions else False
        payload[learner_id] = {
            "fragment_adopt_count": counts.get(learner_id, 0),
            "last_adopted_fragments": adopted,
            "last_loaded_fragment_versions": versions,
            "has_adopted_after_initial": bool(counts.get(learner_id, 0) > 0 or adopted or inferred),
        }
    return payload


def _learner_adoption_pause(
    root: Path,
    learner_metric_rows: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    cycle_elapsed: dict[str, float] = defaultdict(float)
    learner_ids = {str(row["learner_id"]) for row in learner_metric_rows if row.get("learner_id")}
    for row in learner_metric_rows:
        learner_id = row.get("learner_id")
        raw = row.get("local_cycle_elapsed_seconds")
        if not learner_id or raw in (None, ""):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0.0:
            cycle_elapsed[learner_id] += value

    events_by_learner: dict[str, list[float | None]] = defaultdict(list)
    for path in RunPaths(root).iter_learner_logs():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("event_type") or "")
                if event_type != "global_adopted" and not event_type.endswith("fragments_adopted"):
                    continue
                learner_id = str(event.get("learner_id") or event.get("actor") or path.stem)
                learner_ids.add(learner_id)
                raw = event.get("adoption_pause_seconds")
                try:
                    value = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is not None and not math.isfinite(value):
                    value = None
                events_by_learner[learner_id].append(value)

    payload: dict[str, dict[str, Any]] = {}
    for learner_id in sorted(learner_ids):
        events = events_by_learner.get(learner_id, [])
        timed = [value for value in events if value is not None]
        available = len(timed) == len(events)
        total = sum(timed) if available else None
        mean = total / len(timed) if available and timed else None
        denominator = cycle_elapsed.get(learner_id, 0.0)
        fraction = (
            total / denominator if available and total is not None and denominator > 0.0 else None
        )
        payload[learner_id] = {
            "status": "available" if available else "unavailable",
            "adoption_count": len(events),
            "adoption_pause_total_seconds": total,
            "adoption_pause_mean_seconds": mean,
            "completed_cycle_elapsed_seconds": denominator,
            "adoption_pause_fraction": fraction,
        }
    return payload


def _syncer_log_flags(root: Path) -> dict[str, bool]:
    paths = list(RunPaths(root).iter_syncer_logs())
    flags = {
        "exists": bool(paths),
        "error": False,
        "no_progress_timeout": False,
        "uncaught_exception": False,
    }
    if not paths:
        return flags
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace").lower() for path in paths)
    flags["error"] = '"event_type": "error"' in text or '"event_type":"error"' in text
    flags["no_progress_timeout"] = "no_progress_timeout" in text
    flags["uncaught_exception"] = "traceback" in text or "uncaught" in text
    return flags


def summarize_run(shared_root: str | Path, db_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(shared_root)
    latest = safe_read_json(root / "control" / "latest.json")
    stop = safe_read_json(root / "control" / "stop.json")
    run_summary = safe_read_json(root / "control" / "summary.json")
    db = Path(db_path) if db_path is not None else root / "control" / "syncer_metadata.sqlite3"
    syncer_rows = _read_csv_rows(root / "metrics" / "syncer_metrics.csv")
    learner_rows = _read_csv_rows(root / "metrics" / "learner_metrics.csv")
    heartbeats = _read_heartbeats(root)

    latest_kind = None
    global_merge_event = None
    fragment_versions: dict[str, int] = {}
    materialized_weight_exists = False
    fragment_sizes: dict[str, Any] = {"min": 0, "max": 0, "mean": 0.0, "imbalance_ratio": 0.0}
    if latest:
        latest_kind = latest.get("latest_kind", "full")
        global_merge_event = int(latest.get("global_merge_event", latest.get("version", 0)))
        if latest_kind == "fragment":
            fragment_versions = {
                str(fragment_id): int(info["version"])
                for fragment_id, info in (latest.get("fragments") or {}).items()
            }
            materialized_path = latest.get("materialized_weight_path")
            materialized_weight_exists = bool(
                materialized_path and Path(materialized_path).exists()
            )
            fragment_index_path = (
                latest.get("fragment_index_path") or root / "fragments" / "fragment_index.json"
            )
            try:
                fragment_sizes = fragment_size_summary(load_fragment_index(fragment_index_path))
            except Exception as exc:
                fragment_sizes = {
                    "error": repr(exc),
                    "min": 0,
                    "max": 0,
                    "mean": 0.0,
                    "imbalance_ratio": 0.0,
                }

    selected_counts = []
    metric_staleness = []
    for row in syncer_rows:
        try:
            selected_counts.append(int(float(row.get("selected_count") or 0)))
        except ValueError:
            pass
        for key in ("fragment_staleness_min", "fragment_staleness_mean", "fragment_staleness_max"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                metric_staleness.append(float(raw))
            except ValueError:
                pass

    db_summary = _db_summary(db, root)
    db_staleness = [float(value) for value in db_summary.get("fragment_staleness_values", [])]
    learner_local_steps = {
        learner_id: int(payload.get("last_local_step") or 0)
        for learner_id, payload in heartbeats.items()
    }

    summary = {
        "shared_root": str(root),
        "latest": latest,
        "latest_kind": latest_kind,
        "global_merge_event": global_merge_event,
        "fragment_versions": fragment_versions,
        "fragment_sizes": fragment_sizes,
        "fragment_merge_counts": fragment_versions,
        "selected_count_distribution": _distribution(selected_counts),
        "fragment_staleness_distribution": _numeric_summary(db_staleness or metric_staleness),
        "learner_local_steps": learner_local_steps,
        "learner_fragment_adoption": _learner_fragment_adoption(heartbeats, learner_rows),
        "learner_adoption_pause": _learner_adoption_pause(root, learner_rows),
        "staleness_evidence": staleness_observational_summary(syncer_rows, learner_rows),
        "loss_summary": _loss_summary(learner_rows),
        "stop": stop,
        "stop_reason": (stop or {}).get("reason"),
        "run_summary": run_summary,
        "complete_training_time_seconds": (run_summary or {}).get("complete_training_time_seconds"),
        "learner_resources": (run_summary or {}).get("learner_resources"),
        "syncer_resource_cost": syncer_resource_cost(
            syncer_rows,
            (run_summary or {}).get("complete_training_time_seconds"),
        ),
        "materialized_weight_exists": materialized_weight_exists,
        "heartbeats": heartbeats,
        "syncer_metrics": _read_csv_summary(root / "metrics" / "syncer_metrics.csv"),
        "learner_metrics": _read_csv_summary(root / "metrics" / "learner_metrics.csv"),
        "update_manifest": _read_csv_summary(root / "metrics" / "update_manifest.csv"),
        "db": db_summary,
        "syncer_log_flags": _syncer_log_flags(root),
    }
    return summary


def _parse_fragment_ids(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def assert_fragment_run(args: argparse.Namespace, *, require_local_steps: bool) -> None:
    summary = summarize_run(args.run_root, args.db)
    errors: list[str] = []
    expected_ids = _parse_fragment_ids(args.expected_fragment_ids)
    expected_id_text = {str(fragment_id) for fragment_id in expected_ids}
    if summary.get("latest_kind") != "fragment":
        errors.append(f"latest_kind is {summary.get('latest_kind')!r}, expected 'fragment'")
    if int(summary.get("global_merge_event") or -1) != int(args.expected_global_merge_events):
        errors.append(
            f"global_merge_event is {summary.get('global_merge_event')}, "
            f"expected {args.expected_global_merge_events}"
        )
    found_ids = set(summary.get("fragment_versions") or {})
    if not expected_id_text.issubset(found_ids):
        errors.append(f"fragment ids {sorted(found_ids)} do not include {sorted(expected_id_text)}")
    expected_versions = expected_fragment_versions_after_events(
        len(expected_ids),
        int(args.expected_global_merge_events),
    )
    actual_versions = {
        int(key): int(value) for key, value in (summary.get("fragment_versions") or {}).items()
    }
    for fragment_id in expected_ids:
        expected_version = expected_versions[fragment_id]
        if actual_versions.get(fragment_id) != expected_version:
            errors.append(
                f"fragment {fragment_id} version is {actual_versions.get(fragment_id)}, "
                f"expected {expected_version}"
            )
    if summary.get("stop_reason") != "stop_after_outer_steps":
        errors.append(
            f"stop_reason is {summary.get('stop_reason')!r}, expected 'stop_after_outer_steps'"
        )
    if not summary.get("materialized_weight_exists"):
        errors.append("materialized full checkpoint is missing")

    syncer_rows = _read_csv_rows(Path(args.run_root) / "metrics" / "syncer_metrics.csv")
    if len(syncer_rows) < int(args.expected_global_merge_events):
        errors.append(
            f"syncer metric rows {len(syncer_rows)} < expected events {args.expected_global_merge_events}"
        )
    low_quorum_rows = []
    for row in syncer_rows:
        try:
            selected = int(float(row.get("selected_count") or 0))
        except ValueError:
            selected = 0
        if selected < int(args.min_selected_count):
            low_quorum_rows.append(row.get("global_merge_event") or row.get("version"))
    if low_quorum_rows:
        errors.append(f"merge events below min selected count: {low_quorum_rows}")

    heartbeats = summary.get("heartbeats") or {}
    if len(heartbeats) < int(args.expected_learners):
        errors.append(f"learner heartbeats {len(heartbeats)} < expected {args.expected_learners}")
    if require_local_steps:
        for index in range(int(args.expected_learners)):
            learner_id = f"learner_{index:03d}"
            local_step = int((summary.get("learner_local_steps") or {}).get(learner_id, 0))
            if local_step < int(args.expected_local_steps):
                errors.append(
                    f"{learner_id} local_step {local_step} < expected {args.expected_local_steps}"
                )

    db_summary = summary.get("db") or {}
    if not db_summary.get("exists"):
        errors.append("persistent SQLite DB is missing")
    integrity = db_summary.get("fragment_update_integrity") or {}
    if integrity and not integrity.get("ok"):
        errors.append(f"applied fragment update integrity failed: {integrity}")
    learners_with_updates = set(db_summary.get("fragment_learners_with_updates") or [])
    if len(learners_with_updates) < int(args.expected_learners):
        errors.append(
            f"learners with fragment updates {sorted(learners_with_updates)} "
            f"< expected {args.expected_learners}"
        )
    selected_by_event = db_summary.get("fragment_selected_counts_by_event") or {}
    for event in range(1, int(args.expected_global_merge_events) + 1):
        count = int(selected_by_event.get(str(event), 0))
        if count < int(args.min_selected_count):
            errors.append(
                f"DB selected count for event {event} is {count}, expected >= {args.min_selected_count}"
            )

    adoption = summary.get("learner_fragment_adoption") or {}
    for index in range(int(args.expected_learners)):
        learner_id = f"learner_{index:03d}"
        if not (adoption.get(learner_id) or {}).get("has_adopted_after_initial"):
            errors.append(f"{learner_id} did not record fragment adoption")

    loss_summary = summary.get("loss_summary") or {}
    if int(loss_summary.get("count") or 0) == 0:
        errors.append("learner loss metrics are missing")
    if loss_summary.get("obvious_divergence"):
        errors.append(f"learner losses show obvious divergence: {loss_summary}")

    log_flags = summary.get("syncer_log_flags") or {}
    if (
        log_flags.get("error")
        or log_flags.get("no_progress_timeout")
        or log_flags.get("uncaught_exception")
    ):
        errors.append(f"syncer log contains failure markers: {log_flags}")

    if errors:
        raise SystemExit(
            "fragment assertion failed:\n" + "\n".join(f"- {error}" for error in errors)
        )


def _summary_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shared_root", help="Run shared root")
    parser.add_argument("--db", help="persistent SQLite DB path")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    return parser


def _assert_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"analysis {name}")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--db")
    parser.add_argument("--expected-learners", type=int, required=True)
    parser.add_argument("--expected-local-steps", type=int, default=0)
    parser.add_argument("--expected-global-merge-events", type=int, required=True)
    parser.add_argument("--expected-fragment-ids", required=True)
    parser.add_argument("--min-selected-count", type=int, required=True)
    return parser


def _print_summary(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return
    print(f"shared_root: {summary['shared_root']}")
    print(f"latest_kind: {summary.get('latest_kind')}")
    print(f"latest_version: {(summary.get('latest') or {}).get('version')}")
    print(f"global_merge_event: {summary.get('global_merge_event')}")
    print(f"stop_reason: {summary.get('stop_reason')}")
    print(f"syncer_metric_rows: {summary['syncer_metrics']['rows']}")
    print(f"learner_metric_rows: {summary['learner_metrics']['rows']}")
    print(f"db_exists: {summary['db'].get('exists')}")
    if summary.get("latest_kind") == "fragment":
        print(f"fragment_versions: {summary.get('fragment_versions')}")
        print(f"materialized_weight_exists: {summary.get('materialized_weight_exists')}")
    db = summary["db"]
    if db.get("exists"):
        if "global_versions" in db:
            print(f"global_versions: {db.get('global_versions')}")
            print(f"applied_updates: {db.get('applied_updates')}")
            print(f"pending_updates: {db.get('pending_updates')}")
            print(f"dropped_updates: {db.get('dropped_updates')}")
        if "fragment_applied_updates" in db:
            print(f"fragment_applied_updates: {db.get('fragment_applied_updates')}")
            print(f"fragment_pending_updates: {db.get('fragment_pending_updates')}")
            print(f"fragment_dropped_updates: {db.get('fragment_dropped_updates')}")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "summary":
        args = _summary_parser().parse_args(argv[1:])
        _print_summary(summarize_run(args.shared_root, args.db), as_json=args.json)
        return
    if argv and argv[0] == "assert-fragment-smoke":
        args = _assert_parser("assert-fragment-smoke").parse_args(argv[1:])
        assert_fragment_run(args, require_local_steps=False)
        return
    if argv and argv[0] == "assert-fragment-5000":
        args = _assert_parser("assert-fragment-5000").parse_args(argv[1:])
        assert_fragment_run(args, require_local_steps=True)
        return
    args = _summary_parser().parse_args(argv)
    _print_summary(summarize_run(args.shared_root, args.db), as_json=args.json)


if __name__ == "__main__":
    main()
