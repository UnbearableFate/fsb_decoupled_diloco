#!/usr/bin/env python3
"""Validate frozen Plan 02 source/config/descriptor identity before runtime import."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _capture_current_source(project_root: Path) -> dict[str, Any]:
    helper_path = Path(__file__).with_name("capture_source_identity.py")
    spec = importlib.util.spec_from_file_location("plan02_capture_source_identity", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source identity helper: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module.capture(project_root)
    if not isinstance(identity, dict):
        raise RuntimeError("source identity helper returned a non-object")
    return identity


def check_gate(
    *,
    project_root: Path,
    descriptor_path: Path,
    bootstrap_marker_path: Path,
) -> dict[str, Any]:
    descriptor = _read_object(descriptor_path)
    marker = _read_object(bootstrap_marker_path)
    current_source = _capture_current_source(project_root)
    mismatches: list[str] = []

    descriptor_sha256 = _sha256_file(descriptor_path)
    expected_descriptor_sha256 = str(marker.get("descriptor_sha256", ""))
    if descriptor_sha256 != expected_descriptor_sha256:
        mismatches.append("descriptor_sha256")

    config_path_value = descriptor.get("resolved_config_path")
    if not isinstance(config_path_value, str) or not config_path_value:
        mismatches.append("resolved_config_path")
        config_path = None
        config_sha256 = None
    else:
        config_path = Path(config_path_value)
        if not config_path.is_absolute():
            config_path = descriptor_path.parent / config_path
        try:
            config_sha256 = _sha256_file(config_path)
        except FileNotFoundError:
            config_sha256 = None
            mismatches.append("resolved_config_missing")
    expected_config_sha256 = str(descriptor.get("resolved_config_sha256", ""))
    if config_sha256 != expected_config_sha256:
        mismatches.append("resolved_config_sha256")
    if expected_config_sha256 != str(marker.get("config_sha256", "")):
        mismatches.append("marker_config_sha256")

    for key in ("git_commit", "git_dirty", "source_fingerprint"):
        descriptor_value = descriptor.get(key)
        marker_value = marker.get(key)
        current_value = current_source.get(key)
        if descriptor_value != marker_value:
            mismatches.append(f"marker_{key}")
        if descriptor_value != current_value:
            mismatches.append(key)

    for key in ("protocol_version", "schema_version", "run_id"):
        if descriptor.get(key) != marker.get(key):
            mismatches.append(f"marker_{key}")

    return {
        "gate_format_version": 1,
        "status": "PASS" if not mismatches else "BLOCKED",
        "checked_at": time.time(),
        "project_root": str(project_root.resolve()),
        "descriptor_path": str(descriptor_path.resolve()),
        "bootstrap_marker_path": str(bootstrap_marker_path.resolve()),
        "descriptor_sha256": descriptor_sha256,
        "resolved_config_path": str(config_path.resolve()) if config_path is not None else None,
        "resolved_config_sha256": config_sha256,
        "current_source": {
            key: current_source.get(key)
            for key in ("git_commit", "git_dirty", "source_fingerprint")
        },
        "mismatches": sorted(set(mismatches)),
        "fs_diloco_imported": "fs_diloco" in __import__("sys").modules,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--bootstrap-marker", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        payload = check_gate(
            project_root=args.project_root,
            descriptor_path=args.descriptor,
            bootstrap_marker_path=args.bootstrap_marker,
        )
    except BaseException as exc:
        payload = {
            "gate_format_version": 1,
            "status": "BLOCKED",
            "checked_at": time.time(),
            "error": f"{type(exc).__name__}: {exc}",
            "mismatches": ["gate_exception"],
            "fs_diloco_imported": "fs_diloco" in __import__("sys").modules,
        }
    _atomic_write_json(args.output_json, payload)
    raise SystemExit(0 if payload["status"] == "PASS" else 3)


if __name__ == "__main__":
    main()
