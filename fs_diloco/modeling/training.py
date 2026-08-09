"""Optimizer/scheduler helpers shared by full and torch-baseline training."""

from __future__ import annotations

import math

import torch

from ..core.config import Config


def maybe_autocast(device: torch.device, precision: str) -> torch.autocast:
    """Return the configured training autocast context without a runtime dependency."""

    enabled = device.type == "cuda" and precision.lower() in {"bf16", "bfloat16"}
    return torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=enabled)


def build_inner_optimizer_and_scheduler(
    model: torch.nn.Module,
    config: Config,
    *,
    completed_local_steps: int,
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler | None]:
    name = config.inner_optimizer.name.lower()
    if name != "adamw":
        raise ValueError(f"unsupported inner optimizer: {config.inner_optimizer.name}")
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.inner_optimizer.lr,
        betas=tuple(config.inner_optimizer.betas),
        eps=config.inner_optimizer.eps,
        weight_decay=config.inner_optimizer.weight_decay,
    )
    if config.inner_optimizer.scheduler == "none":
        return optimizer, None
    completed_local_steps = int(completed_local_steps)
    if completed_local_steps < 0:
        raise ValueError("completed_local_steps must be >= 0")
    for group in optimizer.param_groups:
        group["initial_lr"] = config.inner_optimizer.lr
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: inner_lr_multiplier(config, step),
        # LambdaLR performs its initial step during construction. Starting from
        # N-1 therefore leaves it at the phase for N completed optimizer steps.
        last_epoch=completed_local_steps - 1,
    )
    return optimizer, scheduler


def inner_lr_multiplier(config: Config, completed_local_steps: int) -> float:
    """Return the LR multiplier for the next cumulative local optimizer step."""

    completed_local_steps = int(completed_local_steps)
    if completed_local_steps < 0:
        raise ValueError("completed_local_steps must be >= 0")
    if config.inner_optimizer.scheduler == "none":
        return 1.0
    if config.inner_optimizer.scheduler != "cosine":
        raise ValueError(
            f"unsupported inner optimizer scheduler: {config.inner_optimizer.scheduler}"
        )
    warmup_steps = int(config.inner_optimizer.warmup_steps)
    if warmup_steps > 0 and completed_local_steps < warmup_steps:
        return float(completed_local_steps + 1) / float(warmup_steps)
    total_steps = config.inner_optimizer.scheduler_total_steps
    if total_steps is None or int(total_steps) <= warmup_steps:
        raise ValueError(
            "cosine scheduler requires scheduler_total_steps greater than warmup_steps"
        )
    progress = min(
        1.0,
        max(
            0.0,
            float(completed_local_steps - warmup_steps) / float(int(total_steps) - warmup_steps),
        ),
    )
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return max(float(config.inner_optimizer.min_lr_ratio), cosine)


def current_inner_learning_rate(optimizer: torch.optim.Optimizer) -> float:
    """Return the first param-group LR used by the next optimizer step."""

    return float(optimizer.param_groups[0]["lr"])
