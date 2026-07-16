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
import time
from pathlib import Path
from typing import Any


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=60000")
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
        """
    )
    conn.commit()
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
    return {"integrity_check": integrity, "counter": counter, "events": events}


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
    for mode in ("stress", "verify", "kill-reopen", "_kill-once"):
        sub = subparsers.add_parser(mode)
        sub.add_argument("--db", type=Path, required=True)
        if mode == "stress":
            sub.add_argument("--writer-id", required=True)
            sub.add_argument("--count", type=int, required=True)
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
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
