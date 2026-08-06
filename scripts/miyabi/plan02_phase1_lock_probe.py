#!/usr/bin/env python3
"""Cross-node Phase 1 HA SIGSTOP and writer-lock boundary probe."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fs_diloco.storage.atomic_io import atomic_write_json, safe_read_json
from fs_diloco.storage.fenced_store import FencedSQLiteStore
from fs_diloco.storage.leader_lease import (
    LeaderLeaseStore,
    LeaderToken,
    LeaseSafetyTracker,
    StaleLeaderTokenError,
)
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.schema_bootstrap import BootstrapIdentity, initialize_new_run


LEASE_SECONDS = 1.0


def _identity(run_id: str) -> BootstrapIdentity:
    return BootstrapIdentity(
        run_id=run_id,
        source_fingerprint="sha256:plan02-phase1-lock-probe",
        config_sha256="plan02-phase1-lock-probe",
        mode="full",
    )


def _lease(paths: RunPaths, run_id: str, *, busy_timeout_ms: int = 5000) -> LeaderLeaseStore:
    return LeaderLeaseStore(
        paths.sqlite_db,
        _identity(run_id),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=LEASE_SECONDS,
        max_clock_skew_seconds=0.0,
        busy_timeout_ms=busy_timeout_ms,
    )


def _fenced(
    paths: RunPaths,
    run_id: str,
    token: LeaderToken,
    *,
    busy_timeout_ms: int = 5000,
) -> FencedSQLiteStore:
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=LEASE_SECONDS,
        max_clock_skew_seconds=0.0,
    )
    return FencedSQLiteStore(
        paths.sqlite_db,
        _identity(run_id),
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=0.0,
        busy_timeout_ms=busy_timeout_ms,
        lease_safety_check=tracker.assert_safe,
    )


def _wait(path: Path, description: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = safe_read_json(path)
        if payload is not None:
            return payload
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {description}: {path}")


def _bootstrap(case_root: Path, run_id: str) -> RunPaths:
    paths = RunPaths(case_root)
    prepare_authority_dirs(paths)
    initialize_new_run(
        paths.sqlite_db,
        _identity(run_id),
        marker_path=paths.bootstrap_complete_json,
    )
    return paths


def _outside_child(case_root: Path, run_id: str) -> None:
    paths = RunPaths(case_root)
    lease = _lease(paths, run_id)
    token = lease.acquire(owner_id="outside-old", hostname=os.uname().nodename, pid=os.getpid())
    store = _fenced(paths, run_id, token).bind(token)
    store.set_run_state("outside-before-stop", True)
    atomic_write_json(
        case_root / "old_ready.json",
        {"pid": os.getpid(), "epoch": token.epoch, "acquired_at": time.time()},
    )
    os.kill(os.getpid(), signal.SIGSTOP)
    try:
        store.set_run_state("outside-stale-write", True)
    except StaleLeaderTokenError as exc:
        result = {"stale_write_rejected": True, "error": str(exc)}
    else:
        result = {"stale_write_rejected": False}
    atomic_write_json(case_root / "old_resumed.json", result)
    store.close()
    lease.close()


def _inside_child(case_root: Path, run_id: str) -> None:
    paths = RunPaths(case_root)
    lease = _lease(paths, run_id)
    token = lease.acquire(owner_id="inside-old", hostname=os.uname().nodename, pid=os.getpid())
    store = _fenced(paths, run_id, token)

    def operation(conn: Any) -> None:
        conn.execute(
            "INSERT INTO run_state(key, value, updated_at) VALUES (?, ?, ?)",
            ("inside-tentative", json.dumps(True), time.time()),
        )
        atomic_write_json(
            case_root / "old_ready.json",
            {"pid": os.getpid(), "epoch": token.epoch, "acquired_at": time.time()},
        )
        os.kill(os.getpid(), signal.SIGSTOP)

    store._transaction(token, operation)
    raise RuntimeError("inside-lock child unexpectedly resumed and committed")


def _holder(case_root: Path, run_id: str, kind: str) -> dict[str, Any]:
    paths = _bootstrap(case_root, run_id)
    child_mode = f"_{kind}_child"
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            child_mode,
            "--case-root",
            str(case_root),
            "--run-id",
            run_id,
        ]
    )
    ready = _wait(case_root / "old_ready.json", f"{kind} stopped owner")
    atomic_write_json(
        case_root / "holder_ready.json",
        {**ready, "holder_host": os.uname().nodename},
    )
    request = _wait(case_root / "holder_action.json", f"{kind} holder action")
    action = str(request["action"])
    if action == "continue":
        os.kill(child.pid, signal.SIGCONT)
    elif action == "kill":
        os.kill(child.pid, signal.SIGKILL)
    else:
        raise RuntimeError(f"unsupported holder action: {action}")
    returncode = child.wait(timeout=15.0)
    result = {
        "role": "holder",
        "kind": kind,
        "hostname": os.uname().nodename,
        "child_returncode": returncode,
        "action": action,
        "sqlite_path": str(paths.sqlite_db),
    }
    atomic_write_json(case_root / "holder_done.json", result)
    return result


def _contender(case_root: Path, run_id: str, kind: str) -> dict[str, Any]:
    holder = _wait(case_root / "holder_ready.json", f"{kind} holder readiness")
    if holder["holder_host"] == os.uname().nodename:
        raise RuntimeError("holder and contender must run on different nodes")
    sleep_for = float(holder["acquired_at"]) + LEASE_SECONDS + 0.10 - time.time()
    if sleep_for > 0:
        time.sleep(sleep_for)
    paths = RunPaths(case_root)
    blocked_seconds = 0.0
    blocked_error = None
    if kind == "inside":
        started = time.monotonic()
        contender = _lease(paths, run_id, busy_timeout_ms=250)
        try:
            contender.acquire(
                owner_id="inside-contender-before-kill",
                hostname=os.uname().nodename,
                pid=os.getpid(),
            )
        except sqlite3.OperationalError as exc:
            blocked_error = str(exc)
        else:
            raise RuntimeError("contender acquired while old writer held BEGIN IMMEDIATE")
        finally:
            contender.close()
        blocked_seconds = time.monotonic() - started
        if not blocked_error or not any(
            word in blocked_error.lower() for word in ("locked", "busy")
        ):
            raise RuntimeError(f"unexpected writer-lock error: {blocked_error!r}")
        action = "kill"
    else:
        action = "continue"

    successor = _lease(paths, run_id)
    if kind == "inside":
        atomic_write_json(case_root / "holder_action.json", {"action": action})
        holder_done = _wait(case_root / "holder_done.json", "inside holder kill")
        if int(holder_done["child_returncode"]) != -signal.SIGKILL:
            raise RuntimeError(f"inside holder was not killed: {holder_done}")
    token = successor.acquire(
        owner_id=f"{kind}-successor",
        hostname=os.uname().nodename,
        pid=os.getpid(),
    )
    if token.epoch != 2:
        raise RuntimeError(f"successor epoch was {token.epoch}, expected 2")
    store = _fenced(paths, run_id, token).bind(token)
    store.set_run_state(f"{kind}-successor-write", True)
    if kind == "outside":
        atomic_write_json(case_root / "holder_action.json", {"action": action})
        resumed = _wait(case_root / "old_resumed.json", "outside stale write result")
        if not resumed.get("stale_write_rejected"):
            raise RuntimeError(f"outside stale writer was not fenced: {resumed}")
        _wait(case_root / "holder_done.json", "outside holder completion")
    tentative = store.get_run_state("inside-tentative")
    if kind == "inside" and tentative is not None:
        raise RuntimeError("killed writer's tentative transaction was visible")
    integrity = store.integrity_check()
    store.close()
    successor.release(token)
    successor.close()
    return {
        "role": "contender",
        "kind": kind,
        "hostname": os.uname().nodename,
        "successor_epoch": token.epoch,
        "blocked_seconds": blocked_seconds,
        "blocked_error": blocked_error,
        "integrity_check": integrity,
        "tentative_value": tentative,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("holder", "contender", "_outside_child", "_inside_child"),
    )
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kind", choices=("outside", "inside"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "_outside_child":
        _outside_child(args.case_root, args.run_id)
        return
    if args.mode == "_inside_child":
        _inside_child(args.case_root, args.run_id)
        return
    if args.kind is None or args.output is None:
        raise SystemExit("--kind and --output are required for holder/contender")
    if args.mode == "holder":
        result = _holder(args.case_root, args.run_id, args.kind)
    else:
        result = _contender(args.case_root, args.run_id, args.kind)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
