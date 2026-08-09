"""Filesystem request/response boundary used before learner torch import."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._validation import identity as validate_identity
from .contributor import ContributorFence, decode_contributor_fence
from .control_v4 import read_current_control
from .data_cursor import ContributorResumeState
from ..storage.atomic_io import publish_immutable_bytes, safe_read_json
from ..storage.paths import RunPaths


ADMISSION_REQUEST_FORMAT_VERSION = 1
ADMISSION_RESPONSE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class AdmissionContext:
    actor_id: str
    attempt_id: str
    fence: ContributorFence
    resume: ContributorResumeState


def new_attempt_id() -> str:
    return f"attempt-{uuid.uuid4()}"


def static_logical_launch_id(*, descriptor_sha256: str, learner_id: str) -> str:
    digest = hashlib.sha256(
        f"{descriptor_sha256}\0static\0{learner_id}".encode("utf-8")
    ).hexdigest()
    return f"static-{digest[:32]}"


def dynamic_placement_id(*, hostname: str, accelerator: str) -> str:
    """Return a stable, opaque placement identity accepted by authority validation."""
    digest = hashlib.sha256(f"{hostname}\0{accelerator}".encode("utf-8")).hexdigest()
    return validate_identity(f"placement-{digest[:32]}", name="placement_id")


def publish_static_request(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    learner_id: str,
    logical_launch_id: str,
    attempt_id: str,
    expected_generation: int | None,
    allow_logical_replacement: bool = False,
) -> Path:
    payload = {
        "format_version": ADMISSION_REQUEST_FORMAT_VERSION,
        "mode": "static",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "learner_id": learner_id,
        "logical_launch_id": logical_launch_id,
        "attempt_id": attempt_id,
        "expected_generation": expected_generation,
        "allow_logical_replacement": bool(allow_logical_replacement),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "created_at": time.time(),
    }
    target = paths.registration_requests / "static" / learner_id / f"{attempt_id}.json"
    _publish_json(target, payload)
    return target


def publish_dynamic_request(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    instance_id: str,
    stream_id: int,
    admission_token_sha256: str,
    launch_request_id: str | None = None,
    replace_instance_id: str | None = None,
) -> Path:
    hostname = socket.gethostname()
    accelerator = os.environ.get("CUDA_VISIBLE_DEVICES") or "cpu"
    payload = {
        "format_version": ADMISSION_REQUEST_FORMAT_VERSION,
        "mode": "dynamic",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "instance_id": instance_id,
        "stream_id": int(stream_id),
        "launch_request_id": launch_request_id,
        "replace_instance_id": replace_instance_id,
        "placement_id": dynamic_placement_id(
            hostname=hostname,
            accelerator=accelerator,
        ),
        "admission_token_sha256": admission_token_sha256,
        "hostname": hostname,
        "pid": os.getpid(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "created_at": time.time(),
    }
    target = paths.registration_requests / "dynamic" / f"{instance_id}.json"
    _publish_json(target, payload)
    return target


def iter_admission_requests(paths: RunPaths) -> tuple[tuple[Path, dict[str, Any]], ...]:
    results: list[tuple[Path, dict[str, Any]]] = []
    root = paths.registration_requests
    if not root.is_dir():
        return ()
    for path in sorted(root.glob("*/*/*.json")) + sorted(root.glob("dynamic/*.json")):
        payload = safe_read_json(path)
        if isinstance(payload, dict):
            results.append((path, payload))
    return tuple(results)


def publish_admission_response(
    paths: RunPaths,
    *,
    epoch: int,
    owner_id: str,
    request: dict[str, Any],
    fence: ContributorFence,
    resume: ContributorResumeState,
) -> Path:
    actor_id = (
        str(request["learner_id"])
        if request.get("mode") == "static"
        else str(request["instance_id"])
    )
    attempt_id = str(request["attempt_id"]) if request.get("mode") == "static" else actor_id
    payload = {
        "format_version": ADMISSION_RESPONSE_FORMAT_VERSION,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "fence": fence.as_dict(),
        "resume": {
            "cursor": resume.cursor,
            "last_receipt_id": resume.last_receipt_id,
            "last_receipt_sha256": resume.last_receipt_sha256,
            "next_cycle_seq": resume.next_cycle_seq,
            "stream_epoch": resume.stream_epoch,
        },
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
    }
    target = (
        paths.epoch_membership_dir(epoch, owner_id)
        / "admissions_v4"
        / actor_id
        / f"{attempt_id}.json"
    )
    _publish_json(target, payload)
    return target


def read_admission_response(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    actor_id: str,
    attempt_id: str,
    max_clock_skew_seconds: float,
) -> AdmissionContext | None:
    current = read_current_control(
        paths,
        run_id=run_id,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if current is None:
        return None
    path = (
        paths.epoch_membership_dir(current.epoch, current.owner_id)
        / "admissions_v4"
        / actor_id
        / f"{attempt_id}.json"
    )
    if not path.is_file():
        return None
    payload = safe_read_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"malformed admission response: {path}")
    expected = {
        "format_version": ADMISSION_RESPONSE_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": current.epoch,
        "leader_owner_id": current.owner_id,
    }
    mismatches = {
        key: (payload.get(key), value)
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"admission response identity mismatch: {mismatches}")
    resume_payload = payload.get("resume")
    if not isinstance(resume_payload, dict):
        raise RuntimeError("admission response resume state is invalid")
    fence = decode_contributor_fence(payload.get("fence"))
    return AdmissionContext(
        actor_id=actor_id,
        attempt_id=attempt_id,
        fence=fence,
        resume=ContributorResumeState(
            cursor=int(resume_payload["cursor"]),
            last_receipt_id=resume_payload["last_receipt_id"],
            last_receipt_sha256=resume_payload["last_receipt_sha256"],
            next_cycle_seq=int(resume_payload["next_cycle_seq"]),
            stream_epoch=(
                None
                if resume_payload["stream_epoch"] is None
                else int(resume_payload["stream_epoch"])
            ),
        ),
    )


def highest_static_generation(paths: RunPaths, learner_id: str) -> int | None:
    highest: int | None = None
    if not paths.syncer_epochs.is_dir():
        return None
    for path in paths.syncer_epochs.glob(f"e*_*/membership/admissions_v4/{learner_id}/*/*.json"):
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        fence = payload.get("fence")
        if isinstance(fence, dict) and fence.get("kind") == "static":
            generation = int(fence["binding_generation"])
            highest = generation if highest is None else max(highest, generation)
    # Current layout has exactly one attempt path level. Keep both patterns to
    # read artifacts made by early P4 development snapshots.
    for path in paths.syncer_epochs.glob(f"e*_*/membership/admissions_v4/{learner_id}/*.json"):
        payload = safe_read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("fence"), dict):
            generation = int(payload["fence"].get("binding_generation", 0))
            if generation > 0:
                highest = generation if highest is None else max(highest, generation)
    return highest


def _publish_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    publish_immutable_bytes(path, data)
