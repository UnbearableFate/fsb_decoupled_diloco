"""Versioned run-artifact classification and generic-cleanup safety policy."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping


PLAN03_REQUIREMENTS = frozenset({"AUDIT-01", "FS-05"})


class ArtifactClass(str, Enum):
    AUTHORITY = "authority"
    AUDIT = "audit"
    TELEMETRY = "telemetry"
    CACHE = "cache"
    PAYLOAD = "payload"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


GENERIC_CLEANUP_CLASSES = frozenset(
    {ArtifactClass.TELEMETRY, ArtifactClass.CACHE, ArtifactClass.PAYLOAD, ArtifactClass.TEMPORARY}
)


@dataclass(frozen=True)
class ArtifactPolicy:
    format_version: int
    classes: Mapping[ArtifactClass, tuple[str, ...]]
    generic_cleanup_forbidden: tuple[str, ...]
    policy_sha256: str

    @classmethod
    def from_dict(cls, value: Any) -> "ArtifactPolicy":
        if not isinstance(value, dict) or set(value) != {
            "format_version",
            "classes",
            "generic_cleanup_forbidden",
            "policy_sha256",
        }:
            raise ValueError("artifact policy fields are invalid")
        if value["format_version"] != 1 or not isinstance(value["classes"], dict):
            raise ValueError("unsupported artifact policy")
        expected_classes = {
            item.value for item in ArtifactClass if item is not ArtifactClass.UNKNOWN
        }
        if set(value["classes"]) != expected_classes:
            raise ValueError("artifact policy must define every concrete artifact class")
        classes: dict[ArtifactClass, tuple[str, ...]] = {}
        for name, patterns in value["classes"].items():
            if not isinstance(patterns, list) or not patterns:
                raise ValueError("artifact policy class patterns must be non-empty lists")
            normalized = tuple(_validate_pattern(pattern) for pattern in patterns)
            if len(set(normalized)) != len(normalized):
                raise ValueError("artifact policy class patterns must be unique")
            classes[ArtifactClass(name)] = normalized
        forbidden = value["generic_cleanup_forbidden"]
        if not isinstance(forbidden, list) or not forbidden:
            raise ValueError("artifact policy cleanup-forbidden paths must be a non-empty list")
        normalized_forbidden = tuple(_validate_pattern(pattern) for pattern in forbidden)
        payload = {key: item for key, item in value.items() if key != "policy_sha256"}
        actual = hashlib.sha256(_canonical(payload)).hexdigest()
        if value["policy_sha256"] != actual:
            raise ValueError("artifact policy checksum mismatch")
        return cls(
            format_version=1,
            classes=classes,
            generic_cleanup_forbidden=normalized_forbidden,
            policy_sha256=actual,
        )

    def classify(self, relative_path: str) -> ArtifactClass:
        path = _validate_relative_path(relative_path)
        matches = [
            kind
            for kind, patterns in self.classes.items()
            if any(_matches(path, pattern) for pattern in patterns)
        ]
        protected = [
            kind for kind in matches if kind in {ArtifactClass.AUTHORITY, ArtifactClass.AUDIT}
        ]
        if protected:
            if len(protected) > 1:
                raise ValueError(
                    f"artifact path matches multiple protected policy classes: {relative_path}"
                )
            return protected[0]
        if ArtifactClass.TEMPORARY in matches:
            return ArtifactClass.TEMPORARY
        if len(matches) > 1:
            raise ValueError(f"artifact path matches multiple policy classes: {relative_path}")
        return matches[0] if matches else ArtifactClass.UNKNOWN

    def allows_generic_cleanup(self, relative_path: str) -> bool:
        path = _validate_relative_path(relative_path)
        if any(_matches(path, pattern) for pattern in self.generic_cleanup_forbidden):
            return False
        return self.classify(path) in GENERIC_CLEANUP_CLASSES


def build_artifact_policy() -> dict[str, Any]:
    policy: dict[str, Any] = {
        "format_version": 1,
        "classes": {
            "authority": [
                ".identity",
                ".complete",
                "run_config.resolved.yaml",
                "control/bootstrap_complete.json",
                "control/run_descriptor.json",
                "control/run_config.resolved.yaml",
                "control/run_source_manifest.json",
                "control/artifact_policy.json",
                "control/syncer_metadata.sqlite3",
                "control/param_index.json",
                "control/bootstrap_scheduler_jobs.json",
                "control/dynamic_close_request.json",
                "control/scheduler_operator_requests/**",
                "control/registration_requests/**",
                "control/registration_history_v4/**",
                "control/registration_dispositions_v4/**",
                "control/static_replacement_requests/**",
                "control/syncer_epochs/**",
                "control/syncer_launch_claims/**",
                "updates/receipts/**",
                "updates/proposals/**",
                "metrics/attestations/**",
            ],
            "audit": ["audit", "audit/**"],
            "telemetry": ["metrics", "metrics/**", "logs", "logs/**"],
            "cache": [
                "heartbeats",
                "heartbeats/**",
                "updates/latest",
                "updates/latest/**",
                "control/latest.json",
                "control/stop.json",
                "control/summary.json",
            ],
            "payload": [
                "weights/**",
                "optim/**",
                "updates/payloads/**",
                "eval_checkpoints/**",
            ],
            "temporary": ["**/*.tmp", "**/*.part", "**/*.staging", "**/.tmp-*"],
        },
        "generic_cleanup_forbidden": [
            ".identity",
            ".complete",
            "control/syncer_metadata.sqlite3",
            "control/syncer_metadata.sqlite3-*",
            "audit",
            "audit/**",
        ],
    }
    policy["policy_sha256"] = hashlib.sha256(_canonical(policy)).hexdigest()
    ArtifactPolicy.from_dict(policy)
    return policy


def _validate_pattern(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError("artifact policy patterns must be relative POSIX paths")
    if any(part == ".." for part in PurePosixPath(value).parts):
        raise ValueError("artifact policy patterns cannot traverse parents")
    return value


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError("artifact path must be a relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path must be canonical")
    return path.as_posix()


def _matches(path: str, pattern: str) -> bool:
    if any(character in pattern for character in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
