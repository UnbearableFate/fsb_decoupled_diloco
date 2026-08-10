"""Fenced online archive/compaction and identity-checked artifact GC."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import PurePosixPath
from typing import Any

from ...core.config import MaintenanceSection
from ...storage.atomic_io import fsync_directory, read_json, sha256_file
from ...storage.audit_archive import (
    build_audit_batch,
    build_audit_partition,
    delete_claimed_audit_batch_object,
    publish_audit_batch,
    publish_audit_partition,
)
from ...storage.authority import LeaderAuthority, LeaderSession
from ...storage.paths import RunPaths


def delete_claimed_artifact_object(
    paths: RunPaths,
    *,
    relative_path: str,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    """Delete one claimed immutable artifact without following a symlink ancestor."""

    relative = PurePosixPath(relative_path)
    allowed = (
        PurePosixPath("weights/epochs"),
        PurePosixPath("optim/epochs"),
        PurePosixPath("updates/payloads"),
    )
    if (
        relative.is_absolute()
        or "\\" in relative_path
        or "\0" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not any(relative.parts[: len(prefix.parts)] == prefix.parts for prefix in allowed)
    ):
        raise ValueError("artifact GC target is outside an allowed immutable artifact root")
    current = paths.shared_root
    for component in relative.parts[:-1]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("artifact GC target parent must be a non-symlink directory")
    target = current / relative.name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o222
        or int(metadata.st_size) != int(expected_size)
    ):
        raise RuntimeError("artifact GC target immutable identity changed")
    if sha256_file(target) != expected_sha256:
        raise RuntimeError("artifact GC target digest changed")
    target.unlink()
    fsync_directory(target.parent)
    return True


class MaintenanceService:
    """Run one bounded maintenance pass after commits and at terminal close."""

    def __init__(
        self,
        *,
        authority: LeaderAuthority,
        leader: LeaderSession,
        paths: RunPaths,
        config: MaintenanceSection,
        telemetry: Any,
    ) -> None:
        self.authority = authority
        self.leader = leader
        self.paths = paths
        self.config = config
        self.telemetry = telemetry
        self.sequence = 0

    def tick(self, *, force: bool = False) -> dict[str, Any]:
        self.sequence += 1
        result: dict[str, Any] = {
            "archived_batch": None,
            "compacted_partition": None,
            "artifact_gc": [],
            "audit_gc": [],
        }
        latest = self.authority.read.latest_committed_version()
        if latest is not None and latest.version > 0:
            cutoff = latest.version - 1
            records = self.authority.read.audit_history_records(cutoff_version=cutoff)
            if records and (force or len(records) >= self.config.archive_batch_rows):
                archive_identity = hashlib.sha256(
                    json.dumps(
                        {"cutoff_version": cutoff, "records": records},
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()[:32]
                batch_id = f"authority-through-v{cutoff}-{archive_identity}"
                payload = build_audit_batch(
                    batch_id=batch_id,
                    record_kind="authority_history",
                    cutoff_version=cutoff,
                    records=records,
                )
                path, digest = publish_audit_batch(self.paths, payload)
                archived = self.leader.archive_audit_batch(
                    command_id=f"maintenance-archive-{batch_id}",
                    batch_id=batch_id,
                    cutoff_version=cutoff,
                    relative_path=self.paths.relative(path),
                    sha256=digest,
                )
                result["archived_batch"] = archived
                self.telemetry.event(
                    "maintenance_audit_archived",
                    batch_id=batch_id,
                    cutoff_version=cutoff,
                    row_count=len(records),
                )
        hot = self.authority.read.audit_hot_batches()
        if len(hot) >= self.config.recent_batch_dedup_count:
            sources = hot[: self.config.recent_batch_dedup_count]
            source_payloads = [
                read_json(self.paths.shared_root / str(row["relative_path"])) for row in sources
            ]
            identity = hashlib.sha256(
                json.dumps(
                    [(row["archive_batch_id"], row["sha256"]) for row in sources],
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:32]
            partition_id = f"authority-{identity}"
            partition = build_audit_partition(
                partition_id=partition_id,
                record_kind="authority_history",
                batches=source_payloads,
            )
            partition_path, partition_sha, manifest_path, manifest_sha = publish_audit_partition(
                self.paths, partition
            )
            result["compacted_partition"] = self.leader.compact_audit_batches(
                command_id=f"maintenance-compact-{partition_id}",
                partition_id=partition_id,
                record_kind="authority_history",
                batch_ids=tuple(str(row["archive_batch_id"]) for row in sources),
                partition_relative_path=self.paths.relative(partition_path),
                partition_sha256=partition_sha,
                manifest_relative_path=self.paths.relative(manifest_path),
                manifest_sha256=manifest_sha,
            )
            self.telemetry.event(
                "maintenance_audit_compacted",
                partition_id=partition_id,
                source_batch_count=len(sources),
            )
        artifact_claims = (
            self.leader.claim_orphan_gc(
                command_id=(
                    f"maintenance-artifact-claim-e{self.leader.token.epoch}-n{self.sequence}"
                ),
                limit=max(64, self.config.archive_batch_rows * 4),
            )
            if self.authority.read.artifact_gc_ready(claimant_epoch=self.leader.token.epoch)
            else ()
        )
        if artifact_claims:
            for claim in artifact_claims:
                delete_claimed_artifact_object(
                    self.paths,
                    relative_path=str(claim["relative_path"]),
                    expected_size=int(claim["size_bytes"]),
                    expected_sha256=str(claim["sha256"]),
                )
            completed = self.leader.complete_artifact_gc(
                command_id=(
                    f"maintenance-artifact-complete-e{self.leader.token.epoch}-n{self.sequence}"
                ),
                relative_paths=tuple(str(item["relative_path"]) for item in artifact_claims),
            )
            result["artifact_gc"] = list(completed)
        audit_claims = (
            self.leader.claim_audit_gc(
                command_id=f"maintenance-audit-claim-e{self.leader.token.epoch}-n{self.sequence}",
                limit=max(64, self.config.recent_batch_dedup_count),
            )
            if self.authority.read.audit_gc_ready(claimant_epoch=self.leader.token.epoch)
            else ()
        )
        if audit_claims:
            for claim in audit_claims:
                try:
                    delete_claimed_audit_batch_object(
                        self.paths,
                        relative_path=str(claim["relative_path"]),
                        expected_sha256=str(claim["sha256"]),
                    )
                except FileNotFoundError:
                    pass
            completed = self.leader.complete_audit_gc(
                command_id=f"maintenance-audit-complete-e{self.leader.token.epoch}-n{self.sequence}",
                relative_paths=tuple(str(item["relative_path"]) for item in audit_claims),
            )
            result["audit_gc"] = list(completed)
        return result
