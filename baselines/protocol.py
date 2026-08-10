"""Training and communication primitives shared by torch baselines."""

from __future__ import annotations

import contextlib
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import torch
import torch.distributed as dist

from ..fs_diloco.modeling.hf_data import Batch
from ..fs_diloco.modeling.training import maybe_autocast


@dataclass(frozen=True)
class TrainStepResult:
    loss: float
    tokens: int
    examples: int
    grad_norm: float
    gradient_sync_window_seconds: float


def _gradient_norm(parameters: list[torch.nn.Parameter]) -> float:
    squared = torch.zeros((), dtype=torch.float64)
    for parameter in parameters:
        if parameter.grad is None:
            continue
        grad = parameter.grad.detach()
        squared += torch.sum(grad.to(device="cpu", dtype=torch.float64).square())
    return float(torch.sqrt(squared).item())


def train_optimizer_step(
    model: torch.nn.Module,
    batch_iter: Any,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
    *,
    device: torch.device,
    config: Any,
    ddp_gradient_sync: bool,
) -> TrainStepResult:
    """Run one accumulated optimizer step.

    DDP synchronization is disabled around every non-final microbatch.  The
    final forward/backward pair runs in the normal DDP context and therefore
    performs exactly one reducer synchronization for the optimizer step.
    """

    accumulation_steps = int(config.training.gradient_accumulation_steps)
    if accumulation_steps < 1:
        raise ValueError("training.gradient_accumulation_steps must be >= 1")
    if ddp_gradient_sync and not hasattr(model, "no_sync"):
        raise TypeError("ddp_gradient_sync requires a model with no_sync()")

    optimizer.zero_grad(set_to_none=True)
    total_loss = 0.0
    total_tokens = 0
    total_examples = 0
    sync_window_seconds = 0.0
    for microbatch_index in range(accumulation_steps):
        is_final = microbatch_index + 1 == accumulation_steps
        sync_context = (
            contextlib.nullcontext() if not ddp_gradient_sync or is_final else model.no_sync()
        )
        batch: Batch = next(batch_iter).to(device)
        backward_start = None
        with sync_context:
            with maybe_autocast(device, config.training.precision):
                output = model(input_ids=batch.input_ids, labels=batch.labels)
                if output.loss is None:
                    raise RuntimeError("model did not return a loss")
                unscaled_loss = output.loss
                if not torch.isfinite(unscaled_loss.detach()):
                    raise FloatingPointError(
                        f"non-finite loss: {float(unscaled_loss.detach().cpu())}"
                    )
                loss = unscaled_loss / accumulation_steps
            if ddp_gradient_sync and is_final:
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                backward_start = time.monotonic()
            loss.backward()
        if backward_start is not None:
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            sync_window_seconds = time.monotonic() - backward_start
        total_loss += float(unscaled_loss.detach().cpu())
        total_tokens += int(batch.num_tokens)
        total_examples += int(batch.num_examples)

    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if config.training.grad_clip is not None:
        grad_norm = float(
            torch.nn.utils.clip_grad_norm_(parameters, float(config.training.grad_clip))
            .detach()
            .cpu()
        )
    else:
        grad_norm = _gradient_norm(parameters)
    if not math.isfinite(grad_norm):
        raise FloatingPointError(f"non-finite gradient norm: {grad_norm}")

    optimizer.step()
    if scheduler is not None:
        scheduler.step()
    return TrainStepResult(
        loss=total_loss / accumulation_steps,
        tokens=total_tokens,
        examples=total_examples,
        grad_norm=grad_norm,
        gradient_sync_window_seconds=sync_window_seconds,
    )


def broadcast_trainable_parameters(model: torch.nn.Module, *, source_rank: int = 0) -> None:
    """Broadcast the common periodic-average initialization from one rank."""

    for parameter in model.parameters():
        if parameter.requires_grad:
            dist.broadcast(parameter.detach(), src=source_rank)


def average_trainable_parameters(
    model: torch.nn.Module,
    *,
    world_size: int | None = None,
    communication_dtype: torch.dtype = torch.bfloat16,
    all_reduce_fn: Callable[[torch.Tensor], None] | None = None,
) -> tuple[float, int]:
    """Average trainable parameters through one flattened BF16 all-reduce."""

    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if not parameters:
        raise ValueError("model has no trainable parameters")
    if world_size is None:
        world_size = dist.get_world_size()
    world_size = int(world_size)
    if world_size < 1:
        raise ValueError("world_size must be >= 1")

    flat = torch.cat(
        [parameter.detach().reshape(-1).to(dtype=communication_dtype) for parameter in parameters]
    )
    if not torch.isfinite(flat).all():
        raise FloatingPointError("non-finite parameter before periodic average")
    reduce = all_reduce_fn
    if reduce is None:

        def reduce(tensor: torch.Tensor) -> None:
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)

    started = time.monotonic()
    reduce(flat)
    flat.div_(world_size)
    elapsed = time.monotonic() - started
    if not torch.isfinite(flat).all():
        raise FloatingPointError("non-finite parameter after periodic average")

    offset = 0
    with torch.no_grad():
        for parameter in parameters:
            numel = parameter.numel()
            parameter.copy_(flat[offset : offset + numel].view_as(parameter))
            offset += numel
    return elapsed, int(flat.numel())


def should_average(step: int, interval: int) -> bool:
    if step < 1:
        raise ValueError("step must be >= 1")
    if interval < 1:
        raise ValueError("interval must be >= 1")
    return step % interval == 0
