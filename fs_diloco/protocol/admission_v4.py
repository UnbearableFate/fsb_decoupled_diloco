"""Filesystem request/response boundary used before learner torch import."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._validation import identity as validate_identity
from .contributor import ContributorFence, decode_contributor_fence
from .control_v4 import read_current_control
from .cycle_receipt import contributor_fence_namespace
from .data_cursor import ContributorResumeState
from ..storage.atomic_io import (
    atomic_write_json,
    fsync_directory,
    publish_immutable_bytes,
    safe_read_json,
)
from ..storage.paths import RunPaths


ADMISSION_REQUEST_FORMAT_VERSION = 1
ADMISSION_RESPONSE_FORMAT_VERSION = 1
ADMISSION_REJECTION_FORMAT_VERSION = 1
ADMISSION_CURRENT_FORMAT_VERSION = 1
ADMISSION_DISPOSITION_FORMAT_VERSION = 1
STATIC_REPLACEMENT_REQUEST_FORMAT_VERSION = 1


class AdmissionRejectedError(RuntimeError):
    """The current leader durably rejected this exact admission request."""


class AdmissionSupersededError(RuntimeError):
    """The response fence is no longer the contributor's current admission."""


class AdmissionAuthorizationError(ValueError):
    """An operator replacement authorization is malformed or mismatched."""


@dataclass(frozen=True)
class AdmissionContext:
    actor_id: str
    attempt_id: str
    fence: ContributorFence
    resume: ContributorResumeState


@dataclass(frozen=True)
class AdmissionRequestObservation:
    path: Path
    original: bytes | None
    identity: tuple[int, int] | None
    payload: dict[str, Any] | None
    read_error_type: str | None = None
    read_errno: int | None = None


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
) -> Path:
    path, _request_sha256 = publish_static_request_with_sha256(
        paths,
        run_id=run_id,
        descriptor_sha256=descriptor_sha256,
        learner_id=learner_id,
        logical_launch_id=logical_launch_id,
        attempt_id=attempt_id,
        expected_generation=expected_generation,
    )
    return path


def publish_static_request_with_sha256(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    learner_id: str,
    logical_launch_id: str,
    attempt_id: str,
    expected_generation: int | None,
) -> tuple[Path, str]:
    payload = {
        "format_version": ADMISSION_REQUEST_FORMAT_VERSION,
        "mode": "static",
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "learner_id": learner_id,
        "logical_launch_id": logical_launch_id,
        "attempt_id": attempt_id,
        "expected_generation": expected_generation,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "created_at": time.time(),
    }
    target = paths.registration_requests / "static" / learner_id / f"{attempt_id}.json"
    _publish_json(target, payload)
    return target, admission_request_sha256(payload)


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
    path, _request_sha256 = publish_dynamic_request_with_sha256(
        paths,
        run_id=run_id,
        descriptor_sha256=descriptor_sha256,
        instance_id=instance_id,
        stream_id=stream_id,
        admission_token_sha256=admission_token_sha256,
        launch_request_id=launch_request_id,
        replace_instance_id=replace_instance_id,
    )
    return path


def publish_dynamic_request_with_sha256(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    instance_id: str,
    stream_id: int,
    admission_token_sha256: str,
    launch_request_id: str | None = None,
    replace_instance_id: str | None = None,
) -> tuple[Path, str]:
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
    return target, admission_request_sha256(payload)


def iter_admission_requests(paths: RunPaths) -> tuple[AdmissionRequestObservation, ...]:
    results: list[AdmissionRequestObservation] = []
    root = paths.registration_requests
    if not root.is_dir():
        return ()
    for path in sorted(root.glob("*/*/*.json")) + sorted(root.glob("dynamic/*.json")):
        try:
            original, identity = _read_hot_request(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            results.append(
                AdmissionRequestObservation(
                    path=path,
                    original=None,
                    identity=None,
                    payload=None,
                    read_error_type=type(exc).__name__,
                    read_errno=exc.errno,
                )
            )
            continue
        try:
            payload = json.loads(original)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        results.append(
            AdmissionRequestObservation(
                path=path,
                original=original,
                identity=identity,
                payload=payload if isinstance(payload, dict) else None,
            )
        )
    return tuple(results)


def admission_request_error(
    request: dict[str, Any],
    *,
    run_id: str,
    descriptor_sha256: str,
) -> tuple[str, str] | None:
    """Return a stable invalid-request classification before using request path fields."""

    if request.get("run_id") != run_id or request.get("descriptor_sha256") != descriptor_sha256:
        return "ForeignAdmissionRequest", "request run or descriptor identity does not match"
    common = {
        "format_version",
        "mode",
        "run_id",
        "descriptor_sha256",
        "hostname",
        "pid",
        "pbs_job_id",
        "created_at",
    }
    mode = request.get("mode")
    expected = (
        common | {"learner_id", "logical_launch_id", "attempt_id", "expected_generation"}
        if mode == "static"
        else common
        | {
            "instance_id",
            "stream_id",
            "launch_request_id",
            "replace_instance_id",
            "placement_id",
            "admission_token_sha256",
        }
        if mode == "dynamic"
        else set()
    )
    if (
        request.get("format_version") != ADMISSION_REQUEST_FORMAT_VERSION
        or set(request) != expected
    ):
        return "MalformedAdmissionRequest", "request fields or format version are invalid"
    try:
        _require_identity(request["hostname"], name="hostname")
        _require_nonnegative_integer(request["pid"], name="pid")
        _require_optional_string(request["pbs_job_id"], name="pbs_job_id")
        _require_timestamp(request["created_at"], name="created_at")
        if mode == "static":
            _require_identity(request["learner_id"], name="learner_id")
            _require_identity(request["logical_launch_id"], name="logical_launch_id")
            _require_identity(request["attempt_id"], name="attempt_id")
            if request["expected_generation"] is not None:
                _require_nonnegative_integer(
                    request["expected_generation"], name="expected_generation"
                )
        else:
            _require_identity(request["instance_id"], name="instance_id")
            _require_identity(request["placement_id"], name="placement_id")
            _require_nonnegative_integer(request["stream_id"], name="stream_id")
            _require_optional_identity(request["launch_request_id"], name="launch_request_id")
            _require_optional_identity(request["replace_instance_id"], name="replace_instance_id")
            digest = request["admission_token_sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("admission_token_sha256 is invalid")
    except (TypeError, ValueError) as exc:
        return "MalformedAdmissionRequest", str(exc)
    return None


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
    namespace = contributor_fence_namespace(fence)
    target = paths.epoch_admission_response_path(epoch, owner_id, actor_id, attempt_id, namespace)
    publication = _publish_json(target, payload)
    current = _admission_current_payload(
        paths,
        run_id=str(request["run_id"]),
        descriptor_sha256=str(request["descriptor_sha256"]),
        epoch=epoch,
        owner_id=owner_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        fence=fence,
        response_path=target,
        response_sha256=publication.sha256,
    )
    current_path = paths.epoch_current_admission_path(epoch, owner_id, fence.stable_contributor_key)
    if safe_read_json(current_path) != current:
        atomic_write_json(current_path, current)
    return target


def publish_admission_rejection(
    paths: RunPaths,
    *,
    epoch: int,
    owner_id: str,
    request: dict[str, Any],
    error_type: str,
    message: str,
) -> Path:
    actor_id, attempt_id = _request_actor_attempt(request)
    payload = {
        "format_version": ADMISSION_REJECTION_FORMAT_VERSION,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
        "error_type": error_type,
        "message": message,
    }
    target = paths.epoch_admission_rejection_path(
        epoch,
        owner_id,
        actor_id,
        attempt_id,
        admission_request_sha256(request),
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
    stable_contributor_key: str,
    request_sha256: str,
    max_clock_skew_seconds: float,
) -> AdmissionContext | None:
    current = read_current_control(
        paths,
        run_id=run_id,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if current is None:
        return None
    if current.drain is not None or current.terminal is not None:
        raise AdmissionRejectedError("learner admission is closed by terminal control")
    rejection_path = paths.epoch_admission_rejection_path(
        current.epoch,
        current.owner_id,
        actor_id,
        attempt_id,
        request_sha256,
    )
    rejection = safe_read_json(rejection_path)
    if rejection is not None:
        _raise_valid_rejection(
            rejection,
            path=rejection_path,
            run_id=run_id,
            descriptor_sha256=descriptor_sha256,
            actor_id=actor_id,
            attempt_id=attempt_id,
            epoch=current.epoch,
            owner_id=current.owner_id,
        )
    pointer_path = paths.epoch_current_admission_path(
        current.epoch, current.owner_id, stable_contributor_key
    )
    pointer = safe_read_json(pointer_path)
    if pointer is None:
        return None
    if pointer.get("kind") == "superseded":
        raise AdmissionSupersededError("admission response was superseded by another fence")
    try:
        pointer_fence = decode_contributor_fence(pointer.get("fence"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("current admission pointer fence is invalid") from exc
    if (
        pointer_fence.stable_contributor_key != stable_contributor_key
        or pointer.get("actor_id") != actor_id
        or pointer.get("attempt_id") != attempt_id
    ):
        # A current pointer for another actor/attempt is also the normal state
        # while this request is waiting to be processed.  The polling reader
        # has not yet captured a fence and therefore cannot classify it as a
        # superseded admission.  Request-specific rejection or an exact
        # matching pointer/response is required to leave the pending state.
        return None
    path = paths.epoch_admission_response_path(
        current.epoch,
        current.owner_id,
        actor_id,
        attempt_id,
        contributor_fence_namespace(pointer_fence),
    )
    if pointer.get("response_path") != paths.relative(path):
        raise RuntimeError("current admission response path is invalid")
    try:
        response_bytes = path.read_bytes()
        payload = json.loads(response_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"malformed admission response: {path}") from exc
    fence, resume = _decode_admission_response_control(
        payload,
        run_id=run_id,
        descriptor_sha256=descriptor_sha256,
        actor_id=actor_id,
        attempt_id=attempt_id,
        epoch=current.epoch,
        owner_id=current.owner_id,
    )
    if fence != pointer_fence:
        raise RuntimeError("current admission pointer fence does not match response")
    expected_pointer = _admission_current_payload(
        paths,
        run_id=run_id,
        descriptor_sha256=descriptor_sha256,
        epoch=current.epoch,
        owner_id=current.owner_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        fence=fence,
        response_path=path,
        response_sha256=hashlib.sha256(response_bytes).hexdigest(),
    )
    if pointer != expected_pointer:
        raise AdmissionSupersededError("admission response was superseded by another fence")
    return AdmissionContext(actor_id=actor_id, attempt_id=attempt_id, fence=fence, resume=resume)


def _decode_admission_response_control(
    payload: Any,
    *,
    run_id: str,
    descriptor_sha256: str,
    actor_id: str,
    attempt_id: str,
    epoch: int,
    owner_id: str,
) -> tuple[ContributorFence, ContributorResumeState]:
    expected = {
        "format_version": ADMISSION_RESPONSE_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": epoch,
        "leader_owner_id": owner_id,
    }
    if not isinstance(payload, dict) or set(payload) != {*expected, "fence", "resume"}:
        raise RuntimeError("admission response fields are invalid")
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("admission response identity is invalid")
    resume_payload = payload.get("resume")
    if not isinstance(resume_payload, dict) or set(resume_payload) != {
        "cursor",
        "last_receipt_id",
        "last_receipt_sha256",
        "next_cycle_seq",
        "stream_epoch",
    }:
        raise RuntimeError("admission response resume state is invalid")
    try:
        fence = decode_contributor_fence(payload.get("fence"))
        resume = ContributorResumeState(
            cursor=resume_payload["cursor"],
            last_receipt_id=resume_payload["last_receipt_id"],
            last_receipt_sha256=resume_payload["last_receipt_sha256"],
            next_cycle_seq=resume_payload["next_cycle_seq"],
            stream_epoch=resume_payload["stream_epoch"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("admission response resume state is invalid") from exc
    return fence, resume


def _admission_current_payload(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    epoch: int,
    owner_id: str,
    actor_id: str,
    attempt_id: str,
    fence: ContributorFence,
    response_path: Path,
    response_sha256: str,
) -> dict[str, Any]:
    return {
        "format_version": ADMISSION_CURRENT_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
        "stable_contributor_key": fence.stable_contributor_key,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "fence": fence.as_dict(),
        "response_path": paths.relative(response_path),
        "response_sha256": response_sha256,
    }


def _raise_valid_rejection(
    rejection: dict[str, Any],
    *,
    path: Path,
    run_id: str,
    descriptor_sha256: str,
    actor_id: str,
    attempt_id: str,
    epoch: int,
    owner_id: str,
) -> None:
    _validate_rejection_control(
        rejection,
        path=path,
        run_id=run_id,
        descriptor_sha256=descriptor_sha256,
        actor_id=actor_id,
        attempt_id=attempt_id,
        epoch=epoch,
        owner_id=owner_id,
    )
    raise AdmissionRejectedError(
        f"learner admission rejected: {rejection['error_type']}: {rejection['message']}"
    )


def _validate_rejection_control(
    rejection: Any,
    *,
    path: Path,
    run_id: str,
    descriptor_sha256: str,
    actor_id: str,
    attempt_id: str,
    epoch: int,
    owner_id: str,
) -> None:
    expected = {
        "format_version": ADMISSION_REJECTION_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": epoch,
        "leader_owner_id": owner_id,
    }
    if (
        not isinstance(rejection, dict)
        or set(rejection) != {*expected, "error_type", "message"}
        or any(rejection.get(key) != value for key, value in expected.items())
    ):
        raise RuntimeError(f"malformed admission rejection: {path}")
    if not isinstance(rejection["error_type"], str) or not isinstance(rejection["message"], str):
        raise RuntimeError(f"malformed admission rejection: {path}")


def admission_request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(request, newline=True)).hexdigest()


def publish_admission_disposition(
    paths: RunPaths,
    *,
    request: dict[str, Any],
    epoch: int,
    owner_id: str,
    outcome: str,
    control_path: Path,
    fence: ContributorFence | None = None,
    error_type: str | None = None,
) -> Path:
    if outcome not in {"admitted", "rejected"}:
        raise ValueError("admission disposition outcome must be admitted or rejected")
    request_sha = admission_request_sha256(request)
    payload = {
        "format_version": ADMISSION_DISPOSITION_FORMAT_VERSION,
        "request_sha256": request_sha,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
        "outcome": outcome,
        "control_path": paths.relative(control_path),
        "fence": None if fence is None else fence.as_dict(),
        "error_type": error_type,
    }
    target = paths.registration_disposition_path(request_sha)
    _publish_json(target, payload)
    return target


def archive_disposed_admission_request(
    paths: RunPaths,
    *,
    request_path: Path,
    request: dict[str, Any],
    original: bytes | None = None,
    identity: tuple[int, int] | None = None,
) -> Path:
    request_sha = admission_request_sha256(request)
    disposition = safe_read_json(paths.registration_disposition_path(request_sha))
    _validate_admission_disposition(paths, request=request, disposition=disposition)
    expected_root = paths.registration_requests.resolve()
    resolved_parent = request_path.parent.resolve()
    if expected_root != resolved_parent and expected_root not in resolved_parent.parents:
        raise RuntimeError("admission request archive path escaped hot discovery root")
    if original is None or identity is None:
        original, identity = _read_hot_request(request_path)
    if json.loads(original) != request:
        raise RuntimeError("admission request changed before archival")
    target = paths.registration_history_path(request_sha)
    publish_immutable_bytes(target, _canonical_json_bytes(request, newline=True))
    _remove_hot_request(request_path, identity=identity)
    return target


def dispose_invalid_admission_request(
    paths: RunPaths,
    *,
    request_path: Path,
    run_id: str,
    descriptor_sha256: str,
    epoch: int,
    owner_id: str,
    error_type: str,
    message: str,
    original: bytes | None = None,
    identity: tuple[int, int] | None = None,
) -> Path:
    """Durably archive and remove one malformed or foreign hot request."""

    if original is None or identity is None:
        original, identity = _read_hot_request(request_path)
    request_sha = hashlib.sha256(original).hexdigest()
    history_payload = {
        "format_version": ADMISSION_REQUEST_FORMAT_VERSION,
        "kind": "invalid_admission_request",
        "request_sha256": request_sha,
        "raw_base64": base64.b64encode(original).decode("ascii"),
    }
    history = paths.registration_history_path(request_sha)
    _publish_json(history, history_payload)
    disposition_payload = {
        "format_version": ADMISSION_DISPOSITION_FORMAT_VERSION,
        "request_sha256": request_sha,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
        "outcome": "rejected",
        "control_path": paths.relative(history),
        "fence": None,
        "error_type": error_type,
        "message": message,
    }
    target = paths.registration_disposition_path(request_sha)
    existing = safe_read_json(target)
    if existing is None:
        if target.exists():
            raise RuntimeError("invalid admission disposition is unreadable")
        _publish_json(target, disposition_payload)
    else:
        _validate_invalid_admission_disposition(
            paths,
            original=original,
            history_payload=history_payload,
            disposition=existing,
            run_id=run_id,
            descriptor_sha256=descriptor_sha256,
            expected_error_type=error_type,
            expected_message=message,
        )
    _remove_hot_request(request_path, identity=identity)
    return target


def _validate_invalid_admission_disposition(
    paths: RunPaths,
    *,
    original: bytes,
    history_payload: dict[str, Any],
    disposition: Any,
    run_id: str,
    descriptor_sha256: str,
    expected_error_type: str,
    expected_message: str,
) -> None:
    request_sha = hashlib.sha256(original).hexdigest()
    history = paths.registration_history_path(request_sha)
    if safe_read_json(history) != history_payload:
        raise RuntimeError("invalid admission history does not match the raw request")
    expected_fields = {
        "format_version",
        "request_sha256",
        "run_id",
        "descriptor_sha256",
        "leader_epoch",
        "leader_owner_id",
        "outcome",
        "control_path",
        "fence",
        "error_type",
        "message",
    }
    if (
        not isinstance(disposition, dict)
        or set(disposition) != expected_fields
        or disposition.get("format_version") != ADMISSION_DISPOSITION_FORMAT_VERSION
        or disposition.get("request_sha256") != request_sha
    ):
        raise RuntimeError("invalid admission disposition fields are invalid")
    epoch = disposition.get("leader_epoch")
    if (
        disposition.get("run_id") != run_id
        or disposition.get("descriptor_sha256") != descriptor_sha256
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 1
        or not isinstance(disposition.get("leader_owner_id"), str)
        or not disposition["leader_owner_id"]
        or disposition.get("outcome") != "rejected"
        or disposition.get("control_path") != paths.relative(history)
        or disposition.get("fence") is not None
        or disposition.get("error_type") != expected_error_type
        or disposition.get("message") != expected_message
    ):
        raise RuntimeError("invalid admission disposition identity is invalid")


def _validate_admission_disposition(
    paths: RunPaths,
    *,
    request: dict[str, Any],
    disposition: Any,
) -> None:
    request_sha = admission_request_sha256(request)
    expected = {
        "format_version": ADMISSION_DISPOSITION_FORMAT_VERSION,
        "request_sha256": request_sha,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
    }
    fields = {
        *expected,
        "leader_epoch",
        "leader_owner_id",
        "outcome",
        "control_path",
        "fence",
        "error_type",
    }
    if (
        not isinstance(disposition, dict)
        or set(disposition) != fields
        or any(disposition.get(key) != value for key, value in expected.items())
    ):
        raise RuntimeError("admission disposition fields or identity are invalid")
    epoch = disposition.get("leader_epoch")
    owner_id = disposition.get("leader_owner_id")
    outcome = disposition.get("outcome")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 1
        or not isinstance(owner_id, str)
        or not owner_id
        or outcome not in {"admitted", "rejected"}
    ):
        raise RuntimeError("admission disposition authority or outcome is invalid")
    actor_id, attempt_id = _request_actor_attempt(request)
    if outcome == "admitted":
        try:
            fence = decode_contributor_fence(disposition.get("fence"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("admission disposition fence is invalid") from exc
        expected_control = paths.epoch_admission_response_path(
            epoch,
            owner_id,
            actor_id,
            attempt_id,
            contributor_fence_namespace(fence),
        )
        if disposition.get("error_type") is not None or disposition.get(
            "control_path"
        ) != paths.relative(expected_control):
            raise RuntimeError("admitted disposition control is invalid")
        try:
            control_bytes = expected_control.read_bytes()
            control = json.loads(control_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("admitted disposition control is invalid") from exc
        control_fence, _resume = _decode_admission_response_control(
            control,
            run_id=str(request["run_id"]),
            descriptor_sha256=str(request["descriptor_sha256"]),
            actor_id=actor_id,
            attempt_id=attempt_id,
            epoch=epoch,
            owner_id=owner_id,
        )
        if control_fence != fence:
            raise RuntimeError("admitted disposition control fence is invalid")
        pointer_path = paths.epoch_current_admission_path(
            epoch, owner_id, fence.stable_contributor_key
        )
        pointer = safe_read_json(pointer_path)
        expected_pointer = _admission_current_payload(
            paths,
            run_id=str(request["run_id"]),
            descriptor_sha256=str(request["descriptor_sha256"]),
            epoch=epoch,
            owner_id=owner_id,
            actor_id=actor_id,
            attempt_id=attempt_id,
            fence=fence,
            response_path=expected_control,
            response_sha256=hashlib.sha256(control_bytes).hexdigest(),
        )
        if pointer != expected_pointer:
            raise RuntimeError("current admission pointer is invalid")
        return
    expected_control = paths.epoch_admission_rejection_path(
        epoch, owner_id, actor_id, attempt_id, request_sha
    )
    if (
        disposition.get("fence") is not None
        or not isinstance(disposition.get("error_type"), str)
        or not disposition["error_type"]
        or disposition.get("control_path") != paths.relative(expected_control)
    ):
        raise RuntimeError("rejected disposition control is invalid")
    control = safe_read_json(expected_control)
    try:
        _validate_rejection_control(
            control,
            path=expected_control,
            run_id=str(request["run_id"]),
            descriptor_sha256=str(request["descriptor_sha256"]),
            actor_id=actor_id,
            attempt_id=attempt_id,
            epoch=epoch,
            owner_id=owner_id,
        )
    except RuntimeError as exc:
        raise RuntimeError("rejected disposition control is invalid") from exc
    if disposition["error_type"] != control["error_type"]:
        raise RuntimeError("rejected disposition control is invalid")


def repair_rejected_admission_control(
    paths: RunPaths,
    *,
    request: dict[str, Any],
    disposition: Any,
    epoch: int,
    owner_id: str,
) -> Path | None:
    """Republish an already validated rejection into the current leader epoch."""

    _validate_admission_disposition(paths, request=request, disposition=disposition)
    if disposition["outcome"] != "rejected":
        return None
    actor_id, attempt_id = _request_actor_attempt(request)
    source = paths.epoch_admission_rejection_path(
        int(disposition["leader_epoch"]),
        str(disposition["leader_owner_id"]),
        actor_id,
        attempt_id,
        admission_request_sha256(request),
    )
    payload = safe_read_json(source)
    _validate_rejection_control(
        payload,
        path=source,
        run_id=str(request["run_id"]),
        descriptor_sha256=str(request["descriptor_sha256"]),
        actor_id=actor_id,
        attempt_id=attempt_id,
        epoch=int(disposition["leader_epoch"]),
        owner_id=str(disposition["leader_owner_id"]),
    )
    return publish_admission_rejection(
        paths,
        epoch=epoch,
        owner_id=owner_id,
        request=request,
        error_type=str(payload["error_type"]),
        message=str(payload["message"]),
    )


def publish_static_replacement_authorization(
    paths: RunPaths,
    *,
    run_id: str,
    descriptor_sha256: str,
    old_fence: ContributorFence,
    new_logical_launch_id: str,
    new_attempt_id: str,
    reason: str,
) -> Path:
    if old_fence.kind != "static":
        raise ValueError("static replacement authorization requires a static fence")
    if not reason.strip():
        raise ValueError("static replacement authorization reason must not be empty")
    validate_identity(new_logical_launch_id, name="new_logical_launch_id")
    validate_identity(new_attempt_id, name="new_attempt_id")
    payload = {
        "format_version": STATIC_REPLACEMENT_REQUEST_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "old_fence": old_fence.as_dict(),
        "learner_id": old_fence.stable_contributor_key,
        "new_logical_launch_id": new_logical_launch_id,
        "new_attempt_id": new_attempt_id,
        "reason": reason,
        "created_at": time.time(),
    }
    target = paths.static_replacement_request_path(old_fence.stable_contributor_key, new_attempt_id)
    _publish_json(target, payload)
    return target


def read_static_replacement_authorization(
    paths: RunPaths,
    *,
    request: dict[str, Any],
    current_fence: ContributorFence,
) -> tuple[str, str] | None:
    if current_fence.kind != "static" or request.get("mode") != "static":
        return None
    target = paths.static_replacement_request_path(
        str(request["learner_id"]), str(request["attempt_id"])
    )
    try:
        data = target.read_bytes()
        payload = json.loads(data)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionAuthorizationError(
            f"invalid static replacement authorization: {target}"
        ) from exc
    expected = {
        "format_version": STATIC_REPLACEMENT_REQUEST_FORMAT_VERSION,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "old_fence": current_fence.as_dict(),
        "learner_id": request["learner_id"],
        "new_logical_launch_id": request["logical_launch_id"],
        "new_attempt_id": request["attempt_id"],
    }
    if not isinstance(payload, dict) or set(payload) != {*expected, "reason", "created_at"}:
        raise AdmissionAuthorizationError("static replacement authorization fields are invalid")
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AdmissionAuthorizationError("static replacement authorization identity mismatch")
    reason = payload.get("reason")
    created_at = payload.get("created_at")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(float(created_at))
        or float(created_at) < 0.0
    ):
        raise AdmissionAuthorizationError("static replacement authorization payload is invalid")
    return reason, hashlib.sha256(data).hexdigest()


def _request_actor_attempt(request: dict[str, Any]) -> tuple[str, str]:
    if request.get("mode") == "static":
        return str(request["learner_id"]), str(request["attempt_id"])
    if request.get("mode") == "dynamic":
        actor_id = str(request["instance_id"])
        return actor_id, actor_id
    raise ValueError("unknown admission request mode")


def highest_static_generation(paths: RunPaths, learner_id: str) -> int | None:
    highest: int | None = None
    if not paths.syncer_epochs.is_dir():
        return None
    for path in paths.syncer_epochs.glob(
        f"e*_*/membership/admissions_v4/responses/{learner_id}/**/*.json"
    ):
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        fence = payload.get("fence")
        if isinstance(fence, dict) and fence.get("kind") == "static":
            generation = int(fence["binding_generation"])
            highest = generation if highest is None else max(highest, generation)
    # Keep the early P4 development layout readable for generation discovery.
    for path in paths.syncer_epochs.glob(f"e*_*/membership/admissions_v4/{learner_id}/*.json"):
        payload = safe_read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("fence"), dict):
            generation = int(payload["fence"].get("binding_generation", 0))
            if generation > 0:
                highest = generation if highest is None else max(highest, generation)
    return highest


def _canonical_json_bytes(payload: dict[str, Any], *, newline: bool) -> bytes:
    suffix = "\n" if newline else ""
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + suffix
    ).encode("utf-8")


def _publish_json(path: Path, payload: dict[str, Any]):
    return publish_immutable_bytes(path, _canonical_json_bytes(payload, newline=True))


def _require_nonnegative_integer(value: Any, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_identity(value: Any, *, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an identity string")
    validate_identity(value, name=name)


def _require_optional_string(value: Any, *, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > 256):
        raise ValueError(f"{name} must be null or a non-empty bounded string")


def _require_optional_identity(value: Any, *, name: str) -> None:
    if value is not None:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be null or an identity string")
        validate_identity(value, name=name)


def _require_timestamp(value: Any, *, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError(f"{name} must be a finite non-negative timestamp")


def _read_hot_request(path: Path) -> tuple[bytes, tuple[int, int]]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"admission request is not a regular file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (metadata.st_dev, metadata.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise OSError(f"admission request changed during open: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), (opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


def _remove_hot_request(path: Path, *, identity: tuple[int, int]) -> None:
    current = path.lstat()
    if (current.st_dev, current.st_ino) != identity or not stat.S_ISREG(current.st_mode):
        raise RuntimeError("admission request changed before hot-path removal")
    path.unlink()
    fsync_directory(path.parent)
