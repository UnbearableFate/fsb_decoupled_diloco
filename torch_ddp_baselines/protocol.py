"""Training and communication primitives for the standalone baselines."""

from __future__ import annotations

import contextlib
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import torch
import torch.distributed as dist

from .config import OptimizerConfig
from .data import Batch


@dataclass(frozen=True)
class TrainStepResult:
    """Report one completed accumulated optimizer step."""

    loss: float  # Mean unscaled microbatch loss.
    tokens: int  # Tokens consumed by this optimizer step.
    examples: int  # Fixed-length blocks consumed by this optimizer step.
    grad_norm: float  # Global gradient norm before clipping.
    gradient_sync_window_seconds: float  # Final backward window containing DDP sync.


def _gradient_norm(parameters: list[torch.nn.Parameter]) -> float:
    """Compute a finite CPU float64 norm when clipping is disabled."""

    squared = torch.zeros((), dtype=torch.float64)
    for parameter in parameters:
        if parameter.grad is None:
            continue
        gradient = parameter.grad.detach()
        squared += torch.sum(gradient.to(device="cpu", dtype=torch.float64).square())
    return float(torch.sqrt(squared).item())


def train_optimizer_step(
    model: torch.nn.Module,
    batch_iter: Any,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler,
    *,
    device: torch.device,
    accumulation_steps: int,
    grad_clip: float,
    ddp_gradient_sync: bool,
) -> TrainStepResult:
    """Run one optimizer step with exactly one DDP reducer synchronization."""

    if accumulation_steps < 1:
        raise ValueError("accumulation_steps must be >= 1")
    if ddp_gradient_sync and not hasattr(model, "no_sync"):
        raise TypeError("ddp_gradient_sync requires a model with no_sync()")

    optimizer.zero_grad(set_to_none=True)
    total_loss = 0.0
    total_tokens = 0
    total_examples = 0
    sync_window_seconds = 0.0
    for microbatch_index in range(accumulation_steps):
        final_microbatch = microbatch_index + 1 == accumulation_steps
        sync_context = (
            contextlib.nullcontext()
            if not ddp_gradient_sync or final_microbatch
            else model.no_sync()
        )
        batch: Batch = next(batch_iter).to(device)
        backward_started: float | None = None
        with sync_context:
            output = model(input_ids=batch.input_ids, labels=batch.labels)
            if output.loss is None:
                raise RuntimeError("model did not return a loss")
            unscaled_loss = output.loss
            if not torch.isfinite(unscaled_loss.detach()):
                raise FloatingPointError(
                    f"non-finite loss: {float(unscaled_loss.detach().cpu())}"
                )
            if ddp_gradient_sync and final_microbatch:
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                backward_started = time.monotonic()
            (unscaled_loss / accumulation_steps).backward()
        if backward_started is not None:
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            sync_window_seconds = time.monotonic() - backward_started
        total_loss += float(unscaled_loss.detach().cpu())
        total_tokens += batch.num_tokens
        total_examples += batch.num_examples

    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    grad_norm = float(
        torch.nn.utils.clip_grad_norm_(parameters, grad_clip).detach().cpu()
        if grad_clip is not None
        else _gradient_norm(parameters)
    )
    if not math.isfinite(grad_norm):
        raise FloatingPointError(f"non-finite gradient norm: {grad_norm}")
    optimizer.step()
    scheduler.step()
    return TrainStepResult(
        loss=total_loss / accumulation_steps,
        tokens=total_tokens,
        examples=total_examples,
        grad_norm=grad_norm,
        gradient_sync_window_seconds=sync_window_seconds,
    )


def broadcast_trainable_parameters(model: torch.nn.Module, *, source_rank: int = 0) -> None:
    """Give periodic-average ranks one common initial parameter vector."""

    for parameter in model.parameters():
        if parameter.requires_grad:
            dist.broadcast(parameter.detach(), src=source_rank)


def average_trainable_parameters(
    model: torch.nn.Module,
    *,
    world_size: int | None = None,
    all_reduce_fn: Callable[[torch.Tensor], None] | None = None,
) -> tuple[float, int]:
    """Average one flattened BF16 trainable-parameter vector across ranks."""

    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if not parameters:
        raise ValueError("model has no trainable parameters")
    actual_world_size = dist.get_world_size() if world_size is None else int(world_size)
    if actual_world_size < 1:
        raise ValueError("world_size must be >= 1")
    flattened = torch.cat(
        [parameter.detach().reshape(-1).to(dtype=torch.bfloat16) for parameter in parameters]
    )
    if not torch.isfinite(flattened).all():
        raise FloatingPointError("non-finite parameter before periodic average")

    def distributed_reduce(tensor: torch.Tensor) -> None:
        """Apply the production sum collective when no test reducer is supplied."""

        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)

    started = time.monotonic()
    (all_reduce_fn or distributed_reduce)(flattened)
    flattened.div_(actual_world_size)
    elapsed = time.monotonic() - started
    if not torch.isfinite(flattened).all():
        raise FloatingPointError("non-finite parameter after periodic average")
    offset = 0
    with torch.no_grad():
        for parameter in parameters:
            count = parameter.numel()
            parameter.copy_(flattened[offset : offset + count].view_as(parameter))
            offset += count
    return elapsed, int(flattened.numel())


def should_average(step: int, interval: int) -> bool:
    """Return whether an optimizer step closes a periodic averaging interval."""

    if step < 1 or interval < 1:
        raise ValueError("step and interval must be >= 1")
    return step % interval == 0


def learning_rate_multiplier(
    step: int,
    *,
    warmup_steps: int,
    total_steps: int,
    min_lr_ratio: float,
) -> float:
    """Return the linear-warmup then cosine multiplier for one zero-based step."""

    if warmup_steps > 0 and step < warmup_steps:
        return float(step + 1) / float(warmup_steps)
    progress = min(
        1.0,
        max(0.0, float(step - warmup_steps) / float(total_steps - warmup_steps)),
    )
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return max(min_lr_ratio, cosine)


def build_optimizer_and_scheduler(
    model: torch.nn.Module,
    config: OptimizerConfig,
    *,
    total_steps: int,
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]:
    """Construct the baseline's sole AdamW and cosine-schedule implementation."""

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.lr,
        betas=config.betas,
        eps=config.eps,
        weight_decay=config.weight_decay,
    )
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: learning_rate_multiplier(
            step,
            warmup_steps=config.warmup_steps,
            total_steps=total_steps,
            min_lr_ratio=config.min_lr_ratio,
        ),
    )
    return optimizer, scheduler
