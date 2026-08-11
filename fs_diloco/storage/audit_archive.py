"""Immutable authority audit-batch publication and offline deduplication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .atomic_io import fsync_directory, publish_immutable_bytes, read_json, sha256_file
from .paths import RunPaths


def command_receipt_path(paths: RunPaths, command_id: str) -> Path:
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("command_id must not be empty")
    identity = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    return paths.audit_command_receipts / identity[:2] / f"{identity}.json"


def _ensure_real_directory(path: Path, *, create: bool) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        path.mkdir()
        fsync_directory(path.parent)
        metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("archived command receipt parent is not a real directory")
    return True


def _command_receipt_parent(paths: RunPaths, command_id: str, *, create: bool) -> Path | None:
    target = command_receipt_path(paths, command_id)
    current = paths.shared_root
    for component in ("audit", "command_receipts", target.parent.name):
        current = current / component
        if not _ensure_real_directory(current, create=create):
            return None
    return current


def publish_command_receipt(paths: RunPaths, row: dict[str, Any]) -> Path:
    expected_fields = {
        "command_id",
        "command_kind",
        "request_sha256",
        "owner_epoch",
        "result_json",
        "committed_at",
    }
    if set(row) != expected_fields:
        raise ValueError("archived command receipt has unexpected fields")
    payload: dict[str, Any] = {"format_version": 1, **row}
    payload["content_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    target = command_receipt_path(paths, str(row["command_id"]))
    _command_receipt_parent(paths, str(row["command_id"]), create=True)
    publish_immutable_bytes(target, _canonical(payload) + b"\n")
    return target


def read_command_receipt(paths: RunPaths, command_id: str) -> dict[str, Any] | None:
    target = command_receipt_path(paths, command_id)
    if _command_receipt_parent(paths, command_id, create=False) is None:
        return None
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
        raise RuntimeError("archived command receipt is not an immutable regular file")
    payload = read_json(target)
    if payload.get("format_version") != 1 or payload.get("command_id") != command_id:
        raise RuntimeError("archived command receipt identity is invalid")
    recorded = payload.get("content_sha256")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if recorded != hashlib.sha256(_canonical(content)).hexdigest():
        raise RuntimeError("archived command receipt content hash is invalid")
    expected_fields = {
        "format_version",
        "command_id",
        "command_kind",
        "request_sha256",
        "owner_epoch",
        "result_json",
        "committed_at",
        "content_sha256",
    }
    if set(payload) != expected_fields:
        raise RuntimeError("archived command receipt has unexpected fields")
    if (
        not isinstance(payload["command_kind"], str)
        or not isinstance(payload["request_sha256"], str)
        or len(payload["request_sha256"]) != 64
        or isinstance(payload["owner_epoch"], bool)
        or not isinstance(payload["owner_epoch"], int)
        or payload["owner_epoch"] < 1
        or not isinstance(payload["result_json"], str)
        or isinstance(payload["committed_at"], bool)
        or not isinstance(payload["committed_at"], (int, float))
    ):
        raise RuntimeError("archived command receipt fields are invalid")
    return {key: payload[key] for key in expected_fields - {"format_version", "content_sha256"}}


def build_audit_batch(
    *,
    batch_id: str,
    record_kind: str,
    cutoff_version: int,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    _safe_component(batch_id, name="batch_id")
    _safe_component(record_kind, name="record_kind")
    if (
        isinstance(cutoff_version, bool)
        or not isinstance(cutoff_version, int)
        or cutoff_version < 0
    ):
        raise ValueError("cutoff_version must be a non-negative integer")
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda item: (str(item["table"]), str(item["primary_key"])),
    )
    payload: dict[str, Any] = {
        "format_version": 1,
        "batch_id": batch_id,
        "record_kind": record_kind,
        "cutoff_version": cutoff_version,
        "records": ordered,
    }
    payload["content_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    return payload


def publish_audit_batch(paths: RunPaths, payload: dict[str, Any]) -> tuple[Path, str]:
    validate_audit_batch(payload)
    kind = str(payload["record_kind"])
    batch_id = str(payload["batch_id"])
    _safe_component(kind, name="record_kind")
    _safe_component(batch_id, name="batch_id")
    target = paths.audit_batches / kind / f"{batch_id}.json"
    raw = _canonical(payload) + b"\n"
    publication = publish_immutable_bytes(target, raw)
    return target, publication.sha256


def validate_audit_batch(payload: dict[str, Any]) -> None:
    if payload.get("format_version") != 1:
        raise ValueError("unsupported audit batch version")
    recorded = payload.get("content_sha256")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    actual = hashlib.sha256(_canonical(content)).hexdigest()
    if recorded != actual:
        raise ValueError("audit batch content hash mismatch")
    _safe_component(payload.get("batch_id"), name="batch_id")
    _safe_component(payload.get("record_kind"), name="record_kind")
    cutoff = payload.get("cutoff_version")
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 0:
        raise ValueError("audit batch cutoff must be a non-negative integer")
    if not isinstance(payload.get("records"), list):
        raise ValueError("audit batch records must be a list")
    seen: set[tuple[str, str]] = set()
    for record in payload.get("records", []):
        key = (str(record.get("table", "")), str(record.get("primary_key", "")))
        if not all(key) or key in seen or not isinstance(record.get("row"), dict):
            raise ValueError("audit records require unique table/primary-key identities")
        seen.add(key)


def deduplicate_audit_records(payloads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for payload in payloads:
        validate_audit_batch(payload)
        record_kind = str(payload["record_kind"])
        for record in payload["records"]:
            key = (record_kind, str(record["table"]), str(record["primary_key"]))
            prior = deduplicated.get(key)
            if prior is not None and prior["row"] != record["row"]:
                raise RuntimeError(f"audit primary key has conflicting immutable rows: {key}")
            deduplicated[key] = {"record_kind": record_kind, **record}
    return [deduplicated[key] for key in sorted(deduplicated)]


def build_audit_partition(
    *,
    partition_id: str,
    record_kind: str,
    batches: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    _safe_component(partition_id, name="partition_id")
    _safe_component(record_kind, name="record_kind")
    payloads = sorted((dict(item) for item in batches), key=lambda item: str(item["batch_id"]))
    if not payloads:
        raise ValueError("an audit partition requires at least one source batch")
    for payload in payloads:
        validate_audit_batch(payload)
        if payload["record_kind"] != record_kind:
            raise ValueError("audit partition source record kinds must match")
    batch_ids = [str(payload["batch_id"]) for payload in payloads]
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError("audit partition source batch IDs must be unique")
    partition: dict[str, Any] = {
        "format_version": 1,
        "partition_id": partition_id,
        "record_kind": record_kind,
        "source_batches": [
            {
                "batch_id": payload["batch_id"],
                "cutoff_version": payload["cutoff_version"],
                "row_count": len(payload["records"]),
                "content_sha256": payload["content_sha256"],
                "file_sha256": hashlib.sha256(_canonical(payload) + b"\n").hexdigest(),
            }
            for payload in payloads
        ],
        "records": deduplicate_audit_records(payloads),
    }
    partition["content_sha256"] = hashlib.sha256(_canonical(partition)).hexdigest()
    return partition


def validate_audit_partition(payload: dict[str, Any]) -> None:
    if payload.get("format_version") != 1:
        raise ValueError("unsupported audit partition version")
    _safe_component(payload.get("partition_id"), name="partition_id")
    _safe_component(payload.get("record_kind"), name="record_kind")
    recorded = payload.get("content_sha256")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if recorded != hashlib.sha256(_canonical(content)).hexdigest():
        raise ValueError("audit partition content hash mismatch")
    sources = payload.get("source_batches")
    records = payload.get("records")
    if not isinstance(sources, list) or not sources or not isinstance(records, list):
        raise ValueError("audit partition sources/records are invalid")
    seen_batches: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "batch_id",
            "cutoff_version",
            "row_count",
            "content_sha256",
            "file_sha256",
        }:
            raise ValueError("audit partition source identity is invalid")
        batch_id = _safe_component(source["batch_id"], name="batch_id")
        if batch_id in seen_batches:
            raise ValueError("audit partition source batch IDs must be unique")
        seen_batches.add(batch_id)
        for field in ("content_sha256", "file_sha256"):
            if not isinstance(source[field], str) or len(source[field]) != 64:
                raise ValueError("audit partition source digest is invalid")
        for field in ("cutoff_version", "row_count"):
            if (
                isinstance(source[field], bool)
                or not isinstance(source[field], int)
                or source[field] < 0
            ):
                raise ValueError("audit partition source counts are invalid")
    seen_records: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("audit partition record is invalid")
        key = (
            str(record.get("record_kind", "")),
            str(record.get("table", "")),
            str(record.get("primary_key", "")),
        )
        if not all(key) or key in seen_records or not isinstance(record.get("row"), dict):
            raise ValueError("audit partition records must have unique identities")
        if key[0] != payload["record_kind"]:
            raise ValueError("audit partition record kind mismatch")
        seen_records.add(key)


def publish_audit_partition(
    paths: RunPaths, payload: dict[str, Any]
) -> tuple[Path, str, Path, str]:
    validate_audit_partition(payload)
    kind = str(payload["record_kind"])
    partition_id = str(payload["partition_id"])
    target = paths.audit_partitions / kind / f"{partition_id}.json"
    raw = _canonical(payload) + b"\n"
    publication = publish_immutable_bytes(target, raw)
    manifest: dict[str, Any] = {
        "format_version": 1,
        "partition_id": partition_id,
        "record_kind": kind,
        "partition_relative_path": paths.relative(target),
        "partition_size_bytes": publication.size_bytes,
        "partition_sha256": publication.sha256,
        "partition_content_sha256": payload["content_sha256"],
        "source_batches": payload["source_batches"],
    }
    manifest["manifest_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest_path = target.with_suffix(".manifest.json")
    manifest_publication = publish_immutable_bytes(manifest_path, _canonical(manifest) + b"\n")
    return target, publication.sha256, manifest_path, manifest_publication.sha256


def validate_audit_partition_manifest(
    *,
    paths: RunPaths,
    partition: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    validate_audit_partition(partition)
    recorded = manifest.get("manifest_sha256")
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if recorded != hashlib.sha256(_canonical(content)).hexdigest():
        raise ValueError("audit partition manifest hash mismatch")
    expected = {
        "format_version": 1,
        "partition_id": partition["partition_id"],
        "record_kind": partition["record_kind"],
        "partition_relative_path": paths.relative(
            paths.audit_partitions
            / str(partition["record_kind"])
            / f"{partition['partition_id']}.json"
        ),
        "partition_size_bytes": len(_canonical(partition) + b"\n"),
        "partition_sha256": hashlib.sha256(_canonical(partition) + b"\n").hexdigest(),
        "partition_content_sha256": partition["content_sha256"],
        "source_batches": partition["source_batches"],
    }
    if content != expected:
        raise ValueError("audit partition manifest identity mismatch")


def read_audit_rows(paths: RunPaths, *, table: str) -> tuple[dict[str, Any], ...]:
    """Read one logical table from validated immutable batches and partitions."""

    _safe_component(table, name="table")
    records: dict[str, dict[str, Any]] = {}
    roots = (
        ("batch", paths.audit_batches / "authority_history"),
        ("partition", paths.audit_partitions / "authority_history"),
    )
    for artifact_kind, root in roots:
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RuntimeError(f"audit {artifact_kind} root is not a real directory: {root}")
        for path in sorted(root.glob("*.json")):
            if path.name.endswith(".manifest.json"):
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
                raise RuntimeError(f"audit {artifact_kind} is not immutable: {path}")
            payload = read_json(path)
            if artifact_kind == "batch":
                validate_audit_batch(payload)
            else:
                validate_audit_partition(payload)
                manifest_path = path.with_suffix(".manifest.json")
                manifest_metadata = manifest_path.lstat()
                if not stat.S_ISREG(manifest_metadata.st_mode) or manifest_metadata.st_mode & 0o222:
                    raise RuntimeError(
                        f"audit partition manifest is not immutable: {manifest_path}"
                    )
                validate_audit_partition_manifest(
                    paths=paths,
                    partition=payload,
                    manifest=read_json(manifest_path),
                )
            for record in payload["records"]:
                if record.get("table") != table:
                    continue
                primary_key = str(record.get("primary_key", ""))
                row = record.get("row")
                if not primary_key or not isinstance(row, dict):
                    raise RuntimeError(f"invalid {table} audit record: {path}")
                previous = records.get(primary_key)
                if previous is not None and previous != row:
                    raise RuntimeError(f"conflicting {table} audit record: {primary_key}")
                records[primary_key] = dict(row)
    return tuple(records[key] for key in sorted(records))


def read_logical_authority_rows(
    connection: sqlite3.Connection,
    paths: RunPaths,
    *,
    table: str,
    primary_key: str,
) -> tuple[dict[str, Any], ...]:
    """Merge current SQLite rows with immutable archived rows without conflicts."""

    table_name = _safe_sql_identifier(table, name="table")
    key_name = _safe_sql_identifier(primary_key, name="primary_key")
    hot = [dict(row) for row in connection.execute(f"SELECT * FROM {table_name}").fetchall()]
    merged: dict[str, dict[str, Any]] = {}
    for row in [*read_audit_rows(paths, table=table_name), *hot]:
        if key_name not in row:
            raise RuntimeError(f"{table_name} row has no {key_name}")
        identity = str(row[key_name])
        previous = merged.get(identity)
        if previous is not None and previous != row:
            raise RuntimeError(f"conflicting hot and archived {table_name} row: {identity}")
        merged[identity] = row
    return tuple(merged[key] for key in sorted(merged))


def delete_claimed_audit_batch_object(
    paths: RunPaths,
    *,
    relative_path: str,
    expected_sha256: str,
) -> None:
    relative = PurePosixPath(relative_path)
    expected_prefix = PurePosixPath("audit/batches")
    if (
        relative.is_absolute()
        or "\\" in relative_path
        or "\0" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.parts[: len(expected_prefix.parts)] != expected_prefix.parts
    ):
        raise ValueError("audit GC target must be below audit/batches")
    current = paths.shared_root
    for component in relative.parts[:-1]:
        current = current / component
        parent_metadata = current.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise RuntimeError("audit GC target parent must be a non-symlink directory")
    target = current / relative.name
    metadata = target.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
        raise RuntimeError("audit GC target must be one immutable regular batch")
    if sha256_file(target) != expected_sha256:
        raise RuntimeError("audit GC target digest changed")
    target.unlink()
    fsync_directory(target.parent)


def _safe_component(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError(f"{name} must be a safe path component")
    return value


def _safe_sql_identifier(value: Any, *, name: str) -> str:
    """Return one ASCII SQL identifier that cannot alter a generated query."""

    result = _safe_component(value, name=name)
    if (
        not result.isascii()
        or not (result[0].isalpha() or result[0] == "_")
        or any(not (character.isalnum() or character == "_") for character in result)
    ):
        raise ValueError(f"{name} must be an ASCII SQL identifier")
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
