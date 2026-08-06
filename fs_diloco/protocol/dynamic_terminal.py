"""Dynamic membership drain directive and acknowledgement helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ..core.constants import MEMBERSHIP_FORMAT_VERSION
from ..storage.atomic_io import atomic_write_json, safe_read_json, sha256_file
from ..storage.leader_lease import LeaderToken
from ..storage.paths import RunPaths


_EPOCH_DIR_RE = re.compile(r"^e(?P<epoch>[0-9]{6})_(?P<owner>[0-9a-f]{12})$")


def _digest(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_dynamic_close_request(
    paths: RunPaths,
    *,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
    reason: str = "manual",
    requested_at: float | None = None,
) -> dict[str, Any]:
    """Publish an identity-bound operator request to close dynamic admission."""

    if reason != "manual":
        raise ValueError("operator dynamic close reason must be 'manual'")
    payload: dict[str, Any] = {
        "format_version": MEMBERSHIP_FORMAT_VERSION,
        "run_id": run_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
        "reason": reason,
        "requested_at": time.time() if requested_at is None else float(requested_at),
    }
    payload["payload_sha256"] = _digest(payload)
    if not paths.control.is_dir():
        raise RuntimeError("dynamic close authority directory is missing")
    existing = safe_read_json(paths.dynamic_close_request_json)
    if isinstance(existing, dict):
        if existing.get("payload_sha256") != _digest(existing):
            raise RuntimeError("existing dynamic close request checksum mismatch")
        immutable = ("run_id", "source_fingerprint", "config_sha256", "reason")
        if any(existing.get(key) != payload.get(key) for key in immutable):
            raise RuntimeError("existing dynamic close request identity mismatch")
        return existing
    atomic_write_json(paths.dynamic_close_request_json, payload)
    return payload


def read_dynamic_close_request(
    paths: RunPaths,
    *,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
) -> dict[str, Any] | None:
    """Read a valid operator close request, failing closed on corruption."""

    if not paths.dynamic_close_request_json.exists():
        return None
    payload = safe_read_json(paths.dynamic_close_request_json)
    if not isinstance(payload, dict):
        raise RuntimeError("dynamic close request is not valid JSON")
    expected = {
        "format_version": MEMBERSHIP_FORMAT_VERSION,
        "run_id": run_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
        "reason": "manual",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("dynamic close request identity mismatch")
    if payload.get("payload_sha256") != _digest(payload):
        raise RuntimeError("dynamic close request checksum mismatch")
    try:
        float(payload["requested_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("dynamic close request timestamp is invalid") from exc
    return payload


class DynamicTerminalPublisher:
    def __init__(self, paths: RunPaths, store: Any, token: LeaderToken) -> None:
        self.paths = paths
        self.store = store
        self.token = token

    def publish_drain(self, controller: dict[str, Any]) -> Path:
        if str(controller["state"]) not in {"draining", "closed"}:
            raise ValueError("drain publication requires a draining/closed controller")
        generation = int(controller["generation"])
        payload: dict[str, Any] = {
            "format_version": MEMBERSHIP_FORMAT_VERSION,
            "run_id": self.token.run_id,
            "close_generation": generation,
            "reason": controller.get("reason"),
            "requested_at": controller.get("requested_at"),
            "max_terminal_version": int(controller["max_terminal_version"]),
            "admission_state": str(controller["state"]),
            "published_by_epoch": self.token.epoch,
            "published_by_owner_id": self.token.owner_id,
            "published_at": time.time(),
        }
        payload["payload_sha256"] = _digest(payload)
        path = self.paths.epoch_drain_path(
            self.token.epoch,
            self.token.owner_id,
            generation,
        )
        atomic_write_json(path, payload)
        self.store.record_control_publication(
            self.token,
            kind="drain",
            logical_generation=generation,
            relative_path=self.paths.relative(path),
            sha256=sha256_file(path),
        )
        return path


def read_current_drain(paths: RunPaths, *, run_id: str) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    if not paths.syncer_epochs.is_dir():
        return None
    for directory in paths.syncer_epochs.iterdir():
        if not directory.is_dir():
            continue
        match = _EPOCH_DIR_RE.fullmatch(directory.name)
        if match is None:
            continue
        terminal = directory / "terminal"
        if not terminal.is_dir():
            continue
        for path in terminal.glob("drain_g*.json"):
            payload = safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            if payload.get("format_version") != MEMBERSHIP_FORMAT_VERSION:
                continue
            if payload.get("run_id") != run_id or payload.get("payload_sha256") != _digest(
                payload
            ):
                continue
            epoch = int(match.group("epoch"))
            owner_id = str(payload.get("published_by_owner_id", ""))
            if (
                int(payload.get("published_by_epoch", -1)) != epoch
                or not owner_id
                or paths.owner_short(owner_id) != match.group("owner")
            ):
                continue
            candidates.append((epoch, int(payload["close_generation"]), payload))
    return None if not candidates else max(candidates, key=lambda item: item[:2])[2]
