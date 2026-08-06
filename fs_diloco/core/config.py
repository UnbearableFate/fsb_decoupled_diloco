"""YAML configuration loading and runtime resolution."""

from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

import yaml

from .constants import DEFAULT_RUNS_DIR

T = TypeVar("T")

REMOVED_CONFIG_KEYS: dict[str, str | None] = {
    "sync.upload_mode": None,
    "liveness.quorum_policy": None,
    "inner_optimizer.reset_on_global_update": None,
    "learner.prediction_reconcile_timeout_seconds": (
        "learner.prediction.reconcile_timeout_seconds"
    ),
}


@dataclass
class RunSection:
    name: str = "fs_diloco_gpt2_wikitext2_8l"
    run_id: str | None = None
    shared_root: str | None = None
    log_level: str = "INFO"
    git_commit: str | None = None
    git_dirty: bool | None = None
    source_fingerprint: str | None = None


@dataclass
class InitSection:
    resume: bool = False


@dataclass
class ModelSection:
    name_or_path: str = "gpt2"
    trust_remote_code: bool = False
    dtype: str = "bfloat16"
    compile: bool = False
    synthetic_vocab_size: int = 128
    synthetic_hidden_size: int = 32


@dataclass
class DataSection:
    dataset_name: str = "wikitext"
    dataset_config_name: str | None = "wikitext-2-raw-v1"
    train_split: str = "train"
    validation_split: str = "validation"
    block_size: int = 1024
    num_proc: int = 4
    cache_dir: str | None = None
    streaming: bool = False
    shuffle_blocks: bool = True
    synthetic_num_batches: int = 128


@dataclass
class GraceWindowSection:
    mode: str = "fixed"
    fixed_seconds: float = 20.0
    initial_seconds: float = 10.0
    max_seconds: float = 60.0


@dataclass
class SyncSection:
    num_learners: int = 8
    quorum_min: int = 4
    quorum_max: int = 8
    max_staleness_versions: int = 2
    staleness_lambda: float = 0.25
    selection_policy: str = "most_recent_per_learner"
    scan_interval_seconds: float = 2.0
    ingest_during_publish: bool = False
    capture_terminal_predecessor_for_eval: bool = False
    grace_window: GraceWindowSection = field(default_factory=GraceWindowSection)
    stop_after_outer_steps: int | None = 20
    stop_after_global_tokens: int | None = None
    stop_file_poll_seconds: float = 5.0


@dataclass
class SyncerSection:
    device: str = "auto"
    compute_dtype: str = "float32"
    publish_dtype: str = "float32"
    parallel_checkpoint_writes: bool = True


@dataclass
class SyncerHASection:
    enabled: bool = False
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


@dataclass
class RecoverySubmissionSection:
    enabled: bool = False
    claim_timeout_seconds: float = 120.0
    reconciliation_interval_seconds: float = 60.0
    uncertainty_timeout_seconds: float = 300.0
    backoff_initial_seconds: float = 60.0
    backoff_max_seconds: float = 900.0
    max_attempts_per_observation: int = 3
    max_outstanding_candidates: int = 1
    claim_retention_seconds: float = 3600.0
    candidate_pbs_script: str = "scripts/miyabi/run_syncer_candidate.pbs"
    candidate_walltime: str | None = None


@dataclass
class CoordinationSection:
    syncer_ha: SyncerHASection = field(default_factory=SyncerHASection)
    recovery_submission: RecoverySubmissionSection = field(
        default_factory=RecoverySubmissionSection
    )


@dataclass
class MembershipSection:
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
class ScalingSection:
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
    starvation_observation_seconds: float = 120.0
    learner_pbs_script: str = "scripts/miyabi/run_dynamic_learner.pbs"
    learner_walltime: str | None = None


@dataclass
class TerminalSection:
    admission_close_policy: str = "global_target_or_launch_budget"
    deadline_seconds: float | None = None
    drain_ack_timeout_seconds: float = 300.0
    registration_visibility_grace_seconds: float = 10.0
    proposal_visibility_grace_seconds: float = 20.0
    max_terminal_merges: int = 1
    allow_preclose_admission_during_drain: bool = False


@dataclass
class LivenessSection:
    heartbeat_interval_seconds: float = 30.0
    stale_after_seconds: float = 120.0
    dead_after_seconds: float = 300.0
    no_progress_timeout_seconds: float = 600.0
    syncer_unresponsive_timeout_seconds: float | None = None
    learner_shutdown_timeout_seconds: float | None = None


@dataclass
class TrainingSection:
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
class InnerOptimizerSection:
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
class OuterOptimizerSection:
    name: str = "nesterov"
    lr: float = 0.7
    momentum: float = 0.9
    weight_decay: float = 0.0
    betas: tuple[float, float] = (0.9, 0.999)
    eps: float = 1.0e-8


@dataclass
class IOSection:
    tensor_dtype: str = "float32"
    atomic_write: bool = True
    compute_sha256: bool = False
    checkpoint_digest_mode: str = "off"


@dataclass
class PredictionSection:
    reconcile_timeout_seconds: float = 60.0


@dataclass
class LearnerSection:
    poll_latest_during_inner_steps: bool = False
    adopt_global_after_upload: bool = True
    global_adoption_strategy: str = "replace"
    post_publish_latest_wait_seconds: float = 0.0
    post_publish_latest_poll_seconds: float = 0.2
    prediction: PredictionSection = field(default_factory=PredictionSection)


@dataclass
class FragmentSection:
    enabled: bool = False
    strategy: str = "full"
    num_fragments: int = 1
    schedule: str = "round_robin_global"
    fragments_per_update: int = 1
    reset_inner_optimizer_on_fragment_adopt: bool = True
    materialize_full_every_events: int | None = None


@dataclass
class FailureSimSection:
    enabled: bool = False
    sleep_jitter_seconds: float = 0.0
    upload_skip_probability: float = 0.0
    crash_probability: float = 0.0


@dataclass
class WandbSection:
    enabled: bool = True
    mode: str | None = "offline"
    entity: str | None = None
    group: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Config:
    run: RunSection = field(default_factory=RunSection)
    init: InitSection = field(default_factory=InitSection)
    model: ModelSection = field(default_factory=ModelSection)
    data: DataSection = field(default_factory=DataSection)
    sync: SyncSection = field(default_factory=SyncSection)
    syncer: SyncerSection = field(default_factory=SyncerSection)
    coordination: CoordinationSection = field(default_factory=CoordinationSection)
    membership: MembershipSection = field(default_factory=MembershipSection)
    scaling: ScalingSection = field(default_factory=ScalingSection)
    terminal: TerminalSection = field(default_factory=TerminalSection)
    liveness: LivenessSection = field(default_factory=LivenessSection)
    training: TrainingSection = field(default_factory=TrainingSection)
    inner_optimizer: InnerOptimizerSection = field(default_factory=InnerOptimizerSection)
    outer_optimizer: OuterOptimizerSection = field(default_factory=OuterOptimizerSection)
    io: IOSection = field(default_factory=IOSection)
    learner: LearnerSection = field(default_factory=LearnerSection)
    fragments: FragmentSection = field(default_factory=FragmentSection)
    failure_sim: FailureSimSection = field(default_factory=FailureSimSection)
    wandb: WandbSection = field(default_factory=WandbSection)


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
    data: dict[str, Any] = {}
    if path:
        with Path(path).open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config {path} must contain a mapping")
        data = loaded
    return _from_dict(Config, data)


def load_resolved_config_snapshot(path: str | Path) -> Config:
    """Load a historical resolved snapshot while migrating only known removed keys."""
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config {path} must contain a mapping")

    def pop_dotted(payload: dict[str, Any], dotted: str) -> tuple[bool, Any]:
        parts = dotted.split(".")
        current: Any = payload
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        if not isinstance(current, dict) or parts[-1] not in current:
            return False, None
        return True, current.pop(parts[-1])

    def set_dotted_if_missing(payload: dict[str, Any], dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        current = payload
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current.setdefault(parts[-1], value)

    for removed, replacement in REMOVED_CONFIG_KEYS.items():
        present, value = pop_dotted(loaded, removed)
        if present and replacement is not None:
            set_dotted_if_missing(loaded, replacement, value)
    return _from_dict(Config, loaded)


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
    capture_terminal_predecessor_for_eval: bool | None = None,
    completion_mode: str | None = None,
    parallel_checkpoint_writes: bool | None = None,
    materialize_full_every_events: int | None = None,
    project_root: str | Path | None = None,
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
    if capture_terminal_predecessor_for_eval is not None:
        config.sync.capture_terminal_predecessor_for_eval = bool(
            capture_terminal_predecessor_for_eval
        )
    if completion_mode is not None:
        config.training.completion_mode = str(completion_mode)
    if parallel_checkpoint_writes is not None:
        config.syncer.parallel_checkpoint_writes = bool(parallel_checkpoint_writes)
    if materialize_full_every_events is not None:
        config.fragments.materialize_full_every_events = int(materialize_full_every_events)
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
    if config.training.completion_mode == "global_only" and (
        config.sync.stop_after_outer_steps is None and config.sync.stop_after_global_tokens is None
    ):
        raise ValueError(
            "training.completion_mode=global_only requires a configured global stop target"
        )
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
    ha = config.coordination.syncer_ha
    recovery = config.coordination.recovery_submission
    membership = config.membership
    scaling = config.scaling
    terminal = config.terminal
    membership.mode = membership.mode.lower()
    if membership.mode not in {"static", "dynamic"}:
        raise ValueError("membership.mode must be one of: static, dynamic")
    dynamic = membership.mode == "dynamic"
    if dynamic and (not ha.enabled or config.fragments.enabled):
        raise ValueError("dynamic membership requires full mode with syncer HA enabled")
    if scaling.enabled and not dynamic:
        raise ValueError("scaling.enabled requires membership.mode=dynamic")
    if ha.enabled and config.fragments.enabled:
        raise ValueError("coordination.syncer_ha is not supported with fragments")
    if recovery.enabled and not ha.enabled:
        raise ValueError("coordination.recovery_submission requires coordination.syncer_ha.enabled")
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
        raise ValueError(
            "membership.max_active_instance_records must be >= stream_pool_size"
        )
    if membership.heartbeat_dead_after_seconds <= membership.heartbeat_stale_after_seconds:
        raise ValueError("membership heartbeat_dead_after_seconds must exceed stale timeout")
    if membership.expired_retention_seconds < membership.revocation_grace_seconds:
        raise ValueError("membership expired_retention_seconds must cover revocation_grace_seconds")
    if (
        membership.initial_membership_deadline_seconds
        < membership.registration_request_ttl_seconds
    ):
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
    if (
        scaling.launch_request_ttl_seconds
        < 2.0 * scaling.scheduler_reconcile_interval_seconds
    ):
        raise ValueError("scaling launch_request_ttl_seconds must cover two reconciliations")
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
    if (
        scaling.productive_upload_grace_max_seconds
        < scaling.productive_upload_grace_min_seconds
    ):
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
    has_global_target = (
        config.sync.stop_after_outer_steps is not None
        or config.sync.stop_after_global_tokens is not None
    )
    if (
        dynamic
        and terminal.admission_close_policy == "global_target"
        and not has_global_target
    ):
        raise ValueError("global_target close policy requires a configured global target")
    if (
        dynamic
        and terminal.admission_close_policy == "global_target_or_launch_budget"
        and not (
            has_global_target
            or (scaling.enabled and scaling.max_total_launch_requests > 0)
        )
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
    if config.fragments.enabled:
        if config.fragments.num_fragments < 1:
            raise ValueError("fragments.num_fragments must be >= 1")
        if config.fragments.fragments_per_update != 1:
            raise ValueError("only fragments.fragments_per_update=1 is supported")
        if config.fragments.schedule != "round_robin_global":
            raise ValueError(f"unsupported fragments.schedule: {config.fragments.schedule}")
        if config.fragments.strategy not in {"full", "balanced_tensor"}:
            raise ValueError(f"unsupported fragments.strategy: {config.fragments.strategy}")
        if (
            config.fragments.materialize_full_every_events is None
            or int(config.fragments.materialize_full_every_events) <= 0
        ):
            raise ValueError("fragments.materialize_full_every_events must be a positive integer")
        if config.learner.global_adoption_strategy != "replace":
            raise ValueError(
                "learner.global_adoption_strategy is only supported by the full learner"
            )
    if ha.renew_interval_seconds <= 0.0:
        raise ValueError("coordination.syncer_ha.renew_interval_seconds must be > 0")
    if ha.lease_duration_seconds < 5.0 * ha.renew_interval_seconds:
        raise ValueError(
            "coordination.syncer_ha.lease_duration_seconds must be at least "
            "5 * renew_interval_seconds"
        )
    if ha.max_clock_skew_seconds < 0.0:
        raise ValueError("coordination.syncer_ha.max_clock_skew_seconds must be >= 0")
    if not 0.0 < ha.heartbeat_interval_seconds <= ha.renew_interval_seconds:
        raise ValueError(
            "coordination.syncer_ha.heartbeat_interval_seconds must be > 0 and "
            "<= renew_interval_seconds"
        )
    if ha.heartbeat_stale_after_seconds < 3.0 * ha.heartbeat_interval_seconds:
        raise ValueError(
            "coordination.syncer_ha.heartbeat_stale_after_seconds must be at least "
            "3 * heartbeat_interval_seconds"
        )
    if ha.lease_duration_seconds < (
        ha.heartbeat_stale_after_seconds + 2.0 * ha.max_clock_skew_seconds
    ):
        raise ValueError(
            "coordination.syncer_ha.lease_duration_seconds must cover "
            "heartbeat_stale_after_seconds + 2 * max_clock_skew_seconds"
        )
    if not 0 < ha.lease_busy_timeout_ms <= ha.renew_interval_seconds * 1000.0:
        raise ValueError(
            "coordination.syncer_ha.lease_busy_timeout_ms must be > 0 and <= "
            "renew_interval_seconds * 1000"
        )
    if ha.business_busy_timeout_ms <= 0:
        raise ValueError("coordination.syncer_ha.business_busy_timeout_ms must be > 0")
    if not 0.0 < ha.candidate_acquire_poll_seconds <= ha.renew_interval_seconds:
        raise ValueError(
            "coordination.syncer_ha.candidate_acquire_poll_seconds must be > 0 "
            "and <= renew_interval_seconds"
        )
    if ha.candidate_wait_seconds < (ha.lease_duration_seconds + ha.max_clock_skew_seconds):
        raise ValueError(
            "coordination.syncer_ha.candidate_wait_seconds must cover "
            "lease_duration_seconds + max_clock_skew_seconds"
        )
    if ha.learner_recovery_wait_seconds < ha.candidate_wait_seconds:
        raise ValueError(
            "coordination.syncer_ha.learner_recovery_wait_seconds must be >= candidate_wait_seconds"
        )
    if ha.canonical_repair_wait_seconds < 2.0 * ha.heartbeat_interval_seconds:
        raise ValueError(
            "coordination.syncer_ha.canonical_repair_wait_seconds must be at "
            "least 2 * heartbeat_interval_seconds"
        )
    if ha.max_retained_epoch_dirs < 1:
        raise ValueError("coordination.syncer_ha.max_retained_epoch_dirs must be >= 1")
    for field_name in (
        "claim_timeout_seconds",
        "reconciliation_interval_seconds",
        "uncertainty_timeout_seconds",
        "backoff_initial_seconds",
        "backoff_max_seconds",
        "claim_retention_seconds",
    ):
        if float(getattr(recovery, field_name)) <= 0.0:
            raise ValueError(f"coordination.recovery_submission.{field_name} must be > 0")
    if recovery.backoff_max_seconds < recovery.backoff_initial_seconds:
        raise ValueError(
            "coordination.recovery_submission.backoff_max_seconds must be >= "
            "backoff_initial_seconds"
        )
    if recovery.max_attempts_per_observation < 1:
        raise ValueError(
            "coordination.recovery_submission.max_attempts_per_observation must be >= 1"
        )
    if recovery.max_outstanding_candidates < 1:
        raise ValueError("coordination.recovery_submission.max_outstanding_candidates must be >= 1")
    if recovery.enabled:
        candidate_walltime = recovery.candidate_walltime
        parts = [] if candidate_walltime is None else candidate_walltime.split(":")
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
                "coordination.recovery_submission.candidate_walltime must be an "
                "explicit estimated HH:MM:SS value when recovery submission is enabled"
            )
    if config.io.checkpoint_digest_mode is False:
        # PyYAML 1.1 treats an unquoted ``off`` scalar as boolean false.
        config.io.checkpoint_digest_mode = "off"
    elif not isinstance(config.io.checkpoint_digest_mode, str):
        raise ValueError("io.checkpoint_digest_mode must be a string")
    config.io.checkpoint_digest_mode = config.io.checkpoint_digest_mode.lower()
    if config.io.checkpoint_digest_mode not in {"off", "checker", "always"}:
        raise ValueError("io.checkpoint_digest_mode must be one of: off, checker, always")
    from ..runtime.adoption import validate_global_adoption_strategy

    validate_global_adoption_strategy(config)
    if config.learner.post_publish_latest_wait_seconds < 0.0:
        raise ValueError("learner.post_publish_latest_wait_seconds must be >= 0")
    if config.learner.post_publish_latest_poll_seconds <= 0.0:
        raise ValueError("learner.post_publish_latest_poll_seconds must be > 0")
    config.training.block_size = config.data.block_size
    return config


def write_resolved_config(config: Config, path: str | Path) -> None:
    from ..storage.atomic_io import atomic_write_text

    atomic_write_text(path, yaml.safe_dump(config_to_dict(config), sort_keys=False))
