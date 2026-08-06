"""Immutable HA run descriptor loading and source/config identity gates."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, load_resolved_config_snapshot
from .constants import DYNAMIC_SCHEMA_VERSION, HA_SCHEMA_VERSION, PROTOCOL_VERSION
from ..storage.atomic_io import read_json, sha256_file
from ..storage.paths import RunPaths
from ..storage.schema_bootstrap import BootstrapIdentity


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


@dataclass(frozen=True)
class LoadedRunDescriptor:
    paths: RunPaths
    descriptor: dict[str, Any]
    config: Config
    identity: BootstrapIdentity


def load_run_descriptor(
    shared_root: str | Path,
    *,
    expected_run_id: str | None = None,
    expected_git_commit: str | None = None,
    expected_git_dirty: bool | None = None,
    expected_source_fingerprint: str | None = None,
    expected_descriptor_sha256: str | None = None,
) -> LoadedRunDescriptor:
    paths = RunPaths(Path(shared_root).resolve())
    descriptor = read_json(paths.run_descriptor_json)
    recorded_descriptor_sha = str(descriptor.get("descriptor_sha256", ""))
    actual_descriptor_sha = _sha256_json_without(descriptor, "descriptor_sha256")
    if not recorded_descriptor_sha or recorded_descriptor_sha != actual_descriptor_sha:
        raise RuntimeError("run descriptor self-checksum mismatch")
    if (
        expected_descriptor_sha256 is not None
        and actual_descriptor_sha != expected_descriptor_sha256
    ):
        raise RuntimeError("run descriptor does not match the submitted job identity")
    descriptor_mode = str(descriptor.get("mode", ""))
    if descriptor_mode == "full_ha_static":
        expected_schema_version = HA_SCHEMA_VERSION
        identity_mode = "full"
    elif descriptor_mode == "full_ha_dynamic":
        expected_schema_version = DYNAMIC_SCHEMA_VERSION
        identity_mode = "full_dynamic"
    else:
        raise RuntimeError(f"unsupported run descriptor mode: {descriptor_mode!r}")
    checks = {
        "shared_root": str(paths.shared_root),
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": expected_schema_version,
        "mode": descriptor_mode,
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
    config = load_resolved_config_snapshot(config_path)
    expected_config_mode = "dynamic" if descriptor_mode == "full_ha_dynamic" else "static"
    if config.membership.mode != expected_config_mode:
        raise RuntimeError("resolved config membership mode does not match descriptor")
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
    identity = BootstrapIdentity(
        run_id=str(descriptor["run_id"]),
        source_fingerprint=str(descriptor["source_fingerprint"]),
        config_sha256=str(descriptor["resolved_config_sha256"]),
        mode=identity_mode,
    )
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
