"""Strict Full Protocol configuration loading and runtime resolution."""

from __future__ import annotations

import dataclasses
import math
import os
import re
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

import yaml

from .adoption_rules import validate_global_adoption_config
from .constants import DEFAULT_RUNS_DIR
from .versions import CONFIG_SCHEMA_VERSION

T = TypeVar("T")


class ConfigSection:
    """Pure structural validation shared by every dataclass config section."""

    def validate(self, *, path: str | None = None) -> None:
        section_path = path or type(self).__name__.removesuffix("Section").lower()
        type_hints = get_type_hints(type(self))
        for field_info in dataclasses.fields(self):
            value = getattr(self, field_info.name)
            field_path = f"{section_path}.{field_info.name}" if section_path else field_info.name
            annotation = type_hints.get(field_info.name, field_info.type)
            _validate_config_value(value, annotation, field_path)
            if isinstance(value, ConfigSection):
                value.validate(path=field_path)


def _validate_config_value(value: Any, annotation: Any, path: str) -> None:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {types.UnionType, Union}:
        if value is None and type(None) in args:
            return
        errors: list[ValueError] = []
        for option in (item for item in args if item is not type(None)):
            try:
                _validate_config_value(value, option, path)
                return
            except ValueError as exc:
                errors.append(exc)
        raise ValueError(f"{path} has the wrong type") from (errors[-1] if errors else None)
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be a list")
        if args:
            for index, item in enumerate(value):
                _validate_config_value(item, args[0], f"{path}[{index}]")
        return
    if origin is tuple:
        if not isinstance(value, tuple):
            raise ValueError(f"{path} must be a tuple")
        if len(args) == 2 and args[1] is Ellipsis:
            for index, item in enumerate(value):
                _validate_config_value(item, args[0], f"{path}[{index}]")
        elif args and len(value) != len(args):
            raise ValueError(f"{path} has the wrong tuple length")
        else:
            for index, (item, option) in enumerate(zip(value, args, strict=True)):
                _validate_config_value(item, option, f"{path}[{index}]")
        return
    if annotation is Any:
        return
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
        return
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
        return
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be a number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{path} must be finite")
        return
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        return
    if isinstance(annotation, type) and not isinstance(value, annotation):
        raise ValueError(f"{path} must be {annotation.__name__}")


@dataclass
class RunSection(ConfigSection):
    name: str = "full_protocol"
    run_id: str | None = None
    shared_root: str | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    source_fingerprint: str | None = None


@dataclass
class ModelSection(ConfigSection):
    name_or_path: str = "synthetic-tiny"
    revision: str | None = None
    tokenizer_revision: str | None = None
    trust_remote_code: bool = False
    dtype: str = "float32"
    compile: bool = False
    synthetic_vocab_size: int = 128
    synthetic_hidden_size: int = 32


@dataclass
class DataSection(ConfigSection):
    dataset_name: str = "synthetic"
    dataset_config_name: str | None = None
    revision: str | None = None
    train_split: str = "train"
    block_size: int = 16
    cache_dir: str | None = None
    shuffle_blocks: bool = True


@dataclass
class SyncSection(ConfigSection):
    num_learners: int = 8
    quorum_min: int = 4
    quorum_max: int = 8
    staleness_lambda: float = 0.25
    scan_interval_seconds: float = 2.0
    stop_after_outer_steps: int | None = 20
    stop_after_direct_weight_tokens_applied: int | None = None


@dataclass
class SyncerSection(ConfigSection):
    device: str = "auto"
    compute_dtype: str = "float32"
    publish_dtype: str = "float32"


@dataclass
class MembershipSection(ConfigSection):
    mode: str = "static"
    stream_pool_size: int = 8
    bootstrap_instances: int = 8
    initial_membership_deadline_seconds: float = 1800.0
    registration_scan_interval_seconds: float = 2.0
    registration_request_ttl_seconds: float = 120.0
    heartbeat_dead_after_seconds: float = 300.0


@dataclass
class ScalingSection(ConfigSection):
    enabled: bool = False
    desired_contributors: int = 8
    low_contributor_threshold: int = 6
    consecutive_low_windows: int = 2
    productive_window_count: int = 2
    startup_grace_seconds: float = 180.0
    productive_upload_grace_factor: float = 2.0
    productive_upload_grace_min_seconds: float = 60.0
    productive_upload_grace_max_seconds: float = 600.0
    cooldown_seconds: float = 300.0
    max_pending_launch_requests: int = 2
    max_total_launch_requests: int = 16
    launch_request_ttl_seconds: float = 900.0
    capacity_observation_retention_count: int = 64
    scheduler_reconcile_interval_seconds: float = 30.0
    scheduler_uncertainty_timeout_seconds: float = 300.0
    starvation_observation_seconds: float = 120.0
    learner_pbs_script: str = "scripts/miyabi/agent/run_learner.pbs"
    learner_walltime: str | None = None
    learner_queue: str | None = None


@dataclass
class TerminalSection(ConfigSection):
    admission_close_policy: str = "global_target_or_launch_budget"
    deadline_seconds: float | None = None
    drain_ack_timeout_seconds: float = 300.0
    registration_visibility_grace_seconds: float = 10.0
    proposal_visibility_grace_seconds: float = 20.0
    max_terminal_merges: int = 1
    allow_preclose_admission_during_drain: bool = False


@dataclass
class TrainingSection(ConfigSection):
    inner_steps: int = 100
    micro_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    max_local_steps: int | None = None
    completion_mode: str = "local_or_global"
    seed: int = 1337
    grad_clip: float | None = None


@dataclass
class InnerOptimizerSection(ConfigSection):
    name: str = "adamw"
    lr: float = 5.0e-5
    betas: tuple[float, float] = (0.9, 0.95)
    eps: float = 1.0e-8
    weight_decay: float = 0.1
    scheduler: str = "none"
    warmup_steps: int = 100
    scheduler_total_steps: int | None = None
    min_lr_ratio: float = 0.1


@dataclass
class OuterOptimizerSection(ConfigSection):
    name: str = "nesterov"
    lr: float = 0.7
    momentum: float = 0.9
    weight_decay: float = 0.0
    betas: tuple[float, float] = (0.9, 0.999)
    eps: float = 1.0e-8


@dataclass
class IOSection(ConfigSection):
    tensor_dtype: str = "float32"


@dataclass
class PredictionSection(ConfigSection):
    reconcile_timeout_seconds: float = 60.0


@dataclass
class LearnerSection(ConfigSection):
    poll_latest_during_inner_steps: bool = False
    adopt_global_after_upload: bool = True
    global_adoption_strategy: str = "replace"
    post_publish_latest_wait_seconds: float = 0.0
    post_publish_latest_poll_seconds: float = 0.2
    prediction: PredictionSection = field(default_factory=PredictionSection)


@dataclass
class LeaderSection(ConfigSection):
    lease_duration_seconds: float = 90.0
    renew_interval_seconds: float = 10.0
    max_clock_skew_seconds: float = 2.0
    business_busy_timeout_ms: int = 60_000
    candidate_acquire_poll_seconds: float = 5.0
    candidate_wait_seconds: float = 180.0
    learner_recovery_wait_seconds: float = 1800.0


@dataclass
class MaintenanceSection(ConfigSection):
    archive_batch_rows: int = 256
    recent_batch_dedup_count: int = 64
    quarantine_records_per_contributor: int = 64
    publication_orphan_grace_seconds: float = 120.0


@dataclass
class Config(ConfigSection):
    config_schema_version: int = CONFIG_SCHEMA_VERSION
    run: RunSection = field(default_factory=RunSection)
    model: ModelSection = field(default_factory=ModelSection)
    data: DataSection = field(default_factory=DataSection)
    sync: SyncSection = field(default_factory=SyncSection)
    syncer: SyncerSection = field(default_factory=SyncerSection)
    membership: MembershipSection = field(default_factory=MembershipSection)
    scaling: ScalingSection = field(default_factory=ScalingSection)
    terminal: TerminalSection = field(default_factory=TerminalSection)
    training: TrainingSection = field(default_factory=TrainingSection)
    inner_optimizer: InnerOptimizerSection = field(default_factory=InnerOptimizerSection)
    outer_optimizer: OuterOptimizerSection = field(default_factory=OuterOptimizerSection)
    io: IOSection = field(default_factory=IOSection)
    learner: LearnerSection = field(default_factory=LearnerSection)
    leader: LeaderSection = field(default_factory=LeaderSection)
    maintenance: MaintenanceSection = field(default_factory=MaintenanceSection)

    def validate(self, *, path: str | None = None) -> None:
        _normalize_and_validate(self)

    def _validate_structure(self) -> None:
        type_hints = get_type_hints(type(self))
        for field_info in dataclasses.fields(self):
            value = getattr(self, field_info.name)
            annotation = type_hints.get(field_info.name, field_info.type)
            _validate_config_value(value, annotation, field_info.name)
            if isinstance(value, ConfigSection):
                value.validate(path=field_info.name)
        if self.config_schema_version != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"config_schema_version must be exactly {CONFIG_SCHEMA_VERSION}")


def _coerce_scalar(value: Any, target_type: Any) -> Any:
    origin = get_origin(target_type)
    args = get_args(target_type)
    if origin is tuple and isinstance(value, list):
        return tuple(value)
    if origin is not None and type(None) in args:
        non_none = [arg for arg in args if arg is not type(None)]
        if value is None:
            return None
        if non_none:
            return _coerce_scalar(value, non_none[0])
    return value


def _from_dict(
    cls: type[T],
    data: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> T:
    type_hints = get_type_hints(cls)
    field_names = {field_info.name for field_info in dataclasses.fields(cls)}
    unknown = sorted(set(data) - field_names)
    if unknown:
        dotted = [".".join((*path, key)) for key in unknown]
        raise ValueError("unknown config keys: " + ", ".join(dotted))
    kwargs: dict[str, Any] = {}
    for field_info in dataclasses.fields(cls):
        if field_info.name not in data:
            continue
        value = data[field_info.name]
        field_type = type_hints.get(field_info.name, field_info.type)
        if dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            kwargs[field_info.name] = _from_dict(
                field_type,
                value,
                path=(*path, field_info.name),
            )
        else:
            kwargs[field_info.name] = _coerce_scalar(value, field_type)
    return cls(**kwargs)


def config_to_dict(config: Config) -> dict[str, Any]:
    payload = dataclasses.asdict(config)
    if config.membership.mode == "static":
        payload["membership"].pop("stream_pool_size")
        payload["membership"].pop("bootstrap_instances")
        payload.pop("scaling")
    return payload


def _validate_source_shape(data: dict[str, Any]) -> None:
    membership = data.get("membership") or {}
    if not isinstance(membership, dict):
        return
    mode = membership.get("mode", "static")
    if mode == "static":
        repeated = sorted({"stream_pool_size", "bootstrap_instances"} & set(membership))
        if repeated:
            raise ValueError(
                "static membership derives contributor counts from sync.num_learners; "
                f"remove: {', '.join(repeated)}"
            )
        if "scaling" in data:
            raise ValueError("static membership must not declare inert scaling configuration")


def _parse_config(path: str | Path) -> Config:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config {path} must contain a mapping")
    _validate_source_shape(loaded)
    return _from_dict(Config, loaded)


def load_config(path: str | Path) -> Config:
    """Load and fully validate one current Full Protocol config."""

    return _normalize_and_validate(_parse_config(path))


def _default_run_id(name: str) -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{name}"


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


def resolve_config(
    path: str | Path,
    *,
    run_id: str | None = None,
    shared_root: str | None = None,
    project_root: str | Path | None = None,
) -> Config:
    config = _parse_config(path)
    git_commit = os.environ.get("FS_DILOCO_GIT_COMMIT")
    source_fingerprint = os.environ.get("FS_DILOCO_SOURCE_FINGERPRINT")
    if git_commit:
        config.run.git_commit = git_commit
    if "FS_DILOCO_GIT_DIRTY" in os.environ:
        config.run.git_dirty = _environment_flag("FS_DILOCO_GIT_DIRTY")
    if source_fingerprint:
        config.run.source_fingerprint = source_fingerprint
    if _environment_flag("FS_DILOCO_REQUIRE_SOURCE_IDENTITY") and (
        not config.run.git_commit or not config.run.source_fingerprint
    ):
        raise ValueError(
            "formal run requires source identity: set FS_DILOCO_GIT_COMMIT and "
            "FS_DILOCO_SOURCE_FINGERPRINT"
        )
    if run_id is not None:
        config.run.run_id = run_id
    if config.run.run_id is None:
        config.run.run_id = os.environ.get("RUN_ID") or _default_run_id(config.run.name)
    if shared_root is not None:
        config.run.shared_root = shared_root
    if config.run.shared_root is None:
        root = Path(project_root or os.getcwd())
        config.run.shared_root = str(root / DEFAULT_RUNS_DIR / config.run.run_id)
    else:
        config.run.shared_root = config.run.shared_root.replace("{run_id}", config.run.run_id)
    return _normalize_and_validate(config)


def _normalize_and_validate(config: Config) -> Config:
    config._validate_structure()
    safe_component = re.compile(r"[A-Za-z0-9._-]+\Z")
    if not config.run.name or safe_component.fullmatch(config.run.name) is None:
        raise ValueError("run.name must be a safe non-empty identity component")
    if config.run.run_id is not None and safe_component.fullmatch(config.run.run_id) is None:
        raise ValueError("run.run_id must be a safe non-empty identity component")
    if config.run.shared_root is not None and not config.run.shared_root:
        raise ValueError("run.shared_root must not be empty when set")
    if (
        config.run.git_commit is not None
        and _IMMUTABLE_HUB_REVISION.fullmatch(config.run.git_commit) is None
    ):
        raise ValueError("run.git_commit must be a 40-character lowercase commit SHA")
    if (
        config.run.source_fingerprint is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", config.run.source_fingerprint) is None
    ):
        raise ValueError("run.source_fingerprint must be a sha256 digest")
    for name, value in (
        ("model.name_or_path", config.model.name_or_path),
        ("data.dataset_name", config.data.dataset_name),
        ("data.train_split", config.data.train_split),
    ):
        if not value:
            raise ValueError(f"{name} must not be empty")
    for name, value in (
        ("data.dataset_config_name", config.data.dataset_config_name),
        ("data.cache_dir", config.data.cache_dir),
    ):
        if value is not None and not value:
            raise ValueError(f"{name} must not be empty when set")
    for name, value in (
        ("model.synthetic_vocab_size", config.model.synthetic_vocab_size),
        ("model.synthetic_hidden_size", config.model.synthetic_hidden_size),
        ("data.block_size", config.data.block_size),
    ):
        if value < 2:
            raise ValueError(f"{name} must be >= 2")
    allowed_dtypes = {"float32", "bfloat16"}
    if config.model.dtype not in allowed_dtypes:
        raise ValueError(f"unsupported model.dtype: {config.model.dtype}")
    if config.io.tensor_dtype not in allowed_dtypes:
        raise ValueError(f"unsupported io.tensor_dtype: {config.io.tensor_dtype}")
    if config.sync.scan_interval_seconds <= 0.0:
        raise ValueError("sync.scan_interval_seconds must be > 0")
    if config.sync.staleness_lambda < 0.0:
        raise ValueError("sync.staleness_lambda must be >= 0")
    if config.syncer.device not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unsupported syncer.device: {config.syncer.device}")
    for field_name in ("compute_dtype", "publish_dtype"):
        configured = getattr(config.syncer, field_name)
        if configured not in allowed_dtypes:
            raise ValueError(f"unsupported syncer.{field_name}: {configured}")
    if config.training.completion_mode not in {"local_or_global", "global_only"}:
        raise ValueError(f"unsupported training.completion_mode: {config.training.completion_mode}")
    if config.training.max_local_steps is not None and config.training.max_local_steps <= 0:
        raise ValueError("training.max_local_steps must be > 0 when set")
    if config.training.grad_clip is not None and config.training.grad_clip <= 0.0:
        raise ValueError("training.grad_clip must be > 0 when set")
    if config.inner_optimizer.name != "adamw":
        raise ValueError(f"unsupported inner_optimizer.name: {config.inner_optimizer.name}")
    if config.inner_optimizer.lr <= 0.0:
        raise ValueError("inner_optimizer.lr must be > 0")
    if config.inner_optimizer.eps <= 0.0:
        raise ValueError("inner_optimizer.eps must be > 0")
    if config.inner_optimizer.weight_decay < 0.0:
        raise ValueError("inner_optimizer.weight_decay must be >= 0")
    if len(config.inner_optimizer.betas) != 2 or not all(
        0.0 <= beta < 1.0 for beta in config.inner_optimizer.betas
    ):
        raise ValueError("inner_optimizer.betas must contain two values in [0, 1)")
    if config.inner_optimizer.scheduler not in {"none", "cosine"}:
        raise ValueError(
            f"unsupported inner_optimizer.scheduler: {config.inner_optimizer.scheduler}"
        )
    if config.inner_optimizer.warmup_steps < 0:
        raise ValueError("inner_optimizer.warmup_steps must be >= 0")
    if not 0.0 < float(config.inner_optimizer.min_lr_ratio) <= 1.0:
        raise ValueError("inner_optimizer.min_lr_ratio must be > 0 and <= 1")
    scheduler_total_steps = config.inner_optimizer.scheduler_total_steps
    if config.inner_optimizer.scheduler == "cosine":
        if scheduler_total_steps is None:
            raise ValueError("inner_optimizer.scheduler_total_steps is required for cosine")
        if int(scheduler_total_steps) <= config.inner_optimizer.warmup_steps:
            raise ValueError(
                "inner_optimizer.scheduler_total_steps must be greater than "
                "inner_optimizer.warmup_steps for cosine"
            )
    elif scheduler_total_steps is not None and int(scheduler_total_steps) <= 0:
        raise ValueError("inner_optimizer.scheduler_total_steps must be > 0 when set")
    membership = config.membership
    scaling = config.scaling
    terminal = config.terminal
    if membership.mode not in {"static", "dynamic"}:
        raise ValueError("membership.mode must be one of: static, dynamic")
    dynamic = membership.mode == "dynamic"
    if dynamic and config.sync.num_learners != membership.stream_pool_size:
        raise ValueError("sync.num_learners must equal membership.stream_pool_size")
    if not dynamic:
        membership.stream_pool_size = config.sync.num_learners
        membership.bootstrap_instances = config.sync.num_learners
    if scaling.enabled and not dynamic:
        raise ValueError("scaling.enabled requires membership.mode=dynamic")
    if membership.stream_pool_size < membership.bootstrap_instances:
        raise ValueError("membership.stream_pool_size must be >= bootstrap_instances")
    if membership.bootstrap_instances < 0:
        raise ValueError("membership.bootstrap_instances must be >= 0")
    if membership.stream_pool_size < 1:
        raise ValueError("membership.stream_pool_size must be >= 1")
    if dynamic and not (
        config.sync.quorum_min
        <= scaling.desired_contributors
        <= config.sync.quorum_max
        <= membership.stream_pool_size
    ):
        raise ValueError(
            "dynamic capacity requires quorum_min <= desired_contributors <= "
            "quorum_max <= stream_pool_size"
        )
    if membership.initial_membership_deadline_seconds < membership.registration_request_ttl_seconds:
        raise ValueError(
            "membership initial_membership_deadline_seconds must cover registration request TTL"
        )
    for field_name in (
        "initial_membership_deadline_seconds",
        "registration_scan_interval_seconds",
        "registration_request_ttl_seconds",
        "heartbeat_dead_after_seconds",
    ):
        if float(getattr(membership, field_name)) <= 0.0:
            raise ValueError(f"membership.{field_name} must be > 0")
    if scaling.enabled:
        if scaling.max_pending_launch_requests > scaling.max_total_launch_requests:
            raise ValueError("scaling max_pending_launch_requests must not exceed max_total")
        if scaling.max_pending_launch_requests < 0 or scaling.max_total_launch_requests < 0:
            raise ValueError("scaling launch request budgets must be non-negative")
        if scaling.launch_request_ttl_seconds < 2.0 * scaling.scheduler_reconcile_interval_seconds:
            raise ValueError("scaling launch_request_ttl_seconds must cover two reconciliations")
        if (
            scaling.scheduler_uncertainty_timeout_seconds
            < 3.0 * scaling.scheduler_reconcile_interval_seconds
        ):
            raise ValueError(
                "scaling scheduler uncertainty timeout must cover three reconciliations"
            )
        if scaling.low_contributor_threshold >= scaling.desired_contributors:
            raise ValueError("scaling low_contributor_threshold must be below desired_contributors")
        if scaling.low_contributor_threshold < 0 or scaling.desired_contributors < 1:
            raise ValueError("scaling contributor thresholds are invalid")
        if scaling.consecutive_low_windows < 2:
            raise ValueError("scaling.consecutive_low_windows must be >= 2")
        if (
            scaling.capacity_observation_retention_count
            < scaling.consecutive_low_windows + scaling.productive_window_count
        ):
            raise ValueError("scaling capacity observation retention is too small")
        if scaling.productive_window_count < 1:
            raise ValueError("scaling.productive_window_count must be >= 1")
        if (
            not scaling.learner_pbs_script
            or Path(scaling.learner_pbs_script).is_absolute()
            or any(part == ".." for part in Path(scaling.learner_pbs_script).parts)
        ):
            raise ValueError("scaling.learner_pbs_script must be a safe relative path")
        if scaling.productive_upload_grace_min_seconds <= 0.0:
            raise ValueError("scaling productive upload grace minimum must be > 0")
        if (
            scaling.productive_upload_grace_max_seconds
            < scaling.productive_upload_grace_min_seconds
        ):
            raise ValueError("scaling productive upload grace maximum must cover minimum")
        learner_walltime = scaling.learner_walltime
        if learner_walltime is None:
            raise ValueError("scaling.learner_walltime is required when scaling is enabled")
        parts = learner_walltime.split(":")
        if (
            len(parts) != 3
            or not all(part.isdigit() for part in parts)
            or len(parts[0]) < 2
            or len(parts[1]) != 2
            or len(parts[2]) != 2
            or int(parts[1]) >= 60
            or int(parts[2]) >= 60
            or all(int(part) == 0 for part in parts)
        ):
            raise ValueError("scaling.learner_walltime must be an estimated HH:MM:SS value")
        walltime_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if walltime_seconds < 600:
            raise ValueError("scaling.learner_walltime must be at least 00:10:00")
        if scaling.learner_queue is not None and (
            not scaling.learner_queue
            or any(
                not (character.isalnum() or character in "_.-")
                for character in scaling.learner_queue
            )
        ):
            raise ValueError("scaling.learner_queue contains unsafe PBS characters")
        for field_name in (
            "startup_grace_seconds",
            "productive_upload_grace_factor",
            "cooldown_seconds",
            "launch_request_ttl_seconds",
            "scheduler_reconcile_interval_seconds",
            "scheduler_uncertainty_timeout_seconds",
            "starvation_observation_seconds",
        ):
            if float(getattr(scaling, field_name)) <= 0.0:
                raise ValueError(f"scaling.{field_name} must be > 0")
    if terminal.admission_close_policy not in {
        "global_target_or_launch_budget",
        "global_target",
        "manual",
        "deadline",
    }:
        raise ValueError("unsupported terminal.admission_close_policy")
    if terminal.deadline_seconds is not None and terminal.deadline_seconds <= 0.0:
        raise ValueError("terminal.deadline_seconds must be > 0 when set")
    if terminal.admission_close_policy == "deadline" and terminal.deadline_seconds is None:
        raise ValueError("terminal.deadline_seconds is required for deadline close policy")
    has_global_target = config.sync.stop_after_outer_steps is not None
    if terminal.admission_close_policy == "global_target" and not has_global_target:
        raise ValueError("global_target close policy requires a configured global target")
    if (
        dynamic
        and terminal.admission_close_policy == "global_target_or_launch_budget"
        and not (has_global_target or (scaling.enabled and scaling.max_total_launch_requests > 0))
    ):
        raise ValueError(
            "global_target_or_launch_budget requires a global target or finite scale budget"
        )
    if terminal.max_terminal_merges < 0:
        raise ValueError("terminal.max_terminal_merges must be >= 0")
    for field_name in (
        "drain_ack_timeout_seconds",
        "registration_visibility_grace_seconds",
        "proposal_visibility_grace_seconds",
    ):
        if float(getattr(terminal, field_name)) <= 0.0:
            raise ValueError(f"terminal.{field_name} must be > 0")
    validate_global_adoption_config(config)
    if config.learner.post_publish_latest_wait_seconds < 0.0:
        raise ValueError("learner.post_publish_latest_wait_seconds must be >= 0")
    if config.learner.post_publish_latest_poll_seconds <= 0.0:
        raise ValueError("learner.post_publish_latest_poll_seconds must be > 0")
    if not 1 <= config.sync.quorum_min <= config.sync.quorum_max <= config.sync.num_learners:
        raise ValueError("sync quorum must satisfy 1 <= min <= max <= num_learners")
    for name, value in (
        ("training.inner_steps", config.training.inner_steps),
        ("training.micro_batch_size", config.training.micro_batch_size),
        (
            "training.gradient_accumulation_steps",
            config.training.gradient_accumulation_steps,
        ),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0")
    direct_token_target = config.sync.stop_after_direct_weight_tokens_applied
    if direct_token_target is not None and direct_token_target <= 0:
        raise ValueError("sync.stop_after_direct_weight_tokens_applied must be > 0")
    if (
        config.training.completion_mode == "global_only"
        and config.sync.stop_after_outer_steps is None
        and direct_token_target is None
    ):
        raise ValueError("global_only completion requires a global stop target")
    if config.sync.stop_after_outer_steps is not None and config.sync.stop_after_outer_steps <= 0:
        raise ValueError("sync.stop_after_outer_steps must be > 0 when set")
    outer = config.outer_optimizer
    if outer.name not in {"sgd", "momentum", "nesterov", "adamw"}:
        raise ValueError(f"unsupported outer_optimizer.name: {outer.name}")
    if outer.lr <= 0.0:
        raise ValueError("outer_optimizer.lr must be > 0")
    if not 0.0 <= outer.momentum < 1.0:
        raise ValueError("outer_optimizer.momentum must be in [0, 1)")
    if outer.weight_decay < 0.0:
        raise ValueError("outer_optimizer.weight_decay must be >= 0")
    if len(outer.betas) != 2 or not all(0.0 <= beta < 1.0 for beta in outer.betas):
        raise ValueError("outer_optimizer.betas must contain two values in [0, 1)")
    if outer.eps <= 0.0:
        raise ValueError("outer_optimizer.eps must be > 0")
    _validate_leader(config.leader)
    _validate_maintenance(config.maintenance, config.leader)
    _validate_input_revisions(config)
    return config


def _validate_leader(leader: LeaderSection) -> None:
    positive = (
        "lease_duration_seconds",
        "renew_interval_seconds",
        "candidate_acquire_poll_seconds",
        "candidate_wait_seconds",
        "learner_recovery_wait_seconds",
    )
    for name in positive:
        if float(getattr(leader, name)) <= 0.0:
            raise ValueError(f"leader.{name} must be > 0")
    if leader.max_clock_skew_seconds < 0.0:
        raise ValueError("leader.max_clock_skew_seconds must be >= 0")
    for name in ("business_busy_timeout_ms",):
        if getattr(leader, name) <= 0:
            raise ValueError(f"leader.{name} must be > 0")
    if leader.lease_duration_seconds < 5.0 * leader.renew_interval_seconds:
        raise ValueError("leader lease duration must be at least 5 * renew interval")
    if leader.lease_duration_seconds < 2.0 * leader.max_clock_skew_seconds:
        raise ValueError("leader lease must cover clock skew")
    if leader.candidate_acquire_poll_seconds > leader.renew_interval_seconds:
        raise ValueError("leader candidate polling must not exceed renew interval")
    if leader.candidate_wait_seconds < (
        leader.lease_duration_seconds + leader.max_clock_skew_seconds
    ):
        raise ValueError("leader candidate wait must cover lease duration and clock skew")
    if leader.learner_recovery_wait_seconds < leader.candidate_wait_seconds:
        raise ValueError("leader learner recovery wait must cover candidate wait")


def _validate_maintenance(maintenance: MaintenanceSection, leader: LeaderSection) -> None:
    for name in (
        "archive_batch_rows",
        "recent_batch_dedup_count",
        "quarantine_records_per_contributor",
    ):
        if getattr(maintenance, name) <= 0:
            raise ValueError(f"maintenance.{name} must be > 0")
    minimum = leader.lease_duration_seconds + 2.0 * leader.max_clock_skew_seconds
    if maintenance.publication_orphan_grace_seconds < minimum:
        raise ValueError(
            "maintenance.publication_orphan_grace_seconds must cover "
            "lease_duration_seconds + 2 * max_clock_skew_seconds"
        )


_IMMUTABLE_HUB_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SYNTHETIC_MODEL = "synthetic-tiny"


def _is_explicit_local_reference(value: str) -> bool:
    return value.startswith(("/", "./", "../", "file://"))


def _require_immutable_hub_revision(value: str | None, *, name: str) -> None:
    if not isinstance(value, str) or _IMMUTABLE_HUB_REVISION.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 40-character lowercase Hub commit SHA")


def _validate_input_revisions(config: Config) -> None:
    model_name = config.model.name_or_path
    if _is_explicit_local_reference(model_name):
        raise ValueError("local model input requires descriptor-bound content identity")
    if model_name != _SYNTHETIC_MODEL:
        _require_immutable_hub_revision(config.model.revision, name="model.revision")
        _require_immutable_hub_revision(
            config.model.tokenizer_revision or config.model.revision,
            name="model.tokenizer_revision",
        )
    dataset_name = config.data.dataset_name
    if _is_explicit_local_reference(dataset_name):
        raise ValueError("local data input requires descriptor-bound content identity")
    if dataset_name != "synthetic":
        _require_immutable_hub_revision(config.data.revision, name="data.revision")


def resolved_config_bytes(config: Config) -> bytes:
    """Return the canonical bytes used by the immutable run snapshot."""

    return yaml.safe_dump(config_to_dict(config), sort_keys=False, allow_unicode=True).encode(
        "utf-8"
    )


def write_resolved_config(config: Config, path: str | Path) -> None:
    from ..storage.atomic_io import atomic_write_text

    atomic_write_text(path, resolved_config_bytes(config).decode("utf-8"))
