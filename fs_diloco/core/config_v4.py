"""Strict transitional configuration boundary for Full Protocol v4.

The production entrypoints remain on the frozen v1-v3 ``Config`` until the P4
cutover.  This module establishes the v4 schema/profile validator without
silently teaching the old runtime to accept removed keys.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .config import Config, _from_dict
from .versions import CONFIG_SCHEMA_VERSION


PLAN03_REQUIREMENTS = frozenset({"DATA-04", "TOK-07"})


class ConfigProfile(str, Enum):
    FULL_V4 = "full_v4"
    TORCH_BASELINE = "torch_baseline"


@dataclass(frozen=True)
class LeaderSection:
    lease_duration_seconds: float = 90.0
    renew_interval_seconds: float = 10.0
    max_clock_skew_seconds: float = 2.0
    heartbeat_interval_seconds: float = 5.0
    heartbeat_stale_after_seconds: float = 30.0
    lease_busy_timeout_ms: int = 5000
    business_busy_timeout_ms: int = 60_000
    candidate_acquire_poll_seconds: float = 5.0
    candidate_wait_seconds: float = 180.0
    learner_recovery_wait_seconds: float = 1800.0
    canonical_repair_wait_seconds: float = 120.0
    max_retained_epoch_dirs: int = 32

    def validate(self) -> None:
        positive = (
            "lease_duration_seconds",
            "renew_interval_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_stale_after_seconds",
            "candidate_acquire_poll_seconds",
            "candidate_wait_seconds",
            "learner_recovery_wait_seconds",
            "canonical_repair_wait_seconds",
        )
        for name in positive:
            _require_finite_number(
                getattr(self, name), f"coordination.leader.{name}", positive=True
            )
        _require_finite_number(
            self.max_clock_skew_seconds,
            "coordination.leader.max_clock_skew_seconds",
            nonnegative=True,
        )
        _require_positive_int(
            self.lease_busy_timeout_ms, "coordination.leader.lease_busy_timeout_ms"
        )
        _require_positive_int(
            self.business_busy_timeout_ms,
            "coordination.leader.business_busy_timeout_ms",
        )
        _require_positive_int(
            self.max_retained_epoch_dirs,
            "coordination.leader.max_retained_epoch_dirs",
        )
        if self.lease_duration_seconds < 5.0 * self.renew_interval_seconds:
            raise ValueError("leader lease duration must be at least 5 * renew interval")
        if not self.heartbeat_interval_seconds <= self.renew_interval_seconds:
            raise ValueError("leader heartbeat interval must not exceed renew interval")
        if self.heartbeat_stale_after_seconds < 3.0 * self.heartbeat_interval_seconds:
            raise ValueError("leader heartbeat stale timeout must cover three heartbeats")
        if self.lease_duration_seconds < (
            self.heartbeat_stale_after_seconds + 2.0 * self.max_clock_skew_seconds
        ):
            raise ValueError("leader lease must cover heartbeat stale timeout and clock skew")
        if self.lease_busy_timeout_ms > self.renew_interval_seconds * 1000.0:
            raise ValueError("leader lease busy timeout must not exceed renew interval")
        if self.candidate_acquire_poll_seconds > self.renew_interval_seconds:
            raise ValueError("candidate acquire polling must not exceed renew interval")
        if self.candidate_wait_seconds < (
            self.lease_duration_seconds + self.max_clock_skew_seconds
        ):
            raise ValueError("candidate wait must cover lease duration and clock skew")
        if self.learner_recovery_wait_seconds < self.candidate_wait_seconds:
            raise ValueError("learner recovery wait must cover candidate wait")
        if self.canonical_repair_wait_seconds < 2.0 * self.heartbeat_interval_seconds:
            raise ValueError("canonical repair wait must cover two heartbeats")


@dataclass(frozen=True)
class MaintenanceSection:
    archive_batch_rows: int = 256
    recent_batch_dedup_count: int = 64
    hot_receipts_per_contributor: int = 64
    hot_observations_per_contributor: int = 64
    quarantine_records_per_contributor: int = 64
    publication_orphan_grace_seconds: float = 120.0

    def validate(self, leader: LeaderSection) -> None:
        for name in (
            "archive_batch_rows",
            "recent_batch_dedup_count",
            "hot_receipts_per_contributor",
            "hot_observations_per_contributor",
            "quarantine_records_per_contributor",
        ):
            _require_positive_int(getattr(self, name), f"maintenance.{name}")
        _require_finite_number(
            self.publication_orphan_grace_seconds,
            "maintenance.publication_orphan_grace_seconds",
            positive=True,
        )
        minimum = leader.lease_duration_seconds + 2.0 * leader.max_clock_skew_seconds
        if self.publication_orphan_grace_seconds < minimum:
            raise ValueError(
                "maintenance.publication_orphan_grace_seconds must cover "
                "lease_duration_seconds + 2 * max_clock_skew_seconds"
            )


@dataclass
class ConfigV4:
    shared: Config = field(default_factory=Config)
    config_schema_version: int = CONFIG_SCHEMA_VERSION
    leader: LeaderSection = field(default_factory=LeaderSection)
    maintenance: MaintenanceSection = field(default_factory=MaintenanceSection)
    stop_after_direct_weight_tokens_applied: int | None = None

    def validate(self, profile: ConfigProfile | str) -> None:
        try:
            selected_profile = ConfigProfile(profile)
        except ValueError as exc:
            raise ValueError(f"unknown config validation profile: {profile}") from exc
        if isinstance(self.config_schema_version, bool) or (
            not isinstance(self.config_schema_version, int)
            or self.config_schema_version != CONFIG_SCHEMA_VERSION
        ):
            raise ValueError(f"config_schema_version must be exactly {CONFIG_SCHEMA_VERSION}")
        self.shared.validate(
            profile=(
                "torch_baseline"
                if selected_profile is ConfigProfile.TORCH_BASELINE
                else "full_v4_shared"
            )
        )
        _validate_shared(self.shared)
        if self.stop_after_direct_weight_tokens_applied is not None:
            _require_positive_int(
                self.stop_after_direct_weight_tokens_applied,
                "sync.stop_after_direct_weight_tokens_applied",
            )
        baseline = bool(self.shared.torch_baseline.enabled)
        if selected_profile is ConfigProfile.TORCH_BASELINE:
            if not baseline:
                raise ValueError("torch_baseline profile requires torch_baseline.enabled=true")
            _validate_baseline(self.shared)
            return
        if baseline:
            raise ValueError("full_v4 profile cannot be used to validate a torch baseline config")
        self.leader.validate()
        self.maintenance.validate(self.leader)
        if self.shared.init.resume:
            raise ValueError("init.resume is removed from Full Protocol v4")
        if self.shared.coordination.syncer_ha.enabled:
            raise ValueError(
                "coordination.syncer_ha is removed from Full Protocol v4; leader is mandatory"
            )
        if self.shared.fragments.enabled:
            raise ValueError("fragments are not supported by Full Protocol v4")
        if self.shared.sync.stop_after_global_tokens is not None:
            raise ValueError(
                "sync.stop_after_global_tokens is ambiguous and removed; use "
                "sync.stop_after_direct_weight_tokens_applied"
            )
        if self.shared.data.streaming:
            raise ValueError(
                "data.streaming=true is not resumable in v4; use indexed/materialized data"
            )
        if (
            self.shared.training.completion_mode == "global_only"
            and self.shared.sync.stop_after_outer_steps is None
            and self.stop_after_direct_weight_tokens_applied is None
        ):
            raise ValueError("global_only completion requires an unambiguous global stop target")


_REMOVED_V4_PATHS = (
    ("init", "resume"),
    ("fragments",),
    ("coordination", "syncer_ha"),
    ("sync", "stop_after_global_tokens"),
)


def load_config_v4(path: str | Path, *, profile: ConfigProfile | str) -> ConfigV4:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config {path} must contain a mapping")
    for dotted in _REMOVED_V4_PATHS:
        current: Any = loaded
        for part in dotted:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            raise ValueError(f"removed v4 config key is present: {'.'.join(dotted)}")
    payload = dict(loaded)
    schema_version = payload.pop("config_schema_version", None)
    coordination = payload.pop("coordination", {})
    if not isinstance(coordination, dict):
        raise ValueError("coordination must be a mapping")
    coordination_payload = dict(coordination)
    leader_payload = coordination_payload.pop("leader", {})
    payload["coordination"] = {
        "recovery_submission": coordination_payload.pop("recovery_submission", {})
    }
    if coordination_payload:
        raise ValueError("unknown coordination keys: " + ", ".join(sorted(coordination_payload)))
    maintenance_payload = payload.pop("maintenance", {})
    sync_payload = payload.get("sync", {})
    if not isinstance(sync_payload, dict):
        raise ValueError("sync must be a mapping")
    sync_payload = dict(sync_payload)
    stop_tokens = sync_payload.pop("stop_after_direct_weight_tokens_applied", None)
    payload["sync"] = sync_payload
    config = ConfigV4(
        shared=_from_dict(Config, payload),
        config_schema_version=schema_version,
        leader=_strict_dataclass(LeaderSection, leader_payload, "coordination.leader"),
        maintenance=_strict_dataclass(MaintenanceSection, maintenance_payload, "maintenance"),
        stop_after_direct_weight_tokens_applied=stop_tokens,
    )
    config.validate(profile)
    return config


def _strict_dataclass(cls: type[Any], payload: Any, path: str) -> Any:
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must be a mapping")
    fields = {item.name for item in dataclasses.fields(cls)}
    unknown = sorted(set(payload) - fields)
    if unknown:
        raise ValueError(f"unknown {path} keys: {', '.join(unknown)}")
    return cls(**payload)


def _require_positive_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_finite_number(
    value: Any,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if positive and number <= 0.0:
        raise ValueError(f"{name} must be > 0")
    if nonnegative and number < 0.0:
        raise ValueError(f"{name} must be >= 0")


def _validate_shared(config: Config) -> None:
    _require_positive_int(config.sync.num_learners, "sync.num_learners")
    _require_positive_int(config.sync.quorum_min, "sync.quorum_min")
    _require_positive_int(config.sync.quorum_max, "sync.quorum_max")
    if not config.sync.quorum_min <= config.sync.quorum_max <= config.sync.num_learners:
        raise ValueError("sync quorum must satisfy min <= max <= num_learners")
    _require_finite_number(
        config.sync.scan_interval_seconds, "sync.scan_interval_seconds", positive=True
    )
    _require_finite_number(config.sync.staleness_lambda, "sync.staleness_lambda", nonnegative=True)
    _require_positive_int(config.training.inner_steps, "training.inner_steps")
    _require_positive_int(config.training.micro_batch_size, "training.micro_batch_size")
    _require_positive_int(
        config.training.gradient_accumulation_steps,
        "training.gradient_accumulation_steps",
    )
    if config.training.completion_mode not in {"local_or_global", "global_only"}:
        raise ValueError("unsupported training.completion_mode")
    if config.inner_optimizer.scheduler not in {"none", "cosine"}:
        raise ValueError("unsupported inner_optimizer.scheduler")
    if config.membership.mode not in {"static", "dynamic"}:
        raise ValueError("membership.mode must be static or dynamic")


def _validate_baseline(config: Config) -> None:
    if config.torch_baseline.backend not in {"gloo", "nccl"}:
        raise ValueError("torch baseline backend must be gloo or nccl")
    if config.training.max_local_steps is None:
        raise ValueError("torch baseline requires training.max_local_steps")
    _require_positive_int(config.training.max_local_steps, "training.max_local_steps")
