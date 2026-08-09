"""Strict configuration boundary for mandatory Full Protocol v4 runtimes."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .config import Config, _from_dict, config_to_dict
from .constants import DEFAULT_RUNS_DIR
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
        _validate_full_input_revisions(self.shared)


_REMOVED_V4_PATHS = (
    ("init",),
    ("fragments",),
    ("failure_sim",),
    ("coordination", "syncer_ha"),
    ("coordination", "recovery_submission"),
    ("sync", "stop_after_global_tokens"),
    ("sync", "capture_terminal_predecessor_for_eval"),
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


def config_v4_to_dict(config: ConfigV4) -> dict[str, Any]:
    """Return the flattened on-wire v4 representation.

    ``ConfigV4.shared`` deliberately reuses the common model/training schema,
    but removed v1-v3 runtime switches must never leak back onto the v4 wire.
    """

    config.validate(ConfigProfile.FULL_V4)
    payload = config_to_dict(config.shared)
    payload["coordination"] = {"leader": dataclasses.asdict(config.leader)}
    payload["config_schema_version"] = config.config_schema_version
    payload["maintenance"] = dataclasses.asdict(config.maintenance)
    sync = payload.get("sync")
    if not isinstance(sync, dict):
        raise RuntimeError("internal sync serialization is invalid")
    if config.stop_after_direct_weight_tokens_applied is not None:
        sync["stop_after_direct_weight_tokens_applied"] = (
            config.stop_after_direct_weight_tokens_applied
        )
    return payload


def resolved_config_v4_bytes(config: ConfigV4) -> bytes:
    return yaml.safe_dump(
        config_v4_to_dict(config),
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")


def write_resolved_config_v4(config: ConfigV4, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resolved_config_v4_bytes(config))


def resolve_config_v4(
    path: str | Path,
    *,
    run_id: str | None = None,
    shared_root: str | None = None,
    project_root: str | Path | None = None,
) -> ConfigV4:
    """Resolve mutable launch identity without accepting legacy runtime keys."""

    config = load_config_v4(path, profile=ConfigProfile.FULL_V4)
    shared = config.shared
    git_commit = os.environ.get("FS_DILOCO_GIT_COMMIT")
    source_fingerprint = os.environ.get("FS_DILOCO_SOURCE_FINGERPRINT")
    if git_commit:
        shared.run.git_commit = git_commit
    if "FS_DILOCO_GIT_DIRTY" in os.environ:
        shared.run.git_dirty = _environment_flag("FS_DILOCO_GIT_DIRTY")
    if source_fingerprint:
        shared.run.source_fingerprint = source_fingerprint
    if _environment_flag("FS_DILOCO_REQUIRE_SOURCE_IDENTITY") and (
        not shared.run.git_commit or not shared.run.source_fingerprint
    ):
        raise ValueError(
            "formal run requires source identity: set FS_DILOCO_GIT_COMMIT and "
            "FS_DILOCO_SOURCE_FINGERPRINT"
        )
    if run_id is not None:
        shared.run.run_id = run_id
    if shared.run.run_id is None:
        shared.run.run_id = os.environ.get("RUN_ID") or (
            time.strftime("%Y%m%d_%H%M%S") + f"_{shared.run.name}"
        )
    if shared_root is not None:
        shared.run.shared_root = shared_root
    if shared.run.shared_root is None:
        root = Path(project_root or os.getcwd())
        shared.run.shared_root = str(root / DEFAULT_RUNS_DIR / shared.run.run_id)
    else:
        shared.run.shared_root = shared.run.shared_root.replace("{run_id}", shared.run.run_id)
    config.validate(ConfigProfile.FULL_V4)
    return config


def migrate_v3_payload_to_v4(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Migrate one full v1-v3 mapping and return an auditable structured diff."""

    if not isinstance(payload, dict):
        raise ValueError("config must contain a mapping")
    migrated = _deep_copy_mapping(payload)
    fragments = migrated.get("fragments")
    if isinstance(fragments, dict) and bool(fragments.get("enabled")):
        raise ValueError("fragment config is unsupported by Full Protocol v4")
    changes: list[dict[str, Any]] = []

    def remove(path: tuple[str, ...], *, reason: str) -> Any:
        current: Any = migrated
        for part in path[:-1]:
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        if not isinstance(current, dict) or path[-1] not in current:
            return None
        previous = current.pop(path[-1])
        changes.append({"op": "remove", "path": ".".join(path), "old": previous, "reason": reason})
        return previous

    remove(("init",), reason="classic resume authority is removed")
    remove(("fragments",), reason="fragment runtime is not expressible in v4")
    remove(("failure_sim",), reason="classic learner failure injection is removed")
    sync = migrated.setdefault("sync", {})
    if not isinstance(sync, dict):
        raise ValueError("sync must be a mapping")
    legacy_token_stop = sync.get("stop_after_global_tokens")
    if legacy_token_stop is not None:
        raise ValueError(
            "sync.stop_after_global_tokens is ambiguous; choose "
            "stop_after_direct_weight_tokens_applied explicitly"
        )
    remove(
        ("sync", "stop_after_global_tokens"),
        reason="null ambiguous legacy token stop has no semantic effect",
    )
    coordination = migrated.setdefault("coordination", {})
    if not isinstance(coordination, dict):
        raise ValueError("coordination must be a mapping")
    remove(
        ("coordination", "recovery_submission"),
        reason="legacy recovery-submission proxy is removed",
    )
    legacy_ha = coordination.pop("syncer_ha", None)
    if legacy_ha is not None:
        if not isinstance(legacy_ha, dict):
            raise ValueError("coordination.syncer_ha must be a mapping")
        leader = {key: value for key, value in legacy_ha.items() if key != "enabled"}
        coordination["leader"] = leader
        changes.append(
            {
                "op": "replace",
                "path": "coordination.syncer_ha",
                "new_path": "coordination.leader",
                "old": legacy_ha,
                "new": leader,
                "reason": "leader fencing is mandatory",
            }
        )
    elif "leader" not in coordination:
        coordination["leader"] = {}
        changes.append(
            {
                "op": "add",
                "path": "coordination.leader",
                "new": coordination["leader"],
                "reason": "materialize mandatory leader defaults",
            }
        )
    migrated["config_schema_version"] = CONFIG_SCHEMA_VERSION
    migrated.setdefault("maintenance", {})
    changes.append(
        {
            "op": "add",
            "path": "config_schema_version",
            "new": CONFIG_SCHEMA_VERSION,
            "reason": "select strict Full Protocol v4 schema",
        }
    )
    # Parse once and keep the source-level payload concise. The immutable
    # resolved snapshot materializes defaults through ``config_v4_to_dict``.
    config = _config_v4_from_payload(migrated)
    config.validate(ConfigProfile.FULL_V4)
    return migrated, changes


def migrate_v3_bytes_to_v4(data: bytes) -> tuple[bytes, dict[str, Any]]:
    loaded = yaml.safe_load(data) or {}
    migrated, changes = migrate_v3_payload_to_v4(loaded)
    output = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True).encode("utf-8")
    # A byte round trip is part of migration acceptance, not merely an in-memory check.
    loaded_v4 = yaml.safe_load(output)
    _config_v4_from_payload(loaded_v4).validate(ConfigProfile.FULL_V4)
    return output, {
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "changes": changes,
    }


def _config_v4_from_payload(loaded: Any) -> ConfigV4:
    if not isinstance(loaded, dict):
        raise ValueError("config must contain a mapping")
    for dotted in _REMOVED_V4_PATHS:
        current: Any = loaded
        for part in dotted:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            raise ValueError(f"removed v4 config key is present: {'.'.join(dotted)}")
    payload = _deep_copy_mapping(loaded)
    schema_version = payload.pop("config_schema_version", None)
    coordination = payload.pop("coordination", {})
    if not isinstance(coordination, dict):
        raise ValueError("coordination must be a mapping")
    leader_payload = coordination.pop("leader", {})
    if coordination:
        raise ValueError("unknown coordination keys: " + ", ".join(sorted(coordination)))
    maintenance_payload = payload.pop("maintenance", {})
    sync_payload = payload.get("sync", {})
    if not isinstance(sync_payload, dict):
        raise ValueError("sync must be a mapping")
    sync_payload = dict(sync_payload)
    stop_tokens = sync_payload.pop("stop_after_direct_weight_tokens_applied", None)
    payload["sync"] = sync_payload
    return ConfigV4(
        shared=_from_dict(Config, payload),
        config_schema_version=schema_version,
        leader=_strict_dataclass(LeaderSection, leader_payload, "coordination.leader"),
        maintenance=_strict_dataclass(MaintenanceSection, maintenance_payload, "maintenance"),
        stop_after_direct_weight_tokens_applied=stop_tokens,
    )


def _deep_copy_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    # YAML-compatible configuration values are JSON-compatible after safe_load.
    import copy

    return copy.deepcopy(payload)


def _environment_flag(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


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


_IMMUTABLE_HUB_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SYNTHETIC_MODELS = frozenset({"synthetic-tiny", "tiny-synthetic", "tiny-local"})


def _is_explicit_local_reference(value: str) -> bool:
    return value.startswith(("/", "./", "../", "file://"))


def _require_immutable_hub_revision(value: str | None, *, name: str) -> None:
    if not isinstance(value, str) or _IMMUTABLE_HUB_REVISION.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 40-character lowercase Hub commit SHA")


def _validate_full_input_revisions(config: Config) -> None:
    model_name = config.model.name_or_path
    if model_name not in _SYNTHETIC_MODELS and not _is_explicit_local_reference(model_name):
        _require_immutable_hub_revision(config.model.revision, name="model.revision")
        _require_immutable_hub_revision(
            config.model.tokenizer_revision or config.model.revision,
            name="model.tokenizer_revision",
        )
    dataset_name = config.data.dataset_name
    if dataset_name != "synthetic" and not _is_explicit_local_reference(dataset_name):
        _require_immutable_hub_revision(config.data.revision, name="data.revision")
