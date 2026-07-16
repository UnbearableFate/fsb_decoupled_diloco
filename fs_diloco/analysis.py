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

from .atomic_io import safe_read_json
from .fragment_index import fragment_size_summary, load_fragment_index
from .fragment_scheduler import expected_fragment_versions_after_events


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_csv_summary(path: Path) -> dict[str, Any]:
    rows = _read_csv_rows(path)
    return {"exists": path.exists(), "rows": len(rows), "last": rows[-1] if rows else None}


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


def _db_summary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"exists": False}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        payload: dict[str, Any] = {"exists": True, "path": str(path)}
        if _table_exists(conn, "updates"):
            applied = conn.execute("SELECT COUNT(*) AS n FROM updates WHERE status='applied'").fetchone()["n"]
            pending = conn.execute("SELECT COUNT(*) AS n FROM updates WHERE status='pending'").fetchone()["n"]
            dropped = conn.execute("SELECT COUNT(*) AS n FROM updates WHERE status='dropped'").fetchone()["n"]
            versions = conn.execute("SELECT COUNT(*) AS n FROM global_versions").fetchone()["n"]
            contributors = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT applied_version, learner_id, update_id, effective_weight
                    FROM updates
                    WHERE status='applied'
                    ORDER BY applied_version, learner_id
                    """
                )
            ]
            payload.update(
                {
                    "applied_updates": applied,
                    "pending_updates": pending,
                    "dropped_updates": dropped,
                    "global_versions": versions,
                    "contributors": contributors,
                }
            )
        if _table_exists(conn, "fragment_updates"):
            applied_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM fragment_updates
                    WHERE status='applied'
                    ORDER BY applied_global_merge_event, fragment_id, learner_id
                    """
                )
            ]
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM fragment_updates WHERE status='pending'",
            ).fetchone()["n"]
            dropped = conn.execute(
                "SELECT COUNT(*) AS n FROM fragment_updates WHERE status='dropped'",
            ).fetchone()["n"]
            learners_with_updates = [
                row["learner_id"]
                for row in conn.execute(
                    "SELECT DISTINCT learner_id FROM fragment_updates ORDER BY learner_id",
                )
            ]
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
                str(event): len(rows)
                for event, rows in sorted(selected_by_event.items())
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
                    "fragment_update_integrity": _fragment_update_integrity(applied_rows),
                    "fragment_staleness_values": staleness,
                }
            )
    finally:
        conn.close()
    return payload


def _latest_db_dump(root: Path) -> Path | None:
    dumps = sorted((root / "db_dumps").glob("metadata_*_v*.db"))
    return dumps[-1] if dumps else None


def _read_heartbeats(root: Path) -> dict[str, dict[str, Any]]:
    heartbeats: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "heartbeats").glob("learner_*.json")):
        payload = safe_read_json(path)
        if payload:
            heartbeats[path.stem] = payload
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


def _syncer_log_flags(root: Path) -> dict[str, bool]:
    path = root / "logs" / "syncer.jsonl"
    flags = {"exists": path.exists(), "error": False, "no_progress_timeout": False, "uncaught_exception": False}
    if not path.exists():
        return flags
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    flags["error"] = '"event_type": "error"' in text or '"event_type":"error"' in text
    flags["no_progress_timeout"] = "no_progress_timeout" in text
    flags["uncaught_exception"] = "traceback" in text or "uncaught" in text
    return flags


def summarize_run(shared_root: str | Path, db_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(shared_root)
    latest = safe_read_json(root / "control" / "latest.json")
    stop = safe_read_json(root / "control" / "stop.json")
    db = Path(db_path) if db_path is not None else _latest_db_dump(root)
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
            materialized_weight_exists = bool(materialized_path and Path(materialized_path).exists())
            fragment_index_path = latest.get("fragment_index_path") or root / "fragments" / "fragment_index.json"
            try:
                fragment_sizes = fragment_size_summary(load_fragment_index(fragment_index_path))
            except Exception as exc:
                fragment_sizes = {"error": repr(exc), "min": 0, "max": 0, "mean": 0.0, "imbalance_ratio": 0.0}

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

    db_summary = _db_summary(db)
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
        "loss_summary": _loss_summary(learner_rows),
        "stop": stop,
        "stop_reason": (stop or {}).get("reason"),
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
    actual_versions = {int(key): int(value) for key, value in (summary.get("fragment_versions") or {}).items()}
    for fragment_id in expected_ids:
        expected_version = expected_versions[fragment_id]
        if actual_versions.get(fragment_id) != expected_version:
            errors.append(
                f"fragment {fragment_id} version is {actual_versions.get(fragment_id)}, "
                f"expected {expected_version}"
            )
    if summary.get("stop_reason") != "stop_after_outer_steps":
        errors.append(f"stop_reason is {summary.get('stop_reason')!r}, expected 'stop_after_outer_steps'")
    if not summary.get("materialized_weight_exists"):
        errors.append("materialized full checkpoint is missing")

    syncer_rows = _read_csv_rows(Path(args.run_root) / "metrics" / "syncer_metrics.csv")
    if len(syncer_rows) < int(args.expected_global_merge_events):
        errors.append(f"syncer metric rows {len(syncer_rows)} < expected events {args.expected_global_merge_events}")
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
                errors.append(f"{learner_id} local_step {local_step} < expected {args.expected_local_steps}")

    db_summary = summary.get("db") or {}
    if not db_summary.get("exists"):
        errors.append("SQLite DB dump is missing")
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
            errors.append(f"DB selected count for event {event} is {count}, expected >= {args.min_selected_count}")

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
    if log_flags.get("error") or log_flags.get("no_progress_timeout") or log_flags.get("uncaught_exception"):
        errors.append(f"syncer log contains failure markers: {log_flags}")

    if errors:
        raise SystemExit("fragment assertion failed:\n" + "\n".join(f"- {error}" for error in errors))


def _summary_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shared_root", help="Run shared root")
    parser.add_argument("--db", help="SQLite DB or DB dump path")
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
