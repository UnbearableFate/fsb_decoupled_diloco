"""Epoch-scoped HA control publication and authoritative readers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.constants import (
    CONTROL_EPOCH_FORMAT_VERSION,
    SYNCER_HEARTBEAT_FORMAT_VERSION,
)
from ..storage.atomic_io import atomic_write_json, safe_read_json
from ..storage.fenced_store import FencedSQLiteStore
from ..storage.leader_lease import LeaderToken
from ..storage.paths import RunPaths


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _publish_immutable_json(path: Path, payload: dict[str, Any]) -> str:
    """Publish once without replacing a concurrently-created canonical artifact."""
    data = _json_bytes(payload)
    digest = _sha256_bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            observed = path.read_bytes()
            if observed != data:
                raise RuntimeError(f"immutable control publication collision: {path}")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return digest
    finally:
        temporary.unlink(missing_ok=True)


def _verified_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    data = path.read_bytes()
    observed = _sha256_bytes(data)
    if observed != expected_sha256:
        raise RuntimeError(f"control checksum mismatch for {path}: {observed} != {expected_sha256}")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise RuntimeError(f"control artifact must contain an object: {path}")
    return payload


_EPOCH_DIRECTORY_RE = re.compile(r"^e(?P<epoch>[0-9]{6})_(?P<owner>[0-9a-f]{12})$")


@dataclass(frozen=True)
class _FilesystemEpoch:
    epoch: int
    owner_id: str
    directory: Path
    heartbeat: dict[str, Any] | None
    latest: dict[str, Any] | None


class EpochControlPublisher:
    def __init__(
        self,
        paths: RunPaths,
        store: FencedSQLiteStore,
        token: LeaderToken,
    ) -> None:
        self.paths = paths
        self.store = store
        self.token = token

    def publish_heartbeat(self, lease_row: dict[str, Any]) -> dict[str, Any]:
        if (
            int(lease_row["epoch"]) != self.token.epoch
            or str(lease_row["owner_id"]) != self.token.owner_id
            or str(lease_row["state"]) != "active"
        ):
            raise RuntimeError("heartbeat lease row does not match publisher token")
        core = {
            "format_version": SYNCER_HEARTBEAT_FORMAT_VERSION,
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "heartbeat_seq": int(lease_row["heartbeat_seq"]),
            "renewed_at": float(lease_row["renewed_at"]),
            "lease_expires_at": float(lease_row["lease_expires_at"]),
        }
        core["payload_sha256"] = _sha256_bytes(_json_bytes(core))
        path = self.paths.syncer_heartbeat_path(self.token.epoch, self.token.owner_id)
        atomic_write_json(path, core)
        return core

    def publish_latest(self, version_row: dict[str, Any]) -> dict[str, Any]:
        epoch = int(version_row["commit_epoch"])
        owner = str(version_row["commit_owner_id"])
        if epoch > self.token.epoch:
            raise RuntimeError("cannot publish a future leader's version")
        payload = {
            "format_version": CONTROL_EPOCH_FORMAT_VERSION,
            "kind": "latest",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "source_commit_epoch": epoch,
            "source_commit_owner_id": owner,
            "version": int(version_row["version"]),
            "publication_id": str(version_row["publication_id"]),
            "weight_path": str(version_row["weight_path"]),
            "optim_path": str(version_row["optim_path"]),
            "weight_size_bytes": int(version_row["weight_size_bytes"]),
            "optim_size_bytes": int(version_row["optim_size_bytes"]),
            "weight_sha256": version_row.get("weight_sha256"),
            "optim_sha256": version_row.get("optim_sha256"),
            # Canonical artifacts are immutable and retries in the same epoch must
            # reproduce identical bytes. The DB commit timestamp is stable.
            "published_at": float(version_row["created_at"]),
        }
        pointer = self.paths.epoch_version_pointer_path(
            self.token.epoch, self.token.owner_id, payload["version"]
        )
        digest = _publish_immutable_json(pointer, payload)
        relative_path = self.paths.relative(pointer)
        self.store.record_control_publication(
            self.token,
            kind="latest",
            logical_generation=payload["version"],
            relative_path=relative_path,
            sha256=digest,
            created_at=payload["published_at"],
        )
        head = {
            "format_version": CONTROL_EPOCH_FORMAT_VERSION,
            "kind": "latest_head",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "version": payload["version"],
            "pointer_path": relative_path,
            "pointer_sha256": digest,
        }
        atomic_write_json(self.paths.epoch_head_path(self.token.epoch, self.token.owner_id), head)
        # Fixed caches remain convenience mirrors and are deliberately non-authoritative.
        atomic_write_json(self.paths.latest_json, payload)
        return payload

    def repair_latest_from_db(self) -> dict[str, Any] | None:
        row = self.store.latest_global_version()
        if row is None:
            return None
        return self.publish_latest(row)

    def publish_terminal(
        self,
        terminal_row: dict[str, Any],
        *,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if int(terminal_row["finalized_by_epoch"]) != self.token.epoch:
            raise RuntimeError("only the finalizing epoch may publish terminal control")
        generation = int(terminal_row["generation"])
        stop_core = {
            "format_version": CONTROL_EPOCH_FORMAT_VERSION,
            "kind": "stop",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "generation": generation,
            "stop_reason": str(terminal_row["stop_reason"]),
            "reason": str(terminal_row["stop_reason"]),
            "final_version": int(terminal_row["final_version"]),
            "total_seen_tokens": int(terminal_row["total_seen_tokens"]),
            "finalized_at": float(terminal_row["finalized_at"]),
        }
        stop_payload = {
            **stop_core,
            "payload_sha256": _sha256_bytes(_json_bytes(stop_core)),
        }
        stop_path = self.paths.epoch_stop_path(self.token.epoch, self.token.owner_id, generation)
        stop_digest = _publish_immutable_json(stop_path, stop_payload)
        self.store.record_control_publication(
            self.token,
            kind="stop",
            logical_generation=generation,
            relative_path=self.paths.relative(stop_path),
            sha256=stop_digest,
            created_at=float(terminal_row["finalized_at"]),
        )
        atomic_write_json(self.paths.stop_json, stop_payload)
        if summary is not None:
            summary_payload = {
                **summary,
                "format_version": CONTROL_EPOCH_FORMAT_VERSION,
                "kind": "summary",
                "run_id": self.token.run_id,
                "epoch": self.token.epoch,
                "owner_id": self.token.owner_id,
                "generation": generation,
            }
            summary_path = self.paths.epoch_summary_path(
                self.token.epoch, self.token.owner_id, generation
            )
            summary_digest = _publish_immutable_json(summary_path, summary_payload)
            self.store.record_control_publication(
                self.token,
                kind="summary",
                logical_generation=generation,
                relative_path=self.paths.relative(summary_path),
                sha256=summary_digest,
            )
            atomic_write_json(self.paths.summary_json, summary_payload)
        return stop_payload


class EpochControlReader:
    """Resolve the highest valid epoch without opening the authority database."""

    def __init__(self, paths: RunPaths, *, run_id: str) -> None:
        self.paths = paths
        self.run_id = run_id

    def close(self) -> None:
        """Compatibility no-op; filesystem readers hold no persistent resource."""

    def _coordinates(self, directory: Path) -> tuple[int, str] | None:
        match = _EPOCH_DIRECTORY_RE.fullmatch(directory.name)
        if match is None:
            return None
        return int(match.group("epoch")), match.group("owner")

    def _heartbeat(
        self,
        directory: Path,
        *,
        epoch: int,
        owner_short: str,
    ) -> dict[str, Any] | None:
        path = directory / "heartbeat.json"
        if not path.is_file():
            return None
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid syncer heartbeat JSON: {path}")
        core = {key: value for key, value in payload.items() if key != "payload_sha256"}
        if payload.get("payload_sha256") != _sha256_bytes(_json_bytes(core)):
            raise RuntimeError(f"syncer heartbeat checksum mismatch: {path}")
        owner_id = str(payload.get("owner_id", ""))
        if (
            payload.get("format_version") != SYNCER_HEARTBEAT_FORMAT_VERSION
            or payload.get("run_id") != self.run_id
            or int(payload.get("epoch", -1)) != epoch
            or not owner_id
            or self.paths.owner_short(owner_id) != owner_short
        ):
            raise RuntimeError(f"syncer heartbeat identity mismatch: {path}")
        return payload

    def _latest(
        self,
        directory: Path,
        *,
        epoch: int,
        owner_short: str,
    ) -> tuple[str, dict[str, Any]] | None:
        head_path = directory / "latest" / "head.json"
        if not head_path.is_file():
            return None
        head = safe_read_json(head_path)
        if not isinstance(head, dict):
            raise RuntimeError(f"invalid canonical latest head: {head_path}")
        owner_id = str(head.get("owner_id", ""))
        version = int(head.get("version", -1))
        expected_pointer = self.paths.epoch_version_pointer_path(epoch, owner_id, version)
        try:
            expected_relative = self.paths.relative(expected_pointer)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"canonical latest path escaped run root: {head_path}") from exc
        if (
            head.get("format_version") != CONTROL_EPOCH_FORMAT_VERSION
            or head.get("kind") != "latest_head"
            or head.get("run_id") != self.run_id
            or int(head.get("epoch", -1)) != epoch
            or not owner_id
            or self.paths.owner_short(owner_id) != owner_short
            or version < 0
            or head.get("pointer_path") != expected_relative
            or not isinstance(head.get("pointer_sha256"), str)
        ):
            raise RuntimeError(f"canonical latest head identity mismatch: {head_path}")
        payload = _verified_json(expected_pointer, str(head["pointer_sha256"]))
        if (
            payload.get("format_version") != CONTROL_EPOCH_FORMAT_VERSION
            or payload.get("kind") != "latest"
            or payload.get("run_id") != self.run_id
            or int(payload.get("epoch", -1)) != epoch
            or payload.get("owner_id") != owner_id
            or int(payload.get("version", -1)) != version
        ):
            raise RuntimeError(f"canonical latest identity mismatch: {expected_pointer}")
        return owner_id, payload

    def _current_epoch(self) -> _FilesystemEpoch | None:
        candidates: list[_FilesystemEpoch] = []
        if not self.paths.syncer_epochs.is_dir():
            return None
        for directory in sorted(self.paths.syncer_epochs.iterdir()):
            if not directory.is_dir():
                continue
            coordinates = self._coordinates(directory)
            if coordinates is None:
                continue
            epoch, owner_short = coordinates
            heartbeat = self._heartbeat(directory, epoch=epoch, owner_short=owner_short)
            latest_result = self._latest(directory, epoch=epoch, owner_short=owner_short)
            if heartbeat is None and latest_result is None:
                continue
            owners = {
                str(payload["owner_id"])
                for payload in (
                    heartbeat,
                    None if latest_result is None else latest_result[1],
                )
                if payload is not None
            }
            if len(owners) != 1:
                raise RuntimeError(f"conflicting epoch owner evidence: {directory}")
            owner_id = next(iter(owners))
            candidates.append(
                _FilesystemEpoch(
                    epoch=epoch,
                    owner_id=owner_id,
                    directory=directory,
                    heartbeat=heartbeat,
                    latest=None if latest_result is None else latest_result[1],
                )
            )
        if not candidates:
            return None
        highest_epoch = max(candidate.epoch for candidate in candidates)
        highest = [candidate for candidate in candidates if candidate.epoch == highest_epoch]
        if len(highest) != 1:
            raise RuntimeError(f"multiple valid owners published the same epoch: {highest_epoch}")
        return highest[0]

    def current_leader(self) -> dict[str, Any] | None:
        current = self._current_epoch()
        if current is None:
            return None
        return {
            "epoch": current.epoch,
            "owner_id": current.owner_id,
            **({} if current.heartbeat is None else current.heartbeat),
        }

    def read_current_heartbeat(self) -> dict[str, Any] | None:
        current = self._current_epoch()
        return None if current is None else current.heartbeat

    def read_current_latest(self) -> dict[str, Any] | None:
        current = self._current_epoch()
        if current is None or current.latest is None:
            return None
        resolved = dict(current.latest)
        for key in ("weight_path", "optim_path"):
            artifact = Path(str(resolved[key]))
            if not artifact.is_absolute():
                artifact = self.paths.shared_root / artifact
            resolved[key] = str(artifact)
        resolved["param_index_path"] = str(self.paths.param_index_json)
        return resolved

    def read_current_terminal(self) -> dict[str, Any] | None:
        current = self._current_epoch()
        if current is None:
            return None
        terminal_dir = current.directory / "terminal"
        if not terminal_dir.is_dir():
            return None
        candidates: list[dict[str, Any]] = []
        for path in sorted(terminal_dir.glob("stop_g*.json")):
            payload = safe_read_json(path)
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid canonical terminal JSON: {path}")
            core = {key: value for key, value in payload.items() if key != "payload_sha256"}
            if payload.get("payload_sha256") != _sha256_bytes(_json_bytes(core)):
                raise RuntimeError(f"canonical terminal checksum mismatch: {path}")
            generation = int(payload.get("generation", -1))
            expected_path = self.paths.epoch_stop_path(current.epoch, current.owner_id, generation)
            if (
                payload.get("format_version") != CONTROL_EPOCH_FORMAT_VERSION
                or payload.get("kind") != "stop"
                or payload.get("run_id") != self.run_id
                or int(payload.get("epoch", -1)) != current.epoch
                or payload.get("owner_id") != current.owner_id
                or generation < 0
                or path != expected_path
            ):
                raise RuntimeError(f"canonical terminal identity mismatch: {path}")
            candidates.append(payload)
        if not candidates:
            return None
        return max(candidates, key=lambda payload: int(payload["generation"]))
