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


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)


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


def _percentile_or_none(values: list[float], percentile: float) -> float | None:
    return _percentile(values, percentile) if values else None


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
    starvation_error: str | None = None
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
                    else:
                        action = "acquire"
                        epoch = int(epoch) + 1
                        renew_seq = 0
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
                    if action == "renew":
                        renew_count += 1
                    else:
                        acquire_count += 1
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
                        starvation_error = (
                            f"writer {writer_id} starved at sequence {sequence}: {exc}"
                        )
                        break
                    time.sleep(rng.uniform(0.001, 0.010))
            if starvation_error is not None:
                break
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        own_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM probe_contention_events WHERE writer_id = ?",
                (writer_id,),
            ).fetchone()[0]
        )
        if integrity != ["ok"] or (
            starvation_error is None and own_rows != count
        ):
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
        "starved": starvation_error is not None,
        "starvation_error": starvation_error,
        "wait_seconds": {
            "samples": len(wait_samples),
            "p50": _percentile_or_none(wait_samples, 0.50),
            "p95": _percentile_or_none(wait_samples, 0.95),
            "p99": _percentile_or_none(wait_samples, 0.99),
            "max": max(wait_samples) if wait_samples else None,
        },
        "integrity_check": integrity,
        "elapsed_seconds": time.monotonic() - started,
    }


def _wait_for_json(path: Path, *, deadline: float) -> dict[str, Any]:
    while True:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = None
        if isinstance(value, dict):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"clock exchange file did not appear: {path}")
        time.sleep(0.001)


def _clock_offset_interval(
    *,
    local_sent_wall_ns: int,
    local_received_wall_ns: int,
    remote_received_wall_ns: int,
    remote_sent_wall_ns: int,
) -> tuple[int, int]:
    lower_ns = remote_sent_wall_ns - local_received_wall_ns
    upper_ns = remote_received_wall_ns - local_sent_wall_ns
    return lower_ns, upper_ns


def clock_exchange(
    exchange_dir: Path,
    *,
    role: str,
    writer_id: str,
    output_json: Path,
    rounds: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if role not in {"coordinator", "responder"}:
        raise ValueError(f"unknown clock exchange role: {role}")
    if rounds < 3:
        raise ValueError("clock exchange requires at least three rounds")
    exchange_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    hostname = os.uname().nodename
    observations: list[dict[str, Any]] = []
    if role == "responder":
        for round_index in range(rounds):
            request_path = exchange_dir / f"request_{round_index:04d}.json"
            response_path = exchange_dir / f"response_{round_index:04d}.json"
            request = _wait_for_json(request_path, deadline=deadline)
            remote_received_wall_ns = time.time_ns()
            remote_received_monotonic_ns = time.monotonic_ns()
            remote_sent_wall_ns = time.time_ns()
            remote_sent_monotonic_ns = time.monotonic_ns()
            _atomic_write_json(
                response_path,
                {
                    "round": round_index,
                    "request_nonce": request["request_nonce"],
                    "responder_hostname": hostname,
                    "remote_received_wall_ns": remote_received_wall_ns,
                    "remote_received_monotonic_ns": remote_received_monotonic_ns,
                    "remote_sent_wall_ns": remote_sent_wall_ns,
                    "remote_sent_monotonic_ns": remote_sent_monotonic_ns,
                },
            )
        payload = {
            "mode": "clock-exchange",
            "role": role,
            "writer_id": writer_id,
            "hostname": hostname,
            "pid": os.getpid(),
            "rounds_completed": rounds,
            "status": "PASS",
        }
    else:
        for round_index in range(rounds):
            request_path = exchange_dir / f"request_{round_index:04d}.json"
            response_path = exchange_dir / f"response_{round_index:04d}.json"
            request_nonce = f"{os.getpid()}-{round_index}-{time.monotonic_ns()}"
            local_sent_wall_ns = time.time_ns()
            local_sent_monotonic_ns = time.monotonic_ns()
            _atomic_write_json(
                request_path,
                {
                    "round": round_index,
                    "request_nonce": request_nonce,
                    "coordinator_hostname": hostname,
                    "local_sent_wall_ns": local_sent_wall_ns,
                    "local_sent_monotonic_ns": local_sent_monotonic_ns,
                },
            )
            response = _wait_for_json(response_path, deadline=deadline)
            local_received_wall_ns = time.time_ns()
            local_received_monotonic_ns = time.monotonic_ns()
            if response.get("request_nonce") != request_nonce:
                raise RuntimeError(f"clock response nonce mismatch at round {round_index}")
            lower_ns, upper_ns = _clock_offset_interval(
                local_sent_wall_ns=local_sent_wall_ns,
                local_received_wall_ns=local_received_wall_ns,
                remote_received_wall_ns=int(response["remote_received_wall_ns"]),
                remote_sent_wall_ns=int(response["remote_sent_wall_ns"]),
            )
            wall_elapsed_ns = local_received_wall_ns - local_sent_wall_ns
            monotonic_elapsed_ns = local_received_monotonic_ns - local_sent_monotonic_ns
            observations.append(
                {
                    "round": round_index,
                    "coordinator_hostname": hostname,
                    "responder_hostname": str(response["responder_hostname"]),
                    "local_sent_wall_ns": local_sent_wall_ns,
                    "local_received_wall_ns": local_received_wall_ns,
                    "remote_received_wall_ns": int(response["remote_received_wall_ns"]),
                    "remote_sent_wall_ns": int(response["remote_sent_wall_ns"]),
                    "round_trip_monotonic_seconds": monotonic_elapsed_ns / 1_000_000_000.0,
                    "wall_monotonic_elapsed_delta_seconds": abs(
                        wall_elapsed_ns - monotonic_elapsed_ns
                    )
                    / 1_000_000_000.0,
                    "offset_lower_bound_seconds": lower_ns / 1_000_000_000.0,
                    "offset_upper_bound_seconds": upper_ns / 1_000_000_000.0,
                    "interval_valid": lower_ns <= upper_ns,
                }
            )
        lower_bound = max(float(item["offset_lower_bound_seconds"]) for item in observations)
        upper_bound = min(float(item["offset_upper_bound_seconds"]) for item in observations)
        responder_hostnames = sorted(
            {str(item["responder_hostname"]) for item in observations}
        )
        intersection_valid = lower_bound <= upper_bound
        payload = {
            "mode": "clock-exchange",
            "method": "filesystem_two_way_offset_interval",
            "role": role,
            "writer_id": writer_id,
            "hostname": hostname,
            "pid": os.getpid(),
            "hostnames": sorted({hostname, *responder_hostnames}),
            "host_count": len({hostname, *responder_hostnames}),
            "rounds_requested": rounds,
            "rounds_completed": len(observations),
            "observations": observations,
            "intersection_valid": intersection_valid,
            "offset_interval_seconds": {
                "lower": lower_bound,
                "upper": upper_bound,
            },
            "absolute_offset_upper_bound_seconds": (
                max(abs(lower_bound), abs(upper_bound))
                if intersection_valid
                else None
            ),
            "maximum_round_trip_seconds": max(
                float(item["round_trip_monotonic_seconds"]) for item in observations
            ),
            "maximum_wall_monotonic_elapsed_delta_seconds": max(
                float(item["wall_monotonic_elapsed_delta_seconds"])
                for item in observations
            ),
            "wall_clock_resolution_seconds": time.get_clock_info("time").resolution,
            "monotonic_clock_resolution_seconds": time.get_clock_info("monotonic").resolution,
            "status": "PASS" if intersection_valid else "BLOCKED",
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
        "verify-readonly",
        "kill-reopen",
        "contend",
        "clock-exchange",
        "_kill-once",
    ):
        sub = subparsers.add_parser(mode)
        if mode != "clock-exchange":
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
        elif mode == "clock-exchange":
            sub.add_argument("--exchange-dir", type=Path, required=True)
            sub.add_argument("--role", choices=("coordinator", "responder"), required=True)
            sub.add_argument("--writer-id", required=True)
            sub.add_argument("--output-json", type=Path, required=True)
            sub.add_argument("--rounds", type=int, default=20)
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
    elif args.mode == "clock-exchange":
        summary = clock_exchange(
            args.exchange_dir,
            role=args.role,
            writer_id=args.writer_id,
            output_json=args.output_json,
            rounds=args.rounds,
            timeout_seconds=args.timeout_seconds,
        )
    elif args.mode == "kill-reopen":
        summary = kill_reopen(args.db, cycles=args.cycles, seed=args.seed)
    elif args.mode == "_kill-once":
        kill_once(args.db, cycle=args.cycle, phase=args.phase)
        return
    elif args.mode == "verify":
        conn = connect(args.db)
        try:
            summary = dict(verify(conn), mode="verify")
        finally:
            conn.close()
    else:
        conn = open_readonly(args.db)
        try:
            summary = dict(verify(conn), mode="verify-readonly")
        finally:
            conn.close()
    summary.setdefault("hostname", os.uname().nodename)
    summary.setdefault("pid", os.getpid())
    if args.mode == "contend" and args.output_json is not None:
        _atomic_write_json(args.output_json, summary)
    print(json.dumps(summary, sort_keys=True))
    if summary.get("starved") is True or summary.get("status") == "BLOCKED":
        raise SystemExit(4)


if __name__ == "__main__":
    main()
