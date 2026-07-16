#!/usr/bin/env python3
"""Independent plan-01 run invariant checker with a three-value output contract."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected an object at {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected an object row at {path}")
            rows.append(value)
    return rows


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def check(args: argparse.Namespace) -> str:
    root = Path(args.run_root).resolve()
    control = root / "control"
    db_path = control / "syncer_metadata.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise RuntimeError("integrity check failed")
        pragmas = {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        }
        if str(pragmas["journal_mode"]).lower() != "delete" or int(
            pragmas["synchronous"]
        ) != 2:
            raise RuntimeError("unsafe SQLite pragmas")
        globals_ = [dict(row) for row in conn.execute("SELECT * FROM global_versions")]
        if len(globals_) != 1:
            raise RuntimeError("active global set is not current-only")
        current = globals_[0]
        version = int(current["version"])
        if version < args.expected_version:
            raise RuntimeError("expected version not reached")
        active_updates = int(
            conn.execute(
                "SELECT COUNT(*) FROM updates WHERE status IN ('pending', 'selected')"
            ).fetchone()[0]
        )
        if active_updates > 2 * args.expected_learners:
            raise RuntimeError("active proposal bound exceeded")
    finally:
        conn.close()

    latest = read_json(control / "latest.json")
    if int(latest["version"]) != version:
        raise RuntimeError("latest disagrees with DB")
    for key in ("weight_path", "optim_path"):
        if latest[key] != current[key] or not Path(str(current[key])).is_file():
            raise RuntimeError("committed checkpoint mismatch")
    expected_weights = {Path(str(current["weight_path"])).resolve()}
    expected_optim = {Path(str(current["optim_path"])).resolve()}
    actual_weights = {path.resolve() for path in (root / "weights").glob("*.safetensors")}
    actual_optim = {path.resolve() for path in (root / "optim").glob("*.safetensors")}
    if actual_weights != expected_weights or actual_optim != expected_optim:
        raise RuntimeError("checkpoint retention is not current-only")
    pointer_count = len(list((root / "updates" / "latest").glob("learner_*.json")))
    if pointer_count != args.expected_learners:
        raise RuntimeError("fixed proposal surface mismatch")
    if (root / "db_dumps").exists() or list(root.glob("**/*-wal")):
        raise RuntimeError("legacy DB artifact exists")

    archive = read_jsonl(root / "metrics" / "update_history.jsonl")
    update_ids = [str(row["update_id"]) for row in archive]
    if len(update_ids) != len(set(update_ids)):
        raise RuntimeError("duplicate update archive identity")
    version_archive = read_jsonl(root / "metrics" / "global_version_history.jsonl")
    version_ids = [
        (str(row.get("version_kind", "full")), int(row["version"]))
        for row in version_archive
    ]
    if len(version_ids) != len(set(version_ids)):
        raise RuntimeError("duplicate version archive identity")
    archived_global_versions = {version for kind, version in version_ids if kind == "full"}
    if not set(range(version)).issubset(archived_global_versions):
        raise RuntimeError("global version archive is incomplete")

    events = read_jsonl(root / "logs" / "syncer.jsonl")
    event_types = {str(row.get("event_type")) for row in events}
    if event_types & {"error", "no_progress_timeout", "db_dumped"}:
        raise RuntimeError("failure or dump event exists")

    if args.require_complete:
        stop = read_json(control / "stop.json")
        summary = read_json(control / "summary.json")
        if int(stop["version"]) != version or int(summary["final_version"]) != version:
            raise RuntimeError("terminal versions disagree")
        if version != args.expected_version or len(version_archive) != version:
            raise RuntimeError("completed version/history count mismatch")
        if list((root / "updates" / "payloads").glob("**/*.safetensors")):
            raise RuntimeError("terminal proposal tensor remains")
        if list((root / "updates" / "payloads").glob("**/*.meta.json")):
            raise RuntimeError("terminal proposal metadata remains")
        if list(root.glob("**/.*.tmp")):
            raise RuntimeError("temporary artifact remains")
        with (root / "metrics" / "syncer_metrics.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            metric_rows = list(csv.DictReader(handle))
        if len(metric_rows) != version:
            raise RuntimeError("metric/global version count mismatch")
        sqlite_seconds = [float(row["sqlite_commit_seconds"]) for row in metric_rows]
        maintenance_seconds = [float(row["maintenance_seconds"]) for row in metric_rows]
        if percentile_95(sqlite_seconds) >= 2.0:
            raise RuntimeError("SQLite commit p95 exceeded")
        training_seconds = float(summary["complete_training_time_seconds"])
        if (sum(sqlite_seconds) + sum(maintenance_seconds)) / training_seconds >= 0.05:
            raise RuntimeError("SQLite plus maintenance overhead exceeded")
        return "PASS"
    return "PASS_WITH_FOLLOWUPS"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--expected-learners", type=int, required=True)
    parser.add_argument("--expected-version", type=int, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    try:
        result = check(args)
    except Exception:
        result = "BLOCKED"
    print(result)
    raise SystemExit(0 if result != "BLOCKED" else 1)


if __name__ == "__main__":
    main()
