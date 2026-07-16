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


@dataclass
class RunSection:
    name: str = "fs_diloco_gpt2_wikitext2_8l"
    run_id: str | None = None
    shared_root: str | None = None
    log_level: str = "INFO"


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
    synthetic_num_batches: int = 128


@dataclass
class GraceWindowSection:
    mode: str = "fixed"
    fixed_seconds: float = 20.0
    max_seconds: float = 60.0


@dataclass
class SyncSection:
    num_learners: int = 8
    upload_mode: str = "params"
    quorum_min: int = 4
    quorum_max: int = 8
    max_staleness_versions: int = 2
    staleness_lambda: float = 0.25
    selection_policy: str = "most_recent_per_learner"
    scan_interval_seconds: float = 2.0
    grace_window: GraceWindowSection = field(default_factory=GraceWindowSection)
    stop_after_outer_steps: int | None = 20
    stop_after_global_tokens: int | None = None
    stop_file_poll_seconds: float = 5.0


@dataclass
class LivenessSection:
    heartbeat_interval_seconds: float = 30.0
    stale_after_seconds: float = 120.0
    dead_after_seconds: float = 300.0
    no_progress_timeout_seconds: float = 600.0
    quorum_policy: str = "fixed"


@dataclass
class TrainingSection:
    inner_steps: int = 100
    micro_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    block_size: int = 1024
    max_local_steps: int | None = None
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
    scheduler: str = "cosine"
    warmup_steps: int = 100
    reset_on_global_update: bool = True


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
class LearnerSection:
    poll_latest_during_inner_steps: bool = False
    adopt_global_after_upload: bool = True
    global_adoption_strategy: str = "replace"
    post_publish_latest_wait_seconds: float = 0.0
    post_publish_latest_poll_seconds: float = 0.2


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


def _from_dict(cls: type[T], data: dict[str, Any]) -> T:
    type_hints = get_type_hints(cls)
    field_names = {field_info.name for field_info in dataclasses.fields(cls)}
    unknown = sorted(set(data) - field_names)
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown config key(s) for {cls.__name__}: {joined}")
    kwargs: dict[str, Any] = {}
    for field_info in dataclasses.fields(cls):
        if field_info.name not in data:
            continue
        value = data[field_info.name]
        field_type = type_hints.get(field_info.name, field_info.type)
        if dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            kwargs[field_info.name] = _from_dict(field_type, value)
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


def _default_run_id(name: str) -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{name}"


def resolve_config(
    path: str | Path | None = None,
    *,
    run_id: str | None = None,
    shared_root: str | None = None,
    num_learners: int | None = None,
    project_root: str | Path | None = None,
) -> Config:
    config = load_config(path)
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
    if config.fragments.enabled:
        if config.fragments.num_fragments < 1:
            raise ValueError("fragments.num_fragments must be >= 1")
        if config.fragments.fragments_per_update != 1:
            raise ValueError("only fragments.fragments_per_update=1 is supported")
        if config.fragments.schedule != "round_robin_global":
            raise ValueError(f"unsupported fragments.schedule: {config.fragments.schedule}")
        if config.fragments.strategy not in {"full", "balanced_tensor"}:
            raise ValueError(f"unsupported fragments.strategy: {config.fragments.strategy}")
        if config.learner.global_adoption_strategy != "replace":
            raise ValueError(
                "learner.global_adoption_strategy is only supported by the full learner"
            )
    if config.learner.global_adoption_strategy not in {
        "replace",
        "rebase_post_publish_delta",
    }:
        raise ValueError(
            "unsupported learner.global_adoption_strategy: "
            f"{config.learner.global_adoption_strategy}"
        )
    if config.learner.global_adoption_strategy == "rebase_post_publish_delta" and (
        not config.learner.adopt_global_after_upload
        or not config.learner.poll_latest_during_inner_steps
    ):
        raise ValueError(
            "rebase_post_publish_delta requires adopt_global_after_upload=true and "
            "poll_latest_during_inner_steps=true"
        )
    if config.learner.post_publish_latest_wait_seconds < 0.0:
        raise ValueError("learner.post_publish_latest_wait_seconds must be >= 0")
    if config.learner.post_publish_latest_poll_seconds <= 0.0:
        raise ValueError("learner.post_publish_latest_poll_seconds must be > 0")
    config.training.block_size = config.data.block_size
    return config


def write_resolved_config(config: Config, path: str | Path) -> None:
    from ..storage.atomic_io import atomic_write_text

    atomic_write_text(path, yaml.safe_dump(config_to_dict(config), sort_keys=False))
