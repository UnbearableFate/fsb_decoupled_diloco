"""JSONL process logging."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from ..storage.atomic_io import ensure_dir
from ..storage.atomic_io import fsync_directory


class ActorTelemetryWriter:
    """Single-writer JSONL telemetry for one immutable actor attempt identity."""

    def __init__(self, path: str | Path, *, actor_kind: str, actor_id: str, attempt_id: str):
        self.path = Path(path)
        self.actor_kind = actor_kind
        self.actor_id = actor_id
        self.attempt_id = attempt_id
        ensure_dir(self.path.parent)
        self._claim_path = self.path.with_suffix(self.path.suffix + ".writer-claim")
        descriptor = os.open(self._claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        try:
            claim = json.dumps(
                {
                    "actor_kind": actor_kind,
                    "actor_id": actor_id,
                    "attempt_id": attempt_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                },
                sort_keys=True,
            ).encode("utf-8")
            os.write(descriptor, claim + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self._claim_path.parent)

    def event(self, event_type: str, **payload: Any) -> None:
        reserved = {"timestamp", "actor_kind", "actor_id", "attempt_id", "event_type"}
        collision = sorted(reserved & set(payload))
        if collision:
            raise ValueError(f"telemetry payload overrides reserved identity fields: {collision}")
        row = {
            "timestamp": time.time(),
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "attempt_id": self.attempt_id,
            "event_type": event_type,
            **payload,
        }
        existed = self.path.exists()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            fsync_directory(self.path.parent)
