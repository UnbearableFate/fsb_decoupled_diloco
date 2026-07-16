"""Weights & Biases helpers for syncer-side training telemetry."""

from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import Config, config_to_dict


def _slug(value: object, *, max_length: int = 48) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        return "none"
    return text[:max_length].strip("-") or "none"


def _lr_slug(value: float) -> str:
    return f"{value:g}".replace(".", "p").replace("-", "m")


def syncer_wandb_project_name(_config: Config) -> str:
    return "fs-diloco-miyabi-syncer"


def syncer_wandb_run_name(config: Config, *, timestamp: str | None = None) -> str:
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    model = _slug(Path(config.model.name_or_path).name or config.model.name_or_path, max_length=24)
    dataset_parts = [config.data.dataset_name]
    if config.data.dataset_config_name:
        dataset_parts.append(config.data.dataset_config_name)
    dataset = _slug("-".join(dataset_parts), max_length=32)
    outer = _slug(config.outer_optimizer.name, max_length=16)
    return (
        f"{ts}_{_slug(config.run.name, max_length=24)}"
        f"_m-{model}_d-{dataset}"
        f"_L{config.sync.num_learners}_q{config.sync.quorum_min}-{config.sync.quorum_max}"
        f"_is{config.training.inner_steps}_mb{config.training.micro_batch_size}"
        f"_ga{config.training.gradient_accumulation_steps}"
        f"_outer-{outer}-lr{_lr_slug(config.outer_optimizer.lr)}"
    )


def syncer_wandb_tags(config: Config) -> list[str]:
    tags = [
        "syncer",
        f"model:{_slug(Path(config.model.name_or_path).name or config.model.name_or_path, max_length=32)}",
        f"data:{_slug(config.data.dataset_name, max_length=32)}",
        f"learners:{config.sync.num_learners}",
        f"outer:{_slug(config.outer_optimizer.name, max_length=24)}",
    ]
    tags.extend(str(tag) for tag in config.wandb.tags)
    return tags


def wandb_config(config: Config, *, device: str, hostname: str, shared_root: str) -> dict[str, Any]:
    payload = config_to_dict(config)
    payload["runtime"] = {
        "role": "syncer",
        "device": device,
        "hostname": hostname,
        "shared_root": shared_root,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
    payload["derived"] = {
        "global_batch_tokens_per_inner_step": (
            config.sync.num_learners
            * config.training.micro_batch_size
            * config.training.gradient_accumulation_steps
            * config.training.block_size
        ),
    }
    return payload


def selected_update_summary(selected: list[dict[str, Any]], *, current_version: int) -> dict[str, float]:
    summary: dict[str, float] = {}
    for key in ("train_loss", "param_norm", "grad_norm", "delta_norm"):
        values = []
        for row in selected:
            value = row.get(key)
            if value is None:
                continue
            numeric = float(value)
            if math.isfinite(numeric):
                values.append(numeric)
        if values:
            summary[f"selected/{key}_mean"] = sum(values) / len(values)
            summary[f"selected/{key}_min"] = min(values)
            summary[f"selected/{key}_max"] = max(values)
    if selected:
        stale = [max(0, current_version - int(row["base_global_version"])) for row in selected]
        summary["selected/staleness_mean"] = sum(stale) / len(stale)
        summary["selected/staleness_max"] = float(max(stale))
    return summary


def wandb_is_disabled(config: Config) -> bool:
    env_disabled = os.environ.get("WANDB_DISABLED", "").lower() in {"1", "true", "yes", "on"}
    return env_disabled or not config.wandb.enabled
