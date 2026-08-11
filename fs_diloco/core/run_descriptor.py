"""Immutable HA run descriptor loading and source/config identity gates."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .versions import (
    AUTHORITY_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    RUN_DESCRIPTOR_FORMAT_VERSION,
)
from ..storage.atomic_io import publish_immutable_bytes, read_json, sha256_file
from ..storage.paths import RunPaths
from ..storage.run_initializer import validate_completed_run_for_actor


def _sha256_json_without(payload: dict[str, Any], field: str) -> str:
    content = {key: value for key, value in payload.items() if key != field}
    data = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _optional_environment_flag(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


def _validate_descriptor_shape(descriptor: dict[str, Any]) -> None:
    """Reject descriptors outside the one current stream-pool schema."""

    required = {
        "format_version",
        "run_id",
        "shared_root",
        "resolved_config_path",
        "resolved_config_sha256",
        "source_manifest_path",
        "source_manifest_sha256",
        "source_fingerprint",
        "git_commit",
        "git_dirty",
        "bootstrap_slots",
        "stream_pool_size",
        "protocol_version",
        "schema_version",
        "source_lock_sha256",
        "model_identity",
        "tokenizer_identity",
        "dataset_identity",
        "created_at",
        "descriptor_sha256",
    }
    if set(descriptor) != required:
        missing = sorted(required - set(descriptor))
        unknown = sorted(set(descriptor) - required)
        raise RuntimeError(
            f"run descriptor fields are invalid: missing={missing}, unknown={unknown}"
        )
    if descriptor["format_version"] != RUN_DESCRIPTOR_FORMAT_VERSION:
        raise RuntimeError("unsupported run descriptor format version")
    pool_size = descriptor["stream_pool_size"]
    bootstrap_slots = descriptor["bootstrap_slots"]
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1:
        raise RuntimeError("run descriptor stream_pool_size is invalid")
    if (
        isinstance(bootstrap_slots, bool)
        or not isinstance(bootstrap_slots, int)
        or not 0 <= bootstrap_slots <= pool_size
    ):
        raise RuntimeError("run descriptor bootstrap_slots is invalid")


@dataclass(frozen=True)
class LoadedRunDescriptor:
    paths: RunPaths
    descriptor: dict[str, Any]
    config: Config
    identity: "DescriptorAuthorityIdentity"


@dataclass(frozen=True)
class DescriptorAuthorityIdentity:
    run_id: str
    source_fingerprint: str
    config_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "source_fingerprint": self.source_fingerprint,
            "config_sha256": self.config_sha256,
        }


def load_run_descriptor(
    shared_root: str | Path,
    *,
    expected_run_id: str | None = None,
    expected_git_commit: str | None = None,
    expected_git_dirty: bool | None = None,
    expected_source_fingerprint: str | None = None,
    expected_descriptor_sha256: str | None = None,
) -> LoadedRunDescriptor:
    """Load and cross-check the immutable descriptor, config, and run identity."""

    paths = RunPaths(Path(shared_root).resolve())
    descriptor = read_json(paths.run_descriptor_json)
    _validate_descriptor_shape(descriptor)
    recorded_descriptor_sha = str(descriptor.get("descriptor_sha256", ""))
    actual_descriptor_sha = _sha256_json_without(descriptor, "descriptor_sha256")
    if not recorded_descriptor_sha or recorded_descriptor_sha != actual_descriptor_sha:
        raise RuntimeError("run descriptor self-checksum mismatch")
    if (
        expected_descriptor_sha256 is not None
        and actual_descriptor_sha != expected_descriptor_sha256
    ):
        raise RuntimeError("run descriptor does not match the submitted job identity")
    checks = {
        "shared_root": str(paths.shared_root),
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": AUTHORITY_SCHEMA_VERSION,
    }
    if expected_run_id is not None:
        checks["run_id"] = expected_run_id
    if expected_git_commit is not None:
        checks["git_commit"] = expected_git_commit
    if expected_git_dirty is not None:
        checks["git_dirty"] = expected_git_dirty
    if expected_source_fingerprint is not None:
        checks["source_fingerprint"] = expected_source_fingerprint
    mismatches = {
        key: (descriptor.get(key), expected)
        for key, expected in checks.items()
        if descriptor.get(key) != expected
    }
    if mismatches:
        raise RuntimeError(f"run descriptor identity mismatch: {mismatches}")
    config_path = Path(str(descriptor["resolved_config_path"])).resolve()
    source_path = Path(str(descriptor["source_manifest_path"])).resolve()
    if config_path != paths.resolved_config_yaml.resolve():
        raise RuntimeError("run descriptor config path escaped the canonical run path")
    if source_path != paths.run_source_manifest_json.resolve():
        raise RuntimeError("run descriptor source path escaped the canonical run path")
    if sha256_file(config_path) != descriptor.get("resolved_config_sha256"):
        raise RuntimeError("resolved config checksum mismatch")
    if sha256_file(source_path) != descriptor.get("source_manifest_sha256"):
        raise RuntimeError("source manifest checksum mismatch")
    source = read_json(source_path)
    if source.get("manifest_sha256") != _sha256_json_without(source, "manifest_sha256"):
        raise RuntimeError("source manifest self-checksum mismatch")
    for key in ("git_commit", "git_dirty", "source_fingerprint"):
        if source.get(key) != descriptor.get(key):
            raise RuntimeError(f"source manifest {key} mismatch")
    config = load_config(config_path)
    if config.membership.stream_pool_size != descriptor["stream_pool_size"]:
        raise RuntimeError("resolved config stream pool size does not match descriptor")
    if config.membership.bootstrap_instances != descriptor["bootstrap_slots"]:
        raise RuntimeError("resolved config bootstrap count does not match descriptor")
    if config.run.run_id != descriptor.get("run_id"):
        raise RuntimeError("resolved config run_id mismatch")
    if Path(str(config.run.shared_root)).resolve() != paths.shared_root:
        raise RuntimeError("resolved config shared_root mismatch")
    if config.run.git_commit != descriptor.get("git_commit"):
        raise RuntimeError("resolved config git_commit mismatch")
    if config.run.git_dirty != descriptor.get("git_dirty"):
        raise RuntimeError("resolved config git_dirty mismatch")
    if config.run.source_fingerprint != descriptor.get("source_fingerprint"):
        raise RuntimeError("resolved config source fingerprint mismatch")
    identity = DescriptorAuthorityIdentity(
        run_id=str(descriptor["run_id"]),
        source_fingerprint=str(descriptor["source_fingerprint"]),
        config_sha256=str(descriptor["resolved_config_sha256"]),
    )
    validate_completed_run_for_actor(paths.shared_root)
    return LoadedRunDescriptor(paths, descriptor, config, identity)


def load_run_descriptor_from_environment() -> LoadedRunDescriptor:
    shared_root = os.environ.get("FS_DILOCO_SHARED_ROOT") or os.environ.get("SHARED_ROOT")
    if not shared_root:
        raise RuntimeError("FS_DILOCO_SHARED_ROOT is required")
    return load_run_descriptor(
        shared_root,
        expected_run_id=os.environ.get("FS_DILOCO_EXPECTED_RUN_ID"),
        expected_git_commit=os.environ.get("FS_DILOCO_EXPECTED_GIT_COMMIT"),
        expected_git_dirty=_optional_environment_flag("FS_DILOCO_EXPECTED_GIT_DIRTY"),
        expected_source_fingerprint=os.environ.get("FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"),
        expected_descriptor_sha256=os.environ.get("FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"),
    )


def write_actor_attestation(
    loaded: LoadedRunDescriptor,
    *,
    actor_kind: str,
    actor_id: str,
    attempt_id: str,
    runtime_evidence: Mapping[str, Any],
    scheduler_job_id: str | None = None,
    accelerator_identity: str | None = None,
    observed_at: float | None = None,
) -> Path:
    """Publish one immutable runtime/source attestation per actor attempt."""

    for name, value in (
        ("actor_kind", actor_kind),
        ("actor_id", actor_id),
        ("attempt_id", attempt_id),
    ):
        if not value or "/" in value or "\\" in value or "\0" in value or value in {".", ".."}:
            raise ValueError(f"{name} must be a safe path component")
    required_runtime_fields = {
        "torch_version",
        "cuda_runtime_version",
        "gpu_driver_version",
        "module_environment",
        "resource_allocation",
    }
    if not isinstance(runtime_evidence, Mapping) or set(runtime_evidence) != (
        required_runtime_fields
    ):
        raise ValueError("runtime_evidence fields are invalid")
    torch_version = runtime_evidence["torch_version"]
    if not isinstance(torch_version, str) or not torch_version:
        raise ValueError("runtime_evidence.torch_version must not be empty")
    for name in ("cuda_runtime_version", "gpu_driver_version"):
        value = runtime_evidence[name]
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"runtime_evidence.{name} must be null or a non-empty string")
    modules = runtime_evidence["module_environment"]
    if not isinstance(modules, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in modules
    ):
        raise ValueError("runtime_evidence.module_environment must be a string sequence")
    resources = runtime_evidence["resource_allocation"]
    if not isinstance(resources, Mapping) or not resources:
        raise ValueError("runtime_evidence.resource_allocation must be a non-empty mapping")
    normalized_runtime_evidence = {
        "torch_version": torch_version,
        "cuda_runtime_version": runtime_evidence["cuda_runtime_version"],
        "gpu_driver_version": runtime_evidence["gpu_driver_version"],
        "module_environment": list(modules),
        "resource_allocation": dict(resources),
    }
    descriptor = loaded.descriptor
    payload = {
        "format_version": 1,
        "run_id": descriptor["run_id"],
        "descriptor_sha256": descriptor["descriptor_sha256"],
        "source_fingerprint": descriptor["source_fingerprint"],
        "source_lock_sha256": descriptor.get("source_lock_sha256"),
        "model_identity": descriptor.get("model_identity"),
        "tokenizer_identity": descriptor.get("tokenizer_identity"),
        "dataset_identity": descriptor.get("dataset_identity"),
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "python_version": platform.python_version(),
        "runtime_evidence": normalized_runtime_evidence,
        "scheduler_job_id": scheduler_job_id,
        "accelerator_identity": accelerator_identity,
        "observed_at": time.time() if observed_at is None else float(observed_at),
    }
    raw_without_hash = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    payload["attestation_sha256"] = hashlib.sha256(raw_without_hash).hexdigest()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    target = loaded.paths.actor_attestation_path(actor_kind, actor_id, attempt_id)
    publish_immutable_bytes(target, raw + b"\n")
    return target
