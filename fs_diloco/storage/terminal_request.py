"""Immutable filesystem boundary for an operator-requested terminal close."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from .atomic_io import publish_immutable_bytes, safe_read_json
from .paths import RunPaths


MANUAL_TERMINAL_REQUEST_FORMAT_VERSION = 1


def publish_manual_terminal_request(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    reason: str,
    created_at: float | None = None,
) -> dict[str, Any]:
    if not reason or len(reason) > 256:
        raise ValueError("manual terminal reason must contain 1..256 characters")
    timestamp = float(time.time() if created_at is None else created_at)
    if not math.isfinite(timestamp):
        raise ValueError("manual terminal request timestamp must be finite")
    body = {
        "format_version": MANUAL_TERMINAL_REQUEST_FORMAT_VERSION,
        "kind": "manual_terminal_close",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "reason": reason,
        "created_at": timestamp,
    }
    request_id = (
        "terminal-"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
    )
    payload = {**body, "request_id": request_id}
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    publish_immutable_bytes(paths.dynamic_close_request_json, encoded)
    return payload


def read_manual_terminal_request(
    paths: RunPaths, *, run_id: str, descriptor_sha256: str
) -> dict[str, Any] | None:
    payload = safe_read_json(paths.dynamic_close_request_json)
    if payload is None:
        return None
    required = {
        "format_version",
        "kind",
        "run_id",
        "descriptor_sha256",
        "reason",
        "created_at",
        "request_id",
    }
    created_at = payload.get("created_at")
    reason = payload.get("reason")
    if (
        set(payload) != required
        or payload.get("format_version") != MANUAL_TERMINAL_REQUEST_FORMAT_VERSION
        or payload.get("kind") != "manual_terminal_close"
        or payload.get("run_id") != run_id
        or payload.get("descriptor_sha256") != descriptor_sha256
        or not isinstance(reason, str)
        or not reason
        or len(reason) > 256
        or isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(float(created_at))
    ):
        return None
    body = {key: payload[key] for key in required if key != "request_id"}
    expected_id = (
        "terminal-"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
    )
    if payload["request_id"] != expected_id:
        return None
    return payload
