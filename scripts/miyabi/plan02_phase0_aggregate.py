#!/usr/bin/env python3
"""Aggregate raw Plan 02 Phase 0 probe outputs into one auditable input."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _clock_summary(path: Path, max_clock_skew_seconds: float) -> dict[str, Any]:
    exchange = _read_object(path)
    absolute_bound = exchange.get("absolute_offset_upper_bound_seconds")
    within_bound = (
        exchange.get("status") == "PASS"
        and exchange.get("intersection_valid") is True
        and isinstance(absolute_bound, (int, float))
        and float(absolute_bound) <= max_clock_skew_seconds
    )
    return {
        **exchange,
        "max_clock_skew_seconds": max_clock_skew_seconds,
        "within_bound": within_bound,
    }


def _contention_summary(paths: list[Path], db_path: Path) -> dict[str, Any]:
    writers = [_read_object(path) for path in paths]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        pragmas = {
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
        }
        event_count = int(
            conn.execute("SELECT COUNT(*) FROM probe_contention_events").fetchone()[0]
        )
        distinct_writers = int(
            conn.execute(
                "SELECT COUNT(DISTINCT writer_id) FROM probe_contention_events"
            ).fetchone()[0]
        )
        action_counts = {
            str(action): int(count)
            for action, count in conn.execute(
                "SELECT action, COUNT(*) FROM probe_contention_events GROUP BY action"
            )
        }
    finally:
        conn.close()
    requested = sum(int(writer["requested_transactions"]) for writer in writers)
    committed = sum(int(writer["committed_transactions"]) for writer in writers)
    hostnames = sorted({str(writer["hostname"]) for writer in writers})
    return {
        "writers": writers,
        "writer_count": len(writers),
        "distinct_db_writers": distinct_writers,
        "hostnames": hostnames,
        "host_count": len(hostnames),
        "requested_transactions": requested,
        "committed_transactions": committed,
        "event_count": event_count,
        "busy_errors": sum(int(writer["busy_errors"]) for writer in writers),
        "starvation_count": sum(bool(writer["starved"]) for writer in writers),
        "maximum_writer_wait_seconds": max(
            float(writer["wait_seconds"]["max"])
            for writer in writers
            if writer["wait_seconds"]["max"] is not None
        ),
        "action_counts": action_counts,
        "integrity_check": integrity,
        "pragmas": pragmas,
        "all_transactions_committed": requested == committed == event_count,
    }


def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    visibility_write = _read_object(args.visibility_write)
    visibility_read = _read_object(args.visibility_read)
    clock = _clock_summary(args.clock_exchange, args.max_clock_skew_seconds)
    contention = _contention_summary(args.contention_results, args.sqlite_db)
    visibility = {
        "writer": visibility_write,
        "reader": visibility_read,
        "same_committed_state": (
            int(visibility_write["counter"]) == int(visibility_read["counter"])
            and int(visibility_write["events"]) == int(visibility_read["events"])
            and int(visibility_read["counter"]) > 0
        ),
        "writer_hostname": visibility_write.get("hostname"),
        "reader_hostname": visibility_read.get("hostname"),
    }
    return {
        "artifact_format_version": 1,
        "plan_id": "fsb_decoupled_diloco_plan_02",
        "phase": "Phase 0",
        "captured_at": time.time(),
        "pbs_job_id": args.pbs_job_id,
        "hosts": args.hosts,
        "source_identity": _read_object(args.source_identity),
        "sqlite_writer_lock": _read_object(args.writer_lock),
        "old_cache_writer": _read_object(args.old_cache_writer),
        "clock_sqlite": {
            "clock": clock,
            "visibility": visibility,
            "contention": contention,
        },
        "pbs_capability": _read_object(args.pbs_capability),
        "source_pinning": _read_object(args.source_pinning),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writer-lock", type=Path, required=True)
    parser.add_argument("--old-cache-writer", type=Path, required=True)
    parser.add_argument("--clock-exchange", type=Path, required=True)
    parser.add_argument(
        "--contention-result",
        dest="contention_results",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--sqlite-db", type=Path, required=True)
    parser.add_argument("--visibility-write", type=Path, required=True)
    parser.add_argument("--visibility-read", type=Path, required=True)
    parser.add_argument("--pbs-capability", type=Path, required=True)
    parser.add_argument("--source-pinning", type=Path, required=True)
    parser.add_argument("--source-identity", type=Path, required=True)
    parser.add_argument("--pbs-job-id", required=True)
    parser.add_argument("--host", dest="hosts", action="append", required=True)
    parser.add_argument("--max-clock-skew-seconds", type=float, default=2.0)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = aggregate(args)
    _atomic_write_json(args.output_json, payload)


if __name__ == "__main__":
    main()
