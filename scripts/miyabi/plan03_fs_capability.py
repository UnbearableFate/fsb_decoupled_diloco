#!/usr/bin/env python3
"""Probe the exact POSIX/SQLite primitives required by FullProtocolV4."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


AT_FDCWD = -100
RENAME_NOREPLACE = 1


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "libc does not expose renameat2")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), target)


def _probe_create_no_replace(root: Path) -> dict[str, Any]:
    staging = root / "object.staging"
    final = root / "object.final"
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"plan03-object")
        handle.flush()
        os.fsync(handle.fileno())
    os.link(staging, final)
    collision_errno = None
    try:
        os.link(staging, final)
    except OSError as exc:
        collision_errno = exc.errno
    if collision_errno != errno.EEXIST or final.read_bytes() != b"plan03-object":
        raise RuntimeError(f"hard-link no-replace failed: errno={collision_errno}")
    return {
        "status": "PASS",
        "collision_errno": collision_errno,
        "same_inode": staging.stat().st_ino == final.stat().st_ino,
    }


def _probe_directory_no_replace(root: Path) -> dict[str, Any]:
    source = root / "run.staging"
    source.mkdir()
    try:
        _rename_noreplace(source, root / "run.final")
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            return {
                "status": "UNSUPPORTED",
                "errno": exc.errno,
                "error": str(exc),
                "fallback_required": True,
            }
        raise
    collision = root / "collision.staging"
    collision.mkdir()
    collision_errno = None
    try:
        _rename_noreplace(collision, root / "run.final")
    except OSError as exc:
        collision_errno = exc.errno
    if collision_errno != errno.EEXIST or not collision.is_dir():
        raise RuntimeError(f"directory no-replace failed: errno={collision_errno}")
    return {
        "status": "PASS",
        "collision_errno": collision_errno,
        "source_preserved_on_collision": True,
    }


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fallback_publish(
    staging: Path,
    final: Path,
    *,
    identity: bytes,
    link_limit: int | None = None,
) -> bool:
    try:
        final.mkdir()
        _write_exclusive(final / ".identity", identity)
        _fsync_dir(final)
        _fsync_dir(final.parent)
    except FileExistsError:
        if (final / ".identity").read_bytes() != identity:
            raise RuntimeError("final reservation identity collision")
    object_names = ("descriptor.json", "authority.sqlite", "bootstrap.json")
    for index, name in enumerate(object_names):
        if link_limit is not None and index >= link_limit:
            return False
        try:
            os.link(staging / name, final / name)
        except FileExistsError:
            if (staging / name).read_bytes() != (final / name).read_bytes():
                raise RuntimeError(f"reserved final object collision: {name}")
        _fsync_dir(final)
    if link_limit is not None and link_limit < len(object_names) + 1:
        return False
    try:
        os.link(staging / ".complete", final / ".complete")
    except FileExistsError:
        if (staging / ".complete").read_bytes() != (final / ".complete").read_bytes():
            raise RuntimeError("completion marker collision")
    _fsync_dir(final)
    _fsync_dir(final.parent)
    return True


def _fallback_visible(final: Path, *, identity: bytes) -> bool:
    marker = final / ".complete"
    if not marker.is_file():
        return False
    return (
        (final / ".identity").read_bytes() == identity
        and marker.read_bytes() == b"plan03-complete-manifest"
        and all(
            (final / name).is_file()
            for name in ("descriptor.json", "authority.sqlite", "bootstrap.json")
        )
    )


def _probe_directory_publish_fallback(root: Path) -> dict[str, Any]:
    staging = root / "fallback.staging"
    staging.mkdir()
    for name, payload in {
        "descriptor.json": b"descriptor",
        "authority.sqlite": b"sqlite",
        "bootstrap.json": b"bootstrap",
        ".complete": b"plan03-complete-manifest",
    }.items():
        _write_exclusive(staging / name, payload)
    _fsync_dir(staging)
    identity = b"plan03-identity-a"
    crash_prefixes = []
    for prefix in range(4):
        final = root / f"fallback-crash-{prefix}"
        completed = _fallback_publish(staging, final, identity=identity, link_limit=prefix)
        visible_before_retry = _fallback_visible(final, identity=identity)
        if completed or visible_before_retry:
            raise RuntimeError(f"crash prefix {prefix} became visible")
        retried = _fallback_publish(staging, final, identity=identity)
        visible_after_retry = _fallback_visible(final, identity=identity)
        if not retried or not visible_after_retry:
            raise RuntimeError(f"crash prefix {prefix} did not recover")
        crash_prefixes.append(
            {
                "prefix": prefix,
                "visible_before_retry": visible_before_retry,
                "visible_after_retry": visible_after_retry,
            }
        )
    collision = root / "fallback-collision"
    collision.mkdir()
    _write_exclusive(collision / ".identity", b"plan03-identity-b")
    collision_error = None
    try:
        _fallback_publish(staging, collision, identity=identity)
    except RuntimeError as exc:
        collision_error = str(exc)
    if (
        collision_error != "final reservation identity collision"
        or (collision / ".complete").exists()
    ):
        raise RuntimeError(f"identity collision was not preserved: {collision_error}")
    complete = root / "fallback-complete"
    if not _fallback_publish(staging, complete, identity=identity):
        raise RuntimeError("complete fallback publication returned false")
    second_identity_error = None
    try:
        _fallback_publish(staging, complete, identity=b"plan03-identity-b")
    except RuntimeError as exc:
        second_identity_error = str(exc)
    if second_identity_error != "final reservation identity collision":
        raise RuntimeError("completed root accepted a different identity")
    return {
        "status": "PASS",
        "protocol": "exclusive-mkdir+identity-reservation+hardlink-objects+complete-marker",
        "crash_prefixes": crash_prefixes,
        "same_identity_retry": True,
        "different_identity_collision": "fail_closed",
        "completed_root_overwrite": "fail_closed",
    }


def _probe_dir_fd_nofollow(root: Path) -> dict[str, Any]:
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(
            "dirfd-object",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.write(descriptor, b"dirfd")
        os.fsync(descriptor)
        os.close(descriptor)
        os.symlink("dirfd-object", root / "dirfd-symlink")
        symlink_errno = None
        try:
            os.open("dirfd-symlink", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as exc:
            symlink_errno = exc.errno
        if symlink_errno != errno.ELOOP:
            raise RuntimeError(f"O_NOFOLLOW did not reject symlink: errno={symlink_errno}")
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"status": "PASS", "symlink_errno": symlink_errno, "parent_directory_fsync": True}


def _probe_sqlite_delete_lock(root: Path) -> dict[str, Any]:
    path = root / "authority.sqlite"
    first = sqlite3.connect(path, timeout=1.0)
    second = sqlite3.connect(path, timeout=0.05)
    try:
        journal = str(first.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        first.execute("PRAGMA synchronous=FULL")
        first.execute("CREATE TABLE authority(value INTEGER NOT NULL)")
        first.commit()
        first.execute("BEGIN IMMEDIATE")
        first.execute("INSERT INTO authority(value) VALUES (1)")
        started = time.monotonic()
        lock_errno = None
        lock_message = None
        try:
            second.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            lock_message = str(exc)
            lock_errno = getattr(exc, "sqlite_errorcode", None)
        waited = time.monotonic() - started
        if lock_message is None or "locked" not in lock_message.lower():
            raise RuntimeError(f"second writer did not observe lock: {lock_message}")
        first.rollback()
        second.execute("BEGIN IMMEDIATE")
        second.execute("INSERT INTO authority(value) VALUES (2)")
        second.commit()
        integrity = [str(row[0]) for row in second.execute("PRAGMA integrity_check")]
        if journal != "delete" or integrity != ["ok"]:
            raise RuntimeError(f"unsafe SQLite result: journal={journal}, integrity={integrity}")
    finally:
        first.close()
        second.close()
    return {
        "status": "PASS",
        "journal_mode": journal,
        "contended_error_code": lock_errno,
        "contended_error": lock_message,
        "waited_seconds": waited,
        "success_after_release": True,
        "integrity_check": integrity,
    }


def probe(shared_parent: Path) -> dict[str, Any]:
    shared_parent = shared_parent.resolve()
    if not shared_parent.is_dir():
        raise FileNotFoundError(shared_parent)
    temporary = Path(tempfile.mkdtemp(prefix=".plan03-fs-capability-", dir=shared_parent))
    try:
        results = {
            "hardlink_create_no_replace": _probe_create_no_replace(temporary),
            "directory_rename_no_replace": _probe_directory_no_replace(temporary),
            "directory_publish_fallback": _probe_directory_publish_fallback(temporary),
            "dir_fd_openat_nofollow_and_parent_fsync": _probe_dir_fd_nofollow(temporary),
            "sqlite_delete_journal_lock": _probe_sqlite_delete_lock(temporary),
        }
        acceptable = {"PASS", "UNSUPPORTED"}
        return {
            "artifact_version": 1,
            "experiment_id": "p0-shared-fs-capability",
            "status": "PASS"
            if all(item["status"] in acceptable for item in results.values())
            and results["directory_publish_fallback"]["status"] == "PASS"
            else "FAIL",
            "host": os.uname().nodename,
            "pbs_job_id": os.environ.get("PBS_JOBID"),
            "shared_parent": str(shared_parent),
            "temporary_path_removed": True,
            "results": results,
        }
    finally:
        shutil.rmtree(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared-parent", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.shared_parent), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
