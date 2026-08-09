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


def iter_admission_requests(paths: RunPaths) -> tuple[tuple[Path, dict[str, Any] | None], ...]:
    results: list[tuple[Path, dict[str, Any] | None]] = []
    root = paths.registration_requests
    if not root.is_dir():
        return ()
    for path in sorted(root.glob("*/*/*.json")) + sorted(root.glob("dynamic/*.json")):
        try:
            original, _identity = _read_hot_request(path)
            payload = json.loads(original)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError):
            payload = None
        results.append((path, payload if isinstance(payload, dict) else None))
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
    target = paths.epoch_admission_response_path(epoch, owner_id, actor_id, attempt_id)
    publication = _publish_json(target, payload)
    current = {
        "format_version": ADMISSION_CURRENT_FORMAT_VERSION,
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "leader_epoch": int(epoch),
        "leader_owner_id": owner_id,
        "stable_contributor_key": fence.stable_contributor_key,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "fence": fence.as_dict(),
        "response_path": paths.relative(target),
        "response_sha256": publication.sha256,
    }
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
    target = paths.epoch_admission_rejection_path(epoch, owner_id, actor_id, attempt_id)
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
    if current.drain is not None or current.terminal is not None:
        raise AdmissionRejectedError("learner admission is closed by terminal control")
    path = paths.epoch_admission_response_path(
        current.epoch, current.owner_id, actor_id, attempt_id
    )
    if not path.is_file():
        rejection_path = paths.epoch_admission_rejection_path(
            current.epoch, current.owner_id, actor_id, attempt_id
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
        return None
    try:
        response_bytes = path.read_bytes()
        payload = json.loads(response_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"malformed admission response: {path}") from exc
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
    if set(payload) != {*expected, "fence", "resume"}:
        raise RuntimeError("admission response fields are invalid")
    mismatches = {
        key: (payload.get(key), value)
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"admission response identity mismatch: {mismatches}")
    resume_payload = payload.get("resume")
    if not isinstance(resume_payload, dict) or set(resume_payload) != {
        "cursor",
        "last_receipt_id",
        "last_receipt_sha256",
        "next_cycle_seq",
        "stream_epoch",
    }:
        raise RuntimeError("admission response resume state is invalid")
    fence = decode_contributor_fence(payload.get("fence"))
    pointer_path = paths.epoch_current_admission_path(
        current.epoch, current.owner_id, fence.stable_contributor_key
    )
    pointer = safe_read_json(pointer_path)
    if not isinstance(pointer, dict):
        return None
    expected_pointer = {
        "format_version": ADMISSION_CURRENT_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "leader_epoch": current.epoch,
        "leader_owner_id": current.owner_id,
        "stable_contributor_key": fence.stable_contributor_key,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "fence": fence.as_dict(),
        "response_path": paths.relative(path),
        "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
    }
    if pointer != expected_pointer:
        raise AdmissionSupersededError("admission response was superseded by another fence")
    try:
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
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("admission response resume state is invalid") from exc


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
    expected = {
        "format_version": ADMISSION_REJECTION_FORMAT_VERSION,
        "run_id": run_id,
        "descriptor_sha256": descriptor_sha256,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": epoch,
        "leader_owner_id": owner_id,
    }
    if set(rejection) != {*expected, "error_type", "message"} or any(
        rejection.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError(f"malformed admission rejection: {path}")
    if not isinstance(rejection["error_type"], str) or not isinstance(rejection["message"], str):
        raise RuntimeError(f"malformed admission rejection: {path}")
    raise AdmissionRejectedError(
        f"learner admission rejected: {rejection['error_type']}: {rejection['message']}"
    )


def admission_request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(request, newline=False)).hexdigest()


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
) -> Path:
    request_sha = admission_request_sha256(request)
    disposition = safe_read_json(paths.registration_disposition_path(request_sha))
    _validate_admission_disposition(paths, request=request, disposition=disposition)
    expected_root = paths.registration_requests.resolve()
    resolved_parent = request_path.parent.resolve()
    if expected_root != resolved_parent and expected_root not in resolved_parent.parents:
        raise RuntimeError("admission request archive path escaped hot discovery root")
    original, identity = _read_hot_request(request_path)
    if json.loads(original) != request:
        raise RuntimeError("admission request changed before archival")
    target = paths.registration_history_path(request_sha)
    publish_immutable_bytes(target, original)
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
) -> Path:
    """Durably archive and remove one malformed or foreign hot request."""

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
    disposition = {
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
    _publish_json(target, disposition)
    _remove_hot_request(request_path, identity=identity)
    return target


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
    expected_control = (
        paths.epoch_admission_response_path(epoch, owner_id, actor_id, attempt_id)
        if outcome == "admitted"
        else paths.epoch_admission_rejection_path(epoch, owner_id, actor_id, attempt_id)
    )
    if disposition.get("control_path") != paths.relative(expected_control):
        raise RuntimeError("admission disposition control path is invalid")
    control = safe_read_json(expected_control)
    control_identity = {
        "run_id": request["run_id"],
        "descriptor_sha256": request["descriptor_sha256"],
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": epoch,
        "leader_owner_id": owner_id,
    }
    if not isinstance(control, dict) or any(
        control.get(key) != value for key, value in control_identity.items()
    ):
        raise RuntimeError("admission disposition control identity is invalid")
    if outcome == "admitted":
        try:
            fence = decode_contributor_fence(disposition.get("fence"))
            control_fence = decode_contributor_fence(control.get("fence"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("admission disposition fence is invalid") from exc
        if (
            disposition.get("error_type") is not None
            or fence != control_fence
            or control.get("format_version") != ADMISSION_RESPONSE_FORMAT_VERSION
            or set(control)
            != {
                "format_version",
                *control_identity,
                "fence",
                "resume",
            }
        ):
            raise RuntimeError("admitted disposition control is invalid")
    elif (
        disposition.get("fence") is not None
        or not isinstance(disposition.get("error_type"), str)
        or not disposition["error_type"]
        or disposition["error_type"] != control.get("error_type")
        or control.get("format_version") != ADMISSION_REJECTION_FORMAT_VERSION
        or set(control)
        != {
            "format_version",
            *control_identity,
            "error_type",
            "message",
        }
    ):
        raise RuntimeError("rejected disposition control is invalid")


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
        f"e*_*/membership/admissions_v4/responses/{learner_id}/*.json"
    ):
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
