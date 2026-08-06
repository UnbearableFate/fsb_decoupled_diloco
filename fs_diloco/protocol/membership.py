"""Dynamic learner identity, registration, admission, and control artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.constants import DYNAMIC_LEARNER_ID_PREFIX, MEMBERSHIP_FORMAT_VERSION
from ..storage.atomic_io import atomic_write_json, safe_read_json, sha256_file
from ..storage.leader_lease import LeaderToken
from ..storage.paths import RunPaths


_INSTANCE_RE = re.compile(
    rf"^{re.escape(DYNAMIC_LEARNER_ID_PREFIX)}"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_EPOCH_DIR_RE = re.compile(r"^e(?P<epoch>[0-9]{6})_(?P<owner>[0-9a-f]{12})$")
_PBS_JOB_ID_RE = re.compile(r"^[0-9]+(?:\.[A-Za-z0-9_.-]+)?$")


def new_learner_instance_id() -> str:
    return f"{DYNAMIC_LEARNER_ID_PREFIX}{uuid.uuid4()}"


def bootstrap_request_id(
    *, run_id: str, bootstrap_slot: int, config_fingerprint: str
) -> str:
    encoded = json.dumps(
        [run_id, "bootstrap", int(bootstrap_slot), config_fingerprint],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_learner_instance_id(instance_id: str) -> None:
    if _INSTANCE_RE.fullmatch(instance_id) is None:
        raise ValueError(f"invalid dynamic learner instance ID: {instance_id!r}")


def placement_id(*, hostname: str | None = None, gpu_identity: str | None = None) -> str:
    host = (hostname or socket.gethostname()).strip()
    device = (gpu_identity or os.environ.get("CUDA_VISIBLE_DEVICES") or "cpu").strip()
    if not host or any(character in host for character in "\n\r:"):
        raise ValueError("hostname cannot be represented in a placement ID")
    if not device or any(character in device for character in "\n\r"):
        raise ValueError("device identity cannot be represented in a placement ID")
    return f"{host}:{device}"


def _sha256_payload(payload: dict[str, Any], *, excluded: str) -> str:
    canonical = {key: value for key, value in payload.items() if key != excluded}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_registration_request(
    payload: dict[str, Any],
    *,
    path: Path,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
    now: float | None = None,
) -> None:
    now = time.time() if now is None else float(now)
    if payload.get("format_version") != MEMBERSHIP_FORMAT_VERSION:
        raise ValueError("registration format version mismatch")
    instance_id = str(payload.get("instance_id", ""))
    validate_learner_instance_id(instance_id)
    if (
        path.name != f"{instance_id}.json"
        or path.parent.name != "registration_requests"
    ):
        raise ValueError("registration request path does not belong to its instance")
    expected = {
        "run_id": run_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
    }
    mismatches = {
        key: (payload.get(key), value)
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise ValueError(f"registration identity mismatch: {mismatches}")
    if not payload.get("placement_id"):
        raise ValueError("registration request is missing placement_id")
    if not payload.get("launch_request_id"):
        raise ValueError("registration request is missing launch_request_id")
    created_at = float(payload["created_at"])
    expires_at = float(payload["expires_at"])
    if expires_at <= created_at:
        raise ValueError("registration request expiry must follow creation")
    if now > expires_at:
        raise ValueError("registration request expired")
    digest = str(payload.get("request_sha256", ""))
    if not digest or digest != _sha256_payload(payload, excluded="request_sha256"):
        raise ValueError("registration request checksum mismatch")


def write_registration_request(
    paths: RunPaths,
    *,
    run_id: str,
    instance_id: str,
    placement: str,
    launch_request_id: str,
    source_fingerprint: str,
    config_sha256: str,
    ttl_seconds: float,
    pbs_job_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
    gpu_identity: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    validate_learner_instance_id(instance_id)
    timestamp = time.time() if now is None else float(now)
    if ttl_seconds <= 0.0:
        raise ValueError("registration request TTL must be > 0")
    payload: dict[str, Any] = {
        "format_version": MEMBERSHIP_FORMAT_VERSION,
        "run_id": run_id,
        "instance_id": instance_id,
        "placement_id": placement,
        "launch_request_id": launch_request_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
        "pbs_job_id": pbs_job_id,
        "hostname": hostname or socket.gethostname(),
        "pid": os.getpid() if pid is None else int(pid),
        "gpu_identity": gpu_identity,
        "created_at": timestamp,
        "expires_at": timestamp + float(ttl_seconds),
    }
    payload["request_sha256"] = _sha256_payload(payload, excluded="request_sha256")
    path = paths.registration_request_path(instance_id)
    if not paths.registration_requests.is_dir():
        raise RuntimeError("registration request authority directory is missing")
    atomic_write_json(path, payload)
    return payload


def write_bootstrap_scheduler_jobs(
    paths: RunPaths,
    *,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
    config_fingerprint: str,
    jobs_by_slot: dict[int, str],
    now: float | None = None,
) -> dict[str, Any]:
    """Publish external bootstrap qsub receipts for leader reconciliation."""

    rows: list[dict[str, Any]] = []
    for slot, job_id in sorted(jobs_by_slot.items()):
        slot = int(slot)
        job_id = str(job_id).strip()
        if slot < 0:
            raise ValueError("bootstrap scheduler slot must be non-negative")
        if _PBS_JOB_ID_RE.fullmatch(job_id) is None:
            raise ValueError(f"invalid bootstrap scheduler job ID: {job_id!r}")
        rows.append(
            {
                "bootstrap_slot": slot,
                "request_id": bootstrap_request_id(
                    run_id=run_id,
                    bootstrap_slot=slot,
                    config_fingerprint=config_fingerprint,
                ),
                "pbs_job_id": job_id,
            }
        )
    payload: dict[str, Any] = {
        "format_version": MEMBERSHIP_FORMAT_VERSION,
        "run_id": run_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
        "config_fingerprint": config_fingerprint,
        "bootstrap_jobs": rows,
        "published_at": time.time() if now is None else float(now),
    }
    payload["payload_sha256"] = _sha256_payload(payload, excluded="payload_sha256")
    atomic_write_json(paths.bootstrap_scheduler_jobs_json, payload)
    return payload


def read_bootstrap_scheduler_jobs(
    paths: RunPaths,
    *,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
    config_fingerprint: str,
) -> list[dict[str, Any]]:
    if not paths.bootstrap_scheduler_jobs_json.exists():
        return []
    payload = safe_read_json(paths.bootstrap_scheduler_jobs_json)
    if not isinstance(payload, dict):
        raise RuntimeError("bootstrap scheduler manifest is not valid JSON")
    expected = {
        "format_version": MEMBERSHIP_FORMAT_VERSION,
        "run_id": run_id,
        "source_fingerprint": source_fingerprint,
        "config_sha256": config_sha256,
        "config_fingerprint": config_fingerprint,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("bootstrap scheduler manifest identity mismatch")
    if payload.get("payload_sha256") != _sha256_payload(
        payload,
        excluded="payload_sha256",
    ):
        raise RuntimeError("bootstrap scheduler manifest checksum mismatch")
    rows = payload.get("bootstrap_jobs")
    if not isinstance(rows, list):
        raise RuntimeError("bootstrap scheduler manifest jobs are invalid")
    validated: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    seen_requests: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("bootstrap scheduler manifest row is invalid")
        try:
            slot = int(row["bootstrap_slot"])
            request_id = str(row["request_id"])
            job_id = str(row["pbs_job_id"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("bootstrap scheduler manifest row is incomplete")
        expected_request = bootstrap_request_id(
            run_id=run_id,
            bootstrap_slot=slot,
            config_fingerprint=config_fingerprint,
        )
        if (
            slot < 0
            or slot in seen_slots
            or request_id in seen_requests
            or request_id != expected_request
            or _PBS_JOB_ID_RE.fullmatch(job_id) is None
        ):
            raise RuntimeError("bootstrap scheduler manifest row identity mismatch")
        seen_slots.add(slot)
        seen_requests.add(request_id)
        validated.append(
            {
                "bootstrap_slot": slot,
                "request_id": request_id,
                "pbs_job_id": job_id,
            }
        )
    return validated


@dataclass(frozen=True)
class Admission:
    instance_id: str
    placement_id: str
    placement_epoch: int
    stream_id: int
    stream_epoch: int
    admission_generation: int
    admission_token: str
    launch_request_id: str
    stream_restarted: bool
    published_by_epoch: int
    published_by_owner_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Admission":
        content = dict(payload)
        digest = str(content.get("payload_sha256", ""))
        if not digest or digest != _sha256_payload(content, excluded="payload_sha256"):
            raise ValueError("admission artifact checksum mismatch")
        if content.get("format_version") != MEMBERSHIP_FORMAT_VERSION:
            raise ValueError("admission artifact format mismatch")
        if content.get("state") != "admitted":
            raise ValueError(f"registration was not admitted: {content.get('state')}")
        validate_learner_instance_id(str(content.get("instance_id", "")))
        return cls(
            instance_id=str(content["instance_id"]),
            placement_id=str(content["placement_id"]),
            placement_epoch=int(content["placement_epoch"]),
            stream_id=int(content["stream_id"]),
            stream_epoch=int(content["stream_epoch"]),
            admission_generation=int(content["admission_generation"]),
            admission_token=str(content["admission_token"]),
            launch_request_id=str(content["launch_request_id"]),
            stream_restarted=bool(content.get("stream_restarted", False)),
            published_by_epoch=int(content["published_by_epoch"]),
            published_by_owner_id=str(content["published_by_owner_id"]),
        )


class MembershipPublisher:
    def __init__(self, paths: RunPaths, store: Any, token: LeaderToken) -> None:
        self.paths = paths
        self.store = store
        self.token = token

    def publish_bootstrap_ready(self, launch_requests: list[dict[str, Any]]) -> Path:
        payload: dict[str, Any] = {
            "format_version": MEMBERSHIP_FORMAT_VERSION,
            "run_id": self.token.run_id,
            "generation": 1,
            "published_by_epoch": self.token.epoch,
            "published_by_owner_id": self.token.owner_id,
            "bootstrap_requests": [
                {
                    "request_id": str(row["request_id"]),
                    "bootstrap_slot": int(row["bootstrap_slot"]),
                    "state": str(row["state"]),
                }
                for row in sorted(launch_requests, key=lambda item: int(item["bootstrap_slot"]))
            ],
            "created_at": time.time(),
        }
        payload["payload_sha256"] = _sha256_payload(payload, excluded="payload_sha256")
        path = self.paths.epoch_bootstrap_ready_path(self.token.epoch, self.token.owner_id)
        atomic_write_json(path, payload)
        self.store.record_control_publication(
            self.token,
            kind="bootstrap_ready",
            logical_generation=1,
            relative_path=self.paths.relative(path),
            sha256=sha256_file(path),
        )
        return path

    def publish_admission(self, result: dict[str, Any]) -> Path:
        if result.get("state") != "admitted":
            raise ValueError("publish_admission requires an admitted registration")
        return self.publish_registration_result(result)

    def publish_registration_result(self, result: dict[str, Any]) -> Path:
        payload: dict[str, Any] = {
            "format_version": MEMBERSHIP_FORMAT_VERSION,
            "run_id": self.token.run_id,
            **result,
            "published_by_epoch": self.token.epoch,
            "published_by_owner_id": self.token.owner_id,
            "published_at": time.time(),
        }
        payload["payload_sha256"] = _sha256_payload(payload, excluded="payload_sha256")
        path = self.paths.epoch_admission_path(
            self.token.epoch,
            self.token.owner_id,
            str(result["instance_id"]),
        )
        existing = safe_read_json(path)
        if isinstance(existing, dict):
            if existing.get("payload_sha256") != _sha256_payload(
                existing,
                excluded="payload_sha256",
            ):
                raise RuntimeError("existing registration decision checksum mismatch")
            stable_existing = {
                key: value
                for key, value in existing.items()
                if key not in {"payload_sha256", "published_at"}
            }
            stable_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"payload_sha256", "published_at"}
            }
            if stable_existing != stable_payload:
                raise RuntimeError("registration decision publication collision")
            return path
        atomic_write_json(path, payload)
        self.store.record_control_publication(
            self.token,
            kind=f"registration:{result['instance_id']}",
            logical_generation=int(result.get("admission_generation", 1)),
            relative_path=self.paths.relative(path),
            sha256=sha256_file(path),
        )
        return path


def read_bootstrap_ready(
    paths: RunPaths,
    *,
    run_id: str,
    bootstrap_slot: int,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    if not paths.syncer_epochs.is_dir():
        return None
    for directory in paths.syncer_epochs.iterdir():
        if not directory.is_dir():
            continue
        match = _EPOCH_DIR_RE.fullmatch(directory.name)
        if match is None:
            continue
        path = directory / "membership" / "bootstrap_ready_g000001.json"
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("format_version") != MEMBERSHIP_FORMAT_VERSION
            or payload.get("run_id") != run_id
            or payload.get("payload_sha256")
            != _sha256_payload(payload, excluded="payload_sha256")
        ):
            continue
        epoch = int(match.group("epoch"))
        owner_id = str(payload.get("published_by_owner_id", ""))
        if (
            int(payload.get("published_by_epoch", -1)) != epoch
            or not owner_id
            or paths.owner_short(owner_id) != match.group("owner")
        ):
            continue
        rows = payload.get("bootstrap_requests")
        if not isinstance(rows, list):
            continue
        request = next(
            (
                dict(row)
                for row in rows
                if isinstance(row, dict)
                and int(row.get("bootstrap_slot", -1)) == int(bootstrap_slot)
            ),
            None,
        )
        if request is not None:
            candidates.append((epoch, request))
    return None if not candidates else max(candidates, key=lambda item: item[0])[1]


def read_registration_decision(
    paths: RunPaths,
    *,
    run_id: str,
    instance_id: str,
) -> dict[str, Any] | None:
    validate_learner_instance_id(instance_id)
    candidates: list[tuple[int, dict[str, Any]]] = []
    if not paths.syncer_epochs.is_dir():
        return None
    for directory in paths.syncer_epochs.iterdir():
        if not directory.is_dir():
            continue
        match = _EPOCH_DIR_RE.fullmatch(directory.name)
        if match is None:
            continue
        path = directory / "membership" / "admissions" / f"{instance_id}.json"
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        epoch = int(match.group("epoch"))
        owner_id = str(payload.get("published_by_owner_id", ""))
        if (
            payload.get("format_version") != MEMBERSHIP_FORMAT_VERSION
            or payload.get("run_id") != run_id
            or payload.get("instance_id") != instance_id
            or int(payload.get("published_by_epoch", -1)) != epoch
            or not owner_id
            or paths.owner_short(owner_id) != match.group("owner")
            or payload.get("payload_sha256")
            != _sha256_payload(payload, excluded="payload_sha256")
        ):
            continue
        candidates.append((epoch, payload))
    return None if not candidates else max(candidates, key=lambda item: item[0])[1]


def read_admission(
    paths: RunPaths,
    *,
    run_id: str,
    instance_id: str,
) -> Admission | None:
    validate_learner_instance_id(instance_id)
    payload = read_registration_decision(
        paths,
        run_id=run_id,
        instance_id=instance_id,
    )
    if payload is None:
        return None
    try:
        return Admission.from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def ingest_registration_requests(
    store: Any,
    paths: RunPaths,
    *,
    token: LeaderToken,
    run_id: str,
    source_fingerprint: str,
    config_sha256: str,
    stream_pool_size: int,
    desired_contributors: int,
    allow_unsolicited_registration: bool,
    allow_healthy_placement_replacement: bool,
    reuse_stream_for_same_placement: bool,
    now: float | None = None,
) -> list[dict[str, Any]]:
    timestamp = time.time() if now is None else float(now)
    publisher = MembershipPublisher(paths, store.fenced_store, token)
    results: list[dict[str, Any]] = []
    for path in paths.iter_registration_requests():
        try:
            validate_learner_instance_id(path.stem)
        except ValueError:
            # An invalid filename cannot safely select a canonical decision
            # path or a DB identity.  It has no admissible protocol owner.
            path.unlink(missing_ok=True)
            continue
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            path.unlink(missing_ok=True)
            continue
        try:
            validate_registration_request(
                payload,
                path=path,
                run_id=run_id,
                source_fingerprint=source_fingerprint,
                config_sha256=config_sha256,
                now=timestamp,
            )
            result = store.admit_registration(
                payload,
                stream_pool_size=stream_pool_size,
                desired_contributors=desired_contributors,
                allow_unsolicited_registration=allow_unsolicited_registration,
                allow_healthy_placement_replacement=allow_healthy_placement_replacement,
                reuse_stream_for_same_placement=reuse_stream_for_same_placement,
                now=timestamp,
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            instance_id = path.stem
            try:
                result = store.reject_registration(
                    instance_id=instance_id,
                    request_sha256=str(payload.get("request_sha256", "")),
                    launch_request_id=payload.get("launch_request_id"),
                    placement_id=str(payload.get("placement_id", "")),
                    created_at=float(payload.get("created_at", timestamp)),
                    expires_at=float(payload.get("expires_at", timestamp)),
                    reason=str(exc),
                )
            except Exception:
                continue
        preserve_existing_publication = bool(
            result.pop("_preserve_existing_publication", False)
        )
        results.append(result)
        if result.get("state") in {
            "admitted",
            "rejected",
            "expired",
            "capacity_fulfilled",
        } and not preserve_existing_publication:
            publisher.publish_registration_result(result)
        if result.get("state") in {"admitted", "rejected", "expired", "capacity_fulfilled"}:
            path.unlink(missing_ok=True)
    return results
