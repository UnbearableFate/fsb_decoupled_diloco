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
        config.fragments.materialize_full_every_events = int(
            materialize_full_every_events
        )
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
        raise ValueError(
            f"unsupported sync.grace_window.mode: {config.sync.grace_window.mode}"
        )
    for field_name in ("fixed_seconds", "initial_seconds", "max_seconds"):
        if float(getattr(config.sync.grace_window, field_name)) < 0.0:
            raise ValueError(f"sync.grace_window.{field_name} must be >= 0")
    if config.training.completion_mode not in {"local_or_global", "global_only"}:
        raise ValueError(
            f"unsupported training.completion_mode: {config.training.completion_mode}"
        )
    if config.training.completion_mode == "global_only" and (
        config.sync.stop_after_outer_steps is None
        and config.sync.stop_after_global_tokens is None
    ):
        raise ValueError(
            "training.completion_mode=global_only requires a configured global stop target"
        )
    if (
        config.liveness.syncer_unresponsive_timeout_seconds is not None
        and config.liveness.syncer_unresponsive_timeout_seconds <= 0.0
    ):
        raise ValueError(
            "liveness.syncer_unresponsive_timeout_seconds must be > 0 when set"
        )
    if (
        config.liveness.learner_shutdown_timeout_seconds is not None
        and config.liveness.learner_shutdown_timeout_seconds <= 0.0
    ):
        raise ValueError(
            "liveness.learner_shutdown_timeout_seconds must be > 0 when set"
        )
    config.inner_optimizer.scheduler = config.inner_optimizer.scheduler.lower()
    if config.inner_optimizer.scheduler not in {"none", "cosine"}:
        raise ValueError(
            "unsupported inner_optimizer.scheduler: "
            f"{config.inner_optimizer.scheduler}"
        )
    if config.inner_optimizer.warmup_steps < 0:
        raise ValueError("inner_optimizer.warmup_steps must be >= 0")
    if not 0.0 < float(config.inner_optimizer.min_lr_ratio) <= 1.0:
        raise ValueError("inner_optimizer.min_lr_ratio must be > 0 and <= 1")
    scheduler_total_steps = config.inner_optimizer.scheduler_total_steps
    if config.inner_optimizer.scheduler == "cosine":
        if scheduler_total_steps is None:
            raise ValueError(
                "inner_optimizer.scheduler_total_steps is required for cosine"
            )
        if int(scheduler_total_steps) <= config.inner_optimizer.warmup_steps:
            raise ValueError(
                "inner_optimizer.scheduler_total_steps must be greater than "
                "inner_optimizer.warmup_steps for cosine"
            )
    elif scheduler_total_steps is not None and int(scheduler_total_steps) <= 0:
        raise ValueError("inner_optimizer.scheduler_total_steps must be > 0 when set")
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
            raise ValueError(
                "fragments.materialize_full_every_events must be a positive integer"
            )
        if config.learner.global_adoption_strategy != "replace":
            raise ValueError(
                "learner.global_adoption_strategy is only supported by the full learner"
            )
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
