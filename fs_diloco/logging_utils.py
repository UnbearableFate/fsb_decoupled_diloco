"""JSONL process logging."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .atomic_io import ensure_dir


class JsonlLogger:
    def __init__(self, path: str | Path, actor: str, mirror_stdout: bool = True) -> None:
        self.path = Path(path)
        self.actor = actor
        self.mirror_stdout = mirror_stdout
        ensure_dir(self.path.parent)

    def event(self, event_type: str, **payload: Any) -> None:
        row = {
            "timestamp": time.time(),
            "actor": self.actor,
            "event_type": event_type,
            "hostname": socket.gethostname(),
            **payload,
        }
        text = json.dumps(row, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.mirror_stdout:
            print(text, flush=True)

    def exception(self, event_type: str = "error", **payload: Any) -> None:
        payload.setdefault("exc_info", traceback.format_exc())
        self.event(event_type, **payload)


def log_uncaught_exception(logger: JsonlLogger) -> None:
    def _hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        logger.event(
            "error",
            exc_type=exc_type.__name__,
            message=str(exc),
            traceback="".join(traceback.format_exception(exc_type, exc, tb)),
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook
