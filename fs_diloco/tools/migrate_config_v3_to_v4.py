"""Safely migrate one full v1-v3 configuration to strict Full Protocol v4."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..core.config_v4 import migrate_v3_bytes_to_v4


def _write_create_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), 0o644)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        _fsync_directory(path.parent)
    except Exception:
        if linked:
            try:
                target = path.lstat()
                staged = temporary.lstat()
                if (target.st_dev, target.st_ino) == (staged.st_dev, staged.st_ino):
                    path.unlink()
                    _fsync_directory(path.parent)
            except OSError:
                pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(
    path: Path,
    data: bytes,
    *,
    mode: int,
    before_replace: Callable[[], None] | None = None,
) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_descriptor_bytes(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _open_locked_source(path: Path) -> tuple[int, int, bytes, os.stat_result]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.migrate.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        source_descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception:
        os.close(lock_descriptor)
        raise
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("migration input must be a regular file")
        named = path.lstat()
        if (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError("input changed while opening migration source")
        return (
            lock_descriptor,
            source_descriptor,
            _read_descriptor_bytes(source_descriptor),
            metadata,
        )
    except Exception:
        os.close(source_descriptor)
        os.close(lock_descriptor)
        raise


def migrate(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
    in_place: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    source = Path(input_path)
    if in_place and output_path is not None:
        raise ValueError("--in-place and --output are mutually exclusive")
    if in_place != (expected_sha256 is not None):
        raise ValueError("--in-place and --expected-sha256 must be provided together")
    destination: Path | None = None
    if in_place:
        lock_descriptor, source_descriptor, original, metadata = _open_locked_source(source)
        try:
            migrated, report = migrate_v3_bytes_to_v4(original)
            if report["input_sha256"] != expected_sha256:
                raise RuntimeError("input changed: --expected-sha256 does not match")

            def revalidate_source() -> None:
                try:
                    named = source.lstat()
                except OSError as exc:
                    raise RuntimeError("input changed before migration publication") from exc
                current = os.fstat(source_descriptor)
                if (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino) or (
                    current.st_dev,
                    current.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise RuntimeError("input changed before migration publication")
                if hashlib.sha256(_read_descriptor_bytes(source_descriptor)).hexdigest() != str(
                    expected_sha256
                ):
                    raise RuntimeError("input changed before migration publication")

            destination = source
            _atomic_replace(
                destination,
                migrated,
                mode=stat.S_IMODE(metadata.st_mode),
                before_replace=revalidate_source,
            )
        finally:
            os.close(source_descriptor)
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
    else:
        original = source.read_bytes()
        migrated, report = migrate_v3_bytes_to_v4(original)
        if output_path is not None:
            destination = Path(output_path)
            _write_create_new(destination, migrated)
    return {
        "input": str(source),
        "output": None if destination is None else str(destination),
        "write_mode": "in_place" if in_place else ("output" if destination else "dry_run"),
        **report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output")
    destination.add_argument("--in-place", action="store_true")
    parser.add_argument("--expected-sha256")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    report = migrate(
        args.config,
        output_path=args.output,
        in_place=args.in_place,
        expected_sha256=args.expected_sha256,
    )
    json.dump(report, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
