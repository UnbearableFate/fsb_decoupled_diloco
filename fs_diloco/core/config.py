"""YAML configuration loading and runtime resolution."""

from __future__ import annotations

import dataclasses
import math
import os
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

import yaml

from .constants import DEFAULT_RUNS_DIR
from .versions import CONFIG_SCHEMA_VERSION

T = TypeVar("T")

REMOVED_CONFIG_KEYS: dict[str, str | None] = {
    "init": None,
    "fragments": None,
    "failure_sim": None,
    "coordination": None,
    "sync.stop_after_global_tokens": "sync.stop_after_direct_weight_tokens_applied",
    "sync.capture_terminal_predecessor_for_eval": None,
    "sync.upload_mode": None,
    "liveness.quorum_policy": None,
    "inner_optimizer.reset_on_global_update": None,
    "learner.prediction_reconcile_timeout_seconds": (
        "learner.prediction.reconcile_timeout_seconds"
    ),
}


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
    if path == "io.checkpoint_digest_mode" and value is False:
        # PyYAML 1.1 decodes an unquoted ``off`` as false.  The resolver
        # normalizes this one historical spelling before semantic validation.
        return
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
    name: str = "fs_diloco_gpt2_wikitext2_8l"
    run_id: str | None = None
    shared_root: str | None = None
    log_level: str = "INFO"
    git_commit: str | None = None
    git_dirty: bool | None = None
    source_fingerprint: str | None = None


@dataclass
class ModelSection(ConfigSection):
    name_or_path: str = "gpt2"
    revision: str | None = None
    tokenizer_revision: str | None = None
    trust_remote_code: bool = False
    dtype: str = "bfloat16"
    compile: bool = False
    synthetic_vocab_size: int = 128
    synthetic_hidden_size: int = 32


@dataclass
class DataSection(ConfigSection):
    dataset_name: str = "wikitext"
    dataset_config_name: str | None = "wikitext-2-raw-v1"
    revision: str | None = None
    train_split: str = "train"
    validation_split: str = "validation"
    block_size: int = 1024
    num_proc: int = 4
    cache_dir: str | None = None
    streaming: bool = False
    shuffle_blocks: bool = True
    synthetic_num_batches: int = 128


@dataclass
class GraceWindowSection(ConfigSection):
    mode: str = "fixed"
    fixed_seconds: float = 20.0
    initial_seconds: float = 10.0
    max_seconds: float = 60.0


@dataclass
class SyncSection(ConfigSection):
    num_learners: int = 8
    quorum_min: int = 4
    quorum_max: int = 8
    max_staleness_versions: int = 2
    staleness_lambda: float = 0.25
    selection_policy: str = "most_recent_per_learner"
    scan_interval_seconds: float = 2.0
    ingest_during_publish: bool = False
    grace_window: GraceWindowSection = field(default_factory=GraceWindowSection)
    stop_after_outer_steps: int | None = 20
    stop_file_poll_seconds: float = 5.0


@dataclass
class SyncerSection(ConfigSection):
    device: str = "auto"
    compute_dtype: str = "float32"
    publish_dtype: str = "float32"
    parallel_checkpoint_writes: bool = True


@dataclass
class MembershipSection(ConfigSection):
    mode: str = "static"
    stream_pool_size: int = 8
    bootstrap_instances: int = 8
    initial_membership_deadline_seconds: float = 1800.0
    registration_scan_interval_seconds: float = 2.0
    registration_request_ttl_seconds: float = 120.0
    heartbeat_stale_after_seconds: float = 120.0
    heartbeat_dead_after_seconds: float = 300.0
    revocation_grace_seconds: float = 60.0
    expired_retention_seconds: float = 600.0
    max_active_instance_records: int = 16
    allow_unsolicited_registration: bool = False
    allow_healthy_placement_replacement: bool = False
    reuse_stream_for_same_placement: bool = True


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
    learner_pbs_script: str = "scripts/miyabi/run_dynamic_learner.pbs"
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
class LivenessSection(ConfigSection):
    heartbeat_interval_seconds: float = 30.0
    stale_after_seconds: float = 120.0
    dead_after_seconds: float = 300.0
    no_progress_timeout_seconds: float = 600.0
    syncer_unresponsive_timeout_seconds: float | None = None
    learner_shutdown_timeout_seconds: float | None = None


@dataclass
class TrainingSection(ConfigSection):
    inner_steps: int = 100
    micro_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    block_size: int = 1024
    max_local_steps: int | None = None
    completion_mode: str = "local_or_global"
    precision: str = "bf16"
    seed: int = 1337
    log_every_steps: int = 10
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
    atomic_write: bool = True
    compute_sha256: bool = False
    checkpoint_digest_mode: str = "off"


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
class WandbSection(ConfigSection):
    enabled: bool = True
    mode: str | None = "offline"
    entity: str | None = None
    group: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class TorchBaselineSection(ConfigSection):
    enabled: bool = False
    backend: str = "nccl"
    require_distinct_hosts: bool = True


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
    liveness: LivenessSection = field(default_factory=LivenessSection)
    training: TrainingSection = field(default_factory=TrainingSection)
    inner_optimizer: InnerOptimizerSection = field(default_factory=InnerOptimizerSection)
    outer_optimizer: OuterOptimizerSection = field(default_factory=OuterOptimizerSection)
    io: IOSection = field(default_factory=IOSection)
    learner: LearnerSection = field(default_factory=LearnerSection)
    wandb: WandbSection = field(default_factory=WandbSection)
    torch_baseline: TorchBaselineSection = field(default_factory=TorchBaselineSection)

    def validate(self, *, profile: str = "full_v4_shared", path: str | None = None) -> None:
        if profile not in {"full_v4_shared", "torch_baseline"}:
            raise ValueError(f"unknown config validation profile: {profile}")
        for field_info in dataclasses.fields(self):
            section = getattr(self, field_info.name)
            if isinstance(section, ConfigSection):
                section.validate(path=field_info.name)
        if self.config_schema_version != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"config_schema_version must be exactly {CONFIG_SCHEMA_VERSION}")
        if profile == "torch_baseline" and not self.torch_baseline.enabled:
            raise ValueError("torch_baseline profile requires torch_baseline.enabled=true")
        if profile != "torch_baseline" and self.torch_baseline.enabled:
            raise ValueError(f"{profile} profile cannot validate a torch baseline config")


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
        messages: list[str] = []
        for key in unknown:
            dotted = ".".join((*path, key))
            replacement = REMOVED_CONFIG_KEYS.get(dotted)
            if dotted in REMOVED_CONFIG_KEYS:
                message = f"config key {dotted} 字段已移除"
                if replacement is not None:
                    message += f"; use {replacement} instead"
                else:
                    message += "; it has no replacement"
                messages.append(message)
            else:
                messages.append(f"unknown config key: {dotted}")
        raise ValueError("; ".join(messages))
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
    return dataclasses.asdict(config)


def load_config(path: str | Path | None = None) -> Config:
    """Load shared v4/baseline settings without exposing removed runtime modes.

    Repository full-runtime configs are parsed through the complete v4 envelope.
    Partial mappings remain useful for shared modeling tests and baseline tooling,
    but the ``Config`` schema itself has no classic, fragment, or legacy-HA fields.
    """

    data: dict[str, Any] = {}
    if path:
        with Path(path).open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config {path} must contain a mapping")
        data = loaded
        coordination = data.get("coordination")
        if "maintenance" in data or (isinstance(coordination, dict) and "leader" in coordination):
            from .config_v4 import ConfigProfile, load_config_v4

            loaded_v4 = load_config_v4(path, profile=ConfigProfile.FULL_V4)
            return loaded_v4.shared
    config = _from_dict(Config, data)
    config.validate(profile="torch_baseline" if config.torch_baseline.enabled else "full_v4_shared")
    return config


def load_resolved_config_snapshot(path: str | Path) -> Config:
    """Load a current strict snapshot; old run roots use ``legacy.reader`` instead."""

    return load_config(path)


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


def _validate_global_adoption_config(config: Config) -> None:
    strategy = config.learner.global_adoption_strategy
    if strategy == "replace":
        return
    if strategy not in {"rebase_post_publish_delta", "predict_post_publish_global"}:
        raise ValueError(f"unsupported learner.global_adoption_strategy: {strategy}")
    if (
        not config.learner.adopt_global_after_upload
        or not config.learner.poll_latest_during_inner_steps
    ):
        raise ValueError(
            f"{strategy} requires adopt_global_after_upload=true and "
            "poll_latest_during_inner_steps=true"
        )
    if strategy == "predict_post_publish_global":
        if config.outer_optimizer.name.lower() != "nesterov":
            raise ValueError("predict_post_publish_global currently requires outer nesterov")
        if config.outer_optimizer.weight_decay != 0.0:
            raise ValueError("predict_post_publish_global currently requires outer weight_decay=0")
        if config.learner.prediction.reconcile_timeout_seconds <= 0.0:
            raise ValueError("learner.prediction.reconcile_timeout_seconds must be > 0")


def resolve_config(
    path: str | Path | None = None,
    *,
    run_id: str | None = None,
    shared_root: str | None = None,
    num_learners: int | None = None,
    training_seed: int | None = None,
    scan_interval_seconds: float | None = None,
    ingest_during_publish: bool | None = None,
    syncer_device: str | None = None,
    syncer_publish_dtype: str | None = None,
    staleness_lambda: float | None = None,
    max_staleness_versions: int | None = None,
    global_adoption_strategy: str | None = None,
    completion_mode: str | None = None,
    parallel_checkpoint_writes: bool | None = None,
    project_root: str | Path | None = None,
    profile: str | None = None,
) -> Config:
    config = load_config(path)
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
    if num_learners is not None:
        config.sync.num_learners = int(num_learners)
        config.sync.quorum_max = min(config.sync.quorum_max, config.sync.num_learners)
        config.sync.quorum_min = min(config.sync.quorum_min, config.sync.num_learners)
    if training_seed is not None:
        config.training.seed = int(training_seed)
    if scan_interval_seconds is not None:
        config.sync.scan_interval_seconds = float(scan_interval_seconds)
    if ingest_during_publish is not None:
        config.sync.ingest_during_publish = bool(ingest_during_publish)
    if syncer_device is not None:
        config.syncer.device = str(syncer_device)
    if syncer_publish_dtype is not None:
        config.syncer.publish_dtype = str(syncer_publish_dtype)
    if staleness_lambda is not None:
        config.sync.staleness_lambda = float(staleness_lambda)
    if max_staleness_versions is not None:
        config.sync.max_staleness_versions = int(max_staleness_versions)
    if global_adoption_strategy is not None:
        config.learner.global_adoption_strategy = str(global_adoption_strategy)
    if completion_mode is not None:
        config.training.completion_mode = str(completion_mode)
    if parallel_checkpoint_writes is not None:
        config.syncer.parallel_checkpoint_writes = bool(parallel_checkpoint_writes)
    if config.sync.scan_interval_seconds <= 0.0:
        raise ValueError("sync.scan_interval_seconds must be > 0")
    if config.sync.staleness_lambda < 0.0:
        raise ValueError("sync.staleness_lambda must be >= 0")
    if config.sync.max_staleness_versions < 0:
        raise ValueError("sync.max_staleness_versions must be >= 0")
    config.syncer.device = config.syncer.device.lower()
    if config.syncer.device not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unsupported syncer.device: {config.syncer.device}")
    dtype_aliases = {
        "float32": "float32",
        "fp32": "float32",
        "float": "float32",
        "bfloat16": "bfloat16",
        "bf16": "bfloat16",
    }
    for field_name in ("compute_dtype", "publish_dtype"):
        configured = getattr(config.syncer, field_name).lower().replace("torch.", "")
        if configured not in dtype_aliases:
            raise ValueError(f"unsupported syncer.{field_name}: {configured}")
        setattr(config.syncer, field_name, dtype_aliases[configured])
    if config.sync.grace_window.mode not in {"fixed", "adaptive_fastest_upload_eta"}:
        raise ValueError(f"unsupported sync.grace_window.mode: {config.sync.grace_window.mode}")
    for field_name in ("fixed_seconds", "initial_seconds", "max_seconds"):
        if float(getattr(config.sync.grace_window, field_name)) < 0.0:
            raise ValueError(f"sync.grace_window.{field_name} must be >= 0")
    if config.training.completion_mode not in {"local_or_global", "global_only"}:
        raise ValueError(f"unsupported training.completion_mode: {config.training.completion_mode}")
    if (
        config.liveness.syncer_unresponsive_timeout_seconds is not None
        and config.liveness.syncer_unresponsive_timeout_seconds <= 0.0
    ):
        raise ValueError("liveness.syncer_unresponsive_timeout_seconds must be > 0 when set")
    if (
        config.liveness.learner_shutdown_timeout_seconds is not None
        and config.liveness.learner_shutdown_timeout_seconds <= 0.0
    ):
        raise ValueError("liveness.learner_shutdown_timeout_seconds must be > 0 when set")
    config.inner_optimizer.scheduler = config.inner_optimizer.scheduler.lower()
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
    membership.mode = membership.mode.lower()
    if membership.mode not in {"static", "dynamic"}:
        raise ValueError("membership.mode must be one of: static, dynamic")
    dynamic = membership.mode == "dynamic"
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
    if membership.max_active_instance_records < membership.stream_pool_size:
        raise ValueError("membership.max_active_instance_records must be >= stream_pool_size")
    if membership.heartbeat_dead_after_seconds <= membership.heartbeat_stale_after_seconds:
        raise ValueError("membership heartbeat_dead_after_seconds must exceed stale timeout")
    if membership.expired_retention_seconds < membership.revocation_grace_seconds:
        raise ValueError("membership expired_retention_seconds must cover revocation_grace_seconds")
    if membership.initial_membership_deadline_seconds < membership.registration_request_ttl_seconds:
        raise ValueError(
            "membership initial_membership_deadline_seconds must cover registration request TTL"
        )
    for field_name in (
        "initial_membership_deadline_seconds",
        "registration_scan_interval_seconds",
        "registration_request_ttl_seconds",
        "heartbeat_stale_after_seconds",
        "heartbeat_dead_after_seconds",
        "revocation_grace_seconds",
        "expired_retention_seconds",
    ):
        if float(getattr(membership, field_name)) <= 0.0:
            raise ValueError(f"membership.{field_name} must be > 0")
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
        raise ValueError("scaling scheduler uncertainty timeout must cover three reconciliations")
    if scaling.low_contributor_threshold >= scaling.desired_contributors:
        raise ValueError("scaling low_contributor_threshold must be below desired_contributors")
    if scaling.consecutive_low_windows < 2:
        raise ValueError("scaling.consecutive_low_windows must be >= 2")
    if (
        scaling.capacity_observation_retention_count
        < scaling.consecutive_low_windows + scaling.productive_window_count
    ):
        raise ValueError("scaling capacity observation retention is too small")
    if scaling.productive_window_count < 1:
        raise ValueError("scaling.productive_window_count must be >= 1")
    if scaling.productive_upload_grace_min_seconds <= 0.0:
        raise ValueError("scaling productive upload grace minimum must be > 0")
    if scaling.productive_upload_grace_max_seconds < scaling.productive_upload_grace_min_seconds:
        raise ValueError("scaling productive upload grace maximum must cover minimum")
    if scaling.enabled:
        learner_walltime = scaling.learner_walltime
        parts = [] if learner_walltime is None else learner_walltime.split(":")
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
            raise ValueError(
                "scaling.learner_walltime must be an explicit estimated HH:MM:SS "
                "value when scaling is enabled"
            )
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
    if (
        dynamic
        and terminal.admission_close_policy == "deadline"
        and terminal.deadline_seconds is None
    ):
        raise ValueError("terminal.deadline_seconds is required for deadline close policy")
    has_global_target = config.sync.stop_after_outer_steps is not None
    if dynamic and terminal.admission_close_policy == "global_target" and not has_global_target:
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
    if config.io.checkpoint_digest_mode is False:
        # PyYAML 1.1 treats an unquoted ``off`` scalar as boolean false.
        config.io.checkpoint_digest_mode = "off"
    elif not isinstance(config.io.checkpoint_digest_mode, str):
        raise ValueError("io.checkpoint_digest_mode must be a string")
    config.io.checkpoint_digest_mode = config.io.checkpoint_digest_mode.lower()
    if config.io.checkpoint_digest_mode not in {"off", "checker", "always"}:
        raise ValueError("io.checkpoint_digest_mode must be one of: off, checker, always")
    _validate_global_adoption_config(config)
    if config.learner.post_publish_latest_wait_seconds < 0.0:
        raise ValueError("learner.post_publish_latest_wait_seconds must be >= 0")
    if config.learner.post_publish_latest_poll_seconds <= 0.0:
        raise ValueError("learner.post_publish_latest_poll_seconds must be > 0")
    config.torch_baseline.backend = config.torch_baseline.backend.lower()
    if config.torch_baseline.backend not in {"gloo", "nccl"}:
        raise ValueError(f"unsupported torch_baseline.backend: {config.torch_baseline.backend}")
    if config.torch_baseline.enabled:
        if config.sync.num_learners < 1:
            raise ValueError("torch baseline requires sync.num_learners >= 1")
        if config.training.max_local_steps is None:
            raise ValueError("torch baseline requires training.max_local_steps to be configured")
        if int(config.training.max_local_steps) <= 0:
            raise ValueError("torch baseline training.max_local_steps must be > 0")
        if int(config.training.inner_steps) <= 0:
            raise ValueError("torch baseline training.inner_steps must be > 0")
    config.training.block_size = config.data.block_size
    selected_profile = profile or (
        "torch_baseline" if config.torch_baseline.enabled else "full_v4_shared"
    )
    config.validate(profile=selected_profile)
    return config


def resolved_config_bytes(config: Config) -> bytes:
    """Return the canonical bytes used by the immutable run snapshot."""

    return yaml.safe_dump(config_to_dict(config), sort_keys=False).encode("utf-8")


def write_resolved_config(config: Config, path: str | Path) -> None:
    from ..storage.atomic_io import atomic_write_text

    atomic_write_text(path, resolved_config_bytes(config).decode("utf-8"))
