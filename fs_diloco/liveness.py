"""Heartbeat ingestion and learner liveness transitions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .atomic_io import safe_read_json
from .constants import (
    FORMAT_VERSION,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_DEAD,
    LEARNER_STATUS_STOPPED,
    LEARNER_STATUS_STALE,
    learner_id_from_index,
)
from .sqlite_store import SQLiteStore


def valid_learner_ids(num_learners: int) -> set[str]:
    return {learner_id_from_index(i) for i in range(num_learners)}


def validate_heartbeat(
    payload: dict[str, Any],
    *,
    run_id: str,
    num_learners: int,
) -> tuple[bool, str | None]:
    if payload.get("format_version") != FORMAT_VERSION:
        return False, "format_version"
    if payload.get("run_id") != run_id:
        return False, "run_id"
    if payload.get("learner_id") not in valid_learner_ids(num_learners):
        return False, "learner_id"
    if "timestamp" not in payload:
        return False, "timestamp"
    return True, None


def ingest_heartbeats(
    store: SQLiteStore,
    heartbeat_dir: str | Path,
    *,
    run_id: str,
    num_learners: int,
) -> int:
    heartbeat_dir = Path(heartbeat_dir)
    count = 0
    if not heartbeat_dir.exists():
        return 0
    for path in sorted(heartbeat_dir.glob("learner_*.json")):
        payload = safe_read_json(path)
        if payload is None:
            continue
        ok, _reason = validate_heartbeat(payload, run_id=run_id, num_learners=num_learners)
        if not ok:
            continue
        status = payload.get("status") or LEARNER_STATUS_ACTIVE
        store.upsert_learner(
            payload["learner_id"],
            hostname=payload.get("hostname"),
            pid=payload.get("pid"),
            last_seen=float(payload["timestamp"]),
            last_loaded_global_version=payload.get("last_loaded_global_version"),
            last_local_step=payload.get("last_local_step"),
            last_update_id=payload.get("last_update_id"),
            tokens_per_sec=payload.get("tokens_per_sec"),
            last_heartbeat_path=str(path),
            status=status,
            status_reason=payload.get("phase"),
        )
        count += 1
    return count


def classify_liveness(
    *,
    now: float,
    last_seen: float | None,
    current_status: str,
    stale_after_seconds: float,
    dead_after_seconds: float,
) -> tuple[str, str | None]:
    if current_status == LEARNER_STATUS_STOPPED:
        return LEARNER_STATUS_STOPPED, "stopped"
    if last_seen is None:
        return LEARNER_STATUS_DEAD, "never_seen"
    age = now - last_seen
    if age <= stale_after_seconds:
        return LEARNER_STATUS_ACTIVE, None
    if age <= dead_after_seconds:
        return LEARNER_STATUS_STALE, f"heartbeat_age={age:.1f}s"
    return LEARNER_STATUS_DEAD, f"heartbeat_age={age:.1f}s"


def update_liveness_statuses(
    store: SQLiteStore,
    *,
    stale_after_seconds: float,
    dead_after_seconds: float,
    now: float | None = None,
) -> dict[str, int]:
    now = time.time() if now is None else now
    counts = {
        LEARNER_STATUS_ACTIVE: 0,
        LEARNER_STATUS_STALE: 0,
        LEARNER_STATUS_DEAD: 0,
        LEARNER_STATUS_STOPPED: 0,
    }
    for learner in store.list_learners():
        status, reason = classify_liveness(
            now=now,
            last_seen=learner.get("last_seen"),
            current_status=learner.get("status") or LEARNER_STATUS_DEAD,
            stale_after_seconds=stale_after_seconds,
            dead_after_seconds=dead_after_seconds,
        )
        store.update_learner_status(learner["learner_id"], status, reason)
        counts[status] = counts.get(status, 0) + 1
    return counts


def no_progress_timed_out(last_progress_time: float, timeout_seconds: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - last_progress_time > timeout_seconds
