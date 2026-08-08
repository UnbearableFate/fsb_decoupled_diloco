"""Atomic filesystem helpers.

All publications use write-to-temp-then-rename in the target directory. This is
the only operation learners and the syncer rely on for shared-state visibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ImmutablePublication:
    path: Path
    size_bytes: int
    sha256: str
    created: bool


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        _fsync_directory(path.parent)
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


def atomic_write_with_writer(
    path: str | Path, writer: Callable[[Path], None], mode: int = 0o644
) -> Path:
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
        _fsync_directory(path.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise
    return path


def publish_immutable_with_writer(
    path: str | Path,
    writer: Callable[[Path], None],
    *,
    mode: int = 0o444,
    chunk_size: int = 1024 * 1024,
) -> ImmutablePublication:
    """Publish a same-directory immutable object without ever replacing its name.

    An exact existing object is an idempotent replay.  Any different object at
    the destination is an identity collision and fails closed.
    """

    if mode & 0o222:
        raise ValueError("immutable publication mode must not contain write bits")
    target = Path(path)
    ensure_dir(target.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".immutable", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        os.chmod(temporary, mode)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        size = temporary.stat().st_size
        digest = sha256_file(temporary, chunk_size=chunk_size)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            existing_fd = os.open(
                target,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                metadata = os.fstat(existing_fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != size
                    or metadata.st_mode & 0o222
                ):
                    raise FileExistsError(f"immutable target collision: {target}")
                existing_digest = hashlib.sha256()
                offset = 0
                while offset < size:
                    chunk = os.pread(existing_fd, min(chunk_size, size - offset), offset)
                    if not chunk:
                        raise FileExistsError(f"immutable target collision: {target}")
                    existing_digest.update(chunk)
                    offset += len(chunk)
                named = target.lstat()
                final = os.fstat(existing_fd)
                if existing_digest.hexdigest() != digest or (named.st_dev, named.st_ino) != (
                    final.st_dev,
                    final.st_ino,
                ):
                    raise FileExistsError(f"immutable target collision: {target}")
            finally:
                os.close(existing_fd)
            return ImmutablePublication(target, size, digest, False)
        _fsync_directory(target.parent)
        return ImmutablePublication(target, size, digest, True)
    finally:
        temporary.unlink(missing_ok=True)


def publish_immutable_bytes(
    path: str | Path, data: bytes, *, mode: int = 0o444
) -> ImmutablePublication:
    def writer(temporary: Path) -> None:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    return publish_immutable_with_writer(path, writer, mode=mode)


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
