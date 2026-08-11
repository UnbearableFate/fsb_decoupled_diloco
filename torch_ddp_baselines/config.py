"""Strict configuration for the standalone distributed baselines."""

from __future__ import annotations

import dataclasses
import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

import yaml


T = TypeVar("T")
IMMUTABLE_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class ModelConfig:
    """Identify the immutable Hugging Face model and its runtime dtype."""

    name_or_path: str  # Hugging Face model repository identifier.
    revision: str  # Immutable model commit SHA.
    tokenizer_revision: str  # Immutable tokenizer commit SHA.
    dtype: str  # Parameter dtype used during training.
    trust_remote_code: bool  # Whether Hugging Face may execute repository code.


@dataclass(frozen=True)
class DataConfig:
    """Identify the immutable text dataset and deterministic block pipeline."""

    dataset_name: str  # Hugging Face dataset repository identifier.
    dataset_config_name: str  # Named dataset subset.
    revision: str  # Immutable dataset commit SHA.
    train_split: str  # Dataset split used for training.
    block_size: int  # Token count in each non-overlapping training block.
    shuffle_blocks: bool  # Whether each rank shuffles its fixed shard per epoch.


@dataclass(frozen=True)
class TrainingConfig:
    """Define the optimizer-step workload shared by both baseline modes."""

    max_steps: int  # Number of optimizer steps performed by every rank.
    micro_batch_size: int  # Examples processed by one forward/backward pass.
    gradient_accumulation_steps: int  # Microbatches accumulated per optimizer step.
    seed: int  # Base seed for model and data-order determinism.
    grad_clip: float  # Maximum global gradient norm.
    log_every_steps: int  # Optimizer-step interval for JSONL progress events.


@dataclass(frozen=True)
class OptimizerConfig:
    """Define the single supported AdamW plus cosine schedule."""

    lr: float  # Peak AdamW learning rate.
    betas: tuple[float, float]  # AdamW exponential moving-average coefficients.
    eps: float  # AdamW numerical-stability constant.
    weight_decay: float  # Decoupled AdamW weight decay.
    warmup_steps: int  # Linear warmup length in optimizer steps.
    min_lr_ratio: float  # Minimum cosine learning-rate multiplier.


@dataclass(frozen=True)
class DistributedConfig:
    """Define the process-group topology and periodic averaging cadence."""

    world_size: int  # Exact number of distributed ranks.
    backend: str  # PyTorch process-group backend.
    require_distinct_hosts: bool  # Whether every rank must occupy a different host.
    periodic_average_interval: int  # Optimizer steps between parameter averages.


@dataclass(frozen=True)
class BaselineConfig:
    """Contain the complete standalone baseline experiment definition."""

    model: ModelConfig  # Model identity and dtype.
    data: DataConfig  # Dataset identity and batching rules.
    training: TrainingConfig  # Per-rank optimizer-step workload.
    optimizer: OptimizerConfig  # AdamW and cosine-schedule parameters.
    distributed: DistributedConfig  # Distributed topology and communication cadence.

    def validate(self) -> None:
        """Reject configurations outside the current baseline design."""

        for name, value in (
            ("model.name_or_path", self.model.name_or_path),
            ("data.dataset_name", self.data.dataset_name),
            ("data.dataset_config_name", self.data.dataset_config_name),
            ("data.train_split", self.data.train_split),
        ):
            if not value:
                raise ValueError(f"{name} must not be empty")
        for name, value in (
            ("model.revision", self.model.revision),
            ("model.tokenizer_revision", self.model.tokenizer_revision),
            ("data.revision", self.data.revision),
        ):
            if IMMUTABLE_REVISION.fullmatch(value) is None:
                raise ValueError(f"{name} must be a 40-character lowercase commit SHA")
        if self.model.dtype not in {"float32", "bfloat16"}:
            raise ValueError(f"unsupported model.dtype: {self.model.dtype}")
        for name, value in (
            ("data.block_size", self.data.block_size),
            ("training.max_steps", self.training.max_steps),
            ("training.micro_batch_size", self.training.micro_batch_size),
            (
                "training.gradient_accumulation_steps",
                self.training.gradient_accumulation_steps,
            ),
            ("training.log_every_steps", self.training.log_every_steps),
            ("distributed.world_size", self.distributed.world_size),
            (
                "distributed.periodic_average_interval",
                self.distributed.periodic_average_interval,
            ),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.training.max_steps % self.distributed.periodic_average_interval != 0:
            raise ValueError(
                "training.max_steps must end on a periodic averaging boundary"
            )
        if self.training.grad_clip <= 0.0:
            raise ValueError("training.grad_clip must be > 0")
        if self.optimizer.lr <= 0.0 or self.optimizer.eps <= 0.0:
            raise ValueError("optimizer.lr and optimizer.eps must be > 0")
        if self.optimizer.weight_decay < 0.0:
            raise ValueError("optimizer.weight_decay must be >= 0")
        if len(self.optimizer.betas) != 2 or not all(
            0.0 <= beta < 1.0 for beta in self.optimizer.betas
        ):
            raise ValueError("optimizer.betas must contain two values in [0, 1)")
        if not 0 <= self.optimizer.warmup_steps < self.training.max_steps:
            raise ValueError("optimizer.warmup_steps must be within the training run")
        if not 0.0 < self.optimizer.min_lr_ratio <= 1.0:
            raise ValueError("optimizer.min_lr_ratio must be in (0, 1]")
        if self.distributed.backend not in {"gloo", "nccl"}:
            raise ValueError(f"unsupported distributed.backend: {self.distributed.backend}")


def _coerce_value(value: Any, annotation: Any, path: str) -> Any:
    """Construct one strictly typed config value from parsed YAML."""

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if dataclasses.is_dataclass(annotation):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be a mapping")
        return _construct_dataclass(annotation, value, path=path)
    if origin is tuple:
        if not isinstance(value, list) or len(value) != len(arguments):
            raise ValueError(f"{path} must be a {len(arguments)}-item list")
        return tuple(
            _coerce_value(item, item_type, f"{path}[{index}]")
            for index, (item, item_type) in enumerate(zip(value, arguments, strict=True))
        )
    if origin in {types.UnionType, Union}:
        for option in arguments:
            try:
                return _coerce_value(value, option, path)
            except ValueError:
                continue
        raise ValueError(f"{path} has the wrong type")
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be a number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        return value
    raise TypeError(f"unsupported config annotation at {path}: {annotation!r}")


def _construct_dataclass(cls: type[T], payload: dict[str, Any], *, path: str = "") -> T:
    """Build one dataclass while rejecting missing and unknown fields."""

    fields = {field_info.name: field_info for field_info in dataclasses.fields(cls)}
    unknown = sorted(set(payload) - set(fields))
    missing = sorted(set(fields) - set(payload))
    if unknown:
        raise ValueError(f"unknown config keys under {path or '<root>'}: {unknown}")
    if missing:
        raise ValueError(f"missing config keys under {path or '<root>'}: {missing}")
    hints = get_type_hints(cls)
    return cls(
        **{
            name: _coerce_value(
                payload[name],
                hints[name],
                f"{path}.{name}" if path else name,
            )
            for name in fields
        }
    )


def load_config(path: str | Path) -> BaselineConfig:
    """Load and validate one standalone baseline YAML file."""

    with Path(path).open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"config {path} must contain a mapping")
    config = _construct_dataclass(BaselineConfig, payload)
    config.validate()
    return config


def config_to_dict(config: BaselineConfig) -> dict[str, Any]:
    """Return a YAML-serializable snapshot of the resolved config."""

    return dataclasses.asdict(config)

