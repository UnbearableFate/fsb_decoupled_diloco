"""Atomic filesystem helpers.

All publications use write-to-temp-then-rename in the target directory. This is
the only operation learners and the syncer rely on for shared-state visibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: str | Path, data: bytes, mode: int = 0o644) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise
    return path


def atomic_write_text(path: str | Path, text: str, mode: int = 0o644) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: str | Path, payload: dict[str, Any], mode: int = 0o644) -> Path:
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    return atomic_write_text(path, text, mode=mode)


def atomic_write_with_writer(path: str | Path, writer: Callable[[Path], None], mode: int = 0o644) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        writer(tmp_path)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise
    return path


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_read_json(path: str | Path) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_size(path: str | Path) -> int:
    return Path(path).stat().st_size


def wait_for_file(path: str | Path, timeout_seconds: float, poll_seconds: float = 1.0) -> Path:
    import time

    path = Path(path)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if path.exists():
            return path
        time.sleep(poll_seconds)
    raise TimeoutError(f"timed out waiting for {path}")
