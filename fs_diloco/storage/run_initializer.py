"""Crash-recoverable no-replace publication for a fresh shared-FS run root."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .atomic_io import (
    atomic_write_json,
    fsync_directory,
    read_json,
    sha256_file,
)
from .artifact_policy import ArtifactPolicy
from .paths import RunPaths
from .schema_bootstrap import BootstrapIdentity, open_existing


PLAN03_REQUIREMENTS = frozenset({"AUDIT-01", "ENV-01", "INIT-01"})


FaultHook = Callable[[str], None]


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def reservation_path(final_root: Path) -> Path:
    return final_root.parent / f".{final_root.name}.identity-reservation"


def staging_glob(final_root: Path) -> str:
    return f".{final_root.name}.staging.*"


def create_staging_root(final_root: Path) -> Path:
    staging = final_root.parent / f".{final_root.name}.staging.{uuid.uuid4()}"
    staging.mkdir(parents=False, exist_ok=False)
    fsync_directory(final_root.parent)
    return staging


def write_run_identity(staging_root: Path, identity: dict[str, Any]) -> Path:
    payload = dict(identity)
    payload["identity_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    target = staging_root / ".identity"
    atomic_write_json(target, payload, mode=0o444)
    return target


def validate_identity_file(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
        raise RuntimeError("run identity must be a non-writable regular file")
    payload = read_json(path)
    recorded = payload.get("identity_sha256")
    content = {key: value for key, value in payload.items() if key != "identity_sha256"}
    actual = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    if recorded != actual:
        raise RuntimeError("run identity checksum mismatch")
    return payload


def build_complete_manifest(staging_root: Path, *, logical_root: Path) -> dict[str, Any]:
    directories: list[str] = []
    objects: list[dict[str, Any]] = []
    for path in sorted(staging_root.rglob("*")):
        relative = path.relative_to(staging_root).as_posix()
        if relative == ".complete.source":
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"initializer staging contains a symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(relative)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"initializer staging contains a non-regular object: {relative}")
        role = "authority_mutable" if relative == "control/syncer_metadata.sqlite3" else "immutable"
        objects.append(
            {
                "relative_path": relative,
                "size_bytes": metadata.st_size,
                "sha256": sha256_file(path),
                "mode": stat.S_IMODE(metadata.st_mode),
                "role": role,
            }
        )
    manifest: dict[str, Any] = {
        "format_version": 1,
        "logical_root": str(logical_root),
        "directories": directories,
        "objects": objects,
        "mutable_prefixes": [
            "audit",
            "control/scheduler_operator_requests",
            "control/syncer_epochs",
            "control/syncer_launch_claims",
            "control/registration_requests",
            "eval_checkpoints",
            "heartbeats",
            "logs",
            "metrics",
            "optim",
            "updates",
            "weights",
        ],
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return manifest


def write_complete_source(staging_root: Path, manifest: dict[str, Any]) -> Path:
    target = staging_root / ".complete.source"
    atomic_write_json(target, manifest, mode=0o444)
    return target


def claim_identity_reservation(
    *,
    final_root: Path,
    staging_root: Path,
    fault_hook: FaultHook | None = None,
) -> Path:
    reservation = reservation_path(final_root)
    identity = staging_root / ".identity"
    try:
        os.link(identity, reservation, follow_symlinks=False)
    except FileExistsError:
        if not _same_inode(identity, reservation):
            raise FileExistsError("run identity reservation is owned by another staging root")
    else:
        fsync_directory(final_root.parent)
        if _entry_exists(final_root):
            reservation.unlink()
            fsync_directory(final_root.parent)
            raise FileExistsError("run root appeared after reserving a fresh identity")
        _call_fault(fault_hook, "after_identity_reservation")
    return reservation


def find_reserved_staging(final_root: Path, *, allow_missing_owner: bool = False) -> Path | None:
    reservation = reservation_path(final_root)
    if not _entry_exists(reservation):
        return None
    for candidate in sorted(final_root.parent.glob(staging_glob(final_root))):
        identity = candidate / ".identity"
        try:
            candidate_mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if (
            stat.S_ISDIR(candidate_mode)
            and not stat.S_ISLNK(candidate_mode)
            and _entry_exists(identity)
            and _same_inode(identity, reservation)
        ):
            return candidate
    if allow_missing_owner:
        return None
    raise RuntimeError("identity reservation exists without its owning staging root")


def publish_staged_run(
    *,
    final_root: Path,
    staging_root: Path,
    fault_hook: FaultHook | None = None,
) -> dict[str, Any]:
    reservation = reservation_path(final_root)
    identity = staging_root / ".identity"
    if not _same_inode(identity, reservation):
        raise RuntimeError("staging root does not own the run identity reservation")
    manifest = read_json(staging_root / ".complete.source")
    _validate_manifest_self_hash(manifest)
    if not _entry_exists(final_root):
        final_root.mkdir(exist_ok=False)
        fsync_directory(final_root.parent)
        _call_fault(fault_hook, "after_final_mkdir")
    elif not final_root.is_dir() or final_root.is_symlink():
        raise FileExistsError("run root collision is not a directory")
    final_identity = final_root / ".identity"
    _link_exact(identity, final_identity)
    fsync_directory(final_root)
    _call_fault(fault_hook, "after_final_identity")
    for relative in manifest["directories"]:
        _ensure_relative_directory(final_root, str(relative))
    fsync_directory(final_root)
    for index, entry in enumerate(manifest["objects"]):
        relative = str(entry["relative_path"])
        source = staging_root / relative
        _validate_staging_object(source, entry)
        target = final_root / relative
        _ensure_relative_directory(final_root, target.parent.relative_to(final_root).as_posix())
        _link_exact(source, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        fsync_directory(target.parent)
        _call_fault(fault_hook, f"after_object_link:{index}")
    _call_fault(fault_hook, "before_complete_marker")
    complete_source = staging_root / ".complete.source"
    complete = final_root / ".complete"
    _link_exact(complete_source, complete)
    fsync_directory(final_root)
    fsync_directory(final_root.parent)
    _call_fault(fault_hook, "after_complete_marker")
    return validate_completed_run(final_root, strict_initial=True)


def validate_completed_run(final_root: Path, *, strict_initial: bool = False) -> dict[str, Any]:
    return _validate_completed_run(
        final_root,
        strict_initial=strict_initial,
        require_reservation=True,
    )


def _validate_completed_run(
    final_root: Path,
    *,
    strict_initial: bool,
    require_reservation: bool,
) -> dict[str, Any]:
    if final_root.is_symlink():
        raise RuntimeError("run root must not be a symlink")
    final_root = final_root.resolve()
    complete = final_root / ".complete"
    if not complete.is_file() or complete.is_symlink():
        raise FileNotFoundError("run is not visible before a regular .complete marker exists")
    if complete.stat().st_mode & 0o222:
        raise RuntimeError("complete manifest must be non-writable")
    manifest = read_json(complete)
    _validate_manifest_self_hash(manifest)
    if manifest.get("logical_root") != str(final_root):
        raise RuntimeError("complete manifest logical path mismatch")
    identity = final_root / ".identity"
    validate_identity_file(identity)
    reservation = reservation_path(final_root)
    if require_reservation and not _same_inode(identity, reservation):
        raise RuntimeError("run identity reservation is missing or mismatched")
    expected_files = {".complete", ".identity"}
    expected_dirs = set(manifest["directories"])
    for entry in manifest["objects"]:
        relative = str(entry["relative_path"])
        expected_files.add(relative)
        target = final_root / relative
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"manifest object is not regular: {relative}")
        if entry["role"] == "immutable":
            if metadata.st_mode & 0o222 or stat.S_IMODE(metadata.st_mode) != int(entry["mode"]):
                raise RuntimeError(f"immutable manifest object is writable: {relative}")
            if (
                metadata.st_size != int(entry["size_bytes"])
                or sha256_file(target) != entry["sha256"]
            ):
                raise RuntimeError(f"manifest object checksum mismatch: {relative}")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for path in final_root.rglob("*"):
        relative = path.relative_to(final_root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"run root contains a protocol-forbidden symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            actual_dirs.add(relative)
        elif stat.S_ISREG(metadata.st_mode):
            actual_files.add(relative)
        else:
            raise RuntimeError(f"run root contains a protocol-forbidden entry: {relative}")
    if strict_initial:
        if actual_files != expected_files or actual_dirs != expected_dirs:
            raise RuntimeError("initial run root contains entries outside the complete manifest")
    else:
        extras = (actual_files - expected_files) | (actual_dirs - expected_dirs)
        prefixes = tuple(str(item).rstrip("/") for item in manifest["mutable_prefixes"])
        forbidden = sorted(
            item
            for item in extras
            if not any(item == prefix or item.startswith(prefix + "/") for prefix in prefixes)
        )
        if forbidden:
            raise RuntimeError(f"run root contains protocol-external entries: {forbidden}")
    _validate_completed_protocol_identity(final_root)
    return manifest


def repair_identity_reservation(final_root: Path) -> Path:
    final_root = final_root.resolve()
    reservation = reservation_path(final_root)
    if _entry_exists(reservation):
        raise FileExistsError("identity reservation already exists")
    # Validate every immutable object and the complete protocol entry set
    # without relying on the missing sibling reservation.
    _validate_completed_run(
        final_root,
        strict_initial=False,
        require_reservation=False,
    )
    identity = final_root / ".identity"
    os.link(identity, reservation, follow_symlinks=False)
    fsync_directory(final_root.parent)
    return reservation


def remove_completed_staging(*, final_root: Path, staging_root: Path) -> None:
    """Remove only the verified staging alias after final publication is visible."""

    final_root = final_root.resolve()
    staging_root = staging_root.absolute()
    if (
        staging_root.parent != final_root.parent
        or not staging_root.name.startswith(f".{final_root.name}.staging.")
        or staging_root.is_symlink()
        or not staging_root.is_dir()
    ):
        raise RuntimeError("completed staging cleanup target is not an owned sibling staging root")
    validate_completed_run(final_root)
    if not _same_inode(staging_root / ".identity", reservation_path(final_root)):
        raise RuntimeError("completed staging cleanup target does not own the reservation")
    entries = sorted(staging_root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in entries:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RuntimeError("completed staging cleanup refuses non-regular entries")
    for path in entries:
        if stat.S_ISDIR(path.lstat().st_mode):
            path.rmdir()
        else:
            path.unlink()
    staging_root.rmdir()
    fsync_directory(final_root.parent)


def _validate_manifest_self_hash(manifest: dict[str, Any]) -> None:
    recorded = manifest.get("manifest_sha256")
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    actual = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    if recorded != actual:
        raise RuntimeError("complete manifest self-checksum mismatch")
    _validate_manifest_structure(manifest)


def _validate_manifest_structure(manifest: dict[str, Any]) -> None:
    if manifest.get("format_version") != 1:
        raise RuntimeError("unsupported complete manifest version")
    directories = manifest.get("directories")
    objects = manifest.get("objects")
    mutable_prefixes = manifest.get("mutable_prefixes")
    if not isinstance(directories, list) or not isinstance(objects, list):
        raise RuntimeError("complete manifest directories/objects must be lists")
    if not isinstance(mutable_prefixes, list):
        raise RuntimeError("complete manifest mutable prefixes must be a list")
    directory_names = [_canonical_relative_path(item) for item in directories]
    object_names: list[str] = []
    for entry in objects:
        if not isinstance(entry, dict):
            raise RuntimeError("complete manifest object entries must be mappings")
        relative = _canonical_relative_path(entry.get("relative_path"))
        if entry.get("role") not in {"immutable", "authority_mutable"}:
            raise RuntimeError("complete manifest object role is invalid")
        size = entry.get("size_bytes")
        mode = entry.get("mode")
        digest = entry.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o777
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise RuntimeError("complete manifest object metadata is invalid")
        object_names.append(relative)
    prefix_names = [_canonical_relative_path(item) for item in mutable_prefixes]
    if (
        len(set(directory_names)) != len(directory_names)
        or len(set(object_names)) != len(object_names)
        or len(set(prefix_names)) != len(prefix_names)
        or set(directory_names) & set(object_names)
    ):
        raise RuntimeError("complete manifest paths must be unique and type-consistent")
    declared_directories = set(directory_names)
    for relative in (*directory_names, *object_names):
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            if parent.as_posix() not in declared_directories:
                raise RuntimeError("complete manifest omits a required parent directory")
            parent = parent.parent


def _canonical_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise RuntimeError("complete manifest path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("complete manifest path is not canonical")
    return path.as_posix()


def _ensure_relative_directory(root: Path, relative: str) -> Path:
    if relative == ".":
        return root
    canonical = _canonical_relative_path(relative)
    current = root
    for component in PurePosixPath(canonical).parts:
        current = current / component
        try:
            current.mkdir()
        except FileExistsError:
            mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise FileExistsError(f"run directory collision: {canonical}") from None
    return current


def _validate_staging_object(source: Path, entry: dict[str, Any]) -> None:
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"initializer staging object is not regular: {entry['relative_path']}")
    if (
        metadata.st_size != int(entry["size_bytes"])
        or stat.S_IMODE(metadata.st_mode) != int(entry["mode"])
        or sha256_file(source) != entry["sha256"]
    ):
        raise RuntimeError(f"initializer staging object changed: {entry['relative_path']}")
    if entry["role"] == "immutable" and metadata.st_mode & 0o222:
        raise RuntimeError(f"initializer immutable object is writable: {entry['relative_path']}")


def _same_inode(left: Path, right: Path) -> bool:
    try:
        left_stat = left.lstat()
        right_stat = right.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(left_stat.st_mode)
        and stat.S_ISREG(right_stat.st_mode)
        and (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)
    )


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _validate_completed_protocol_identity(final_root: Path) -> None:
    """Reopen immutable identities and the mutable DB after marker publication."""

    paths = RunPaths(final_root)
    descriptor = read_json(paths.run_descriptor_json)
    descriptor_sha = descriptor.get("descriptor_sha256")
    descriptor_content = {
        key: value for key, value in descriptor.items() if key != "descriptor_sha256"
    }
    if descriptor_sha != hashlib.sha256(canonical_json_bytes(descriptor_content)).hexdigest():
        raise RuntimeError("run descriptor self-checksum mismatch")
    if descriptor.get("shared_root") != str(final_root):
        raise RuntimeError("run descriptor logical root mismatch")
    if Path(str(descriptor.get("resolved_config_path", ""))).resolve() != (
        paths.resolved_config_yaml.resolve()
    ):
        raise RuntimeError("run descriptor config path mismatch")
    if Path(str(descriptor.get("source_manifest_path", ""))).resolve() != (
        paths.run_source_manifest_json.resolve()
    ):
        raise RuntimeError("run descriptor source path mismatch")
    if sha256_file(paths.resolved_config_yaml) != descriptor.get("resolved_config_sha256"):
        raise RuntimeError("resolved config checksum mismatch")
    if sha256_file(paths.run_source_manifest_json) != descriptor.get("source_manifest_sha256"):
        raise RuntimeError("source manifest checksum mismatch")
    source = read_json(paths.run_source_manifest_json)
    source_sha = source.get("manifest_sha256")
    source_content = {key: value for key, value in source.items() if key != "manifest_sha256"}
    if source_sha != hashlib.sha256(canonical_json_bytes(source_content)).hexdigest():
        raise RuntimeError("source manifest self-checksum mismatch")
    for field in ("git_commit", "git_dirty", "source_fingerprint"):
        if source.get(field) != descriptor.get(field):
            raise RuntimeError(f"source manifest {field} mismatch")
    identity = validate_identity_file(paths.run_identity_file)
    identity_checks = {
        "run_id": descriptor.get("run_id"),
        "source_fingerprint": descriptor.get("source_fingerprint"),
        "config_sha256": descriptor.get("resolved_config_sha256"),
        "logical_root": str(final_root),
    }
    mismatches = {
        key: (identity.get(key), value)
        for key, value in identity_checks.items()
        if identity.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"run identity does not match descriptor: {mismatches}")
    ArtifactPolicy.from_dict(read_json(paths.artifact_policy_json))
    descriptor_mode = descriptor.get("mode")
    if descriptor_mode == "full_ha_static":
        bootstrap_mode = "full"
    elif descriptor_mode == "full_ha_dynamic":
        bootstrap_mode = "full_dynamic"
    else:
        raise RuntimeError(f"unsupported run descriptor mode: {descriptor_mode!r}")
    connection = open_existing(
        paths.sqlite_db,
        BootstrapIdentity(
            run_id=str(descriptor.get("run_id", "")),
            source_fingerprint=str(descriptor.get("source_fingerprint", "")),
            config_sha256=str(descriptor.get("resolved_config_sha256", "")),
            mode=bootstrap_mode,
        ),
        marker_path=paths.bootstrap_complete_json,
    )
    try:
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        if integrity != ("ok",):
            raise RuntimeError(f"run authority integrity check failed: {integrity}")
    finally:
        connection.close()


def _link_exact(source: Path, target: Path) -> None:
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        if not _same_inode(source, target):
            raise FileExistsError(f"no-replace object collision: {target}")


def _call_fault(fault_hook: FaultHook | None, point: str) -> None:
    if fault_hook is not None:
        fault_hook(point)
