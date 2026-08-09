"""Epoch-fenced v4 filesystem controls; fixed paths are repairable caches only."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..core.versions import CONTROL_FORMAT_VERSION, SYNCER_HEARTBEAT_FORMAT_VERSION
from ..storage.atomic_io import atomic_write_json, publish_immutable_bytes, safe_read_json
from ..storage.leader_lease import LeaderToken
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
        *,
        lease_duration_seconds: float,
    ) -> None:
        self.paths = paths
        self.token = token
        self.lease_duration_seconds = float(lease_duration_seconds)
        self._heartbeat_seq = 0

    def publish_heartbeat(self, *, renewed_at: float | None = None) -> dict[str, Any]:
        now = time.time() if renewed_at is None else float(renewed_at)
        self._heartbeat_seq += 1
        payload = {
            "format_version": SYNCER_HEARTBEAT_FORMAT_VERSION,
            "run_id": self.token.run_id,
            "epoch": self.token.epoch,
            "owner_id": self.token.owner_id,
            "heartbeat_seq": self._heartbeat_seq,
            "renewed_at": now,
            "lease_expires_at": now + self.lease_duration_seconds,
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
        if not isinstance(heartbeat, dict):
            continue
        if heartbeat.get("run_id") != run_id:
            continue
        core = {key: value for key, value in heartbeat.items() if key != "payload_sha256"}
        if heartbeat.get("payload_sha256") != _payload_sha256(core):
            continue
        if observed_at > float(heartbeat["lease_expires_at"]) + float(max_clock_skew_seconds):
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
    if not isinstance(head, dict):
        return None
    if (
        head.get("run_id") != run_id
        or int(head.get("epoch", -1)) != epoch
        or head.get("owner_id") != owner_id
    ):
        return None
    pointer = paths.shared_root / str(head.get("pointer_path", ""))
    try:
        data = pointer.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != head.get("pointer_sha256"):
        return None
    payload = json.loads(data)
    if not isinstance(payload, dict) or payload.get("publication_id") is None:
        return None
    return payload


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
