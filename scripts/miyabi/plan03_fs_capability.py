#!/usr/bin/env python3
"""Probe the exact POSIX/SQLite primitives required by FullProtocolV4."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


AT_FDCWD = -100
RENAME_NOREPLACE = 1
FALLBACK_OBJECT_NAMES = ("descriptor.json", "authority.sqlite", "bootstrap.json")
FALLBACK_STEPS = (
    "link_parent_reservation",
    "fsync_parent_reservation",
    "mkdir_final",
    "fsync_parent_final",
    "link_final_identity",
    "fsync_final_identity",
    "link_descriptor.json",
    "fsync_descriptor.json",
    "link_authority.sqlite",
    "fsync_authority.sqlite",
    "link_bootstrap.json",
    "fsync_bootstrap.json",
    "link_complete_marker",
    "fsync_final_complete",
    "fsync_parent_complete",
)


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


def _read_regular_nofollow(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"not a regular file: {path}")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256_regular_nofollow(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"not a regular file: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _reservation_path(final: Path) -> Path:
    return final.parent / f".{final.name}.identity-reservation"


def _same_regular_inode(first: Path, second: Path) -> bool:
    try:
        first_stat = os.stat(first, follow_symlinks=False)
        second_stat = os.stat(second, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(first_stat.st_mode)
        and stat.S_ISREG(second_stat.st_mode)
        and (first_stat.st_dev, first_stat.st_ino) == (second_stat.st_dev, second_stat.st_ino)
    )


def _link_verified(source: Path, target: Path, *, expected_sha256: str) -> bool:
    if _sha256_regular_nofollow(source) != expected_sha256:
        raise RuntimeError(f"staged object digest mismatch: {source.name}")
    try:
        os.link(source, target, follow_symlinks=False)
        return True
    except FileExistsError:
        try:
            target_sha256 = _sha256_regular_nofollow(target)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"publication collision is not a regular object: {target}") from exc
        if target_sha256 != expected_sha256:
            raise RuntimeError(f"publication object collision: {target.name}")
        return False


def _remove_owned_reservation(staging_identity: Path, reservation: Path) -> None:
    if not _same_regular_inode(staging_identity, reservation):
        raise RuntimeError("created identity reservation no longer belongs to this staging root")
    reservation.unlink()
    _fsync_dir(reservation.parent)


def _assert_protocol_entries(final: Path, *, complete: bool) -> None:
    allowed = {".identity", ".complete", *FALLBACK_OBJECT_NAMES}
    try:
        actual = {entry.name for entry in os.scandir(final)}
    except OSError as exc:
        raise RuntimeError("final root collision could not be inspected") from exc
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise RuntimeError(f"final root contains non-protocol entries: {unexpected}")
    if complete and actual != allowed:
        raise RuntimeError(f"completed final root entry set mismatch: {sorted(actual)}")


def _wait_for_peer_identity(
    final: Path,
    reservation: Path,
    *,
    expected_sha256: str,
    timeout_seconds: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            digest = _sha256_regular_nofollow(final / ".identity")
        except OSError:
            pass
        except RuntimeError:
            return False
        else:
            return digest == expected_sha256 and _same_regular_inode(
                reservation, final / ".identity"
            )
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _load_staged_manifest(staging: Path, *, identity: bytes) -> dict[str, str]:
    marker = json.loads(_read_regular_nofollow(staging / ".complete"))
    if marker.get("schema") != "plan03.init.complete.v1":
        raise RuntimeError("unsupported completion manifest")
    identity_sha256 = hashlib.sha256(identity).hexdigest()
    if marker.get("identity_sha256") != identity_sha256:
        raise RuntimeError("completion manifest identity mismatch")
    objects = marker.get("objects")
    if not isinstance(objects, dict) or set(objects) != set(FALLBACK_OBJECT_NAMES):
        raise RuntimeError("completion manifest object set mismatch")
    result = {str(name): str(digest) for name, digest in objects.items()}
    if any(len(digest) != 64 for digest in result.values()):
        raise RuntimeError("completion manifest contains invalid digest")
    return result


def _fallback_publish(
    staging: Path,
    final: Path,
    *,
    identity: bytes,
    stop_after_step: int | None = None,
    before_final_mkdir: Callable[[], None] | None = None,
) -> bool:
    if stop_after_step is not None and not 0 <= stop_after_step < len(FALLBACK_STEPS):
        raise ValueError("stop_after_step outside crash-prefix range")
    if stop_after_step == 0:
        return False
    object_digests = _load_staged_manifest(staging, identity=identity)
    identity_sha256 = hashlib.sha256(identity).hexdigest()
    completed_steps = 0

    def crash_after_step() -> bool:
        nonlocal completed_steps
        completed_steps += 1
        return stop_after_step == completed_steps

    reservation = _reservation_path(final)
    staging_identity = staging / ".identity"
    reservation_created = _link_verified(
        staging_identity, reservation, expected_sha256=identity_sha256
    )
    reservation_owned_by_staging = _same_regular_inode(staging_identity, reservation)
    if crash_after_step():
        return False
    _fsync_dir(final.parent)
    if crash_after_step():
        return False
    if not reservation_created and not reservation_owned_by_staging and not final.exists():
        raise RuntimeError("identity reservation is owned by another staging root")
    if before_final_mkdir is not None:
        before_final_mkdir()
    try:
        final.mkdir()
    except FileExistsError:
        try:
            mode = os.lstat(final).st_mode
        except OSError as exc:
            raise RuntimeError("final root collision could not be inspected") from exc
        if not stat.S_ISDIR(mode):
            if reservation_created:
                _remove_owned_reservation(staging_identity, reservation)
            raise RuntimeError("final root collision is not a directory")
        if reservation_created:
            if not _wait_for_peer_identity(
                final,
                reservation,
                expected_sha256=identity_sha256,
            ):
                _remove_owned_reservation(staging_identity, reservation)
                raise RuntimeError("final root predates its identity reservation")
        _assert_protocol_entries(final, complete=False)
        try:
            final_identity_sha256 = _sha256_regular_nofollow(final / ".identity")
        except OSError:
            if not reservation_owned_by_staging:
                raise RuntimeError("final initialization is owned by another staging root")
        else:
            if final_identity_sha256 != identity_sha256:
                raise RuntimeError("final root identity collision")
    if crash_after_step():
        return False
    _fsync_dir(final.parent)
    if crash_after_step():
        return False
    _link_verified(reservation, final / ".identity", expected_sha256=identity_sha256)
    if crash_after_step():
        return False
    _fsync_dir(final)
    if crash_after_step():
        return False
    for name in FALLBACK_OBJECT_NAMES:
        _link_verified(staging / name, final / name, expected_sha256=object_digests[name])
        if crash_after_step():
            return False
        _fsync_dir(final)
        if crash_after_step():
            return False
    marker_sha256 = _sha256_regular_nofollow(staging / ".complete")
    _link_verified(staging / ".complete", final / ".complete", expected_sha256=marker_sha256)
    if crash_after_step():
        return _fallback_visible(final, identity=identity)
    _fsync_dir(final)
    if crash_after_step():
        return _fallback_visible(final, identity=identity)
    _fsync_dir(final.parent)
    if crash_after_step():
        return False
    if completed_steps != len(FALLBACK_STEPS):
        raise RuntimeError(f"fallback step accounting drift: {completed_steps}")
    return True


def _fallback_visible(final: Path, *, identity: bytes) -> bool:
    marker = final / ".complete"
    try:
        marker_payload = json.loads(_read_regular_nofollow(marker))
        reservation = _reservation_path(final)
        identity_sha256 = hashlib.sha256(identity).hexdigest()
        if _sha256_regular_nofollow(reservation) != identity_sha256:
            return False
        if _sha256_regular_nofollow(final / ".identity") != identity_sha256:
            return False
        _assert_protocol_entries(final, complete=True)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    if marker_payload.get("schema") != "plan03.init.complete.v1":
        return False
    if marker_payload.get("identity_sha256") != identity_sha256:
        return False
    objects = marker_payload.get("objects")
    if not isinstance(objects, dict) or set(objects) != set(FALLBACK_OBJECT_NAMES):
        return False
    try:
        return all(
            _sha256_regular_nofollow(final / name) == str(objects[name])
            for name in FALLBACK_OBJECT_NAMES
        )
    except (OSError, RuntimeError):
        return False


def _repair_missing_reservation(final: Path, *, identity: bytes) -> bool:
    """Restore a completed run's same-inode sibling reservation after verified loss."""
    reservation = _reservation_path(final)
    if reservation.exists():
        return _fallback_visible(final, identity=identity)
    identity_sha256 = hashlib.sha256(identity).hexdigest()
    try:
        marker = json.loads(_read_regular_nofollow(final / ".complete"))
        _assert_protocol_entries(final, complete=True)
        if marker.get("schema") != "plan03.init.complete.v1":
            return False
        if marker.get("identity_sha256") != identity_sha256:
            return False
        objects = marker.get("objects")
        if not isinstance(objects, dict) or set(objects) != set(FALLBACK_OBJECT_NAMES):
            return False
        if _sha256_regular_nofollow(final / ".identity") != identity_sha256:
            return False
        if not all(
            _sha256_regular_nofollow(final / name) == str(objects[name])
            for name in FALLBACK_OBJECT_NAMES
        ):
            return False
        _link_verified(final / ".identity", reservation, expected_sha256=identity_sha256)
        _fsync_dir(final.parent)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    return _same_regular_inode(final / ".identity", reservation) and _fallback_visible(
        final, identity=identity
    )


def _probe_directory_publish_fallback(root: Path) -> dict[str, Any]:
    staging = root / "fallback.staging"
    staging.mkdir()
    identity = b"plan03-identity-a"
    _write_exclusive(staging / ".identity", identity)
    object_payloads = {
        "descriptor.json": b"descriptor",
        "authority.sqlite": b"sqlite",
        "bootstrap.json": b"bootstrap",
    }
    for name, payload in object_payloads.items():
        _write_exclusive(staging / name, payload)
    manifest = {
        "schema": "plan03.init.complete.v1",
        "identity_sha256": hashlib.sha256(identity).hexdigest(),
        "objects": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in object_payloads.items()
        },
    }
    _write_exclusive(
        staging / ".complete",
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    _fsync_dir(staging)
    crash_prefixes = []
    marker_step_index = FALLBACK_STEPS.index("link_complete_marker")
    for prefix, step in enumerate(FALLBACK_STEPS[: marker_step_index + 1]):
        final = root / f"fallback-crash-{prefix}"
        completed = _fallback_publish(staging, final, identity=identity, stop_after_step=prefix)
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
                "crash_after": "before_any_step" if prefix == 0 else FALLBACK_STEPS[prefix - 1],
                "next_step": step,
                "visible_before_retry": visible_before_retry,
                "visible_after_retry": visible_after_retry,
            }
        )
    post_visibility_prefixes = []
    for completed_steps in range(marker_step_index + 1, len(FALLBACK_STEPS)):
        final = root / f"fallback-post-visible-{completed_steps}"
        completed = _fallback_publish(
            staging,
            final,
            identity=identity,
            stop_after_step=completed_steps,
        )
        visible_before_retry = _fallback_visible(final, identity=identity)
        retried = _fallback_publish(staging, final, identity=identity)
        visible_after_retry = _fallback_visible(final, identity=identity)
        if not completed or not visible_before_retry or not retried or not visible_after_retry:
            raise RuntimeError(f"post-visibility prefix {completed_steps} did not recover")
        post_visibility_prefixes.append(
            {
                "completed_steps": completed_steps,
                "crash_after": FALLBACK_STEPS[completed_steps - 1],
                "visible_before_retry": visible_before_retry,
                "visible_after_retry": visible_after_retry,
            }
        )
    collision = root / "fallback-collision"
    conflicting_staging = root / "fallback-conflicting.staging"
    shutil.copytree(staging, conflicting_staging)
    (conflicting_staging / ".identity").unlink()
    conflicting_identity = b"plan03-identity-b"
    _write_exclusive(conflicting_staging / ".identity", conflicting_identity)
    (conflicting_staging / ".complete").unlink()
    conflicting_manifest = {
        **manifest,
        "identity_sha256": hashlib.sha256(conflicting_identity).hexdigest(),
    }
    _write_exclusive(
        conflicting_staging / ".complete",
        (json.dumps(conflicting_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    _link_verified(
        conflicting_staging / ".identity",
        _reservation_path(collision),
        expected_sha256=hashlib.sha256(conflicting_identity).hexdigest(),
    )
    collision_error = None
    try:
        _fallback_publish(staging, collision, identity=identity)
    except RuntimeError as exc:
        collision_error = str(exc)
    if (
        collision_error != "publication object collision: .fallback-collision.identity-reservation"
        or (collision / ".complete").exists()
    ):
        raise RuntimeError(f"identity collision was not preserved: {collision_error}")
    orphan = root / "fallback-orphan-without-reservation"
    orphan.mkdir()
    orphan_error = None
    try:
        _fallback_publish(staging, orphan, identity=identity)
    except RuntimeError as exc:
        orphan_error = str(exc)
    orphan_retry_error = None
    try:
        _fallback_publish(staging, orphan, identity=identity)
    except RuntimeError as exc:
        orphan_retry_error = str(exc)
    if (
        orphan_error != "final root predates its identity reservation"
        or orphan_retry_error != orphan_error
        or _reservation_path(orphan).exists()
    ):
        raise RuntimeError(f"unreserved final root did not fail closed: {orphan_error}")
    foreign = root / "fallback-foreign-entry"
    foreign.mkdir()
    _write_exclusive(foreign / "foreign.txt", b"must-not-be-claimed")
    foreign_error = None
    try:
        _fallback_publish(staging, foreign, identity=identity)
    except RuntimeError as exc:
        foreign_error = str(exc)
    if (
        foreign_error != "final root predates its identity reservation"
        or _reservation_path(foreign).exists()
        or not (foreign / "foreign.txt").is_file()
    ):
        raise RuntimeError(f"foreign final root did not fail closed: {foreign_error}")
    concurrent_staging = root / "fallback-concurrent.staging"
    shutil.copytree(staging, concurrent_staging)
    concurrent = root / "fallback-concurrent"
    if _fallback_publish(staging, concurrent, identity=identity, stop_after_step=1):
        raise RuntimeError("reservation-only prefix unexpectedly completed")
    concurrent_error = None
    try:
        _fallback_publish(concurrent_staging, concurrent, identity=identity)
    except RuntimeError as exc:
        concurrent_error = str(exc)
    if concurrent_error != "identity reservation is owned by another staging root":
        raise RuntimeError(f"concurrent staging stole reservation: {concurrent_error}")
    if not _fallback_publish(staging, concurrent, identity=identity):
        raise RuntimeError("reservation owner did not recover concurrent run")
    same_staging_peer = root / "fallback-same-staging-peer"

    def peer_wins_final_mkdir() -> None:
        same_staging_peer.mkdir()
        os.link(
            _reservation_path(same_staging_peer),
            same_staging_peer / ".identity",
            follow_symlinks=False,
        )

    if not _fallback_publish(
        staging,
        same_staging_peer,
        identity=identity,
        before_final_mkdir=peer_wins_final_mkdir,
    ) or not _fallback_visible(same_staging_peer, identity=identity):
        raise RuntimeError("same-staging peer mkdir race did not converge")
    complete = root / "fallback-complete"
    if not _fallback_publish(staging, complete, identity=identity):
        raise RuntimeError("complete fallback publication returned false")
    complete_reservation = _reservation_path(complete)
    complete_reservation.unlink()
    _fsync_dir(complete.parent)
    invisible_after_reservation_loss = _fallback_visible(complete, identity=identity)
    if invisible_after_reservation_loss or not _repair_missing_reservation(
        complete, identity=identity
    ):
        raise RuntimeError("verified reservation repair did not recover completed run")
    second_identity_error = None
    try:
        _fallback_publish(conflicting_staging, complete, identity=conflicting_identity)
    except RuntimeError as exc:
        second_identity_error = str(exc)
    if (
        second_identity_error
        != "publication object collision: .fallback-complete.identity-reservation"
    ):
        raise RuntimeError("completed root accepted a different identity")
    return {
        "status": "PASS",
        "protocol": "parent-hardlink-identity-reservation+exclusive-mkdir+hashed-hardlink-objects+complete-manifest",
        "visibility_linearization_point": "link_complete_marker",
        "crash_prefixes": crash_prefixes,
        "post_visibility_prefixes": post_visibility_prefixes,
        "same_identity_retry": True,
        "same_identity_retry_requires_original_staging_inode_until_identity_link": True,
        "different_staging_concurrency": "fail_closed_until_final_identity_exists",
        "same_staging_concurrency": "converges_when_peer_links_reserved_identity",
        "different_identity_collision": "fail_closed",
        "completed_root_overwrite": "fail_closed",
        "preexisting_final_retry": "repeatable_fail_closed_without_reservation_leak",
        "foreign_entry_visibility": "fail_closed",
        "reservation_lifecycle": "retain_with_run; verified completed run supports same-inode repair",
        "reservation_loss_before_repair_visible": invisible_after_reservation_loss,
        "reservation_repair": "PASS",
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


def _capture_source_identity(project_root: Path) -> dict[str, Any]:
    capture_path = project_root / "scripts/miyabi/capture_source_identity.py"
    spec = importlib.util.spec_from_file_location("plan03_fs_capture_source_identity", capture_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source identity helper: {capture_path}")
    capture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture_module)
    source = capture_module.capture(project_root)
    return {
        "git_commit": source["git_commit"],
        "git_dirty": source["git_dirty"],
        "source_fingerprint": source["source_fingerprint"],
    }


def probe(shared_parent: Path, *, project_root: Path | None = None) -> dict[str, Any]:
    shared_parent = shared_parent.resolve()
    if not shared_parent.is_dir():
        raise FileNotFoundError(shared_parent)
    project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    recorded_at = datetime.now(UTC).astimezone()
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
            "recorded_at": recorded_at.isoformat(),
            "source_identity": _capture_source_identity(project_root),
            "shared_parent": str(shared_parent),
            "temporary_path_removed": True,
            "results": results,
        }
    finally:
        shutil.rmtree(temporary)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _result_artifact_path(output_dir: Path, payload: dict[str, Any]) -> Path:
    result = "pass" if payload["status"] == "PASS" else "fail"
    recorded_at = datetime.fromisoformat(str(payload["recorded_at"]))
    timestamp = recorded_at.strftime("%Y%m%d-%H%M%S")
    return output_dir / f"{timestamp}_p0-shared-fs-capability_{result}.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared-parent", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    payload = probe(args.shared_parent, project_root=args.project_root)
    artifact_path = None
    if args.output_dir is not None:
        artifact_path = _result_artifact_path(args.output_dir.resolve(), payload)
        _atomic_write_json(artifact_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if artifact_path is not None:
        print(f"PLAN03_FS_CAPABILITY_ARTIFACT={artifact_path}")
    if payload["status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
