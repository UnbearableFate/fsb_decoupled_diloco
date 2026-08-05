#!/usr/bin/env python3
"""Exercise persistent SQLite transactions, kill/reopen, and cross-node visibility."""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def _configure_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int,
    set_journal_mode: bool,
) -> None:
    startup_busy_timeout_ms = max(5000, int(busy_timeout_ms))
    conn.execute(f"PRAGMA busy_timeout={startup_busy_timeout_ms}")
    if set_journal_mode:
        journal_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        if journal_mode.lower() != "delete":
            raise RuntimeError(f"unexpected journal mode: {journal_mode}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")


def connect(
    path: Path,
    *,
    timeout_seconds: float = 60.0,
    busy_timeout_ms: int = 60000,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout_seconds)
    _configure_connection(
        conn,
        busy_timeout_ms=busy_timeout_ms,
        set_journal_mode=True,
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS probe_counter (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            value INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO probe_counter(singleton, value) VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS probe_events (
            event_id TEXT PRIMARY KEY,
            writer_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(writer_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS probe_lease (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            owner_id TEXT,
            epoch INTEGER NOT NULL,
            renew_seq INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT OR IGNORE INTO probe_lease(
            singleton, owner_id, epoch, renew_seq, updated_at
        ) VALUES (1, NULL, 0, 0, 0.0);
        CREATE TABLE IF NOT EXISTS probe_contention_events (
            writer_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            action TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            wait_seconds REAL NOT NULL,
            busy_retries INTEGER NOT NULL,
            committed_at REAL NOT NULL,
            PRIMARY KEY(writer_id, sequence)
        );
        """
    )
    conn.commit()
    return conn


def open_existing(
    path: Path,
    *,
    timeout_seconds: float,
    busy_timeout_ms: int,
) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=timeout_seconds)
    _configure_connection(
        conn,
        busy_timeout_ms=busy_timeout_ms,
        set_journal_mode=False,
    )
    return conn


def verify(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    counter = int(
        conn.execute("SELECT value FROM probe_counter WHERE singleton = 1").fetchone()[0]
    )
    events = int(conn.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0])
    if integrity != ["ok"]:
        raise RuntimeError(f"integrity_check failed: {integrity}")
    if counter != events:
        raise RuntimeError(f"partial transaction detected: counter={counter}, events={events}")
    pragmas = {
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
        "busy_timeout_ms": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
    }
    if pragmas["journal_mode"].lower() != "delete" or pragmas["synchronous"] != 2:
        raise RuntimeError(f"unsafe SQLite PRAGMA values: {pragmas}")
    return {
        "integrity_check": integrity,
        "counter": counter,
        "events": events,
        "pragmas": pragmas,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot compute a percentile of an empty sample")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


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


def stress(path: Path, *, writer_id: str, count: int) -> dict[str, Any]:
    conn = connect(path)
    started = time.monotonic()
    busy_errors = 0
    try:
        for sequence in range(count):
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO probe_events(event_id, writer_id, sequence, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (f"{writer_id}:{sequence}", writer_id, sequence, time.time()),
                )
                if cur.rowcount:
                    conn.execute(
                        "UPDATE probe_counter SET value = value + 1 WHERE singleton = 1"
                    )
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    busy_errors += 1
                raise
        summary = verify(conn)
    finally:
        conn.close()
    summary.update(
        {
            "mode": "stress",
            "writer_id": writer_id,
            "requested_transactions": count,
            "busy_errors": busy_errors,
            "elapsed_seconds": time.monotonic() - started,
        }
    )
    return summary


def contend(
    path: Path,
    *,
    writer_id: str,
    count: int,
    busy_timeout_ms: int,
    retry_timeout_seconds: float,
    hold_milliseconds: float,
    seed: int,
) -> dict[str, Any]:
    if count <= 0:
        raise ValueError("count must be positive")
    if busy_timeout_ms <= 0 or retry_timeout_seconds <= 0 or hold_milliseconds < 0:
        raise ValueError("invalid contention timing argument")
    rng = random.Random(seed)
    conn = open_existing(
        path,
        timeout_seconds=busy_timeout_ms / 1000.0,
        busy_timeout_ms=busy_timeout_ms,
    )
    started = time.monotonic()
    wait_samples: list[float] = []
    busy_errors = 0
    acquire_count = 0
    renew_count = 0
    try:
        for sequence in range(count):
            transaction_started = time.monotonic()
            deadline = transaction_started + retry_timeout_seconds
            retries = 0
            while True:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    acquired_at = time.monotonic()
                    owner_id, epoch, renew_seq = conn.execute(
                        "SELECT owner_id, epoch, renew_seq FROM probe_lease WHERE singleton = 1"
                    ).fetchone()
                    if owner_id == writer_id:
                        action = "renew"
                        renew_seq = int(renew_seq) + 1
                        renew_count += 1
                    else:
                        action = "acquire"
                        epoch = int(epoch) + 1
                        renew_seq = 0
                        acquire_count += 1
                    wait_seconds = acquired_at - transaction_started
                    conn.execute(
                        """
                        UPDATE probe_lease
                        SET owner_id = ?, epoch = ?, renew_seq = ?, updated_at = ?
                        WHERE singleton = 1
                        """,
                        (writer_id, epoch, renew_seq, time.time()),
                    )
                    conn.execute(
                        """
                        INSERT INTO probe_contention_events(
                            writer_id, sequence, action, epoch, wait_seconds,
                            busy_retries, committed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            writer_id,
                            sequence,
                            action,
                            int(epoch),
                            wait_seconds,
                            retries,
                            time.time(),
                        ),
                    )
                    if hold_milliseconds:
                        time.sleep(hold_milliseconds / 1000.0)
                    conn.commit()
                    wait_samples.append(wait_seconds)
                    break
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    message = str(exc).lower()
                    if "locked" not in message and "busy" not in message:
                        raise
                    busy_errors += 1
                    retries += 1
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"writer {writer_id} starved at sequence {sequence}"
                        ) from exc
                    time.sleep(rng.uniform(0.001, 0.010))
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        own_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM probe_contention_events WHERE writer_id = ?",
                (writer_id,),
            ).fetchone()[0]
        )
        if integrity != ["ok"] or own_rows != count:
            raise RuntimeError(
                f"contention verification failed: integrity={integrity}, own_rows={own_rows}"
            )
    finally:
        conn.close()
    return {
        "mode": "contend",
        "writer_id": writer_id,
        "requested_transactions": count,
        "committed_transactions": len(wait_samples),
        "acquire_count": acquire_count,
        "renew_count": renew_count,
        "busy_errors": busy_errors,
        "starved": len(wait_samples) != count,
        "wait_seconds": {
            "samples": len(wait_samples),
            "p50": _percentile(wait_samples, 0.50),
            "p95": _percentile(wait_samples, 0.95),
            "p99": _percentile(wait_samples, 0.99),
            "max": max(wait_samples),
        },
        "integrity_check": integrity,
        "elapsed_seconds": time.monotonic() - started,
    }


def clock_sample(
    marker: Path,
    *,
    writer_id: str,
    output_json: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while not marker.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"clock marker did not appear: {marker}")
        time.sleep(0.002)
    before_read_ns = time.time_ns()
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    after_read_ns = time.time_ns()
    payload = {
        "mode": "clock-sample",
        "writer_id": writer_id,
        "hostname": os.uname().nodename,
        "pid": os.getpid(),
        "before_read_time_ns": before_read_ns,
        "after_read_time_ns": after_read_ns,
        "midpoint_time_ns": (before_read_ns + after_read_ns) // 2,
        "marker_written_time_ns": int(marker_payload["written_time_ns"]),
        "marker_observation_delay_seconds": max(
            0.0,
            (after_read_ns - int(marker_payload["written_time_ns"])) / 1_000_000_000.0,
        ),
        "wall_clock_resolution_seconds": time.get_clock_info("time").resolution,
        "monotonic_clock_resolution_seconds": time.get_clock_info("monotonic").resolution,
    }
    _atomic_write_json(output_json, payload)
    return payload


def kill_once(path: Path, *, cycle: int, phase: str) -> None:
    conn = connect(path)
    conn.execute("BEGIN IMMEDIATE")
    event_id = f"kill:{cycle}"
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO probe_events(event_id, writer_id, sequence, created_at)
        VALUES (?, 'kill', ?, ?)
        """,
        (event_id, cycle, time.time()),
    )
    if cur.rowcount:
        conn.execute("UPDATE probe_counter SET value = value + 1 WHERE singleton = 1")
    if phase == "before_commit":
        os.kill(os.getpid(), signal.SIGKILL)
    conn.commit()
    if phase == "after_commit":
        os.kill(os.getpid(), signal.SIGKILL)
    conn.close()


def kill_reopen(path: Path, *, cycles: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    started = time.monotonic()
    phases: dict[str, int] = {"before_commit": 0, "after_commit": 0}
    for cycle in range(cycles):
        phase = rng.choice(tuple(phases))
        phases[phase] += 1
        child = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_kill-once",
                "--db",
                str(path),
                "--cycle",
                str(cycle),
                "--phase",
                phase,
            ],
            check=False,
        )
        if child.returncode == 0:
            raise RuntimeError(f"kill child {cycle} unexpectedly exited successfully")
        conn = connect(path)
        try:
            verify(conn)
        finally:
            conn.close()
    conn = connect(path)
    try:
        summary = verify(conn)
    finally:
        conn.close()
    summary.update(
        {
            "mode": "kill-reopen",
            "cycles": cycles,
            "seed": seed,
            "phases": phases,
            "elapsed_seconds": time.monotonic() - started,
        }
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in (
        "stress",
        "verify",
        "kill-reopen",
        "contend",
        "clock-sample",
        "_kill-once",
    ):
        sub = subparsers.add_parser(mode)
        if mode != "clock-sample":
            sub.add_argument("--db", type=Path, required=True)
        if mode in {"stress", "contend"}:
            sub.add_argument("--writer-id", required=True)
            sub.add_argument("--count", type=int, required=True)
        if mode == "contend":
            sub.add_argument("--busy-timeout-ms", type=int, default=10)
            sub.add_argument("--retry-timeout-seconds", type=float, default=30.0)
            sub.add_argument("--hold-milliseconds", type=float, default=5.0)
            sub.add_argument("--seed", type=int, default=1337)
            sub.add_argument("--output-json", type=Path)
        elif mode == "clock-sample":
            sub.add_argument("--marker", type=Path, required=True)
            sub.add_argument("--writer-id", required=True)
            sub.add_argument("--output-json", type=Path, required=True)
            sub.add_argument("--timeout-seconds", type=float, default=30.0)
        elif mode == "kill-reopen":
            sub.add_argument("--cycles", type=int, default=100)
            sub.add_argument("--seed", type=int, default=1337)
        elif mode == "_kill-once":
            sub.add_argument("--cycle", type=int, required=True)
            sub.add_argument("--phase", choices=("before_commit", "after_commit"), required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "stress":
        summary = stress(args.db, writer_id=args.writer_id, count=args.count)
    elif args.mode == "contend":
        summary = contend(
            args.db,
            writer_id=args.writer_id,
            count=args.count,
            busy_timeout_ms=args.busy_timeout_ms,
            retry_timeout_seconds=args.retry_timeout_seconds,
            hold_milliseconds=args.hold_milliseconds,
            seed=args.seed,
        )
        if args.output_json is not None:
            _atomic_write_json(args.output_json, summary)
    elif args.mode == "clock-sample":
        summary = clock_sample(
            args.marker,
            writer_id=args.writer_id,
            output_json=args.output_json,
            timeout_seconds=args.timeout_seconds,
        )
    elif args.mode == "kill-reopen":
        summary = kill_reopen(args.db, cycles=args.cycles, seed=args.seed)
    elif args.mode == "_kill-once":
        kill_once(args.db, cycle=args.cycle, phase=args.phase)
        return
    else:
        conn = connect(args.db)
        try:
            summary = dict(verify(conn), mode="verify")
        finally:
            conn.close()
    summary.setdefault("hostname", os.uname().nodename)
    summary.setdefault("pid", os.getpid())
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
