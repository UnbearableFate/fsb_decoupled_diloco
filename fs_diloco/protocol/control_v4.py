"""Epoch-fenced v4 filesystem controls; fixed paths are repairable caches only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from ..core.versions import CONTROL_FORMAT_VERSION, SYNCER_HEARTBEAT_FORMAT_VERSION
from ..storage.atomic_io import atomic_write_json, publish_immutable_bytes, safe_read_json
from ..storage.leader_lease import CommittedLeaderLease, LeaderToken, StaleLeaderTokenError
from ..storage.paths import RunPaths
from .contributor import ContributorFence, decode_contributor_fence
from .cycle_receipt import CycleReceiptV1

if TYPE_CHECKING:
    from ..storage.authority import CommittedVersion


PLAN03_REQUIREMENTS = frozenset({"AUTH-04", "AUTH-07", "P4-MIGRATE"})


@dataclass(frozen=True)
class CurrentControl:
    epoch: int
    owner_id: str
    heartbeat: dict[str, Any]
    latest: dict[str, Any] | None
    drain: dict[str, Any] | None
    terminal: dict[str, Any] | None


@dataclass(frozen=True)
class ReceiptBarrierResult:
    kind: Literal["ack", "drain", "terminal"]
    payload: dict[str, Any]


class V4ControlPublisher:
    def __init__(
        self,
        paths: RunPaths,
        token: LeaderToken,
    ) -> None:
        self.paths = paths
        self.token = token

    def publish_heartbeat(self, lease: CommittedLeaderLease) -> dict[str, Any]:
        if lease.token != self.token:
            raise StaleLeaderTokenError("heartbeat lease snapshot token mismatch")
        if time.time() >= lease.lease_expires_at:
            raise StaleLeaderTokenError("committed leader lease already expired")
        payload = {
            "format_version": SYNCER_HEARTBEAT_FORMAT_VERSION,
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "heartbeat_seq": lease.heartbeat_seq,
            "renewed_at": lease.renewed_at,
            "lease_expires_at": lease.lease_expires_at,
        }
        payload["payload_sha256"] = _payload_sha256(payload)
        atomic_write_json(
            self.paths.syncer_heartbeat_path(self.token.epoch, self.token.owner_id), payload
        )
        return payload

    def publish_latest(self, version: CommittedVersion) -> dict[str, Any]:
        payload = {
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "latest",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "source_commit_epoch": version.committed_by_epoch,
            "source_commit_owner_id": version.committed_by_owner_id,
            "version": version.version,
            "publication_id": version.publication_id,
            "weight_path": version.weight_relative_path,
            "optim_path": version.optim_relative_path,
            "weight_size_bytes": version.weight_size,
            "optim_size_bytes": version.optim_size,
            "weight_sha256": version.weight_sha256,
            "optim_sha256": version.optim_sha256,
            "theta_sha256": version.theta_sha256,
            "direct_weight_tokens_applied": version.direct_weight_tokens_applied,
            "published_at": version.committed_at,
        }
        pointer = self.paths.epoch_version_pointer_path(
            self.token.epoch, self.token.owner_id, version.version
        )
        digest = _publish_json(pointer, payload)
        head = {
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "latest_head",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "version": version.version,
            "pointer_path": self.paths.relative(pointer),
            "pointer_sha256": digest,
        }
        atomic_write_json(self.paths.epoch_head_path(self.token.epoch, self.token.owner_id), head)
        atomic_write_json(self.paths.latest_json, payload)
        return payload

    def publish_terminal(self, payload: dict[str, Any]) -> Path:
        terminal = {
            **payload,
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "terminal",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
        }
        generation = int(terminal["generation"])
        path = self.paths.epoch_stop_path(self.token.epoch, self.token.owner_id, generation)
        _publish_json(path, terminal)
        atomic_write_json(self.paths.stop_json, terminal)
        atomic_write_json(
            self.paths.summary_json,
            {
                "format_version": CONTROL_FORMAT_VERSION,
                "run_id": self.token.run_id,
                "authority": "full_protocol_v4",
                "all_learners_stopped": terminal.get("state") == "finalized",
                "final_version": int(terminal["final_version"]),
                "direct_weight_tokens_applied": int(terminal["direct_weight_tokens_applied"]),
                "stop_reason": terminal["stop_reason"],
                "terminal_generation": generation,
                "finalized_by_epoch": int(terminal["finalized_by_epoch"]),
                "finalized_by_owner_id": terminal["finalized_by_owner_id"],
                "finalized_at": float(terminal["finalized_at"]),
            },
        )
        return path

    def publish_drain(self, payload: dict[str, Any]) -> Path:
        drain = {
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "drain",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "generation": int(payload["generation"]),
            "reason": payload["reason"],
            "hard_crash_cycle_token_budget": int(payload["hard_crash_cycle_token_budget"]),
        }
        path = self.paths.epoch_membership_dir(self.token.epoch, self.token.owner_id)
        path = path.parent / "terminal" / f"drain_g{drain['generation']:06d}.json"
        _publish_json(path, drain)
        return path

    def publish_error(
        self,
        *,
        attempt_id: str,
        error_type: str,
        message: str,
    ) -> Path:
        payload = {
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "candidate_error",
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "attempt_id": attempt_id,
            "error_type": error_type,
            "message": message,
        }
        path = (
            self.paths.epoch_membership_dir(self.token.epoch, self.token.owner_id).parent
            / "errors"
            / f"{attempt_id}.json"
        )
        _publish_json(path, payload)
        return path

    def publish_receipt_ack(
        self,
        receipt: CycleReceiptV1,
        *,
        descriptor_sha256: str,
    ) -> Path:
        payload = {
            "format_version": CONTROL_FORMAT_VERSION,
            "kind": "receipt_ack",
            "run_id": self.token.run_id,
            "descriptor_sha256": descriptor_sha256,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "stable_contributor_key": receipt.stable_contributor_key,
            "cycle_seq": receipt.cycle_seq,
            "receipt_id": receipt.receipt_id,
            "receipt_sha256": receipt.immutable_sha256(),
            "fence": receipt.contributor_fence.as_dict(),
        }
        path = self.paths.epoch_receipt_ack_path(
            self.token.epoch,
            self.token.owner_id,
            receipt.stable_contributor_key,
            receipt.cycle_seq,
        )
        _publish_json(path, payload)
        return path


def read_current_control(
    paths: RunPaths,
    *,
    run_id: str,
    now: float | None = None,
    max_clock_skew_seconds: float = 0.0,
) -> CurrentControl | None:
    observed_at = time.time() if now is None else float(now)
    candidates: list[CurrentControl] = []
    if not paths.syncer_epochs.is_dir():
        return None
    for directory in sorted(paths.syncer_epochs.glob("e*_*")):
        heartbeat = safe_read_json(directory / "heartbeat.json")
        if not _valid_heartbeat(
            paths,
            directory,
            heartbeat,
            run_id=run_id,
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew_seconds,
        ):
            continue
        if not isinstance(heartbeat, dict):
            continue
        epoch = int(heartbeat["epoch"])
        owner = str(heartbeat["owner_id"])
        latest = _read_latest(paths, directory, run_id=run_id, epoch=epoch, owner_id=owner)
        drain = _read_drain(directory, run_id=run_id, epoch=epoch, owner_id=owner)
        terminal = _read_terminal(directory, run_id=run_id, epoch=epoch, owner_id=owner)
        candidates.append(CurrentControl(epoch, owner, heartbeat, latest, drain, terminal))
    return max(candidates, key=lambda item: item.epoch) if candidates else None


def wait_for_current_latest(
    paths: RunPaths,
    *,
    run_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    max_clock_skew_seconds: float,
    newer_than: int = -1,
) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout_seconds)
    while time.monotonic() < deadline:
        current = read_current_control(
            paths,
            run_id=run_id,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
        if current is not None:
            if current.terminal is not None:
                raise StopIteration(current.terminal)
            if current.latest is not None and int(current.latest["version"]) > newer_than:
                return current.latest
        time.sleep(float(poll_seconds))
    raise TimeoutError("timed out waiting for current fenced global version")


def current_latest_if_newer(
    paths: RunPaths,
    *,
    run_id: str,
    newer_than: int,
    max_clock_skew_seconds: float,
) -> dict[str, Any] | None:
    current = read_current_control(
        paths,
        run_id=run_id,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if current is None or current.latest is None:
        return None
    if int(current.latest["version"]) <= int(newer_than):
        return None
    return current.latest


def wait_for_receipt_barrier(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    receipt: CycleReceiptV1,
    timeout_seconds: float,
    poll_seconds: float,
    max_clock_skew_seconds: float,
) -> ReceiptBarrierResult:
    deadline = time.monotonic() + float(timeout_seconds)
    expected = {
        "format_version": CONTROL_FORMAT_VERSION,
        "kind": "receipt_ack",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "stable_contributor_key": receipt.stable_contributor_key,
        "cycle_seq": receipt.cycle_seq,
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": receipt.immutable_sha256(),
        "fence": receipt.contributor_fence.as_dict(),
    }
    while time.monotonic() < deadline:
        current = read_current_control(
            paths,
            run_id=run_id,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
        if current is not None:
            if current.terminal is not None:
                return ReceiptBarrierResult("terminal", current.terminal)
            if current.drain is not None:
                return ReceiptBarrierResult("drain", current.drain)
            path = paths.epoch_receipt_ack_path(
                current.epoch,
                current.owner_id,
                receipt.stable_contributor_key,
                receipt.cycle_seq,
            )
            payload = safe_read_json(path)
            if isinstance(payload, dict):
                epoch = payload.get("epoch")
                if (
                    not isinstance(epoch, bool)
                    and isinstance(epoch, int)
                    and epoch == current.epoch
                    and payload.get("owner_id") == current.owner_id
                    and all(payload.get(key) == value for key, value in expected.items())
                    and set(payload) == {*expected, "epoch", "owner_id"}
                ):
                    return ReceiptBarrierResult("ack", payload)
        time.sleep(float(poll_seconds))
    raise TimeoutError("timed out waiting for current-epoch receipt acknowledgement")


def _read_latest(
    paths: RunPaths,
    directory: Path,
    *,
    run_id: str,
    epoch: int,
    owner_id: str,
) -> dict[str, Any] | None:
    head = safe_read_json(directory / "latest" / "head.json")
    if not _valid_latest_head(head, run_id=run_id, epoch=epoch, owner_id=owner_id):
        return None
    assert isinstance(head, dict)
    version = int(head["version"])
    pointer = paths.epoch_version_pointer_path(epoch, owner_id, version)
    expected_relative = pointer.relative_to(paths.shared_root).as_posix()
    if head["pointer_path"] != expected_relative:
        return None
    try:
        data = _read_anchored_regular_file(paths.shared_root, PurePosixPath(expected_relative))
    except (OSError, ValueError):
        return None
    if hashlib.sha256(data).hexdigest() != head["pointer_sha256"]:
        return None
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not _valid_latest_payload(
        payload,
        run_id=run_id,
        epoch=epoch,
        owner_id=owner_id,
        version=version,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _valid_heartbeat(
    paths: RunPaths,
    directory: Path,
    heartbeat: Any,
    *,
    run_id: str,
    observed_at: float,
    max_clock_skew_seconds: float,
) -> bool:
    fields = {
        "format_version",
        "run_id",
        "epoch",
        "owner_id",
        "heartbeat_seq",
        "renewed_at",
        "lease_expires_at",
        "payload_sha256",
    }
    if not isinstance(heartbeat, dict) or set(heartbeat) != fields:
        return False
    epoch = heartbeat.get("epoch")
    sequence = heartbeat.get("heartbeat_seq")
    owner = heartbeat.get("owner_id")
    renewed_at = heartbeat.get("renewed_at")
    expires_at = heartbeat.get("lease_expires_at")
    if (
        heartbeat.get("format_version") != SYNCER_HEARTBEAT_FORMAT_VERSION
        or heartbeat.get("run_id") != run_id
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 1
        or not isinstance(owner, str)
        or not owner
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or isinstance(renewed_at, bool)
        or not isinstance(renewed_at, (int, float))
        or not math.isfinite(float(renewed_at))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(float(expires_at))
        or float(expires_at) <= float(renewed_at)
        or directory != paths.syncer_epoch_dir(epoch, owner)
    ):
        return False
    core = {key: value for key, value in heartbeat.items() if key != "payload_sha256"}
    digest = heartbeat.get("payload_sha256")
    return (
        _is_sha256(digest)
        and digest == _payload_sha256(core)
        and observed_at <= float(expires_at) + float(max_clock_skew_seconds)
    )


def _valid_latest_head(head: Any, *, run_id: str, epoch: int, owner_id: str) -> bool:
    fields = {
        "format_version",
        "kind",
        "run_id",
        "epoch",
        "owner_id",
        "version",
        "pointer_path",
        "pointer_sha256",
    }
    if not isinstance(head, dict) or set(head) != fields:
        return False
    version = head.get("version")
    return (
        head.get("format_version") == CONTROL_FORMAT_VERSION
        and head.get("kind") == "latest_head"
        and head.get("run_id") == run_id
        and head.get("epoch") == epoch
        and head.get("owner_id") == owner_id
        and not isinstance(version, bool)
        and isinstance(version, int)
        and version >= 0
        and isinstance(head.get("pointer_path"), str)
        and _is_sha256(head.get("pointer_sha256"))
    )


def _valid_latest_payload(
    payload: Any,
    *,
    run_id: str,
    epoch: int,
    owner_id: str,
    version: int,
) -> bool:
    fields = {
        "format_version",
        "kind",
        "run_id",
        "epoch",
        "owner_id",
        "source_commit_epoch",
        "source_commit_owner_id",
        "version",
        "publication_id",
        "weight_path",
        "optim_path",
        "weight_size_bytes",
        "optim_size_bytes",
        "weight_sha256",
        "optim_sha256",
        "theta_sha256",
        "direct_weight_tokens_applied",
        "published_at",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        return False
    source_epoch = payload.get("source_commit_epoch")
    weight_size = payload.get("weight_size_bytes")
    optim_size = payload.get("optim_size_bytes")
    tokens = payload.get("direct_weight_tokens_applied")
    published_at = payload.get("published_at")
    return (
        payload.get("format_version") == CONTROL_FORMAT_VERSION
        and payload.get("kind") == "latest"
        and payload.get("run_id") == run_id
        and payload.get("epoch") == epoch
        and payload.get("owner_id") == owner_id
        and payload.get("version") == version
        and not isinstance(source_epoch, bool)
        and isinstance(source_epoch, int)
        and source_epoch >= 1
        and isinstance(payload.get("source_commit_owner_id"), str)
        and bool(payload["source_commit_owner_id"])
        and isinstance(payload.get("publication_id"), str)
        and bool(payload["publication_id"])
        and _is_canonical_relative_path(payload.get("weight_path"))
        and _is_canonical_relative_path(payload.get("optim_path"))
        and not isinstance(weight_size, bool)
        and isinstance(weight_size, int)
        and weight_size >= 0
        and not isinstance(optim_size, bool)
        and isinstance(optim_size, int)
        and optim_size >= 0
        and _is_sha256(payload.get("weight_sha256"))
        and _is_sha256(payload.get("optim_sha256"))
        and _is_sha256(payload.get("theta_sha256"))
        and not isinstance(tokens, bool)
        and isinstance(tokens, int)
        and tokens >= 0
        and not isinstance(published_at, bool)
        and isinstance(published_at, (int, float))
        and math.isfinite(float(published_at))
        and float(published_at) >= 0.0
    )


def _read_anchored_regular_file(root: Path, relative: PurePosixPath) -> bytes:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("control pointer path must be canonical and relative")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            relative.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("control pointer is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            named = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
            final = os.fstat(file_descriptor)
            if (named.st_dev, named.st_ino) != (final.st_dev, final.st_ino):
                raise OSError("control pointer identity changed while reading")
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_canonical_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    path = PurePosixPath(value)
    return all(part not in {"", ".", ".."} for part in path.parts) and path.as_posix() == value


def _read_terminal(
    directory: Path, *, run_id: str, epoch: int, owner_id: str
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    terminal_dir = directory / "terminal"
    if not terminal_dir.is_dir():
        return None
    for path in terminal_dir.glob("stop_g*.json"):
        payload = safe_read_json(path)
        if (
            isinstance(payload, dict)
            and payload.get("run_id") == run_id
            and int(payload.get("epoch", -1)) == epoch
            and payload.get("owner_id") == owner_id
        ):
            candidates.append(payload)
    return max(candidates, key=lambda item: int(item["generation"])) if candidates else None


def _read_drain(
    directory: Path, *, run_id: str, epoch: int, owner_id: str
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    terminal_dir = directory / "terminal"
    if not terminal_dir.is_dir():
        return None
    for path in terminal_dir.glob("drain_g*.json"):
        payload = safe_read_json(path)
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "drain"
            and payload.get("run_id") == run_id
            and int(payload.get("epoch", -1)) == epoch
            and payload.get("owner_id") == owner_id
        ):
            candidates.append(payload)
    return max(candidates, key=lambda item: int(item["generation"])) if candidates else None


def publish_terminal_ack(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    generation: int,
    actor_id: str,
    attempt_id: str,
    fence: ContributorFence,
    final_cycle_seq: int,
    final_update_id: str | None,
) -> Path:
    payload = {
        "format_version": CONTROL_FORMAT_VERSION,
        "kind": "terminal_ack",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "generation": int(generation),
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "fence": fence.as_dict(),
        "final_cycle_seq": int(final_cycle_seq),
        "final_update_id": final_update_id,
    }
    path = (
        paths.shared_root
        / "updates"
        / "terminal_acks"
        / fence.stable_contributor_key
        / f"{attempt_id}_g{generation:06d}.json"
    )
    _publish_json(path, payload)
    return path


def iter_terminal_acks(
    paths: RunPaths,
) -> tuple[tuple[Path, dict[str, Any], ContributorFence], ...]:
    root = paths.shared_root / "updates" / "terminal_acks"
    if not root.is_dir():
        return ()
    results: list[tuple[Path, dict[str, Any], ContributorFence]] = []
    for path in sorted(root.glob("*/*.json")):
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        try:
            fence = decode_contributor_fence(payload.get("fence"))
        except ValueError:
            continue
        results.append((path, payload, fence))
    return tuple(results)


def _publish_json(path: Path, payload: dict[str, Any]) -> str:
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    publish_immutable_bytes(path, data)
    return hashlib.sha256(data).hexdigest()


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
